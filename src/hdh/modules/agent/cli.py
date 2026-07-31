"""CLI subcommand for the agentic AI care assistant.  Registered by hdh.cli."""


def register_cli(subparsers):
    p = subparsers.add_parser("agent", help="Ask the AI care-program agent (Claude with database tools)")
    p.add_argument("question", nargs="?", help="Question to ask; omit for interactive mode")
    p.add_argument("--model", help="Override the Claude model (default: claude-opus-5)")
    p.add_argument("--quiet", action="store_true", help="Hide tool-call trace")
    p.set_defaults(func=run)


def run(session, args):
    try:
        import anthropic  # noqa: F401
    except ImportError:
        raise SystemExit("Agent dependencies missing. Install with: pip install hdh[agent]")

    from .agent import run_agent

    def ask(q):
        try:
            answer = run_agent(session, q, model=args.model, verbose=not args.quiet)
        except anthropic.AuthenticationError:
            raise SystemExit(
                "No valid Anthropic API key. Set ANTHROPIC_API_KEY or run `ant auth login`.")
        print(f"\n{answer}\n")

    if args.question:
        ask(args.question)
        return

    print("🩺 hdh care-program agent — interactive mode (blank line to exit)")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        ask(q)
