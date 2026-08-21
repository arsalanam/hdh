"""`hdh interchange` — send orders out, bring results back (design §6).

hdh orders release --visit 17 --outbox exports/outbox
hdh interchange run --partner mock-lab --outbox … --inbox …
hdh interchange import --inbox …
hdh interchange review
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_ROOT = Path("exports") / "interchange"


def register_cli(subparsers) -> None:
    """Register `hdh interchange` and its three operations."""
    parser = subparsers.add_parser("interchange", help="Mock lab/pharmacy round trip for orders")
    sub = parser.add_subparsers(dest="interchange_cmd", required=True)

    run = sub.add_parser("run", help="Let a partner fulfil what is waiting in the outbox")
    run.add_argument("--partner", required=True, help="mock-lab | mock-pharmacy")
    run.add_argument("--outbox", default=str(DEFAULT_ROOT / "outbox"))
    run.add_argument("--inbox", default=str(DEFAULT_ROOT / "inbox"))
    run.add_argument("--seed", type=int, default=None, help="Reproducible results")

    imp = sub.add_parser("import", help="File returned results; unmatched ones go to review")
    imp.add_argument("--inbox", default=str(DEFAULT_ROOT / "inbox"))
    imp.add_argument("--dry-run", action="store_true", help="Report and write nothing")

    review = sub.add_parser("review", help="Results that were refused and need a human")
    review.add_argument("--resolve", type=int, default=None, metavar="ID")
    review.add_argument("--all", action="store_true", help="Include already-resolved items")

    parser.set_defaults(func=run_cli)


def run_cli(session, args) -> None:
    if args.interchange_cmd == "run":
        _run(session, args)
    elif args.interchange_cmd == "import":
        _import(session, args)
    else:
        _review(session, args)


def _run(session, args) -> None:
    from hdh.modules.interchange.bundles import (
        read_bundles,
        read_order_bundle,
        result_bundle,
        write_bundle,
    )
    from hdh.modules.interchange.partners import build_partners

    partners = build_partners(seed=args.seed)
    partner = partners.get(args.partner)
    if partner is None:
        raise SystemExit(f"unknown partner {args.partner!r} (known: {', '.join(sorted(partners))})")

    outbox, inbox = Path(args.outbox), Path(args.inbox)
    bundles = read_bundles(outbox)
    if not bundles:
        print(f"nothing waiting in {outbox}")
        return

    produced = 0
    for path, payload in bundles:
        orders = [o for o in read_order_bundle(payload) if partner.handles(o)]
        if not orders:
            continue
        results = [item for order in orders for item in partner.fulfil(order)]
        if not results:
            # A partner that cannot run the test says so by returning
            # nothing. Better an order that stays open than an invented
            # result — the same refuse-don't-guess line the chart holds.
            print(f"  {partner.name}: nothing to return for {path.name}")
            continue
        written = write_bundle(
            inbox, f"result-{partner.name}-{path.stem}.json", result_bundle(partner.name, results)
        )
        produced += len(results)
        print(f"  {partner.name}: {len(results)} result(s) → {written}")
    print(f"{partner.name} produced {produced} result(s)")


def _import(session, args) -> None:
    from hdh.modules.interchange.importer import import_results

    outcome = import_results(session, Path(args.inbox), dry_run=args.dry_run)
    header = "DRY RUN — nothing written; would file" if args.dry_run else "filed"
    print(f"\n{header}: {len(outcome.filed)}")
    for line in outcome.filed:
        print(f"  ✅ {line}")
    if outcome.rejected:
        print(f"\nrefused (sent to review): {len(outcome.rejected)}")
        for line in outcome.rejected:
            print(f"  ⚠️  {line}")
        print("\n  hdh interchange review")


def _review(session, args) -> None:
    from sqlalchemy import select, update

    from hdh.modules.interchange.importer import rejected_table

    table = rejected_table()
    if args.resolve is not None:
        from datetime import datetime

        found = session.execute(select(table).where(table.c.id == args.resolve)).first()
        if found is None:
            raise SystemExit(f"no review item #{args.resolve}")
        session.execute(update(table).where(table.c.id == args.resolve).values(resolved_at=datetime.now()))
        session.commit()
        print(f"✅ review item #{args.resolve} resolved ({found.reason})")
        return

    statement = select(table)
    if not args.all:
        statement = statement.where(table.c.resolved_at.is_(None))
    rows = session.execute(statement.order_by(table.c.id)).all()
    if not rows:
        print("review queue is empty")
        return
    print(f"results awaiting a decision ({len(rows)}):\n")
    for row in rows:
        reason = row.reason.value if hasattr(row.reason, "value") else row.reason
        mark = "" if row.resolved_at is None else "  [resolved]"
        print(f"  #{row.id:<5} {reason:<18} {row.detail}{mark}")
        if row.payload:
            print(f"         {row.payload}")
