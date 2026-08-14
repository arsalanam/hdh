"""`hdh snomed` — the SNOMED CT module's CLI (milestone B surface).

load / status / lookup / search / subsumes / attributes. Milestone B
loads from ``--source <dir>`` (the synthetic fixture or a licensed RF2
extract); the UTS download (``--download``, milestone C) and the bench
harness join later. All commands receive the session from hdh.cli, the
composition root.
"""

from __future__ import annotations

from sqlalchemy import func, select


def register_cli(subparsers) -> None:
    """Register the `hdh snomed` subcommand tree."""
    p = subparsers.add_parser("snomed", help="SNOMED CT ontology: load, lookup, search, subsumes")
    sub = p.add_subparsers(dest="snomed_cmd", required=True)

    load_p = sub.add_parser("load", help="Load a SNOMED CT RF2 Snapshot release")
    src = load_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source", help="Directory holding the RF2 Snapshot files")
    src.add_argument(
        "--download",
        action="store_true",
        help="Fetch the US Edition via UTS with your UMLS_API_KEY (cached in ~/.hdh/snomed)",
    )
    load_p.add_argument("--release", type=int, help="Release tag YYYYMM (default: newest / from filenames)")
    load_p.add_argument("--force", action="store_true", help="Replace an already-loaded release")

    sub.add_parser("status", help="Show the load ledger and catalog counts")

    lookup_p = sub.add_parser("lookup", help="Full context for one SCTID")
    lookup_p.add_argument("code")

    search_p = sub.add_parser("search", help="Search the term index")
    search_p.add_argument("term", nargs="+")
    search_p.add_argument("--limit", type=int, default=10)

    subsumes_p = sub.add_parser("subsumes", help="Is ancestor a (transitive) ancestor of descendant?")
    subsumes_p.add_argument("ancestor")
    subsumes_p.add_argument("descendant")

    attributes_p = sub.add_parser("attributes", help="Defining attributes of a concept")
    attributes_p.add_argument("code")

    bench_p = sub.add_parser("bench", help="Measure lookup/search/closure latencies")
    bench_p.add_argument("--iterations", type=int, default=200)

    purge_p = sub.add_parser(
        "purge",
        help="Remove ALL SNOMED CT content (licensed — required before building release assets)",
    )
    purge_p.add_argument("--yes", action="store_true", help="Confirm the deletion")

    p.set_defaults(func=run)


def run(session, args) -> None:
    """Dispatch the parsed `hdh snomed` subcommand."""
    command = args.snomed_cmd
    if command == "load":
        _cmd_load(session, args)
    elif command == "status":
        _cmd_status(session)
    elif command == "lookup":
        _cmd_lookup(session, args.code)
    elif command == "search":
        _cmd_search(session, " ".join(args.term), args.limit)
    elif command == "subsumes":
        _cmd_subsumes(session, args.ancestor, args.descendant)
    elif command == "attributes":
        _cmd_attributes(session, args.code)
    elif command == "bench":
        _cmd_bench(session, args.iterations)
    elif command == "purge":
        _cmd_purge(session, args.yes)


def _cmd_load(session, args) -> None:
    from hdh.modules.snomed.loader import LoadError, run_load

    try:
        source, release = args.source, args.release
        if args.download:
            from hdh.modules.snomed.loader.download import download_release

            release, source = download_release(args.release)
        report = run_load(session, source, release=release, force=args.force)
    except LoadError as err:
        raise SystemExit(f"load failed: {err}") from None
    for stage, summary in report:
        print(f"  {stage:<10} {summary}")


