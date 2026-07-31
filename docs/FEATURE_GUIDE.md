# Family Medicine Synthetic Dataset — Feature Guide

**10,000 patients · 165,000+ visits · 777,000+ lab results**

*A medically realistic synthetic OPD dataset for testing agentic AI care programs*

Stack: Python · Faker · SQLAlchemy · SQLite · Alembic

---

## Contents

1. [Overview](#1-overview)
2. [Key Features at a Glance](#2-key-features-at-a-glance)
3. [Getting Started](#3-getting-started)
4. [Data Model](#4-data-model)
5. [The Disease Engine](#5-the-disease-engine)
6. [Command-Line Interface](#6-command-line-interface)
7. [Export Formats](#7-export-formats)
8. [Extensible Schema Architecture](#8-extensible-schema-architecture)
9. [Dataset Statistics](#9-dataset-statistics)
10. [Roadmap & Possible Extensions](#10-roadmap--possible-extensions)

---

## 1. Overview

The Family Medicine Synthetic Dataset is a generator and pre-built database of
medically realistic outpatient (OPD) records. It produces 10,000 synthetic patients
with roughly four years of visit history — 165,000+ visits, 165,000+ diagnoses,
170,000+ prescriptions, and 777,000+ lab results — all driven by age, sex, and
seasonal disease probabilities.

The dataset is purpose-built as a safe, no-PHI sandbox for developing and testing
agentic AI care programs, clinical decision tools, FHIR pipelines, and analytics —
without touching real patient data.

### Who it's for

- AI/ML engineers building or evaluating agentic care programs.
- Healthcare integration developers testing FHIR R4 pipelines.
- Data scientists prototyping risk-stratification or care-gap models.
- Anyone needing realistic clinical data without privacy or compliance overhead.

## 2. Key Features at a Glance

| Feature | Description |
|---|---|
| Realistic generation | Age/sex/season-weighted disease probability engine with comorbidity seeding. |
| 30 conditions | Coverage spanning pediatric, adolescent, adult, and senior care. |
| Full clinical detail | Vitals, ICD-10 diagnoses, formulary-accurate prescriptions, and LOINC-coded labs. |
| Three export formats | Per-patient JSON, FHIR R4 Bundles, and LLM-ready plain-text charts. |
| Maintenance CLI | Generate, inspect, export, inject disease spikes, and advance time. |
| Pre-built database | Ships with a ~87 MB SQLite database of 10,000 patients. |
| Extensible schema | JSON-driven, modular schema registry with Alembic migration support. |

## 3. Getting Started

Install dependencies and generate a dataset in a few commands:

```bash
pip install -r requirements.txt

# Generate 10,000 patients (4 years of history)
python cli.py generate --patients 10000 --years 4

# View statistics
python cli.py stats

# Export to JSON / FHIR R4 / Plain text
python cli.py export --format all --limit 500 --output-dir exports/

# Show one patient chart
python cli.py show --mrn MRN12345678
```

A pre-generated database (`family_medicine.db`) is included, so you can run stats,
show, and export commands immediately without regenerating.

### Project Files

| File | Purpose |
|---|---|
| `models.py` | SQLAlchemy ORM — Patient, Visit, Vital, Diagnosis, Prescription, LabResult. |
| `disease_engine.py` | Age/sex/season probability engine, ICD-10 codes, medication formularies. |
| `generators.py` | Patient and visit-history generators. |
| `exporters.py` | JSON, FHIR R4 Bundle, and plain-text clinical-note exporters. |
| `cli.py` | Maintenance command-line interface. |
| `family_medicine.db` | Pre-generated SQLite database (10,000 patients). |

## 4. Data Model

The schema is defined in `models.py` as SQLAlchemy ORM classes. Each patient anchors
a tree of visits, and each visit carries its own vitals, diagnoses, prescriptions,
and labs.

| Model | Table | Key Columns |
|---|---|---|
| Patient | `patients` | mrn, dob, sex, race, insurance, allergies, fam_hx_*, smoker, bmi_baseline |
| ChronicCondition | `chronic_conditions` | patient_id, icd10_code, onset_date, controlled |
| Visit | `visits` | patient_id, visit_date, visit_type, chief_complaint, provider_name, follow_up_days |
| Vital | `vitals` | visit_id, bp_systolic, bp_diastolic, hr, rr, temp_f, spo2, weight_kg, bmi, pain_scale |
| Diagnosis | `diagnoses` | visit_id, icd10_code, description, is_primary |
| Prescription | `prescriptions` | visit_id, drug_name, drug_class, dose, frequency, duration_days, refills, is_new |
| LabResult | `lab_results` | visit_id, test_name, value, unit, ref_low, ref_high, status, loinc_code |

### Enums

- **Sex:** M / F
- **VisitType:** acute / follow_up / preventive / urgent
- **LabStatus:** normal / high / low / critical

### Key Relationships

```
Patient ──< Visit ──< Diagnosis
                 ──< Prescription
                 ──< LabResult
                 ──1 Vital
Patient ──< ChronicCondition
```

## 5. The Disease Engine

The disease engine (`disease_engine.py`) is the heart of the realism. Each condition is
modeled as a `ConditionProfile` dataclass that defines exactly how a visit for that
condition should look.

### Each ConditionProfile defines

- `icd10_code`, `description`, `chief_complaint`, and `visit_type`.
- Vital deltas from baseline (mean, sd) for BP, HR, RR, temperature, SpO2, and pain.
- `labs` — which lab panels to order (`LabSpec`).
- `rx_options` — condition-appropriate formulary entries (`RxSpec`).
- `follow_up_days` — a clinical-guideline follow-up interval.
- `seasonal_weights` — month-to-multiplier seasonal weighting.

### 30 Conditions by Age Group

| Age Group | Conditions |
|---|---|
| Infant 0–2 | Well-child, otitis media, RSV, febrile illness, rash/eczema, conjunctivitis, URI |
| Child 3–12 | Well-child, otitis media, strep throat, URI, febrile illness, rash, conjunctivitis, sports injury |
| Teen 13–17 | Sports physical, URI, sports injury, acne, anxiety, strep, mono |
| Young Adult 18–35 | Annual physical, influenza, URI, UTI, anxiety, low back pain, laceration, contraception, GERD |
| Adult 36–50 | Annual physical, HTN, hyperlipidemia, T2DM, URI, influenza, GERD, anxiety, back pain, obesity |
| Middle-aged 51–65 | Annual physical, HTN, T2DM, hyperlipidemia, osteoarthritis, GERD, URI, COPD, depression, hypothyroidism |
| Senior 65+ | Annual wellness, HTN, T2DM, hyperlipidemia, osteoarthritis, COPD, falls, polypharmacy review, depression, hypothyroidism, influenza |

### Seasonal Multipliers

Disease incidence is weighted by month so the dataset shows realistic seasonality.

```python
FLU_SEASON  = {Jan:2.5, Feb:2.0, ..., Dec:2.5}   # peaks winter
RSV_SEASON  = {Jan:2.0, Feb:1.5, ..., Dec:2.5}   # peaks late fall/winter
SUMMER_PEAK = {Jun:1.5, Jul:1.5, Aug:1.5, ...}   # UTI, sports injuries, lacerations
```

### Comorbidity Seeding

Chronic conditions are seeded probabilistically from age, family history, smoking
status, and BMI — producing realistic clusters of comorbidities.

```python
def comorbidity_seeds(age, fam_hx, smoker, bmi) -> set[str]:
    # age >= 45: seeds HTN (30%), T2DM (20%), hyperlipidemia (35%)
    # age >= 60: seeds COPD if smoker, hypothyroidism (25%), OA (40%)
```

## 6. Command-Line Interface

All maintenance tasks are driven through `cli.py`.

| Command | Purpose |
|---|---|
| `generate --patients N --years Y` | Generate a fresh dataset. |
| `stats` | Print dataset statistics. |
| `export --format {json\|fhir\|text\|all} --limit N --output-dir DIR` | Export records in one or all formats. |
| `show --mrn MRN########` | Print a single patient's full chart. |
| `list-conditions` | List all available condition codes. |
| `add-spike --condition NAME --month M --n N` | Inject a seasonal disease spike. |
| `advance --months M` | Advance time, adding follow-up visits for chronic patients. |

Example — inject 300 extra influenza visits in January:

```bash
python cli.py add-spike --condition influenza --month 1 --n 300
```

Example — advance the timeline by 6 months:

```bash
python cli.py advance --months 6
```

## 7. Export Formats

Records can be exported in three complementary formats.

### JSON (per patient)

A full denormalized bundle per patient — one file equals one patient's entire record,
including chronic conditions and every visit with its vitals, diagnoses, prescriptions,
and labs.

### FHIR R4 Bundle

A standard FHIR R4 Bundle per patient containing:

- Patient resource.
- Encounter resource per visit.
- Observation resources for vitals (with LOINC codes).
- Condition resources for diagnoses (ICD-10).
- MedicationRequest resources for prescriptions.
- Observation resources for labs (with LOINC codes and reference ranges).

### Plain-Text Clinical Notes

An LLM-ready chart summary — ideal as direct context for language models.

```
PATIENT CHART SUMMARY
MRN: ... | Name: ... | Age: ... | Sex: ...
FAMILY HISTORY: ...
ACTIVE CHRONIC CONDITIONS: [ICD10] Description — Onset: date (Controlled/Uncontrolled)

VISIT HISTORY (N total visits)
DATE: 2022-03-06 [Follow Up] — Provider: Dr. James O'Brien, MD
CHIEF COMPLAINT: Shortness of breath, worsening COPD
VITALS: BP 129/88 | HR 74 | Temp 98.4°F | SpO2 93% | BMI 20.2 | Pain 1/10
ASSESSMENT: J44.1 – COPD with acute exacerbation
  Rx [Refill]: Albuterol inhaler 2 puffs Q4H PRN ×Ongoing
  LABS: WBC 8.02 K/uL (Normal) | FEV1 40.13 %predicted ◄
FOLLOW-UP: Return in 30 days
```

## 8. Extensible Schema Architecture

Beyond the core ORM, the project documents a JSON-driven, modular schema registry that
lets teams extend the data model without editing core class bodies — and keeps Alembic
autogenerate working cleanly.

### Core concept

Declarative JSON is the source of truth — not Python class bodies. A registry merges
schemas across modules, a ClassFactory builds the SQLAlchemy classes, and Alembic then
sees ordinary metadata.

### Modular layout

- `base_module` — the core entities and relationships.
- `ontology_module` — adds ontology columns (e.g. SNOMED tags) and new entities.
- `clinical_module` — adds clinical extensions such as billing CPT codes or lab review fields.

Each module ships a `manifest.json` declaring its name, version, dependencies, and
priority, plus a `schema/` folder split into `entities/` (columns + indexes) and
`relationships/` (relationship definitions).

### Four-phase load order

Entities and relationships live in separate files because columns only need their own
table to exist, while relationships need both sides mapped. The registry loads in four
phases, then the factory builds classes in two passes:

| Phase | What happens |
|---|---|
| Phase 1 — Entity schemas | Load all modules' columns; every tablename and column is now known. |
| Phase 2 — Merge entities | Produce one merged column+index spec per entity (later module wins, logged). |
| Phase 3 — Relationship schemas | Validate all relationship targets against the complete entity set. |
| Phase 4 — Merge relationships | Merge relationship specs (later module wins, logged). |
| Factory Pass 1 | Create mapped classes (columns + indexes only). |
| Factory Pass 2 | Wire relationships — no forward references or deferred resolution needed. |

### Merge collision rules

| Collision | Resolution |
|---|---|
| Same column name, different modules | Later module wins; warning logged. |
| Same relationship name, different modules | Later module wins; warning logged. |
| Extension tries to rename tablename | Hard error, blocked. |
| Relationship targets unknown entity | Hard error at Phase 3 load time. |
| Circular module dependency | Hard error from topological sort. |

### Bootstrap & Alembic

A single `bootstrap()` routine runs the full registry + factory sequence and must run in
every process (CLI, Alembic, workers, API server) — the registry is a runtime construct,
not persisted state. Adding a column is then a three-step workflow:

```bash
# 1. Add the column to the module's entities/*.json schema
# 2. Autogenerate the migration
alembic revision --autogenerate -m "clinical_module: add encounter_duration_min"
# 3. Apply it
alembic upgrade head
```

## 9. Dataset Statistics

The shipped 10,000-patient dataset contains:

| Metric | Count |
|---|---|
| Patients | 10,000 |
| Visits | 165,972 |
| Diagnoses | 165,972 |
| Prescriptions | 170,267 |
| Lab Results | 777,868 |
| Avg visits / patient | 16.6 |

### Top Diagnoses

| ICD-10 | Description | Count |
|---|---|---|
| I10 | Essential hypertension | 21,051 |
| E11.9 | Type 2 diabetes mellitus | 15,804 |
| E78.5 | Hyperlipidemia | 14,585 |
| Z00.00 | General adult medical examination | 14,187 |
| J06.9 | Acute upper respiratory infection | 11,860 |
| M19.90 | Osteoarthritis | 11,568 |
| J11.1 | Influenza | 9,056 |
| Z00.129 | Well-child visit | 7,762 |
| E03.9 | Hypothyroidism | 6,144 |
| J44.1 | COPD with acute exacerbation | 5,955 |

### Age Distribution

| Age Band | Patients | Share |
|---|---|---|
| 0–12 | 2,059 | 20.6% |
| 13–17 | 814 | 8.1% |
| 18–35 | 1,689 | 16.9% |
| 36–50 | 1,415 | 14.2% |
| 51–65 | 1,425 | 14.3% |
| 66+ | 2,600 | 26.0% |

## 10. Roadmap & Possible Extensions

- **Agentic care program layer** — a tool-using AI agent with SQLite tools querying this dataset.
- **Narrative generation** — LLM-generated SOAP-note text per visit.
- **Care-gap detection** — flag patients overdue for preventive visits or with uncontrolled chronic conditions.
- **Risk stratification** — an ML model predicting hospitalization risk from visit patterns.
- **FHIR server** — wrap the exporters in a HAPI FHIR-compatible REST API.
- **Ontology module** — add SNOMED CT codes to diagnosis records.
- **Billing module** — add CPT codes, RVUs, and insurance-claim simulation.

---

*Family Medicine Synthetic Dataset — Feature Guide*
