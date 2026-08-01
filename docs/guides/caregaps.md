# Care-gaps guide

Detection of patients whose care is overdue, with **two pluggable finders**
behind one interface: a deterministic rule engine (default, no dependencies)
and an AI chart reviewer.

## Usage

```bash
hdh care-gaps                          # rules: top 25 gaps, most severe first
hdh care-gaps --limit 100
hdh care-gaps --mrn MRN12345678        # one patient
hdh care-gaps --json                   # machine-readable (includes `source`)
hdh care-gaps --as-of 2026-01-15       # evaluate as of a specific date

hdh care-gaps --finder ai --mrn MRN12345678   # AI chart review, one patient
hdh care-gaps --finder ai --sample 5          # AI: the 5 most complex patients
```

## Choosing a finder

| | `--finder rules` (default) | `--finder ai` |
|---|---|---|
| How it works | Four fixed rules over aggregate SQL | A Claude model reads each chart and reasons clinically (schema-enforced JSON out) |
| Coverage | Exactly what the rules encode | Also gaps no rule expresses: stale HbA1c for a diabetic, duplicate/interacting meds, guideline medication gaps, unaddressed elevated BPs |
| Determinism | Same input → same output, auditable | Non-deterministic; findings vary between runs |
| Cost & speed | Free, instant, whole panel | One model call per chart (cents each); use `--mrn` or `--sample N` (default 5 most-complex patients) — never the whole panel |
| Requirements | none | `hdh[agent]` extra + `ANTHROPIC_API_KEY` |

Both produce the same `CareGap` records (the `source` field says which finder
found it), so downstream consumers — the JSON export, the agent's
`get_care_gaps` tool — work with either. A practical pattern: run rules for
the panel-wide outreach list, then AI review on the handful of most complex
patients.

Real example — same patient, both finders: rules found one gap (missed
follow-up); the AI found that gap **plus** concurrent atorvastatin +
simvastatin (contraindicated duplication), a 6-month-old HbA1c in an
uncontrolled diabetic, and elevated BPs with no dedicated follow-up.

## Plugging in your own finder

Implement the `GapFinder` protocol and register it — the CLI picks it up:

```python
# in hdh/modules/caregaps/finder.py (or your own module at import time)
class MyFinder:
    name = "mine"
    description = "My strategy"
    def find(self, session, *, mrn=None, limit=None, as_of=None, sample=5):
        return [CareGap(...), ...]

FINDERS["mine"] = MyFinder()      # now: hdh care-gaps --finder mine
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
