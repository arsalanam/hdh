"""`hdh icd` — the ICD-10-CM ontology module's CLI (milestone B surface).

load / status / lookup / search / lateral / link. Search is LIKE-based
here; the PostgreSQL FTS + trigram path arrives with the full-catalog
milestone. All commands receive the session from hdh.cli (the composition
root) via the standard register_cli contract.
"""

from __future__ import annotations

from sqlalchemy import func, select


def register_cli(subparsers) -> None:
    """Register the `hdh icd` subcommand tree."""
    p = subparsers.add_parser("icd", help="ICD-10-CM ontology: load, lookup, search, link")
    sub = p.add_subparsers(dest="icd_cmd", required=True)

    load_p = sub.add_parser("load", help="Load an ICD-10-CM release from CMS files")
    src = load_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source", help="Directory holding the release files")
    src.add_argument("--download", action="store_true", help="Fetch the CMS release files (cached in ~/.hdh)")
    load_p.add_argument("--fy", type=int, default=2026, help="Fiscal year (default 2026)")
    load_p.add_argument("--force", action="store_true", help="Replace an already-loaded FY")

    sub.add_parser("status", help="Show the load ledger and catalog counts")

    lookup_p = sub.add_parser("lookup", help="Full context for one code")
    lookup_p.add_argument("code")

    search_p = sub.add_parser("search", help="Search code descriptions")
    search_p.add_argument("term", nargs="+")
    search_p.add_argument("--limit", type=int, default=10)

    lateral_p = sub.add_parser("lateral", help="Contralateral (other-side) code")
    lateral_p.add_argument("code")

    sub.add_parser("link", help="Backfill Diagnosis.concept_id from icd10_code")

    bench_p = sub.add_parser("bench", help="Measure lookup/search/hierarchy latencies")
    bench_p.add_argument("--iterations", type=int, default=200)

    codify_p = sub.add_parser("codify", help="Description → ranked ICD-10-CM candidates")
    codify_p.add_argument("description", nargs="+")
    codify_p.add_argument("--limit", type=int, default=5)
    codify_p.add_argument("--terms", help="Offline mode: search terms (skips the LLM axis extraction)")
    codify_p.add_argument(
        "--axes",
        default=None,
        help="Offline mode: comma-separated axis=value pairs (e.g. laterality=left,encounter=initial)",
    )

    pattern_p = sub.add_parser("pattern", help="Run a validated graph-pattern query (JSON)")
    pattern_p.add_argument("pattern_json")
    pattern_p.add_argument("--limit", type=int, default=20)

    p.set_defaults(func=run)


def run(session, args) -> None:
    """Dispatch an `hdh icd` subcommand."""
    commands = {
        "load": lambda: _cmd_load(session, args),
        "status": lambda: _cmd_status(session),
        "lookup": lambda: _cmd_lookup(session, args.code),
        "search": lambda: _cmd_search(session, " ".join(args.term), args.limit),
        "lateral": lambda: _cmd_lateral(session, args.code),
        "link": lambda: _cmd_link(session),
        "bench": lambda: _cmd_bench(session, args.iterations),
        "codify": lambda: _cmd_codify(session, args),
        "pattern": lambda: _cmd_pattern(session, args),
    }
    commands[args.icd_cmd]()


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_loads"]


def _cmd_load(session, args) -> None:
    from hdh.modules.icd10cm.loader import LoadError, run_load

    try:
        source = args.source
        if args.download:
            from hdh.modules.icd10cm.loader.download import download_release

            print(f"\n⬇ Fetching CMS FY{args.fy} release (cached in ~/.hdh/icd10cm)")
            source = download_release(args.fy)
        report = run_load(session, source, args.fy, force=args.force)
    except LoadError as err:
        raise SystemExit(f"hdh icd load: {err}") from None
    print(f"\n📚 ICD-10-CM FY{args.fy} load")
    for stage, summary in report:
        print(f"   {stage:<10} {summary}")
    print("✅ load complete")


