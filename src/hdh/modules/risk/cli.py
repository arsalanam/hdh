"""CLI subcommands for risk stratification.  Registered by hdh.cli."""

import json


def register_cli(subparsers):
    p = subparsers.add_parser("risk", help="ML risk stratification (train / score)")
    risk_sub = p.add_subparsers(dest="risk_cmd", required=True)

    tr = risk_sub.add_parser("train", help="Train the risk model on this dataset")
    tr.add_argument("--horizon", type=int, default=180,
                    help="Prediction horizon in days (default 180)")
    tr.add_argument("--model-path", default="artifacts/risk_model.joblib")

    sc = risk_sub.add_parser("score", help="Score patients with the trained model")
    sc.add_argument("--mrn", help="Score a single patient")
    sc.add_argument("--top", type=int, default=20, help="Show top-N riskiest (default 20)")
    sc.add_argument("--model-path", default="artifacts/risk_model.joblib")
    sc.add_argument("--json", action="store_true", help="Emit JSON instead of a table")

    p.set_defaults(func=run)


def run(session, args):
    try:
        from . import model
    except ImportError as e:
        raise SystemExit(
            f"Risk module dependencies missing ({e.name}). "
            "Install with: pip install hdh[risk]")

    if args.risk_cmd == "train":
        print("🧮 Extracting features and training risk model...")
        art = model.train(session, horizon_days=args.horizon,
                          model_path=args.model_path)
        print(f"✅ Model saved → {args.model_path}")
        print(f"   Patients: {art['n_patients']:,}  |  "
              f"Positive rate: {art['positive_rate']:.1%}  |  "
              f"Held-out ROC AUC: {art['auc']:.3f}")
        print(f"   Tier thresholds: high ≥ {art['thresholds']['high']:.3f}, "
              f"moderate ≥ {art['thresholds']['moderate']:.3f}")

    elif args.risk_cmd == "score":
        rows = model.score(session, model_path=args.model_path,
                           mrn=args.mrn, top=args.top)
        if args.json:
            print(json.dumps(rows, indent=2))
            return
        print(f"\n📈 RISK STRATIFICATION  (top {len(rows)})")
        print("=" * 88)
        print(f"{'MRN':<14}{'Prob':>7}  {'Tier':<10}{'Age':>4}"
              f"{'Chronic':>9}{'Uncontrolled':>14}{'Urgent12m':>11}{'CritLabs':>10}")
        print("-" * 88)
        for r in rows:
            print(f"{r['mrn']:<14}{r['risk_probability']:>7.3f}  {r['risk_tier']:<10}"
                  f"{r['age']:>4}{r['chronic_conditions']:>9}{r['uncontrolled']:>14}"
                  f"{r['urgent_visits_12mo']:>11}{r['critical_labs_12mo']:>10}")
        print()
