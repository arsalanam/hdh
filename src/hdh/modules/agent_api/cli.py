"""CLI subcommand for the agent HTTP API.  Registered by hdh.cli."""


def register_cli(subparsers):
    """Register ``hdh serve-agent``."""
    p = subparsers.add_parser("serve-agent", help="Serve the hdh agent over HTTP (the UI's backend)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.set_defaults(func=run)


def run(session, args):
    """Start the agent API with uvicorn."""
    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        raise SystemExit(
            "Agent API dependencies missing. Install with: pip install hdh[agent,api]"
        ) from None

    session.close()  # the app opens its own session per request
    app = create_app(db_path=getattr(args, "db", "family_medicine.db"))
    print(f"🤖 HDH Agent API → http://{args.host}:{args.port}  (POST /ask, docs at /docs)")
    uvicorn.run(app, host=args.host, port=args.port)
