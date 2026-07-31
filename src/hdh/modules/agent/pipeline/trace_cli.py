"""CLI viewer for pipeline traces: `hdh trace runs|show|usage`.

Registered by hdh.cli. Reads the trace database only — it never needs the
clinical database or an API key.
"""

import json


def register_cli(subparsers):
    """Register the `hdh trace` subcommand."""
    p = subparsers.add_parser("trace", help="Inspect agent-pipeline traces (runs, turns, steps, token usage)")
    trace_sub = p.add_subparsers(dest="trace_cmd", required=True)

    trace_sub.add_parser("runs", help="List recent runs")

    show = trace_sub.add_parser("show", help="Show one run: its turns and every component step")
    show.add_argument("run_id", help="Run id (prefix is enough)")
    show.add_argument("--json", action="store_true", help="Dump the full structured trace as JSON")

    usage = trace_sub.add_parser("usage", help="Daily token usage (computed from recorded steps)")
    usage.add_argument("--days", type=int, default=7)

    p.set_defaults(func=run)


def run(session, args):
    """Dispatch trace subcommands against the trace store."""
    from .gateway import default_trace_url
    from .tracing import TraceStore

    store = TraceStore(default_trace_url())

    if args.trace_cmd == "runs":
        _show_runs(store)
    elif args.trace_cmd == "show":
        _show_run(store, args.run_id, as_json=args.json)
    elif args.trace_cmd == "usage":
        _show_usage(store, args.days)


def _show_runs(store) -> None:
    """Table of recent runs."""
    runs = store.recent_runs()
    if not runs:
        print("No runs recorded yet — ask the agent something first.")
        return
    print(
        f"\n{'run id':<10}{'started':<21}{'source':<14}{'model':<22}{'turns':>6}{'in tok':>10}{'out tok':>9}"
    )
    print("-" * 92)
    for r in runs:
        print(
            f"{r['run_id'][:8]:<10}{r['started_at']:<21}{r['source']:<14}"
            f"{r['model']:<22}{r['turns']:>6}{r['input_tokens']:>10,}{r['output_tokens']:>9,}"
        )
    print("\nDetails: hdh trace show <run id>\n")


def _show_run(store, run_prefix: str, as_json: bool) -> None:
    """One run's turns and steps, human-readable or as the raw JSON blob."""
    detail = store.run_detail(run_prefix)
    if detail is None:
        raise SystemExit(f"No run matching '{run_prefix}'.")
    if as_json:
        print(json.dumps(detail, indent=2, default=str))
        return
    print(f"\nrun {detail['run_id']}  ·  {detail['started_at']}  ·  {detail['source']}")
    print(f"model {detail['model']}  ·  guard {detail['guard_model']}")
    for turn in detail["turns"]:
        print(
            f"\n  turn {turn['turn_index']} (id {turn['turn_id']}) — {turn['status'].upper()}"
            f" · {turn['attempts']} attempt(s) · "
            f"{turn['input_tokens']:,} in / {turn['output_tokens']:,} out tokens"
        )
        print(f"  Q: {turn['question'][:120]}")
        for step in turn["steps"]:
            out = step["output"] or {}
            summary = out.get("reason") or out.get("label") or out.get("intent") or ""
            print(
                f"    #{step['seq']:<3}{step['stage']:<15} attempt {step['attempt']} "
                f"· {step['status']:<9}· {step['duration_ms']:>7,} ms "
                f"· {step['input_tokens']:>7,}/{step['output_tokens']:<6,} tok  {str(summary)[:50]}"
            )
        if turn["answer"]:
            print(f"  A: {turn['answer'][:160].replace(chr(10), ' ')}...")
    print("\nFull step payloads: hdh trace show <run id> --json\n")


def _show_usage(store, days: int) -> None:
    """Daily token totals derived from the steps table."""
    rows = store.usage_by_day(days)
    if not rows:
        print("No usage recorded yet.")
        return
    print(f"\n{'day':<12}{'input tok':>12}{'output tok':>12}{'steps':>7}")
    print("-" * 43)
    for r in rows:
        print(f"{r['day']:<12}{r['input_tokens']:>12,}{r['output_tokens']:>12,}{r['steps']:>7}")
    print()
