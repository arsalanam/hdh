"""CLI subcommands for the media I/O service.  Registered by hdh.cli."""


def register_cli(subparsers):
    """Register ``hdh serve-media`` and ``hdh media-benchmark``."""
    p = subparsers.add_parser("serve-media", help="Serve the media I/O service (STT/TTS/OCR) over HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument(
        "--backend",
        default=None,
        help="null | local | aws  (default: $HDH_MEDIA_BACKEND, else null)",
    )
    p.set_defaults(func=run)

    b = subparsers.add_parser(
        "media-benchmark",
        help="Measure STT engines (NeMo vs WhisperX) on labeled clinic audio — the WER/DER gate",
    )
    b.add_argument(
        "manifest", help="JSON: a list of {name, audio, reference_text, reference_segments, duration_s}"
    )
    b.add_argument("--candidate", default="nemo", help="The engine on trial (default: nemo)")
    b.add_argument("--baseline", default="whisperx", help="The engine to beat (default: whisperx)")
    b.add_argument(
        "--wer-margin", type=float, default=0.05, help="Max WER the candidate may trail the baseline"
    )
    b.add_argument(
        "--der-margin", type=float, default=0.05, help="Max DER the candidate may trail the baseline"
    )
    b.set_defaults(func=run_benchmark)


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


def run_benchmark(session, args):
    """Run the WER/DER gate from a manifest of labeled clips, print the result.

    This is the GPU step: it builds the real NeMo and WhisperX engines and runs
    them on ``args.manifest``'s audio, so it needs ``hdh[media]`` and a CUDA GPU.
    CI exercises the same metrics and gate with fakes (tests/test_media_benchmark)."""
    import json
    from pathlib import Path

    from hdh.modules.media.benchmark import (
        BenchmarkCase,
        decide_gate,
        mean_der,
        mean_wer,
    )

    session.close()  # stateless; no DB

    manifest_path = Path(args.manifest)
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise SystemExit(f"cannot read manifest {args.manifest}: {err}") from None

    cases = []
    for entry in entries:
        audio_path = (manifest_path.parent / entry["audio"]).resolve()
        try:
            audio = audio_path.read_bytes()
        except OSError as err:
            raise SystemExit(f"cannot read audio {audio_path}: {err}") from None
        cases.append(
            BenchmarkCase(
                name=entry.get("name", audio_path.name),
                audio=audio,
                content_type=entry.get("content_type", "audio/wav"),
                reference_text=entry.get("reference_text", ""),
                reference_segments=entry.get("reference_segments", []),
                duration_s=float(entry.get("duration_s", 0.0)),
            )
        )
    if not cases:
        raise SystemExit("manifest is empty — add at least one labeled clip")

    engines = _benchmark_engines(args.candidate, args.baseline)
    from hdh.modules.media.benchmark import run_benchmark as _run

    scores = _run(engines, cases)

    print(f"\n  STT benchmark — {len(cases)} clip(s), engines: {', '.join(engines)}\n")
    print(f"  {'engine':<18}{'case':<22}{'WER':>8}{'DER':>8}{'RTF':>8}")
    print(f"  {'-' * 62}")
    for s in scores:
        flag = f"  ⚠ {s.error}" if s.error else ""
        print(f"  {s.engine:<18}{s.case:<22}{s.wer:>8.3f}{s.der:>8.3f}{s.rtf:>8.2f}{flag}")
    print(f"  {'-' * 62}")
    for name in engines:
        print(f"  {name:<18}{'MEAN':<22}{mean_wer(scores, name):>8.3f}{mean_der(scores, name):>8.3f}")

    decision = decide_gate(
        scores,
        candidate=args.candidate,
        baseline=args.baseline,
        wer_margin=args.wer_margin,
        der_margin=args.der_margin,
    )
    verdict = "✅ PASS" if decision.candidate_passed else "❌ FAIL"
    print(f"\n  Gate: {verdict} — lock in: {decision.locked}")
    print(f"  {decision.reason}\n")


def _benchmark_engines(candidate: str, baseline: str) -> dict:
    """Build the named engines from media/local.py (GPU + the media extra)."""
    from hdh.modules.media.local import NeMoTranscriber, WhisperXTranscriber

    factories = {"nemo": NeMoTranscriber, "whisperx": WhisperXTranscriber}
    engines = {}
    for name in (baseline, candidate):
        if name not in factories:
            raise SystemExit(f"unknown engine {name!r} (expected: {', '.join(factories)})")
        engines[name] = factories[name]()
    return engines
