"""`hdh loinc` — load a release and query the funnel.

    hdh loinc load --source ~/loinc/Loinc_2.78
    hdh loinc search "hemoglobin a1c"
    hdh loinc code --request 78

LOINC is licensed, so nothing here downloads anything: point ``--source``
at a release you have accepted the licence for, exactly as the SNOMED
loader expects an RF2 directory.
"""

from __future__ import annotations


def register_cli(subparsers) -> None:
    """Register `hdh loinc` and its operations."""
    parser = subparsers.add_parser("loinc", help="LOINC: the vocabulary for what a lab measured")
    sub = parser.add_subparsers(dest="loinc_cmd", required=True)

    load = sub.add_parser("load", help="Load an unpacked LOINC release")
    load.add_argument("--source", required=True, metavar="DIR")

    search = sub.add_parser("search", help="Run the funnel over a free-text test name")
    search.add_argument("mention")
    search.add_argument("--system", default=None, help='Specimen axis, e.g. "ser/plas" or "urine"')
    search.add_argument("--limit", type=int, default=5)

    code = sub.add_parser("code", help="Assign LOINC codes to lab orders that have none")
    code.add_argument("--request", type=int, default=None, help="One order; default is every uncoded lab")
    code.add_argument("--mrn", default=None, help="Restrict to one patient")
    code.add_argument("--min-score", type=float, default=0.6, help="Below this, leave it for a human")
    code.add_argument("--dry-run", action="store_true")

    parser.set_defaults(func=run_cli)


def run_cli(session, args) -> None:
    """Dispatch one `hdh loinc` operation."""
    if args.loinc_cmd == "load":
        _load(session, args)
    elif args.loinc_cmd == "search":
        _search(session, args)
    else:
        _code(session, args)


def _load(session, args) -> None:
    from hdh.modules.loinc.loader import LoincLoadError, run_load

    try:
        report = run_load(session, args.source)
    except LoincLoadError as err:
        raise SystemExit(str(err)) from None
    print("✅ LOINC loaded")
    for line in report.lines():
        print(f"   {line}")


def _service(session):
    from hdh.core.ontology import get_ontology_service

    return get_ontology_service("loinc", session)


def _search(session, args) -> None:
    context = {"limit": args.limit}
    if args.system:
        context["system"] = args.system
    hits = _service(session).normalize(args.mention, context)
    if not hits:
        print(f"no LOINC candidate for {args.mention!r}")
        return
    print(f"candidates for {args.mention!r}:\n")
    for hit in hits:
        print(f"  {hit.concept.code:<12} {hit.score:<6.2f} {hit.concept.display[:60]}")
        print(f"               [{hit.reason}]")


def _code(session, args) -> None:
    """Fill in the code a request was allowed to be created without.

    §2 made `code` nullable because a request is real before anyone codes
    it. This is the other half: a coder arrives later and fills it in — and
    does so through the audited edit path, so the chart can say the code
    came from the LOINC module rather than from the clinician who ordered
    the test.
    """
    from hdh.core.chartedit import Actor, ChartEdit, EditAction, apply_edits
    from hdh.core.models import EditSource, Patient, ServiceKind, ServiceRequest

    query = session.query(ServiceRequest).filter(
        ServiceRequest.kind == ServiceKind.LAB, ServiceRequest.code.is_(None)
    )
    if args.request is not None:
        query = query.filter(ServiceRequest.id == args.request)
    if args.mrn is not None:
        patient = session.query(Patient).filter(Patient.mrn == args.mrn).first()
        if patient is None:
            raise SystemExit(f"no patient {args.mrn}")
        query = query.filter(ServiceRequest.patient_id == patient.id)

    pending = query.order_by(ServiceRequest.id).all()
    if not pending:
        print("no uncoded lab orders")
        return

    service = _service(session)
    edits, skipped = [], []
    for request in pending:
        hits = service.normalize(request.display, {"limit": 1})
        best = hits[0] if hits else None
        if best is None or best.score < args.min_score:
            # Refuse-don't-guess: an uncoded order is legitimate state, and
            # a wrong LOINC on a chart is worse than no LOINC at all.
            score = f"{best.score:.2f}" if best else "no candidate"
            skipped.append(f"#{request.id} {request.display!r} — {score}")
            continue
        edits.append(
            (
                request,
                best,
                ChartEdit(
                    "ServiceRequest",
                    request.id,
                    EditAction.AMEND,
                    {"code": best.concept.code, "code_system": "loinc"},
                    f"coded by the LOINC module ({best.score:.2f})",
                ),
            )
        )

    if edits:
        outcomes = apply_edits(
            session,
            Actor(name="loinc-module", source=EditSource.PIPELINE),
            [edit for _r, _b, edit in edits],
            dry_run=args.dry_run,
        )
        prefix = "would code" if args.dry_run else "coded"
        for (request, best, _edit), outcome in zip(edits, outcomes, strict=True):
            mark = "✅" if outcome.applied else "⚠️ "
            print(f"{mark} {prefix} #{request.id} {request.display[:40]:<40} → {best.concept.code}")
    if skipped:
        print(f"\nleft for a human ({len(skipped)}):")
        for line in skipped:
            print(f"  ⚠️  {line}")
