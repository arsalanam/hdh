"""CLI subcommand for the agentic AI care assistant.  Registered by hdh.cli.

Two engines:
  pipeline (default for one-shot) — the LangGraph state machine: gateway →
      guardrails (topic + quota) → intent → tool executor → assembler →
      validator, with validated-before-streamed output and ≤3 retries.
  simple (--simple, and the interactive chat UI) — the plain tool-runner
      loop with conversation history and context compaction.
"""

import sys


def register_cli(subparsers):
    """Register the `hdh agent` subcommand."""
    p = subparsers.add_parser(
        "agent", help="AI care-program agent — validated pipeline (one-shot) or interactive chat"
    )
    p.add_argument("question", nargs="?", help="One-shot question; omit to start interactive chat")
    p.add_argument("--model", help="Override the Claude model (default: claude-opus-5)")
    p.add_argument(
        "--simple", action="store_true", help="Use the simple tool-runner loop instead of the pipeline"
    )
    p.add_argument(
        "--max-tries",
        type=int,
        default=3,
        metavar="N",
        help="Pipeline: executor attempts before giving up (default 3)",
    )
    p.add_argument("--quiet", action="store_true", help="Hide the tool/stage trace")
    p.add_argument(
        "--compact-after",
        type=int,
        default=100,
        metavar="N",
        help="Chat UI: auto-summarize older turns beyond N messages (default 100; set low, e.g. 8, to demo compaction)",
    )
    p.add_argument(
        "--keep-recent",
        type=int,
        default=20,
        metavar="N",
        help="Chat UI: messages kept verbatim after a compaction (default 20)",
    )
    p.set_defaults(func=run)


def _stream(text: str) -> None:
    """Emit the validated response progressively (post-validation streaming)."""
    for line in text.splitlines():
        print(line, flush=True)
    print()


def _run_pipeline(session, args):
    """One-shot through the LangGraph pipeline with a stage trace."""
    from .pipeline import Gateway

    trace = (lambda stage, message: None) if args.quiet else None
    gateway = Gateway(session, model=args.model, max_attempts=args.max_tries, trace=trace)
    if not args.quiet:
        print(f"┌─ pipeline · model {gateway.config.model} · guard {gateway.config.guard_model}")
    state = gateway.ask(args.question)
    usage = state.get("usage") or {}
    if not args.quiet:
        print(
            f"  └─ streaming validated response "
            f"({usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens)\n"
        )
    _stream(Gateway.answer_of(state))


def _run_simple(session, args):
    """One-shot through the plain tool-runner loop."""
    from .chat import ChatSession

    chat = ChatSession(db_session=session, model=args.model)

    def on_tool(block):
        print(f"  🔧 {block.name}({', '.join(f'{k}={v!r}' for k, v in block.input.items())})")

    answer, _ = chat.ask(args.question, on_tool=None if args.quiet else on_tool)
    print(f"\n{answer}\n")


def run(session, args):
    """Dispatch: pipeline one-shot, simple one-shot, or interactive chat UI."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("Agent dependencies missing. Install with: pip install hdh[agent]") from None

    if args.question:
        try:
            if args.simple:
                _run_simple(session, args)
            else:
                _run_pipeline(session, args)
        except anthropic.AuthenticationError:
            raise SystemExit(
                "No valid Anthropic API key. Set ANTHROPIC_API_KEY or run `ant auth login`."
            ) from None
        except KeyboardInterrupt:
            sys.exit(130)
        return

    from .chat import ChatSession

    try:
        from .ui import run_ui
    except ImportError:
        raise SystemExit(
            "Chat UI dependencies missing (rich, prompt_toolkit). Reinstall with: pip install hdh[agent]"
        ) from None
    chat = ChatSession(
        db_session=session, model=args.model, max_messages=args.compact_after, keep_recent=args.keep_recent
    )
    run_ui(chat, compact_after=args.compact_after)
