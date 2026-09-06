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

# ─── Stats ────────────────────────────────────────────────────────────────────
from hdh.core.chartedit.cli import register as register_chart
from hdh.core.chartedit.cli import run as run_chart
from hdh.core.conditions import default_catalog
from hdh.core.exporters import export_fhir, export_json, export_text, patient_to_text
from hdh.core.generators import build_dataset, generate_visit_history
from hdh.core.models import Condition, Patient, Visit, get_engine, get_session
from hdh.core.orders import register as register_orders
from hdh.core.orders import run as run_orders


def cmd_stats(session):
    """Print dataset statistics: counts, top diagnoses, and age distribution."""
    from sqlalchemy import func

    from hdh.core.models import Condition, LabResult, Prescription, Visit

    n_patients = session.query(func.count(Patient.id)).scalar()
    n_visits = session.query(func.count(Visit.id)).scalar()
    n_dx = session.query(func.count(Condition.id)).scalar()
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
        session.query(Condition.icd10_code, Condition.description, func.count(Condition.id).label("cnt"))
        # description is functionally dependent on the code, but PostgreSQL
        # requires every selected column in the GROUP BY (SQLite is lax)
        .group_by(Condition.icd10_code, Condition.description)
        .order_by(func.count(Condition.id).desc())
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
    chronic_patients = (
        session.query(Patient).join(Condition).filter(Condition.chronic.is_(True)).distinct().all()
    )

    added = 0
    today = date.today()
    for p in chronic_patients:
        # Probability of generating a new visit per month per chronic patient
        if random.random() > 0.35:
            continue

        fam_hx = {
            "diabetes": any("diabetes" in h.condition.lower() for h in p.family_history),
            "hypertension": any("hypertension" in h.condition.lower() for h in p.family_history),
        }
        from hdh.core.generators import RunScope, seed_providers

        scope = RunScope(
            catalog=default_catalog(),
            providers=tuple(seed_providers(session)),
            years=1,
            rng=random.Random(),
        )
        visit_tuples = generate_visit_history(p, fam_hx, bool(p.smoker), scope).visits

        # Keep only visits that fall in the new window
        cutoff = today - timedelta(days=months * 30)
        new_visits = [(v, cp, cn) for v, cp, cn in visit_tuples if v.visit_date >= cutoff]

        from hdh.core.generators import generate_lab, generate_vital
        from hdh.core.models import ConditionStatus, Prescription

        for visit, cprofile, _cname in new_visits[:2]:  # cap at 2 new visits per advance
            visit.patient_id = p.id
            session.add(visit)
            session.flush()

            vital = generate_vital(visit.id, p.age, p.sex, p.bmi_baseline, cprofile)
            session.add(vital)

            session.add(
                Condition(
                    patient_id=p.id,
                    visit_id=visit.id,
                    icd10_code=cprofile.icd10_code,
                    description=cprofile.description,
                    chronic=False,
                    status=ConditionStatus.RESOLVED,
                    onset_date=visit.visit_date,
                    resolved_date=visit.visit_date + timedelta(days=14),
                )
            )

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
    catalog = default_catalog()
    if condition_name not in catalog.names():
        print(f"❌ Unknown condition '{condition_name}'. Available: {list(catalog.names())}")
        return

    cprofile = catalog.get(condition_name)
    patients = session.query(Patient).order_by("id").all()

    if not patients:
        print("❌ No patients in the database.")
        return

    from hdh.core.generators import generate_lab, generate_vital
    from hdh.core.models import ConditionStatus, Prescription

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
        )
        session.add(visit)
        session.flush()

        if cprofile.follow_up_days:  # the return visit is an order now (#59)
            from hdh.core.generators import _follow_up_request

            session.add(_follow_up_request(p, visit, cprofile.follow_up_days))

        vital = generate_vital(visit.id, p.age, p.sex, p.bmi_baseline, cprofile)
        session.add(vital)

        session.add(
            Condition(
                patient_id=p.id,
                visit_id=visit.id,
                icd10_code=cprofile.icd10_code,
                description=cprofile.description,
                chronic=False,
                status=ConditionStatus.RESOLVED,
                onset_date=vdate,
                resolved_date=vdate + timedelta(days=14),
            )
        )

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


def cmd_migrate(args):
    """Copy the SQLite database into the HDH_DB_URL target and report results."""
    import os

    from sqlalchemy import create_engine

    from hdh.core.migrate import MigrationError, migrate_sqlite

    source = args.source or args.db
    target_url = args.target or os.environ.get("HDH_DB_URL")
    if not target_url:
        raise SystemExit("hdh migrate: no target — set HDH_DB_URL (run `just deps`) or pass --target")
    if not os.path.exists(source):
        raise SystemExit(f"hdh migrate: source '{source}' not found")

    target = create_engine(target_url, echo=False)
    print(f"\n🚚 Migrating {source} → {target.url.render_as_string(hide_password=True)}")
    try:
        results = migrate_sqlite(source, target, batch_size=args.batch, force=args.force)
    except MigrationError as err:
        raise SystemExit(f"hdh migrate: {err}") from None
    finally:
        target.dispose()
    for r in results:
        flag = "✓" if r.verified else "✗ COUNT MISMATCH"
        print(f"   {r.table:<20} {r.rows:>10,} rows  {flag}")
    if all(r.verified for r in results):
        print("✅ Migration verified — source file left untouched.")
    else:
        raise SystemExit("hdh migrate: verification failed (see table counts above)")


