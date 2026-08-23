"""`hdh rxnorm` — load a release and ask the drug graph questions.

    hdh rxnorm load --source ~/rxnorm/rrf
    hdh rxnorm search "blorbizide 10 mg oral tablet" --levels SCD
    hdh rxnorm graph 100021

RxNorm is redistributable only under UMLS terms, so nothing here downloads
anything: point ``--source`` at a release you have accepted the terms for.
"""

from __future__ import annotations


def register_cli(subparsers) -> None:
    """Register `hdh rxnorm` and its operations."""
    parser = subparsers.add_parser("rxnorm", help="RxNorm: the drug graph")
    sub = parser.add_subparsers(dest="rxnorm_cmd", required=True)

    load = sub.add_parser("load", help="Load an unpacked RxNorm release (RRF)")
    load.add_argument("--source", required=True, metavar="DIR")

    search = sub.add_parser("search", help="Run the shared funnel over a drug name")
    search.add_argument("mention")
    search.add_argument("--levels", default=None, help="Term types to prefer, e.g. SCD,SBD")
    search.add_argument("--limit", type=int, default=5)

    graph = sub.add_parser("graph", help="What a drug is made of, and what is built from it")
    graph.add_argument("rxcui")

    code = sub.add_parser("code", help="Assign RxCUIs to medication orders that have none")
    code.add_argument("--request", type=int, default=None, help="One order; default is every uncoded one")
    code.add_argument("--mrn", default=None, help="Restrict to one patient")
    code.add_argument("--min-score", type=float, default=0.6, help="Below this, leave it for a human")
    code.add_argument("--dry-run", action="store_true")

    parser.set_defaults(func=run_cli)


def run_cli(session, args) -> None:
    """Dispatch one `hdh rxnorm` operation."""
    if args.rxnorm_cmd == "load":
        _load(session, args)
    elif args.rxnorm_cmd == "search":
        _search(session, args)
    elif args.rxnorm_cmd == "code":
        _code(session, args)
    else:
        _graph(session, args)


def _service(session):
    from hdh.core.ontology import get_ontology_service

    return get_ontology_service("rxnorm", session)


def _load(session, args) -> None:
    from hdh.modules.rxnorm.loader import RxNormLoadError, run_load

    try:
        report = run_load(session, args.source)
    except RxNormLoadError as err:
        raise SystemExit(str(err)) from None
    print("✅ RxNorm loaded")
    for line in report.lines():
        print(f"   {line}")


def _search(session, args) -> None:
    context: dict = {"limit": args.limit}
    if args.levels:
        context["levels"] = [level.strip() for level in args.levels.split(",") if level.strip()]
    hits = _service(session).normalize(args.mention, context)
    if not hits:
        print(f"no RxNorm candidate for {args.mention!r}")
        return
    print(f"candidates for {args.mention!r}:\n")
    for hit in hits:
        level = (hit.concept.properties or {}).get("tty", "?")
        print(f"  {hit.concept.code:<10} {level:<5} {hit.score:<6.2f} {hit.concept.display[:56]}")
        print(f"             [{hit.reason}]")


def _graph(session, args) -> None:
    service = _service(session)
    concept = service.lookup(args.rxcui)
    if concept is None:
        raise SystemExit(f"no RxNorm concept {args.rxcui}")
    level = (concept.properties or {}).get("level", "?")
    print(f"{concept.code}  {concept.display}   ({level})\n")

    def show(title, concepts):
        if concepts:
            print(f"  {title}")
            for item in concepts:
                tty = (item.properties or {}).get("tty", "?")
                print(f"    {item.code:<10} {tty:<5} {item.display[:56]}")

    show("is, more generally:", service.ancestors(args.rxcui))
    show("ingredients:", service.ingredients_of(args.rxcui))
    show("brands:", service.brands_of(args.rxcui))
    for rela, targets in sorted(service.attributes(args.rxcui).items()):
        show(f"{rela}:", targets)


def _code(session, args) -> None:
    """Fill in the code a medication request was created without.

    §2 of the service-requests design made `code` nullable because a
    request is real before anyone codes it. This is the other half — and
    it goes through the audited edit path, so the chart can say the code
    came from the RxNorm module rather than from the clinician who wrote
    the order.

    The strength and route come from the request's own `sig`, which
    comprehension kept verbatim precisely so that a later reader could do
    this (design §5).
    """
    from hdh.core.chartedit import Actor, ChartEdit, EditAction, apply_edits
    from hdh.core.models import EditSource, Patient, ServiceKind, ServiceRequest
    from hdh.modules.rxnorm.coding import resolve

    query = session.query(ServiceRequest).filter(
        ServiceRequest.kind == ServiceKind.MEDICATION, ServiceRequest.code.is_(None)
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
        print("no uncoded medication orders")
        return

    service = _service(session)
    edits, skipped = [], []
    for request in pending:
        coding = resolve(
            service,
            request.display,
            route=request.route,
            raw=f"{request.display} {request.sig or ''}",
            minimum_score=args.min_score,
        )
        if coding is None:
            # Refuse-don't-guess: an uncoded order is legitimate state, and
            # a wrong RxCUI is worse than no RxCUI at all.
            skipped.append(f"#{request.id} {request.display!r} — no confident match")
            continue
        edits.append(
            (
                request,
                coding,
                ChartEdit(
                    "ServiceRequest",
                    request.id,
                    EditAction.AMEND,
                    {"code": coding.rxcui, "code_system": "rxnorm"},
                    f"coded by the RxNorm module at {coding.tty} ({'; '.join(coding.evidence)})",
                ),
            )
        )

    if edits:
        outcomes = apply_edits(
            session,
            Actor(name="rxnorm-module", source=EditSource.PIPELINE),
            [edit for _r, _c, edit in edits],
            dry_run=args.dry_run,
        )
        prefix = "would code" if args.dry_run else "coded"
        for (request, coding, _edit), outcome in zip(edits, outcomes, strict=True):
            mark = "✅" if outcome.applied else "⚠️ "
            print(f"{mark} {prefix} #{request.id} {request.display[:34]:<34} → {coding.rxcui} ({coding.tty})")
            for line in coding.evidence:
                print(f"       · {line}")
    if skipped:
        print(f"\nleft for a human ({len(skipped)}):")
        for line in skipped:
            print(f"  ⚠️  {line}")
