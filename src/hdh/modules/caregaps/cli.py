"""CLI subcommand for care-gap detection.  Registered by hdh.cli."""

import json
from datetime import date


def register_cli(subparsers):
    p = subparsers.add_parser(
        "care-gaps",
        help="Detect care gaps (overdue preventive care, missed follow-ups, uncontrolled chronic conditions)",
    )
    p.add_argument("--mrn", help="Check a single patient")
    p.add_argument("--limit", type=int, default=25, help="Max gaps to show (default 25)")
    p.add_argument("--as-of", help="Reference date YYYY-MM-DD (default: latest visit in DB)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    p.set_defaults(func=run)


def run(session, args):
    """Detect care gaps and print them as a table or JSON."""
    from .detector import detect_gaps, reference_date

    as_of = date.fromisoformat(args.as_of) if args.as_of else reference_date(session)
    gaps = detect_gaps(session, mrn=args.mrn, limit=args.limit, as_of=as_of)

    if args.json:
        print(json.dumps([g.to_dict() for g in gaps], indent=2))
        return

    print(f"\n🩺 CARE GAPS  (as of {as_of}, showing {len(gaps)})")
    print("=" * 100)
    print(f"{'MRN':<14}{'Patient':<26}{'Age':>4}  {'Severity':<9}{'Type':<22}Description")
    print("-" * 100)
    for g in gaps:
        print(f"{g.mrn:<14}{g.patient_name:<26}{g.age:>4}  {g.severity:<9}{g.gap_type:<22}{g.description}")
    if not gaps:
        print("  No care gaps found 🎉")
    print()
