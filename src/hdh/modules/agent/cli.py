"""CLI subcommand for the agentic AI care assistant.  Registered by hdh.cli."""


def register_cli(subparsers):
    p = subparsers.add_parser("agent", help="AI care-program agent — interactive chat with history, or one-shot question")
    p.add_argument("question", nargs="?", help="One-shot question; omit to start interactive chat")
    p.add_argument("--model", help="Override the Claude model (default: claude-opus-5)")
    p.add_argument("--quiet", action="store_true", help="Hide tool-call trace (one-shot mode)")
    p.add_argument("--compact-after", type=int, default=100, metavar="N",
                   help="Auto-summarize older turns once the conversation exceeds N messages (default 100; set low, e.g. 8, to demo compaction)")
    p.add_argument("--keep-recent", type=int, default=20, metavar="N",
                   help="Messages kept verbatim after a compaction (default 20)")
    p.set_defaults(func=run)


def run(session, args):
    try:
        import anthropic  # noqa: F401
    except ImportError:
        raise SystemExit("Agent dependencies missing. Install with: pip install hdh[agent]")

    from .chat import ChatSession

    chat = ChatSession(db_session=session, model=args.model,
                       max_messages=args.compact_after,
                       keep_recent=args.keep_recent)

    if args.question:  # one-shot
        def on_tool(block):
            print(f"  🔧 {block.name}({', '.join(f'{k}={v!r}' for k, v in block.input.items())})")
        try:
            answer, _ = chat.ask(args.question, on_tool=None if args.quiet else on_tool)
        except anthropic.AuthenticationError:
            raise SystemExit(
                "No valid Anthropic API key. Set ANTHROPIC_API_KEY or run `ant auth login`.")
        print(f"\n{answer}\n")
        return

    try:
        from .ui import run_ui
    except ImportError:
        raise SystemExit(
            "Chat UI dependencies missing (rich, prompt_toolkit). "
            "Reinstall with: pip install hdh[agent]")
    run_ui(chat, compact_after=args.compact_after)
