"""
Risk model training and scoring.

The model is a histogram gradient-boosting classifier over the features in
``features.py``. Risk tiers are assigned from the training-set probability
distribution: high = top 10%, moderate = next 20%, low = the rest. The trained
model, feature names, tier thresholds, and evaluation AUC are saved together
in one joblib artifact.
"""

from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from hdh.core.models import Visit
from .features import extract_features, FEATURE_NAMES, DEFAULT_HORIZON_DAYS

DEFAULT_MODEL_PATH = "artifacts/risk_model.joblib"

TIER_QUANTILES = {"high": 0.90, "moderate": 0.70}


def _latest_visit_date(session: Session) -> date:
    return session.query(func.max(Visit.visit_date)).scalar() or date.today()


def train(session: Session, horizon_days: int = DEFAULT_HORIZON_DAYS,
          model_path: str = DEFAULT_MODEL_PATH, seed: int = 42) -> dict:
    """Train and persist the risk model; returns evaluation metadata."""
    import numpy as np
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score

    # Hold out the last `horizon_days` of data as the label window
    cutoff = _latest_visit_date(session) - timedelta(days=horizon_days)
    mrns, rows, labels = extract_features(session, cutoff, horizon_days)

    X = np.asarray(rows, dtype=float)
    y = np.asarray(labels, dtype=int)
    if y.sum() < 10:
        raise RuntimeError(
            f"Only {y.sum()} positive labels in the horizon window — "
            "generate a larger dataset before training."
        )

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.25, random_state=seed, stratify=y)

    clf = HistGradientBoostingClassifier(random_state=seed)
    clf.fit(X_tr, y_tr)
    auc = roc_auc_score(y_te, clf.predict_proba(X_te)[:, 1])

    train_probs = clf.predict_proba(X_tr)[:, 1]
    thresholds = {tier: float(np.quantile(train_probs, q))
                  for tier, q in TIER_QUANTILES.items()}

    artifact = {
        "model": clf,
        "feature_names": FEATURE_NAMES,
        "cutoff": cutoff.isoformat(),
        "horizon_days": horizon_days,
        "thresholds": thresholds,
        "auc": float(auc),
        "n_patients": len(y),
        "positive_rate": float(y.mean()),
    }
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path)
    return artifact


def _tier(prob: float, thresholds: dict) -> str:
    if prob >= thresholds["high"]:
        return "high"
    if prob >= thresholds["moderate"]:
        return "moderate"
    return "low"


def score(session: Session, model_path: str = DEFAULT_MODEL_PATH,
          mrn: str = None, top: int = None) -> list[dict]:
    """Score patients as of the latest data; returns rows sorted by risk."""
    import numpy as np
    import joblib

    if not Path(model_path).exists():
        raise FileNotFoundError(
            f"No trained model at {model_path} — run `hdh risk train` first.")
    artifact = joblib.load(model_path)

    cutoff = _latest_visit_date(session)
    mrns, rows, _ = extract_features(session, cutoff,
                                     artifact["horizon_days"],
                                     with_labels=False)
    X = np.asarray(rows, dtype=float)
    probs = artifact["model"].predict_proba(X)[:, 1]

    idx = {name: i for i, name in enumerate(artifact["feature_names"])}
    results = []
    for m, row, prob in zip(mrns, rows, probs):
        if mrn and m != mrn:
            continue
        results.append({
            "mrn": m,
            "risk_probability": round(float(prob), 4),
            "risk_tier": _tier(prob, artifact["thresholds"]),
            "age": int(row[idx["age"]]),
            "chronic_conditions": int(row[idx["n_chronic"]]),
            "uncontrolled": int(row[idx["n_uncontrolled"]]),
            "urgent_visits_12mo": int(row[idx["urgent_visits_12mo"]]),
            "critical_labs_12mo": int(row[idx["critical_labs_12mo"]]),
        })

    results.sort(key=lambda r: -r["risk_probability"])
    if top:
        results = results[:top]
    return results
