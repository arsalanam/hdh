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

    parser.set_defaults(func=run_cli)


def run_cli(session, args) -> None:
    """Dispatch one `hdh rxnorm` operation."""
    if args.rxnorm_cmd == "load":
        _load(session, args)
    elif args.rxnorm_cmd == "search":
        _search(session, args)
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
