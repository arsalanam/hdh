# Family Medicine Synthetic Dataset — Architecture Reference

> Generated from claude.ai design session. Use this file as context when
> continuing with Claude Code:
> ```bash
> cat ARCHITECTURE.md | claude "your next question here"
> # or
> claude --context ARCHITECTURE.md
> ```

---

## 1. Project Overview

A synthetic family medicine OPD dataset for testing agentic AI care programs.
10,000 patients, 165,000+ visits, 777,000+ lab results — all medically realistic.

**Stack:** Python · Faker · SQLAlchemy · SQLite · Alembic  
**Output formats:** JSON (per patient) · FHIR R4 Bundle · Plain-text clinical notes  
**Database file:** `family_medicine.db` (SQLite, ~87MB for 10k patients)

---

## 2. File Structure

```
family_medicine/
├── models.py           # SQLAlchemy ORM — all table definitions
├── disease_engine.py   # Age/sex/season probability engine, ICD-10, formularies
├── generators.py       # Patient + visit history generators
├── exporters.py        # JSON, FHIR R4, plain-text exporters
├── cli.py              # Maintenance CLI
├── requirements.txt    # faker, sqlalchemy
└── family_medicine.db  # Pre-generated SQLite database
```

---

## 3. Data Models (models.py)

### Tables

| Model | Table | Key columns |
|---|---|---|
| `Patient` | `patients` | mrn, dob, sex, race, insurance, allergies, fam_hx_*, smoker, bmi_baseline |
| `ChronicCondition` | `chronic_conditions` | patient_id, icd10_code, onset_date, controlled |
| `Visit` | `visits` | patient_id, visit_date, visit_type, chief_complaint, provider_name, follow_up_days |
| `Vital` | `vitals` | visit_id, bp_systolic, bp_diastolic, hr, rr, temp_f, spo2, weight_kg, bmi, pain_scale |
| `Diagnosis` | `diagnoses` | visit_id, icd10_code, description, is_primary |
| `Prescription` | `prescriptions` | visit_id, drug_name, drug_class, dose, frequency, duration_days, refills, is_new |
| `LabResult` | `lab_results` | visit_id, test_name, value, unit, ref_low, ref_high, status (LabStatus enum), loinc_code |

### Enums
- `Sex`: M / F
- `VisitType`: acute / follow_up / preventive / urgent
- `LabStatus`: normal / high / low / critical

### Key Relationships
```
Patient ──< Visit ──< Diagnosis
                 ──< Prescription
                 ──< LabResult
                 ──1 Vital
Patient ──< ChronicCondition
```

---

## 4. Disease Engine (disease_engine.py)

### ConditionProfile dataclass
Each condition defines:
- `icd10_code`, `description`, `chief_complaint`, `visit_type`
- Vital **deltas** from baseline (mean, sd) for bp, hr, rr, temp, spo2, pain
- `labs: list[LabSpec]` — which lab panels to order
- `rx_options: list[RxSpec]` — condition-appropriate formulary
- `follow_up_days` — clinical guideline follow-up interval
- `seasonal_weights: dict[month→float]` — seasonal multipliers

### 30 Conditions by Age Group

| Age Group | Conditions |
|---|---|
| **Infant 0–2** | Well-child, otitis media, RSV, febrile illness, rash/eczema, conjunctivitis, URI |
| **Child 3–12** | Well-child, otitis media, strep throat, URI, febrile illness, rash, conjunctivitis, sports injury |
| **Teen 13–17** | Sports physical, URI, sports injury, acne, anxiety, strep, mono |
| **Young Adult 18–35** | Annual physical, influenza, URI, UTI, anxiety, low back pain, laceration, contraception, GERD |
| **Adult 36–50** | Annual physical, HTN, hyperlipidemia, T2DM, URI, influenza, GERD, anxiety, back pain, obesity |
| **Middle-aged 51–65** | Annual physical, HTN, T2DM, hyperlipidemia, osteoarthritis, GERD, URI, COPD, depression, hypothyroidism |
| **Senior 65+** | Annual wellness, HTN, T2DM, hyperlipidemia, osteoarthritis, COPD, falls, polypharmacy review, depression, hypothyroidism, influenza |

### Seasonal Multipliers
```python
FLU_SEASON  = {Jan:2.5, Feb:2.0, ..., Dec:2.5}   # peaks winter
RSV_SEASON  = {Jan:2.0, Feb:1.5, ..., Dec:2.5}   # peaks late fall/winter
SUMMER_PEAK = {Jun:1.5, Jul:1.5, Aug:1.5, ...}   # UTI, sports injuries, lacerations
```

