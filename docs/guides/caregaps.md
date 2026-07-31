# Care-gaps guide

Rule-based detection of patients whose care is overdue. No extra dependencies.

## Usage

```bash
hdh care-gaps                          # top 25 gaps, most severe first
hdh care-gaps --limit 100
hdh care-gaps --mrn MRN12345678        # one patient
hdh care-gaps --json                   # machine-readable
hdh care-gaps --as-of 2026-01-15       # evaluate as of a specific date
```

Example output:

```
🩺 CARE GAPS  (as of 2026-02-27, showing 3)
MRN           Patient          Age  Severity  Type                  Description
MRN51248682   Jonathan Ward     41  high      uncontrolled_chronic  Uncontrolled: Essential hypertension — no visit in 882 days
MRN60417139   Alyssa Smith      65  medium    polypharmacy_review   6 distinct medications in the last year, no visit in 240 days
MRN97408434   Amy Jones         41  low       overdue_preventive    No preventive visit in 421 days (interval: 365d)
```

## The rules

| Rule | Severity | Trigger |
|---|---|---|
| `uncontrolled_chronic` | high | A chronic condition flagged uncontrolled, and no visit of any kind in the last 90 days |
| `missed_follow_up` | medium | The latest visit requested a follow-up in N days; N × 1.5 has elapsed with no return |
| `polypharmacy_review` | medium | Age ≥ 65, ≥ 5 distinct drugs in the last 12 months, no visit in 6 months |
| `overdue_preventive` | low | No preventive visit within the age-based interval (under-2s: ~6 months; everyone else: annual) |

**Reference date:** by default, rules evaluate against the latest visit date in
the database rather than today — a generated dataset has a fixed time window,
and this keeps results meaningful regardless of when it was generated.
Override with `--as-of`.

## Python API

```python
from hdh.modules.caregaps import detect_gaps, reference_date

gaps = detect_gaps(session, limit=50)
for g in gaps:
    print(g.severity, g.mrn, g.gap_type, g.description, g.overdue_days)
    g.to_dict()   # JSON-serializable
```

## Tuning and extending

The thresholds are module constants in
`src/hdh/modules/caregaps/detector.py`: `FOLLOW_UP_GRACE`,
`PREVENTIVE_INTERVALS`, `POLYPHARMACY_MIN_DRUGS`,
`POLYPHARMACY_REVIEW_WINDOW`. To add a rule, append to the per-patient loop in
`detect_gaps()` and emit a `CareGap` with a new `gap_type` — the sorting and
CLI rendering pick it up automatically.