def _cmd_status(session) -> None:
    from hdh.core.models import Base

    tables = Base.metadata.tables
    loads_t, concepts_t = tables["ontology_loads"], tables["ontology_concepts"]
    terms_t, closure_t = tables["ontology_terms"], tables["ontology_closure"]
    rows = session.execute(
        select(loads_t).where(loads_t.c.ontology == "snomed_ct").order_by(loads_t.c.fiscal_year)
    ).all()
    if not rows:
        print("No SNOMED CT release loaded. Run: hdh snomed load --source <rf2-dir>")
        return
    for row in rows:
        print(
            f"release {row.fiscal_year}: {row.concept_count:,} concepts, "
            f"{row.edge_count:,} edges, {row.duration_seconds}s"
        )
    concepts = session.execute(
        select(func.count()).select_from(concepts_t).where(concepts_t.c.ontology == "snomed_ct")
    ).scalar()
    terms = session.execute(select(func.count()).select_from(terms_t)).scalar()
    closure = session.execute(select(func.count()).select_from(closure_t)).scalar()
    print(f"catalog now: {concepts:,} concepts, {terms:,} terms, {closure:,} closure rows")


def _cmd_lookup(session, code: str) -> None:
    from hdh.modules.snomed.ontology import build_service

    service = build_service(session)
    concept = service.lookup(code)
    if concept is None:
        raise SystemExit(f"SCTID '{code}' not found in the loaded catalog")
    properties = concept.properties
    print(f"{concept.code}  {concept.display}")
    print(f"  fsn: {properties.get('fsn')}")
    print(f"  semantic tag: {properties.get('semantic_tag')}   primitive: {properties.get('primitive')}")
    direct = service.children(code)
    nearest = list(service.ancestors(code))[:3]
    if nearest:
        print("  ancestors (nearest): " + "; ".join(f"{c.code} {c.display}" for c in nearest))
    if direct:
        print(f"  children: {len(direct)}")
    synonyms = service.synonyms(code)
    if synonyms:
        print("  terms: " + " | ".join(synonyms[:6]))


def _cmd_search(session, term: str, limit: int) -> None:
    from hdh.modules.snomed.ontology import build_service

    candidates = build_service(session).normalize(term, {"limit": limit})
    if not candidates:
        print("No matches.")
        return
    for candidate in candidates:
        concept = candidate.concept
        tag = concept.properties.get("semantic_tag") or ""
        print(f"  {candidate.score:>5}  {concept.code:<12} {concept.display} ({tag})  [{candidate.reason}]")


def _cmd_subsumes(session, ancestor: str, descendant: str) -> None:
    from hdh.modules.snomed.ontology import build_service

    service = build_service(session)
    verdict = service.subsumes(ancestor, descendant)
    a, d = service.lookup(ancestor), service.lookup(descendant)
    a_name = a.display if a else "?"
    d_name = d.display if d else "?"
    print(f"{ancestor} ({a_name}) subsumes {descendant} ({d_name}): {verdict}")


