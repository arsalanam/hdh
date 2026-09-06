"""`hdh orders` — the human surface for service requests (design §6).

Milestone A's job is to prove requests are first-class, auditable and
analysable, so this is the whole lifecycle a person can drive today:
``add`` a request, ``list`` what is outstanding, ``release`` it from draft
to active. Amending and voiding are already covered — ``ServiceRequest``
is a registered chart entity, so ``hdh chart amend --entity
ServiceRequest`` works without a line of code here.

Everything that creates or changes a row goes through
:mod:`hdh.core.chartedit`, so ``hdh chart history`` shows an order beside
the conditions and prescriptions it sits with. Releasing an order is a
clinical act — it is the moment the ask leaves the building — so it is
audited like any other.

Writing the outbox bundle belongs to milestone C; ``release`` here moves
the status and records who did it.
"""

from __future__ import annotations

#: Ordering for `list`: what a clinician chases first. Draft work is
#: unfinished business, active work is outstanding, and the rest is
#: history.
_STATUS_ORDER = {"draft": 0, "active": 1, "completed": 2, "revoked": 3, "entered_in_error": 4}


def register(subparsers) -> None:
    """Register `hdh orders` and its operations."""
    parser = subparsers.add_parser("orders", help="Service requests: what the chart asked for")
    orders_sub = parser.add_subparsers(dest="orders_cmd", required=True)

    listing = orders_sub.add_parser("list", help="What is outstanding for one patient")
    listing.add_argument("--mrn", required=True)
    listing.add_argument("--status", default=None, help="draft | active | completed | revoked")
    listing.add_argument("--kind", default=None, help="medication | lab | referral | procedure | follow_up")
    listing.add_argument("--all", action="store_true", help="Include completed and revoked orders")

    add = orders_sub.add_parser("add", help="Record an order a clinician placed")
    add.add_argument("--mrn", required=True)
    add.add_argument("--kind", required=True, help="medication | lab | referral | procedure | follow_up")
    add.add_argument("--display", required=True, help='What was ordered: "Basic metabolic panel"')
    add.add_argument("--visit", type=int, default=None, dest="visit_id", help="Authoring encounter")
    add.add_argument("--code", default=None, help="Leave unset until a coder resolves it")
    add.add_argument("--code-system", default=None, help="loinc | rxnorm | snomed_ct")
    add.add_argument("--reason-condition", type=int, default=None, dest="reason_condition_id")
    add.add_argument("--occurrence", default=None, help="When it should happen (ISO date)")
    add.add_argument("--sig", default=None, help="Verbatim directions, as a pharmacy would read them")
    add.add_argument("--route", default=None)
    add.add_argument("--quantity", type=float, default=None)
    add.add_argument("--dry-run", action="store_true")

    release = orders_sub.add_parser("release", help="Draft → active: the ask leaves the building")
    release.add_argument("--visit", type=int, default=None, dest="visit_id")
    release.add_argument("--mrn", default=None, help="Release every draft for one patient")
    release.add_argument("--id", type=int, default=None, dest="row_id", help="One request")
    release.add_argument("--reason", default="released for fulfilment")
    release.add_argument(
        "--outbox",
        default=None,
        metavar="DIR",
        help="Also write a FHIR order bundle here, for a partner to pick up",
    )
    release.add_argument("--dry-run", action="store_true")


def run(session, args) -> None:
    """Dispatch one `hdh orders` operation."""
    if args.orders_cmd == "list":
        _list(session, args)
    elif args.orders_cmd == "add":
        _add(session, args)
    else:
        _release(session, args)


def _actor(session):
    """Who a CLI order/edit is attributed to: the signed-in identity, or the
    OS user when nobody has run `hdh login` (AU2). One helper, in
    core.identity, keeps the provider coupling out of this module."""
    from hdh.core.identity import cli_actor

    return cli_actor(session)


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {mrn}")
    return patient


def _enum(enum_cls, text: str, flag: str):
    """Accept the value a user would type, and say what was allowed if not."""
    try:
        return enum_cls(text.strip().lower())
    except ValueError:
        allowed = " | ".join(member.value for member in enum_cls)
        raise SystemExit(f"{flag}: expected one of {allowed}, got {text!r}") from None


def _list(session, args) -> None:
    from hdh.core.models import RequestStatus, ServiceKind, ServiceRequest

    patient = _patient(session, args.mrn)
    query = session.query(ServiceRequest).filter(ServiceRequest.patient_id == patient.id)
    if args.status:
        query = query.filter(ServiceRequest.status == _enum(RequestStatus, args.status, "--status"))
    elif not args.all:
        # The default question is "what is outstanding?", not "what ever
        # happened?" — completed and revoked orders need --all or --status.
        query = query.filter(ServiceRequest.status.in_((RequestStatus.DRAFT, RequestStatus.ACTIVE)))
    if args.kind:
        query = query.filter(ServiceRequest.kind == _enum(ServiceKind, args.kind, "--kind"))

    rows = sorted(
        query.all(),
        key=lambda r: (_STATUS_ORDER.get(_value(r.status), 9), r.requested_date, r.id),
    )
    if not rows:
        print(f"no orders for {args.mrn}" + ("" if args.all or args.status else " (outstanding)"))
        return
    print(f"orders for {args.mrn} ({len(rows)}):\n")
    for row in rows:
        code = f"{row.code_system}:{row.code}" if row.code else "uncoded"
        when = f" due {row.occurrence_date}" if row.occurrence_date else ""
        fulfilled = len(row.fulfilled_by)
        back = f"  ← {fulfilled} result(s)" if fulfilled else ""
        print(
            f"  #{row.id:<6} {_value(row.status):<16} {_value(row.kind):<11} "
            f"{row.display[:44]:<44} {code:<18} {row.requested_date}{when}{back}"
        )