### Comorbidity Seeding
```python
def comorbidity_seeds(age, fam_hx, smoker, bmi) -> set[str]:
    # age >= 45: seeds HTN (30%), T2DM (20%), hyperlipidemia (35%)
    # age >= 60: seeds COPD if smoker, hypothyroidism (25%), OA (40%)
```

---

## 5. CLI Reference (cli.py)

```bash
# Generate dataset
python cli.py generate --patients 10000 --years 4

# View statistics
python cli.py stats

# Export formats
python cli.py export --format json   --limit 500 --output-dir exports/
python cli.py export --format fhir   --limit 100
python cli.py export --format text   --limit 100
python cli.py export --format all

# Show one patient chart (by MRN)
python cli.py show --mrn MRN21964721

# List all available condition codes
python cli.py list-conditions

# Inject seasonal disease spike
python cli.py add-spike --condition influenza --month 1 --n 300

# Advance time (adds follow-up visits for chronic patients)
python cli.py advance --months 6
```

---

## 6. Export Formats (exporters.py)

### JSON (per patient)
Full denormalized bundle per patient. One file = one patient's entire record.
```json
{
  "mrn": "MRN21964721",
  "name": "Charles Taylor",
  "age": 69,
  "chronic_conditions": [...],
  "visits": [
    {
      "visit_date": "2022-03-06",
      "visit_type": "follow_up",
      "vitals": { "bp": "129/88 mmHg", "bmi": 20.2, ... },
      "diagnoses": [{ "icd10": "J44.1", "description": "COPD..." }],
      "prescriptions": [...],
      "labs": [...]
    }
  ]
}
```

### FHIR R4 Bundle
Standard FHIR R4 Bundle per patient containing:
- `Patient` resource
- `Encounter` resource per visit
- `Observation` resources for vitals (with LOINC codes)
- `Condition` resources for diagnoses (ICD-10)
- `MedicationRequest` resources for prescriptions
- `Observation` resources for labs (with LOINC codes + reference ranges)

### Plain Text Clinical Notes
LLM-ready chart summary. Format:
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

---

## 7. SQLAlchemy Extension Patterns

Four patterns explored for extending model definitions across modules:

### Pattern 1 — Mixin-First (recommended for most cases)
```python
# module_a.py — core columns as mixin (no Base, no __tablename__)
class LabResultCoreMixin:
    id = Column(Integer, primary_key=True)
    test_name = Column(String(100))

# module_b.py — extension mixin
class LabResultExtMixin:
    test_validity = Column(String(20))

# models.py — single assembly point
class LabResult(LabResultCoreMixin, LabResultExtMixin, Base):
    __tablename__ = "lab_results"
```
✅ Alembic autogenerate works perfectly. Low complexity. No ordering risk.

### Pattern 2 — `append_column()` (true runtime injection)
```python
# module_b.py — injects into existing class without modifying it
def extend_lab_result():
    new_col = Column("test_validity", String(20))
    LabResult.__table__.append_column(new_col)
    LabResult.test_validity = new_col

# Must call BEFORE any DB access or Alembic metadata read
extend_lab_result()
```
✅ Module B fully independent. ⚠️ High ordering risk — call before engine.create_all().

### Pattern 3 — Plugin Registry (most scalable)
```python
# registry.py
_column_extensions: dict[str, list] = {}

def register_columns(model_name: str, columns: dict):
    _column_extensions.setdefault(model_name, []).append(columns)

# module_b.py — registers without importing the model
register_columns("LabResult", {
    "test_validity": Column(String(20))
})

# models.py — assembles final class
def make_lab_result_class():
    attrs = { "__tablename__": "lab_results", **base_cols, **get_extensions("LabResult") }
    return type("LabResult", (Base,), attrs)
```
✅ Fully modular. Alembic works. ⚠️ Modules must register before class creation.

### Pattern 4 — `__init_subclass__` (most automatic)
```python
class LabResultExtension(ExtensibleBase, extends=LabResult):
    test_validity = Column(String(20))
    # → automatically injected into LabResult on class definition
```

---

## 8. JSON-Driven Schema Registry (Plugin Registry — Full Design)

### Core Concept
Source of truth is **declarative JSON**, not Python class bodies.
Registry merges schemas → ClassFactory builds SQLAlchemy classes → Alembic sees normal metadata.