def _build_parser() -> argparse.ArgumentParser:
    """Every subcommand this CLI accepts.

    Split out of main() so dispatch reads as dispatch: the parser is
    configuration, and it grows with every feature module added.
    """
    parser = argparse.ArgumentParser(description="Family Medicine Synthetic Dataset CLI")
    parser.add_argument("--db", default="family_medicine.db", help="Path to SQLite database file")

    sub = parser.add_subparsers(dest="command")

    # generate
    gen_p = sub.add_parser("generate", help="Generate synthetic patients and visits")
    gen_p.add_argument("--patients", type=int, default=10_000)
    gen_p.add_argument("--years", type=int, default=4)
    gen_p.add_argument("--quiet", action="store_true")
    gen_p.add_argument("--seed", type=int, default=None, help="Reproducible run: same seed, same dataset")
    gen_p.add_argument(
        "--progression-cadence",
        choices=("yearly", "quarterly"),
        default="yearly",
        help="How often staged chronic conditions re-evaluate severity",
    )

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

    # authentication (AU1): login / logout / whoami — no DB session needed
    from hdh.core.identity.cli import register_cli as register_auth_cli

    register_auth_cli(sub)

    # identity-seed (AU2): link the demo accounts to provider profiles
    sub.add_parser(
        "identity-seed",
        help="Link the demo Keycloak users to provider profiles (idempotent)",
    )

    # list-conditions
    sub.add_parser("list-conditions", help="List all available condition codes")

    # schema
    sub.add_parser("schema", help="Describe the schema registry: modules, extensions, new entities")
    sub.add_parser(
        "db-init",
        help="Install the PostgreSQL extensions the modules need (pg_trgm, vector). Idempotent.",
    )

    # migrate (SQLite → HDH_DB_URL; the exit path from SQLite)
    mig_p = sub.add_parser("migrate", help="Copy a SQLite database into HDH_DB_URL (PostgreSQL)")
    mig_p.add_argument("--source", default=None, help="SQLite file to copy (default: --db)")
    mig_p.add_argument("--target", default=None, help="Target URL (default: HDH_DB_URL)")
    mig_p.add_argument("--batch", type=int, default=5000)
    mig_p.add_argument("--force", action="store_true", help="Clear existing rows in the target first")

    register_chart(sub)  # chart maintenance: amend / void / audit trail
    register_orders(sub)  # service requests: what the chart asked for

    # Feature-module subcommands (each module defers heavy imports to run time)
    from hdh.modules import CLI_MODULES

    for mod_path in CLI_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        mod.register_cli(sub)

    return parser


def main():
    """CLI entry point: parse arguments, open the DB session, dispatch the command."""
    # Windows consoles default to legacy code pages that can't print the
    # box-drawing and status characters used in CLI output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    parser = _build_parser()

    args = parser.parse_args()

    from hdh.core.schema_registry import bootstrap_schema

    schema_registry = bootstrap_schema()

    if args.command == "migrate":
        cmd_migrate(args)
        return

    # Identity commands touch no chart, so they run before any engine opens
    # — a login must work against a database that has not been built yet.
    if args.command in ("login", "logout", "whoami"):
        from hdh.core.identity import cli as auth_cli

        if args.command == "login":
            raise SystemExit(auth_cli.cmd_login(args))
        if args.command == "logout":
            raise SystemExit(auth_cli.cmd_logout(args))
        raise SystemExit(auth_cli.cmd_whoami(args))

    engine = get_engine(args.db)
    session = get_session(engine)

    if args.command == "generate":
        print(f"\n🏥 Generating {args.patients:,} patients with {args.years} years of history...")
        build_dataset(
            session,
            n_patients=args.patients,
            years_of_history=args.years,
            verbose=not args.quiet,
            seed=args.seed,
            progression_cadence=args.progression_cadence,
        )
        cmd_stats(session)

    elif args.command == "chart":
        run_chart(session, args)

    elif args.command == "orders":
        run_orders(session, args)

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

    elif args.command == "identity-seed":
        from hdh.core.identity.seed import seed_demo_identities

        n = seed_demo_identities(session)
        print(f"linked {n} demo accounts to provider profiles.")

    elif args.command == "show":
        cmd_show(session, args.mrn)

    elif args.command == "list-conditions":
        catalog = default_catalog()
        by_pack: dict[str, list[str]] = {}
        for name in catalog.names():
            by_pack.setdefault(catalog.pack_of(name), []).append(name)
        for pack, names in sorted(by_pack.items()):
            print(f"\n{pack} ({len(names)} conditions):")
            for name in names:
                profile = catalog.get(name)
                chronic = " (chronic)" if profile.chronic else ""
                staged = " (staged)" if profile.staging else ""
                print(f"  {name:<30} [{profile.icd10_code}] {profile.description}{chronic}{staged}")

    elif args.command == "schema":
        print(schema_registry.describe())

    elif args.command == "db-init":
        # The CLI composes: core installs what core needs, and each module
        # contributes its own. Core never learns the module exists.
        from hdh.core.dbinit import initialise
        from hdh.modules.careplan import dbsetup as careplan_db

        report = initialise(session, extra=careplan_db.EXTENSIONS)
        report.embedding_column = careplan_db.ensure_embedding_column(session)
        print()
        for line in report.lines():
            print(line)
        print()
        if not report.ok:
            # Not an error exit: a server without pgvector is a real
            # deployment, and the modules that need it refuse clearly on
            # their own. Saying which feature is unavailable beats failing
            # the setup step that was otherwise successful.
            print("  Some features will be unavailable. Everything else is ready.")
        else:
            print("  Database ready.")

    elif hasattr(args, "func"):
        args.func(session, args)

    else:
        parser.print_help()

    session.close()


if __name__ == "__main__":
    main()