def _cmd_bench(session, iterations: int) -> None:
    """Measure the design §6–§7 access patterns against the bare database —
    honest numbers for the closure strategy (subsumption, sweeps)."""
    import random
    import time

    from sqlalchemy import text as sql_text

    from hdh.core.models import Base
    from hdh.modules.snomed.ontology import build_service

    tables = Base.metadata.tables
    concepts_t, closure_t = tables["ontology_concepts"], tables["ontology_closure"]
    codes = [
        row[0]
        for row in session.execute(
            select(concepts_t.c.code).where(concepts_t.c.ontology == "snomed_ct").limit(50_000)
        )
    ]
    if not codes:
        raise SystemExit("hdh snomed bench: catalog is empty — load it first")
    rng = random.Random(2026)
    sample = [rng.choice(codes) for _ in range(iterations)]
    pairs = session.execute(select(closure_t.c.ancestor_id, closure_t.c.descendant_id).limit(200_000)).all()
    pair_sample = [rng.choice(pairs) for _ in range(iterations)]
    sweep_roots = [
        row[0]
        for row in session.execute(
            sql_text(
                "SELECT ancestor_id FROM ontology_closure "
                "GROUP BY ancestor_id ORDER BY COUNT(*) DESC LIMIT 20"
            )
        )
    ]
    service = build_service(session)
    dialect = session.get_bind().dialect.name

    def timed(fn, inputs) -> tuple:
        samples = []
        for item in inputs:
            start = time.perf_counter()
            fn(item)
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        mean = sum(samples) / len(samples)
        return mean, samples[int(len(samples) * 0.95)]

    def do_lookup(code):
        service.lookup(code)

    def do_subsumes(pair):
        session.execute(
            select(closure_t.c.min_depth).where(
                closure_t.c.ancestor_id == pair[0], closure_t.c.descendant_id == pair[1]
            )
        ).first()

    def do_ancestors(code):
        service.ancestors(code)

    def do_sweep(root):
        session.execute(
            select(func.count()).select_from(closure_t).where(closure_t.c.ancestor_id == root)
        ).scalar()

    def do_search(code):
        service.normalize("diabetes" if code[-1] in "02468" else "fracture", {"limit": 5})

    print(f"\n[bench] {iterations} iterations per pattern - dialect: {dialect}")
    print(f"   {'pattern':<24} {'mean':>9} {'p95':>9}   target")
    session.execute(sql_text("SELECT 1"))  # connection warm-up only
    for label, fn, inputs, target in (
        ("lookup(sctid)", do_lookup, sample, 10.0),
        ("subsumes(a,b)", do_subsumes, pair_sample, 10.0),
        ("ancestors(sctid)", do_ancestors, sample, 20.0),
        ("descendant sweep", do_sweep, [rng.choice(sweep_roots) for _ in range(iterations)], 50.0),
        ("search(term)", do_search, sample[: max(iterations // 4, 20)], 100.0),
    ):
        mean, p95 = timed(fn, inputs)
        verdict = "ok" if p95 < target else "OVER"
        print(f"   {label:<24} {mean:>7.2f}ms {p95:>7.2f}ms   <{target:.0f}ms {verdict}")


def _cmd_attributes(session, code: str) -> None:
    from hdh.modules.snomed.ontology import build_service

    service = build_service(session)
    if service.lookup(code) is None:
        raise SystemExit(f"SCTID '{code}' not found in the loaded catalog")
    grouped = service.attributes(code)
    if not grouped:
        print("No defining attributes.")
        return
    for name, targets in sorted(grouped.items()):
        for target in targets:
            print(f"  {name:<24} -> {target.code}  {target.display}")


def _cmd_purge(session, confirmed: bool) -> None:
    """Delete every SNOMED CT row — the inverse of load (issue #31).

    SNOMED CT is licensed: a database destined for a release asset must
    pass scripts/release_check.py, and this is the remediation when it
    doesn't. Chart data and the ICD-10-CM catalog are untouched; the
    ~/.hdh/snomed cache stays (it is per-user, never distributed)."""
    from sqlalchemy import delete

    from hdh.core.models import Base

    if not confirmed:
        raise SystemExit(
            "hdh snomed purge deletes the ENTIRE loaded SNOMED CT catalog "
            "(concepts, terms, edges, closure, ledger). Re-run with --yes to confirm; "
            "reload later from the ~/.hdh/snomed cache with: hdh snomed load --download"
        )
    tables = Base.metadata.tables
    prefix = "snomed_ct:%"
    deleted: list[str] = []
    for label, table, where in (
        ("closure rows", tables["ontology_closure"], lambda t: t.c.ancestor_id.like(prefix)),
        ("term rows", tables["ontology_terms"], lambda t: t.c.concept_id.like(prefix)),
        (
            "edges",
            tables["ontology_edges"],
            lambda t: t.c.source_id.like(prefix) | t.c.target_id.like(prefix),
        ),
        ("concepts", tables["ontology_concepts"], lambda t: t.c.ontology == "snomed_ct"),
        ("ledger rows", tables["ontology_loads"], lambda t: t.c.ontology == "snomed_ct"),
    ):
        result = session.execute(delete(table).where(where(table)))
        deleted.append(f"{result.rowcount:,} {label}")
    session.commit()
    print("purged SNOMED CT content: " + ", ".join(deleted))
    print("verify before building any asset:  uv run python scripts/release_check.py <db>")