### Directory Structure
```
base_module/
├── manifest.json                   # {name, version, depends_on, priority}
└── schema/
    ├── entities/                   # Phase 1: columns + indexes ONLY
    │   ├── visit.json
    │   ├── patient.json
    │   ├── lab_result.json
    │   └── ...
    └── relationships/              # Phase 3: relationships ONLY
        ├── visit_relationships.json
        └── patient_relationships.json

ontology_module/
├── manifest.json                   # {depends_on: ["base_module"], priority: 10}
└── schema/
    ├── entities/
    │   ├── visit.json              # extends base Visit — adds ontology columns
    │   └── visit_ontology_tag.json # new entity
    └── relationships/
        ├── visit_relationships.json          # adds visit.ontology_tags
        └── visit_ontology_tag_relationships.json

clinical_module/
├── manifest.json                   # {depends_on: ["base_module"], priority: 10}
└── schema/
    ├── entities/
    │   ├── visit.json              # adds billing_cpt_codes, facility_code
    │   └── lab_result.json         # adds test_validity, reviewed_by
    └── relationships/
        └── (empty or new rels)
```

### Manifest Format
```json
{
  "name": "ontology_module",
  "version": "1.0.0",
  "depends_on": ["base_module"],
  "schema_dir": "schema",
  "priority": 10
}
```

### Entity Schema Format (entities/*.json)
```json
{
  "entity": "Visit",
  "tablename": "visits",
  "module": "base_module",
  "columns": [
    { "name": "id",           "type": "Integer", "primary_key": true, "autoincrement": true },
    { "name": "patient_id",   "type": "Integer", "foreign_key": "patients.id", "nullable": false },
    { "name": "visit_date",   "type": "Date",    "nullable": false },
    { "name": "visit_type",   "type": "Enum",    "values": ["acute","follow_up","preventive","urgent"] },
    { "name": "chief_complaint", "type": "String", "length": 200 }
  ],
  "indexes": [
    { "columns": ["patient_id", "visit_date"], "unique": false }
  ]
}
```

Extension adds only new columns — never re-declares existing ones:
```json
{
  "entity": "Visit",
  "extends": "base_module",
  "module": "ontology_module",
  "columns": [
    { "name": "visit_ontology_code", "type": "String", "length": 40 },
    { "name": "care_setting",        "type": "Enum",
      "values": ["outpatient","telehealth","home_visit","urgent_care"] }
  ]
}
```

### Relationship Schema Format (relationships/*.json)
```json
{
  "entity": "Visit",
  "module": "base_module",
  "relationships": [
    {
      "name": "patient",
      "target": "Patient",
      "type": "many_to_one",
      "back_populates": "visits",
      "foreign_keys": ["Visit.patient_id"]
    },
    {
      "name": "diagnoses",
      "target": "Diagnosis",
      "type": "one_to_many",
      "back_populates": "visit",
      "cascade": "all, delete-orphan"
    }
  ]
}
```

### Supported Column Types
| JSON type | SQLAlchemy |
|---|---|
| `Integer` | `Integer()` |
| `String` + `length` | `String(length)` |
| `Float` | `Float()` |
| `Date` | `Date()` |
| `DateTime` | `DateTime()` |
| `Boolean` | `Boolean()` |
| `Text` | `Text()` |
| `Enum` + `values` | `SAEnum(*values, name=enum_name)` |

### Supported Relationship Types
| JSON type | SQLAlchemy uselist |
|---|---|
| `one_to_many` | `True` |
| `many_to_many` | `True` |
| `many_to_one` | `False` |
| `one_to_one` | `False` |

---

## 9. Four-Phase Load Order (Why Entities and Relationships Are Separate Files)

**The core problem:** columns only need their own table to exist. Relationships need
BOTH sides to exist as mapped classes. Mixing them in one file forces fragile workarounds.

```
REGISTRY.load_all()
│
├── Phase 1 — Entity schemas (all modules, columns only)
│   After this phase: every tablename and every column is known.
│   No SQLAlchemy class exists yet.
│
├── Phase 2 — Merge entity schemas
│   Produces one merged column+index spec per entity.
│   Merge rule: later module wins on column name collision (logged warning).
│   Guard: extensions cannot rename tablename.
│
├── Phase 3 — Relationship schemas (all modules)
│   ✅ Can validate ALL targets NOW — merged_entity_schemas is complete.
│   Error at load time if relationship targets a non-existent entity.
│   Error message names exact file + entity + missing target.
│
└── Phase 4 — Merge relationship schemas
    Later module wins on relationship name collision (logged warning).
    base_module always provides the starting set.

FACTORY.make_all_classes()
│
├── Pass 1 — Create mapped classes (columns + indexes only)
│   Every class guaranteed to exist by end of this pass.
│
└── Pass 2 — Wire relationships
    No forward references. No deferred resolution.
    Every target guaranteed present from Pass 1.
```