def _value(field) -> str:
    """Enum column or plain string, depending on the backend."""
    return field.value if hasattr(field, "value") else str(field)


def _add(session, args) -> None:
    from datetime import date

    from hdh.core.chartedit import record_creation
    from hdh.core.models import RequestOrigin, RequestStatus, ServiceKind, ServiceRequest, Visit

    patient = _patient(session, args.mrn)
    kind = _enum(ServiceKind, args.kind, "--kind")
    visit_id = args.visit_id
    if visit_id is not None:
        visit = session.get(Visit, visit_id)
        if visit is None:
            raise SystemExit(f"no visit #{visit_id}")
        if visit.patient_id != patient.id:
            raise SystemExit(f"visit #{visit_id} belongs to another patient")
    else:
        # Attach to the latest encounter and SAY so. An order with no visit
        # is legal, but a result returning against one has nowhere to live —
        # the importer refuses it — so silently leaving it unattached would
        # only defer the problem to the least convenient moment.
        latest = (
            session.query(Visit)
            .filter(Visit.patient_id == patient.id)
            .order_by(Visit.visit_date.desc(), Visit.id.desc())
            .first()
        )
        if latest is not None:
            visit_id = latest.id
            print(f"   (attached to the latest visit #{visit_id}, {latest.visit_date})")

    request = ServiceRequest(
        patient_id=patient.id,
        visit_id=visit_id,
        kind=kind,
        status=RequestStatus.DRAFT,
        # A person typed this at a terminal, which is what CLINICIAN means.
        # Provenance is set here and never amended (see the chartedit spec).
        origin=RequestOrigin.CLINICIAN,
        display=args.display,
        code_system=args.code_system,
        code=args.code,
        reason_condition_id=args.reason_condition_id,
        requested_date=date.today(),
        occurrence_date=date.fromisoformat(args.occurrence) if args.occurrence else None,
        sig=args.sig,
        route=args.route,
        quantity=args.quantity,
    )
    if args.dry_run:
        print(f"would add {_value(kind)} order for {args.mrn}: {args.display}")
        return
    session.add(request)
    session.flush()  # the audit event needs the row id
    record_creation(session, _actor(session), "ServiceRequest", request, reason="entered at the CLI")
    session.commit()
    print(f"✅ order #{request.id} ({_value(kind)}, draft): {args.display}")


def _release(session, args) -> None:
    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits
    from hdh.core.models import RequestStatus, ServiceRequest

    if args.row_id is None and args.visit_id is None and args.mrn is None:
        raise SystemExit("release needs --id, --visit, or --mrn")

    query = session.query(ServiceRequest).filter(ServiceRequest.status == RequestStatus.DRAFT)
    if args.row_id is not None:
        query = query.filter(ServiceRequest.id == args.row_id)
    if args.visit_id is not None:
        query = query.filter(ServiceRequest.visit_id == args.visit_id)
    if args.mrn is not None:
        query = query.filter(ServiceRequest.patient_id == _patient(session, args.mrn).id)

    drafts = query.order_by(ServiceRequest.id).all()
    if not drafts:
        # Not an error: asking twice is a normal thing to do, and the honest
        # answer is that there is nothing left in draft.
        print("nothing to release (no draft orders matched)")
        return

    edits = [
        ChartEdit(
            "ServiceRequest",
            row.id,
            EditAction.AMEND,
            {"status": RequestStatus.ACTIVE.value},
            args.reason,
        )
        for row in drafts
    ]
    outcomes = apply_edits(session, _actor(session), edits, dry_run=args.dry_run)
    for outcome in outcomes:
        print(("✅ " if outcome.applied else "⚠️  ") + outcome.detail)
    if not all(outcome.applied for outcome in outcomes):
        raise SystemExit(1)
    if args.outbox and not args.dry_run:
        _write_outbox(session, drafts, args.outbox)


def _write_outbox(session, released, directory: str) -> None:
    """Hand the released orders to a partner (design §6).

    Optional on purpose: releasing an order is a clinical act on its own,
    and hdh must still be usable with no partner configured at all. The
    bundle carries the patient's active diagnoses, as a real requisition
    does — it is what lets a mock lab answer in a way that fits the patient
    rather than returning generic noise (§9 Q4).
    """
    from pathlib import Path

    from hdh.core.models import Condition, ConditionStatus
    from hdh.modules.interchange.bundles import order_bundle, write_bundle
    from hdh.modules.interchange.contracts import OutboundOrder

    diagnoses: dict[int, tuple[str, ...]] = {}
    orders = []
    for row in released:
        if row.patient_id not in diagnoses:
            codes = (
                session.query(Condition.icd10_code)
                .filter(Condition.patient_id == row.patient_id, Condition.status == ConditionStatus.ACTIVE)
                .all()
            )
            diagnoses[row.patient_id] = tuple(sorted({c[0] for c in codes if c[0]}))
        orders.append(
            OutboundOrder(
                request_id=row.id,
                kind=_value(row.kind),
                display=row.display,
                patient_mrn=row.patient.mrn,
                requested_date=row.requested_date,
                code_system=row.code_system,
                code=row.code,
                occurrence_date=row.occurrence_date,
                sig=row.sig,
                diagnoses=diagnoses[row.patient_id],
            )
        )
    name = f"orders-{min(o.request_id for o in orders)}-{max(o.request_id for o in orders)}.json"
    path = write_bundle(Path(directory), name, order_bundle(orders))
    print(f"📤 {len(orders)} order(s) → {path}")
