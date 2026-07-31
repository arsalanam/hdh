# Risk stratification guide

An ML model that predicts near-term clinical deterioration per patient.

```bash
pip install -e ".[risk]"      # scikit-learn, numpy, joblib
```

## What it predicts

**Label:** an urgent visit *or* a critical lab result within the next 180 days
(the prediction horizon, configurable with `--horizon`).

**Features (17):** age, sex, smoker, baseline BMI, family-history count,
chronic-condition count and uncontrolled count, visit counts by type over the
prior 12 months, distinct drugs, high/critical lab counts, and vitals
aggregates (mean/max systolic BP, min SpO2, mean pain).

**Training design:** the last 180 days of the dataset are held out as the
label window; features come from the 12 months before that cutoff. This is a
proper temporal split — the model never sees the future it predicts.

## Train

```bash
hdh risk train
# 🧮 Extracting features and training risk model...
# ✅ Model saved → artifacts/risk_model.joblib
#    Patients: 10,000  |  Positive rate: 6.4%  |  Held-out ROC AUC: 0.714
#    Tier thresholds: high ≥ 0.144, moderate ≥ 0.054
```

The artifact bundles the classifier (`HistGradientBoostingClassifier`),
feature names, horizon, tier thresholds (train-set quantiles: top 10% = high,
next 20% = moderate), and the evaluation AUC. Artifacts are gitignored —
retrain per dataset.

## Score

```bash
hdh risk score --top 20            # riskiest patients
hdh risk score --mrn MRN12345678   # one patient
hdh risk score --json              # machine-readable
```

Scoring computes features as of the latest data and reports probability, tier,
and the main drivers (age, chronic/uncontrolled counts, urgent visits,
critical labs). On the shipped dataset the high tier is dominated by
multimorbid seniors with uncontrolled conditions — a sanity check that the
model learned something clinically plausible.

## Python API

```python
from hdh.modules.risk import model as risk_model
from hdh.modules.risk.features import extract_features, FEATURE_NAMES

artifact = risk_model.train(session, horizon_days=180)
rows = risk_model.score(session, top=50)          # list of dicts
mrns, X, y = extract_features(session, cutoff)     # raw features for your own models
```

## Notes and limitations

- This predicts *synthetic* dynamics — use it to test pipelines, ranking UIs,
  and agent integrations, not to draw clinical conclusions.
- `hdh risk train` needs enough positive labels (≥10); tiny dev datasets may
  refuse to train — generate ≥ a few thousand patients.
- Extension ideas: calibration curves, SHAP-style explanations, a
  time-to-event model. Feature extraction is deliberately separate from the
  classifier so you can swap models without touching SQL.
