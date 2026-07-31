"""CLI subcommand for the FHIR REST API.  Registered by hdh.cli."""


def register_cli(subparsers):
    p = subparsers.add_parser("serve", help="Serve the dataset as a FHIR R4 REST API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=run)


def run(session, args):
    """Start the FHIR R4 API with uvicorn."""
    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        raise SystemExit("API dependencies missing. Install with: pip install hdh[api]") from None

    session.close()  # the app manages its own sessions
    app = create_app(db_path=getattr(args, "db", "family_medicine.db"))
    print(f"🌐 FHIR R4 API → http://{args.host}:{args.port}  (docs at /docs)")
    uvicorn.run(app, host=args.host, port=args.port)
