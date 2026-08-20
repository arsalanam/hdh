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
    release.add_argument("--dry-run", action="store_true")


def run(session, args) -> None:
    """Dispatch one `hdh orders` operation."""
    if args.orders_cmd == "list":
        _list(session, args)
    elif args.orders_cmd == "add":
        _add(session, args)
    else:
        _release(session, args)


def _actor():
    import getpass

    from hdh.core.chartedit import Actor
    from hdh.core.models import EditSource

    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 — headless environments have no user
        name = "cli"
    return Actor(name=name, source=EditSource.CLI)


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
    if args.visit_id is not None:
        visit = session.get(Visit, args.visit_id)
        if visit is None:
            raise SystemExit(f"no visit #{args.visit_id}")
        if visit.patient_id != patient.id:
            raise SystemExit(f"visit #{args.visit_id} belongs to another patient")

    request = ServiceRequest(
        patient_id=patient.id,
        visit_id=args.visit_id,
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
    record_creation(session, _actor(), "ServiceRequest", request, reason="entered at the CLI")
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
    outcomes = apply_edits(session, _actor(), edits, dry_run=args.dry_run)
    for outcome in outcomes:
        print(("✅ " if outcome.applied else "⚠️  ") + outcome.detail)
    if not all(outcome.applied for outcome in outcomes):
        raise SystemExit(1)