def _cmd_status(session) -> None:
    concepts_t, edges_t, loads_t = _tables()
    loads = session.execute(select(loads_t).order_by(loads_t.c.id)).mappings().all()
    if not loads:
        print("No ontology loads yet — run: hdh icd load --source <dir>")
        return
    print("\n📒 Ontology load ledger")
    for row in loads:
        print(
            f"   #{row['id']}  {row['ontology']} FY{row['fiscal_year']}  "
            f"{row['concept_count']:,} concepts · {row['edge_count']:,} edges · "
            f"{row['duration_seconds']}s"
        )
    concepts = session.execute(select(func.count()).select_from(concepts_t)).scalar()
    billable = session.execute(
        select(func.count()).select_from(concepts_t).where(concepts_t.c.is_billable)
    ).scalar()
    edges = session.execute(select(func.count()).select_from(edges_t)).scalar()
    print(f"   catalog: {concepts:,} concepts ({billable:,} billable) · {edges:,} edges")


def _concept_or_exit(session, code: str):
    concepts_t, _e, _l = _tables()
    row = (
        session.execute(
            select(concepts_t).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code == code.upper())
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SystemExit(f"hdh icd: code '{code}' not found (is the catalog loaded?)")
    return row


def _cmd_lookup(session, code: str) -> None:
    concepts_t, edges_t, _l = _tables()
    row = _concept_or_exit(session, code)
    flag = "billable" if row["is_billable"] else row["kind"]
    print(f"\n{row['code']} — {row['display']}  [{flag}]")
    ancestors = session.execute(
        select(concepts_t.c.code, concepts_t.c.display)
        .where(
            concepts_t.c.path.in_(
                [row["path"].rsplit(".", n)[0] for n in range(1, row["hierarchy_depth"] + 1)]
            )
        )
        .order_by(concepts_t.c.hierarchy_depth)
    ).all()
    for depth, (acode, adisplay) in enumerate(ancestors):
        print(f"   {'  ' * depth}└ {acode}: {adisplay}")
    axes = (row["properties"] or {}).get("axes", {})
    if axes:
        print(f"   axes: {', '.join(f'{k}={v}' for k, v in sorted(axes.items()))}")
    for label, edge_type in (("contralateral", "contralateral"), ("variants", "axis_variant")):
        related = session.execute(
            select(concepts_t.c.code, concepts_t.c.display)
            .join(edges_t, edges_t.c.target_id == concepts_t.c.id)
            .where(edges_t.c.source_id == row["id"], edges_t.c.edge_type == edge_type)
        ).all()
        for rcode, rdisplay in related:
            print(f"   {label} → {rcode}: {rdisplay}")
    if row["episode_group"]:
        siblings = session.execute(
            select(concepts_t.c.code, concepts_t.c.episode)
            .where(
                concepts_t.c.episode_group == row["episode_group"],
                concepts_t.c.code != row["code"],
            )
            .order_by(concepts_t.c.code)
        ).all()
        if siblings:
            print(f"   episodes: {', '.join(f'{c} ({e})' for c, e in siblings)}")


def search_concepts(session, term: str, limit: int) -> list:
    """Search the catalog: FTS + trigram fallback on PostgreSQL, LIKE elsewhere.

    Returns (code, display, is_billable) rows, most specific first.
    """
    from sqlalchemy import text

    concepts_t, _e, _l = _tables()
    if session.get_bind().dialect.name == "postgresql":
        fts = text(
            "SELECT code, display, is_billable FROM ontology_concepts "
            "WHERE to_tsvector('english', code || ' ' || display) "
            "      @@ plainto_tsquery('english', :term) "
            "ORDER BY ts_rank(to_tsvector('english', code || ' ' || display), "
            "                 plainto_tsquery('english', :term)) DESC, "
            "         hierarchy_depth DESC LIMIT :k"
        )
        rows = session.execute(fts, {"term": term, "k": limit}).all()
        if rows:
            return rows
        fuzzy = text(  # misspellings: trigram similarity over the whole display
            "SELECT code, display, is_billable FROM ontology_concepts "
            "WHERE similarity(display, :term) > 0.2 "
            "ORDER BY similarity(display, :term) DESC LIMIT :k"
        )
        return session.execute(fuzzy, {"term": term, "k": limit}).all()
    # AND across words, relaxing from the end when over-specific terms
    # ("acromial") zero the result — deterministic and explainable
    words = term.split()
    while words:
        query = select(concepts_t.c.code, concepts_t.c.display, concepts_t.c.is_billable)
        for word in words:
            query = query.where(concepts_t.c.display.ilike(f"%{word}%"))
        rows = session.execute(query.order_by(concepts_t.c.hierarchy_depth.desc()).limit(limit)).all()
        if rows:
            return rows
        words = words[:-1]
    return []


def _cmd_search(session, term: str, limit: int) -> None:
    rows = search_concepts(session, term, limit)
    if not rows:
        print(f"No matches for '{term}'.")
        return
    for code, display, billable in rows:
        marker = "•" if billable else "○"
        print(f" {marker} {code:<10} {display}")


def _cmd_lateral(session, code: str) -> None:
    concepts_t, edges_t, _l = _tables()
    row = _concept_or_exit(session, code)
    other = session.execute(
        select(concepts_t.c.code, concepts_t.c.display)
        .join(edges_t, edges_t.c.target_id == concepts_t.c.id)
        .where(edges_t.c.source_id == row["id"], edges_t.c.edge_type == "contralateral")
    ).first()
    if other is None:
        raise SystemExit(f"hdh icd lateral: '{row['code']}' has no contralateral variant")
    print(f"{row['code']} ⇄ {other.code}: {other.display}")


def _cmd_link(session) -> None:
    from hdh.core.models import Diagnosis

    concepts_t, _e, _l = _tables()
    codes = dict(
        session.execute(select(concepts_t.c.code, concepts_t.c.id).where(concepts_t.c.is_billable)).all()
    )
    # concept_id is registry-injected at runtime — mypy can't see it as a
    # class attribute, so go through the table's dynamic column accessor
    concept_col = Diagnosis.__table__.c.concept_id
    linked = 0
    for code, concept_id in codes.items():
        linked += (
            session.query(Diagnosis)
            .filter(Diagnosis.icd10_code == code, concept_col.is_(None))
            .update({"concept_id": concept_id})
        )
    session.commit()
    unlinked = session.query(Diagnosis).filter(concept_col.is_(None)).count()
    print(f"🔗 linked {linked:,} diagnoses to concepts ({unlinked:,} not in the loaded catalog)")


def _percentiles(samples: list) -> tuple:
    ordered = sorted(samples)
    return (
        sum(ordered) / len(ordered),
        ordered[int(len(ordered) * 0.95)],
    )


def _cmd_bench(session, iterations: int) -> None:
    """Measure the design §7 access patterns against the bare database —
    the honest-numbers check behind the no-cache decision (design §6)."""
    import random
    import time

    from sqlalchemy import text as sql_text

    concepts_t, edges_t, _l = _tables()
    codes = [
        row[0]
        for row in session.execute(select(concepts_t.c.code).where(concepts_t.c.is_billable).limit(50_000))
    ]
    if not codes:
        raise SystemExit("hdh icd bench: catalog is empty — load it first")
    rng = random.Random(2026)
    sample = [rng.choice(codes) for _ in range(iterations)]
    dialect = session.get_bind().dialect.name

    def timed(fn) -> tuple:
        samples = []
        for code in sample:
            start = time.perf_counter()
            fn(code)
            samples.append((time.perf_counter() - start) * 1000)
        return _percentiles(samples)

    def do_lookup(code):
        session.execute(
            select(concepts_t).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code == code)
        ).first()

    def do_descendants(code):
        prefix = session.execute(
            select(concepts_t.c.path).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code == code[:3])
        ).scalar()
        if prefix:
            session.execute(
                select(func.count()).select_from(concepts_t).where(concepts_t.c.path.like(prefix + ".%"))
            ).scalar()

    def do_lateral(code):
        row = session.execute(
            select(concepts_t.c.id).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code == code)
        ).scalar()
        session.execute(
            select(edges_t.c.target_id).where(
                edges_t.c.source_id == row, edges_t.c.edge_type == "contralateral"
            )
        ).first()

    def do_search(code):
        term = "fracture forearm" if code.startswith("S") else "diabetes"
        from hdh.modules.icd10cm.cli import search_concepts

        search_concepts(session, term, 5)

    print(f"\n⏱  {iterations} iterations per pattern · dialect: {dialect}")
    print(f"   {'pattern':<22} {'mean':>9} {'p95':>9}   target")
    session.execute(sql_text("SELECT 1"))  # connection warm-up only
    for label, fn, target in (
        ("lookup(code)", do_lookup, "<10ms"),
        ("descendants(category)", do_descendants, "<20ms"),
        ("lateral(code)", do_lateral, "<10ms"),
        ("search(term)", do_search, "<50ms"),
    ):
        mean, p95 = timed(fn)
        verdict = "✓" if p95 < float(target.strip("<ms")) else "✗ OVER"
        print(f"   {label:<22} {mean:>7.2f}ms {p95:>7.2f}ms   {target} {verdict}")


