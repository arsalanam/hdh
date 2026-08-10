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
    load_p.add_argument("--source", required=True, help="Directory holding the release files")
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
    }
    commands[args.icd_cmd]()


def _tables():
    from hdh.core.models import Base

    t = Base.metadata.tables
    return t["ontology_concepts"], t["ontology_edges"], t["ontology_loads"]


def _cmd_load(session, args) -> None:
    from hdh.modules.icd10cm.loader import LoadError, run_load

    try:
        report = run_load(session, args.source, args.fy, force=args.force)
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
    row = session.execute(select(concepts_t).where(concepts_t.c.code == code.upper())).mappings().first()
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


def _cmd_search(session, term: str, limit: int) -> None:
    concepts_t, _e, _l = _tables()
    # LIKE per word (AND) — the PostgreSQL FTS path replaces this in milestone C
    query = select(concepts_t.c.code, concepts_t.c.display, concepts_t.c.is_billable)
    for word in term.split():
        query = query.where(concepts_t.c.display.ilike(f"%{word}%"))
    rows = session.execute(query.order_by(concepts_t.c.hierarchy_depth.desc()).limit(limit)).all()
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
