"""CLI subcommand for the media I/O service.  Registered by hdh.cli."""


def register_cli(subparsers):
    """Register ``hdh serve-media``."""
    p = subparsers.add_parser("serve-media", help="Serve the media I/O service (STT/TTS/OCR) over HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument(
        "--backend",
        default=None,
        help="null | local | aws  (default: $HDH_MEDIA_BACKEND, else null)",
    )
    p.set_defaults(func=run)


def run(session, args):
    """Start the media service with uvicorn."""
    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        raise SystemExit("Media service dependencies missing. Install with: pip install hdh[api]") from None

    session.close()  # the service is stateless; it opens no DB session
    app = create_app(backend=getattr(args, "backend", None))
    print(f"🎙️  HDH Media Service → http://{args.host}:{args.port}  (POST /stt /tts /ocr, docs at /docs)")
    uvicorn.run(app, host=args.host, port=args.port)
