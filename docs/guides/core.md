# Core guide — synthetic data generation

The core engine generates, inspects, exports, and simulates the synthetic
family-medicine dataset. It needs only the base install:

```bash
pip install -e .
```

## Generate a dataset

```bash
hdh generate --patients 100 --years 2          # quick panel (seconds) — the default advice
hdh generate --patients 10000 --years 4        # full-size: SLOW since v0.4.0 (full charts) — prefer the release download
hdh --db mydata.db generate --patients 1000    # custom database path
```

Generation is seeded, so the same parameters reproduce the same dataset. Each
patient gets demographics, insurance, allergies, family history, and a
multi-year visit history whose frequency reflects age and chronic burden
(infants and seniors visit ~5×/year; each chronic condition adds ~1/year).

Every visit carries: one vitals panel, a primary ICD-10 diagnosis,
condition-appropriate prescriptions from a small formulary, LOINC-coded labs
with reference ranges and status flags, and a follow-up interval.

## Inspect

```bash
hdh stats                      # counts, top-10 diagnoses, age distribution
hdh show --mrn MRN12345678     # one patient's complete chart as text
hdh list-conditions            # all conditions, grouped by pack (chronic/staged flagged)
hdh generate --patients 250 --seed 7 --progression-cadence quarterly
                               # reproducible run; staged conditions re-evaluate quarterly
```

Find MRNs to look at via `hdh care-gaps`, `hdh risk score`, or SQL:

```bash
python -c "
from hdh.core import get_engine, get_session, Patient
s = get_session(get_engine('family_medicine.db'))
for p in s.query(Patient).limit(5): print(p.mrn, p.first_name, p.last_name)"
```

## Export

```bash
hdh export --format json --limit 500 --output-dir exports/
hdh export --format fhir --limit 500 --output-dir exports/
hdh export --format text --limit 500 --output-dir exports/
hdh export --format all                 # everything, all patients
```

| Format | One file per patient containing |
|---|---|
| `json` | Full denormalized record: demographics, chronic conditions, every visit with vitals/dx/rx/labs |
| `fhir` | FHIR R4 `Bundle`: Patient, Encounter per visit, Observations (vitals + labs, LOINC), Conditions (ICD-10), MedicationRequests |
| `text` | LLM-ready plain-text chart summary |

## Simulate

```bash
hdh add-spike --condition influenza --month 1 --n 300   # inject a January flu wave
hdh advance --months 6                                  # follow-up visits for chronic patients
```

`add-spike` is useful for testing outbreak detection / seasonal analytics;
`advance` for testing longitudinal pipelines against a moving timeline.

## Python API

```python
from hdh.core import (
    get_engine, get_session, build_dataset,
    Patient, Visit, CONDITIONS,
    patient_to_json, patient_to_fhir_bundle, patient_to_text,
)

session = get_session(get_engine("family_medicine.db"))
p = session.query(Patient).filter(Patient.mrn == "MRN12345678").one()
print(patient_to_text(p))
```

## Adding a condition

Add a `ConditionProfile` to `CONDITIONS` in `src/hdh/core/disease_engine.py`
(ICD-10, chief complaint, vitals deltas, `LabSpec` panels, `RxSpec` formulary,
follow-up days, seasonal weights), then include it in the right age groups in
`pick_condition`. See [CONTRIBUTING.md](../../CONTRIBUTING.md).


## Condition packs and the catalog

Conditions live in **packs** (`ConditionSource` implementations):
`family-medicine-core` (the OPD set) and `cardiometabolic` (CKD with
severity stages, CAD, heart failure, AFib, stroke history, asthma,
anemia). The generator samples through an immutable `ConditionCatalog`
(`hdh.core.conditions`) — frozen profiles, injected RNG, explicit
`SamplingContext`. Chronic onset is two-phase: baseline seeding at chart
start, then annual rolls where established conditions multiply the rates
(`ComorbidityLink`), so CKD arrives after — and because of — the
hypertension years, with onset dates in clinical order.

Tests (or future modules, via `GENERATOR_MODULES`) can inject their own
catalog: `build_dataset(session, ..., catalog=build_catalog([MyPack()]))`.
Duplicate condition names across packs are a hard error — clinical
content never silently overrides. Design: `docs/design/clinical-breadth.md`.
