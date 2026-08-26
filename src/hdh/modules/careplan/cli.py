"""`hdh careplan` — the module's command surface.

Milestone 1b is the knowledge layer, so the commands are the ones that put
knowledge in and get it out: ``ingest``, ``corpora``, ``search``. Plan
generation arrives with the subagent.

``search`` exists for a reason beyond convenience: retrieval is the part of
a generated plan a reader most needs to inspect, and being able to ask what
the model *would* have been shown — without generating anything — is how
you tell a bad plan from a bad corpus.
"""

from __future__ import annotations


def register_cli(subparsers) -> None:
    """Discovery hook consumed by hdh.cli (see hdh.modules.CLI_MODULES)."""
    parser = subparsers.add_parser("careplan", help="Care plans: knowledge corpora and retrieval")
    sub = parser.add_subparsers(dest="careplan_cmd", required=True)

    ingest = sub.add_parser("ingest", help="Load a knowledge corpus into the store")
    ingest.add_argument("--corpus", help="Corpus name (default: every bundled corpus)")
    ingest.add_argument("--root", help="Directory holding corpora (default: the bundled ones)")

    sub.add_parser("corpora", help="What is ingested, and how much")

    search = sub.add_parser("search", help="Retrieve chunks, exactly as the subagent would")
    search.add_argument("query", nargs="+", help="The clinical situation to retrieve for")
    search.add_argument("--corpus", required=True)
    search.add_argument("-k", type=int, default=5)

    parser.set_defaults(func=run)


def run(session, args) -> None:
    """Dispatch a `hdh careplan` subcommand."""
    {
        "ingest": lambda: _cmd_ingest(session, args),
        "corpora": lambda: _cmd_corpora(session),
        "search": lambda: _cmd_search(session, args),
    }[args.careplan_cmd]()


def _cmd_ingest(session, args) -> None:
    import pathlib

    from hdh.core.dialect import DatabaseFeatureError, require_postgresql
    from hdh.modules.careplan.ingest import CorpusError, available, ingest_corpus

    try:
        require_postgresql(session, "Care-plan knowledge ingestion")
    except DatabaseFeatureError as err:
        raise SystemExit(f"hdh careplan ingest: {err}") from None

    root = pathlib.Path(args.root) if args.root else None
    names = [args.corpus] if args.corpus else available(root)
    if not names:
        raise SystemExit("no corpora found — nothing to ingest")

    total = 0
    for name in names:
        try:
            written = ingest_corpus(session, name, root)
        except CorpusError as err:
            raise SystemExit(f"hdh careplan ingest: {name}: {err}") from None
        print(f"  {name:<24} {written:>4} chunks")
        total += written
    session.commit()
    print(f"\n📚 ingested {total} chunks across {len(names)} corpus/corpora")


def _cmd_corpora(session) -> None:
    from hdh.modules.careplan.knowledge import corpora

    rows = corpora(session)
    if not rows:
        raise SystemExit("nothing ingested yet — run `hdh careplan ingest`")
    print()
    for name, count in rows:
        print(f"  {name:<24} {count:>4} chunks")


def _cmd_search(session, args) -> None:
    from hdh.core.dialect import DatabaseFeatureError
    from hdh.modules.careplan.knowledge import PgStore

    query = " ".join(args.query)
    try:
        hits = PgStore(session).search(query, args.corpus, k=args.k)
    except DatabaseFeatureError as err:
        raise SystemExit(f"hdh careplan search: {err}") from None

    if not hits:
        # An empty result is an answer, not a failure: a plan element with
        # no retrieved evidence should not be generated at all.
        print(f"\nno chunks in {args.corpus!r} match {query!r} — nothing to cite")
        return
    print(f"\n{len(hits)} chunk(s) for {query!r}\n")
    for hit in hits:
        first_line = hit.chunk.strip().splitlines()[0]
        print(f"  {hit.score:.3f}  {hit.citation()}")
        print(f"         {first_line[:96]}")
        print(f"         source: {hit.source}  ·  {hit.license}")
        print()