def _cmd_codify(session, args) -> None:
    """Description → ranked candidates, with the explanation rendered."""
    import os

    from hdh.modules.icd10cm.service import CodifyError, codify, stub_extractor

    description = " ".join(args.description)
    if args.terms:
        axes = {}
        for pair in (args.axes or "").split(","):
            if "=" in pair:
                axis, _, value = pair.partition("=")
                axes[axis.strip()] = value.strip()
        extractor = stub_extractor(args.terms, axes)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        from hdh.modules.icd10cm.llm import llm_extractor

        extractor = llm_extractor()
    else:
        raise SystemExit(
            "hdh icd codify: set ANTHROPIC_API_KEY for LLM extraction, or use "
            "--terms/--axes for the offline path"
        )

    try:
        extraction, candidates = codify(session, description, extractor, limit=args.limit)
    except CodifyError as err:
        raise SystemExit(f"hdh icd codify: {err}") from None
    axes_shown = ", ".join(f"{k}={v}" for k, v in sorted(extraction.axes.items())) or "none stated"
    print(f'\n🩺 "{description}"')
    print(f"   understood: terms=[{extraction.terms}] axes: {axes_shown}")
    if not candidates:
        print("   no candidates — try different terms")
        return
    for rank, cand in enumerate(candidates, start=1):
        marks = []
        if cand.matched:
            marks.append("matches " + ",".join(cand.matched))
        if cand.conflicts:
            marks.append("CONFLICTS " + ",".join(cand.conflicts))
        if cand.unstated:
            marks.append("unstated " + ",".join(cand.unstated))
        exact = " ← exact" if cand.exact and rank == 1 else ""
        print(f"   {rank}. {cand.code:<10} {cand.display}")
        print(f"      {'; '.join(marks) or 'no axes requested'}{exact}")
    top = candidates[0]
    if top.unstated:
        print(f"   💬 ask about: {', '.join(top.unstated)} — the description never said")


def _cmd_pattern(session, args) -> None:
    """Validate + compile + run a JSON graph pattern."""
    import json as json_lib

    from hdh.modules.icd10cm.patterns import PatternError, run_pattern

    try:
        pattern = json_lib.loads(args.pattern_json)
    except json_lib.JSONDecodeError as err:
        raise SystemExit(f"hdh icd pattern: not valid JSON ({err})") from None
    try:
        hits = run_pattern(session, pattern, limit=args.limit)
    except PatternError as err:
        raise SystemExit(f"hdh icd pattern: {err}") from None
    if not hits:
        print("No matches.")
        return
    for hit in hits:
        marker = "•" if hit.is_billable else "○"
        print(f" {marker} {hit.code:<10} {hit.display}")
