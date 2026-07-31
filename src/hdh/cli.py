"""
CLI for the Family Medicine synthetic dataset (Health Data Hub).

Core commands:
  hdh generate   --patients 10000 --years 4
  hdh stats
  hdh export     --format json|fhir|text  [--limit N] [--output-dir path]
  hdh advance    --months 6
  hdh add-spike  --condition influenza --multiplier 3.0 --month 12 --n 200
  hdh show       --mrn MRN12345678

Feature modules (hdh.modules.*) register additional subcommands, e.g.:
  hdh care-gaps / hdh risk / hdh agent / hdh narrative / hdh serve
"""

import argparse
import importlib
import random
import sys
from datetime import date, timedelta

from hdh.core.disease_engine import CONDITIONS
from hdh.core.exporters import export_fhir, export_json, export_text, patient_to_text
from hdh.core.generators import build_dataset, generate_visit_history
from hdh.core.models import ChronicCondition, Patient, Visit, get_engine, get_session

# ─── Stats ────────────────────────────────────────────────────────────────────


def cmd_stats(session):
    from sqlalchemy import func

    from hdh.core.models import Diagnosis, LabResult, Prescription, Visit

    n_patients = session.query(func.count(Patient.id)).scalar()
    n_visits = session.query(func.count(Visit.id)).scalar()
    n_dx = session.query(func.count(Diagnosis.id)).scalar()
    n_rx = session.query(func.count(Prescription.id)).scalar()
    n_labs = session.query(func.count(LabResult.id)).scalar()

    print("\n" + "=" * 55)
    print("  FAMILY MEDICINE DATASET — STATISTICS")
    print("=" * 55)
    print(f"  Patients        : {n_patients:>10,}")
    print(f"  Visits          : {n_visits:>10,}")
    print(f"  Diagnoses       : {n_dx:>10,}")
    print(f"  Prescriptions   : {n_rx:>10,}")
    print(f"  Lab Results     : {n_labs:>10,}")
    avg_v = n_visits / n_patients if n_patients else 0
    print(f"  Avg visits/pt   : {avg_v:>10.1f}")
    print("=" * 55)

    # Top 10 diagnoses
    top_dx = (
        session.query(Diagnosis.icd10_code, Diagnosis.description, func.count(Diagnosis.id).label("cnt"))
        .group_by(Diagnosis.icd10_code)
        .order_by(func.count(Diagnosis.id).desc())
        .limit(10)
        .all()
    )
    print("\n  TOP 10 DIAGNOSES:")
    for row in top_dx:
        print(f"    {row.icd10_code:<12} {row.description:<45} {row.cnt:>7,}")
    print()

    # Age distribution
    print("  PATIENT AGE DISTRIBUTION:")
    brackets = [
        ("0–12", 0, 12),
        ("13–17", 13, 17),
        ("18–35", 18, 35),
        ("36–50", 36, 50),
        ("51–65", 51, 65),
        ("66+", 66, 120),
    ]
    today = date.today()
    for label, lo, hi in brackets:
        lo_date = today - timedelta(days=hi * 365 + 365)
        hi_date = today - timedelta(days=lo * 365)
        cnt = (
            session.query(func.count(Patient.id))
            .filter(Patient.date_of_birth.between(lo_date, hi_date))
            .scalar()
        )
        bar = "█" * (cnt * 30 // (n_patients or 1))
        print(f"    {label:<7} {cnt:>7,}  {bar}")
    print()


# ─── Advance Time ─────────────────────────────────────────────────────────────


def cmd_advance(session, months: int):
    """
    Simulate passage of time: add new visits for chronic-condition patients
    and age forward any follow-up visits that were scheduled.
    """
    print(f"\n⏩ Advancing dataset by {months} months...")

    # Focus on patients with chronic conditions — they get follow-up visits
    chronic_patients = session.query(Patient).join(ChronicCondition).distinct().all()

    added = 0
    today = date.today()
    for p in chronic_patients:
        # Probability of generating a new visit per month per chronic patient
        if random.random() > 0.35:
            continue

        fam_hx = {"diabetes": p.fam_hx_diabetes, "hypertension": p.fam_hx_hypertension}
        visit_tuples, _ = generate_visit_history(p, fam_hx, p.smoker, years=1)

        # Keep only visits that fall in the new window
        cutoff = today - timedelta(days=months * 30)
        new_visits = [(v, cp, cn) for v, cp, cn in visit_tuples if v.visit_date >= cutoff]

        from hdh.core.generators import generate_lab, generate_vital
        from hdh.core.models import Diagnosis, Prescription

        for visit, cprofile, _cname in new_visits[:2]:  # cap at 2 new visits per advance
            visit.patient_id = p.id
            session.add(visit)
            session.flush()

            vital = generate_vital(visit.id, p.age, p.sex, p.bmi_baseline, cprofile)
            session.add(vital)

            dx = Diagnosis(
                visit_id=visit.id,
                icd10_code=cprofile.icd10_code,
                description=cprofile.description,
                is_primary=True,
            )
            session.add(dx)

            if cprofile.rx_options:
                rx_spec = random.choice(cprofile.rx_options)
                rx = Prescription(
                    visit_id=visit.id,
                    drug_name=rx_spec.drug_name,
                    drug_class=rx_spec.drug_class,
                    dose=rx_spec.dose,
                    frequency=rx_spec.frequency,
                    duration_days=rx_spec.duration_days,
                    refills=rx_spec.refills,
                    is_new=False,
                )
                session.add(rx)

            for lab_spec in cprofile.labs:
                lr = generate_lab(visit.id, lab_spec)
                session.add(lr)

            added += 1

    session.commit()
    print(f"✅ Added {added} new visits across {len(chronic_patients)} chronic-condition patients.")


# ─── Add Seasonal Spike ───────────────────────────────────────────────────────


def cmd_add_spike(session, condition_name: str, multiplier: float, month: int, n: int):
    """Inject a seasonal spike of a given condition for a given month."""
    if condition_name not in CONDITIONS:
        print(f"❌ Unknown condition '{condition_name}'. Available: {list(CONDITIONS.keys())}")
        return

    cprofile = CONDITIONS[condition_name]
    patients = session.query(Patient).order_by("id").all()

    if not patients:
        print("❌ No patients in the database.")
        return

    from hdh.core.generators import generate_lab, generate_vital
    from hdh.core.models import Diagnosis, Prescription

    # Pick a random day in the requested month (current or last year)
    year = date.today().year if month <= date.today().month else date.today().year - 1
    from calendar import monthrange

    _, max_day = monthrange(year, month)
    selected_patients = random.sample(patients, k=min(n, len(patients)))
    added = 0

    for p in selected_patients:
        vdate = date(year, month, random.randint(1, max_day))

        visit = Visit(
            patient_id=p.id,
            visit_date=vdate,
            visit_type=cprofile.visit_type,
            chief_complaint=cprofile.chief_complaint,
            provider_name=random.choice(["Dr. Sarah Mitchell, MD", "Dr. James O'Brien, MD"]),
            follow_up_days=cprofile.follow_up_days,
        )
        session.add(visit)
        session.flush()

        vital = generate_vital(visit.id, p.age, p.sex, p.bmi_baseline, cprofile)
        session.add(vital)

        dx = Diagnosis(
            visit_id=visit.id,
            icd10_code=cprofile.icd10_code,
            description=cprofile.description,
            is_primary=True,
        )
        session.add(dx)

        if cprofile.rx_options:
            rx_spec = random.choice(cprofile.rx_options)
            rx = Prescription(
                visit_id=visit.id,
                drug_name=rx_spec.drug_name,
                drug_class=rx_spec.drug_class,
                dose=rx_spec.dose,
                frequency=rx_spec.frequency,
                duration_days=rx_spec.duration_days,
                refills=rx_spec.refills,
                is_new=True,
            )
            session.add(rx)

        for lab_spec in cprofile.labs:
            lr = generate_lab(visit.id, lab_spec)
            session.add(lr)

        added += 1

    session.commit()
    print(f"✅ Spike injected: {added} '{condition_name}' visits added for month {month}.")


# ─── Show Patient ─────────────────────────────────────────────────────────────


def cmd_show(session, mrn: str):
    p = session.query(Patient).filter(Patient.mrn == mrn).first()
    if not p:
        print(f"❌ Patient MRN '{mrn}' not found.")
        return
    print(patient_to_text(p))


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    # Windows consoles default to legacy code pages that can't print the
    # box-drawing and status characters used in CLI output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(description="Family Medicine Synthetic Dataset CLI")
    parser.add_argument("--db", default="family_medicine.db", help="Path to SQLite database file")

    sub = parser.add_subparsers(dest="command")

    # generate
    gen_p = sub.add_parser("generate", help="Generate synthetic patients and visits")
    gen_p.add_argument("--patients", type=int, default=10_000)
    gen_p.add_argument("--years", type=int, default=4)
    gen_p.add_argument("--quiet", action="store_true")

    # stats
    sub.add_parser("stats", help="Print dataset statistics")

    # export
    exp_p = sub.add_parser("export", help="Export dataset")
    exp_p.add_argument("--format", choices=["json", "fhir", "text", "all"], default="all")
    exp_p.add_argument("--limit", type=int, default=None)
    exp_p.add_argument("--output-dir", default="exports")

    # advance
    adv_p = sub.add_parser("advance", help="Advance time and add new visits")
    adv_p.add_argument("--months", type=int, default=3)

    # add-spike
    spk_p = sub.add_parser("add-spike", help="Inject a seasonal disease spike")
    spk_p.add_argument("--condition", required=True)
    spk_p.add_argument("--multiplier", type=float, default=2.0)
    spk_p.add_argument("--month", type=int, default=12)
    spk_p.add_argument("--n", type=int, default=100)

    # show
    show_p = sub.add_parser("show", help="Print one patient's chart")
    show_p.add_argument("--mrn", required=True)

    # list-conditions
    sub.add_parser("list-conditions", help="List all available condition codes")

    # Feature-module subcommands (each module defers heavy imports to run time)
    from hdh.modules import CLI_MODULES

    for mod_path in CLI_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        mod.register_cli(sub)

    args = parser.parse_args()

    engine = get_engine(args.db)
    session = get_session(engine)

    if args.command == "generate":
        print(f"\n🏥 Generating {args.patients:,} patients with {args.years} years of history...")
        build_dataset(session, n_patients=args.patients, years_of_history=args.years, verbose=not args.quiet)
        cmd_stats(session)

    elif args.command == "stats":
        cmd_stats(session)

    elif args.command == "export":
        odir = args.output_dir
        lim = args.limit
        if args.format in ("json", "all"):
            export_json(session, f"{odir}/json", limit=lim)
        if args.format in ("fhir", "all"):
            export_fhir(session, f"{odir}/fhir", limit=lim)
        if args.format in ("text", "all"):
            export_text(session, f"{odir}/text", limit=lim)

    elif args.command == "advance":
        cmd_advance(session, args.months)

    elif args.command == "add-spike":
        cmd_add_spike(session, args.condition, args.multiplier, args.month, args.n)

    elif args.command == "show":
        cmd_show(session, args.mrn)

    elif args.command == "list-conditions":
        print("\nAvailable conditions:")
        for k, v in CONDITIONS.items():
            print(f"  {k:<30} [{v.icd10_code}] {v.description}")

    elif hasattr(args, "func"):
        args.func(session, args)

    else:
        parser.print_help()

    session.close()


if __name__ == "__main__":
    main()