### Merge Collision Rules
| Collision type | Resolution |
|---|---|
| Same column name, different modules | Later module wins, warning logged |
| Same relationship name, different modules | Later module wins, warning logged |
| Extension tries to rename tablename | Hard error, blocked |
| Relationship targets unknown entity | Hard error at Phase 3 load time |
| Circular module dependency | Hard error from topological sort |

---

## 10. Module Registry Key Methods

```python
registry = ModuleRegistry()

# Registration (any order — registry sorts by dependency + priority)
registry.register_module("base_module")            # by directory path
registry.register_module("ontology_module")
registry.register_module_by_import("myapp.clinical_module")  # by dotted import

# Load + merge everything
registry.load_all()

# Debug
registry.describe()
# → Module load order: base_module → ontology_module → clinical_module
# → Entities (7): Visit (12 cols, 5 rels), Patient (18 cols, 3 rels), ...

# Access merged schemas
schema  = registry.get_merged_entity_schema("Visit")
rel_sch = registry.get_merged_rel_schema("Visit")
names   = registry.all_entity_names()
```

---

## 11. Class Factory Key Methods

```python
factory = ClassFactory(base=Base, registry=registry)

# Build everything (two-pass, returns dict of mapped classes)
classes = factory.make_all_classes()

# Access individual classes
Visit   = factory.get("Visit")
Patient = factory.get("Patient")

# Use normally with SQLAlchemy sessions
with Session(engine) as session:
    v = Visit(visit_date=date.today(), visit_type="acute")
    v.visit_ontology_code = "SNOMED:11429006"  # if ontology_module loaded
    session.add(v)
    session.commit()
```

---

## 12. Alembic Integration

```python
# alembic/env.py
from app import bootstrap

engine, classes = bootstrap()   # runs full registry + factory sequence
# Base.metadata now contains ALL columns from ALL modules

def run_migrations_online():
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
```

Adding a new column workflow:
```bash
# 1. Add column to JSON schema in the appropriate module's entities/ file
# 2. Run autogenerate
alembic revision --autogenerate -m "clinical_module: add encounter_duration_min"
# 3. Apply
alembic upgrade head
```

---

## 13. App Bootstrap Sequence

```python
# app.py — run once at startup in every process
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine
from registry.module_registry import ModuleRegistry
from registry.class_factory import ClassFactory

class Base(DeclarativeBase):
    pass

def bootstrap(db_url: str = "sqlite:///family_medicine.db"):
    registry = ModuleRegistry()
    registry.register_module("base_module")
    registry.register_module("ontology_module")   # optional
    registry.register_module("clinical_module")   # optional

    registry.load_all()
    registry.describe()

    factory = ClassFactory(base=Base, registry=registry)
    classes = factory.make_all_classes()

    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)

    return engine, classes
```

**Important:** Bootstrap must run in every process — CLI, Alembic, Celery workers, API server.
The registry is a runtime construct, not persisted state.

---

## 14. Generated Dataset Statistics (10,000 patients)

```
Patients        :     10,000
Visits          :    165,972
Diagnoses       :    165,972
Prescriptions   :    170,267
Lab Results     :    777,868
Avg visits/pt   :       16.6

Top diagnoses:
  I10     Essential hypertension                    21,051
  E11.9   Type 2 diabetes mellitus                  15,804
  E78.5   Hyperlipidemia                            14,585
  Z00.00  General adult medical examination          14,187
  J06.9   Acute upper respiratory infection          11,860
  M19.90  Osteoarthritis                            11,568
  J11.1   Influenza                                  9,056
  Z00.129 Well-child visit                           7,762
  E03.9   Hypothyroidism                             6,144
  J44.1   COPD with acute exacerbation               5,955

Age distribution:
  0–12    2,059  (20.6%)
  13–17     814  (8.1%)
  18–35   1,689  (16.9%)
  36–50   1,415  (14.2%)
  51–65   1,425  (14.3%)
  66+     2,600  (26.0%)
```

---

## 15. Next Steps / Possible Extensions

- **Agentic care program layer** — LangGraph agent with SQLite tools querying this dataset
- **Narrative generation** — add SOAP note text generation per visit using an LLM
- **Care gap detection** — flag patients overdue for preventive visits, uncontrolled chronic conditions
- **Risk stratification** — ML model trained on visit patterns to predict hospitalization risk
- **FHIR server** — wrap exporters in a HAPI FHIR-compatible REST API
- **Ontology module** — add SNOMED CT codes to diagnosis records
- **Billing module** — add CPT codes, RVUs, insurance claim simulation
