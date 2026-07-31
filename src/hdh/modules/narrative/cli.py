"""CLI subcommand for SOAP-note narratives.  Registered by hdh.cli."""


def register_cli(subparsers):
    p = subparsers.add_parser("narrative", help="Generate SOAP-note narratives for a patient's visits")
    p.add_argument("--mrn", required=True)
    p.add_argument("--last", type=int, default=3, help="Number of most recent visits (default 3)")
    p.add_argument(
        "--llm",
        action="store_true",
        help="Rewrite notes as natural prose with Claude (requires hdh[agent] + API key)",
    )
    p.set_defaults(func=run)


def run(session, args):
    from hdh.core.models import Patient

    from .soap import patient_soap_notes, polish_with_llm

    p = session.query(Patient).filter(Patient.mrn == args.mrn).first()
    if not p:
        raise SystemExit(f"❌ Patient MRN '{args.mrn}' not found.")

    notes = patient_soap_notes(p, last_n=args.last)
    if args.llm:
        try:
            notes = [polish_with_llm(n) for n in notes]
        except ImportError:
            raise SystemExit("--llm requires: pip install hdh[agent]") from None

    for note in notes:
        print("\n" + note)
    print()
