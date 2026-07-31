# Billing guide (scaffold)

Simulates professional billing for visits: E/M CPT assignment, work RVUs, and
rough charge estimates. Library-only for now — no CLI command.

## Usage

```python
from hdh.modules.billing import cpt_for_visit, estimate_claim

cpt, rvu = cpt_for_visit(visit, patient_age=52)     # ("99213", 1.30)
estimate_claim(visit, patient_age=52)
# {"visit_date": "2025-11-02", "cpt": "99213", "work_rvu": 1.3,
#  "estimated_charge_usd": 43.28}

total = sum(estimate_claim(v, p.age)["estimated_charge_usd"] for v in p.visits)
```

## Coding logic

| Visit | CPT | Work RVU |
|---|---|---|
| Preventive, by age (infant → 65+) | 99381–99387 | 1.50–2.50 |
| Urgent | 99215 | 2.80 |
| Follow-up with ≥ 2 prescriptions | 99214 | 1.92 |
| Everything else (established office) | 99213 | 1.30 |

`estimate_claim` multiplies the work RVU by a CMS-style conversion factor
(default 33.29 $/RVU, overridable) — deliberately simplified: real payment
uses total RVUs (work + practice expense + malpractice) and geographic
adjustment.

## Extending

- Lab/procedure CPTs derived from each visit's LOINC-coded lab orders.
- Full RVU components and GPCI adjustment.
- Claims lifecycle simulation (submitted → adjudicated → paid/denied) keyed to
  each patient's insurer, for testing revenue-cycle analytics.
- An `hdh billing` subcommand (see CONTRIBUTING.md for the `register_cli`
  recipe) and an X12-837P-like JSON claims exporter.
