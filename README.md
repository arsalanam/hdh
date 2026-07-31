# Family Medicine Synthetic Dataset
**10,000 patients · 165,000+ visits · 777,000+ lab results**

## Quick Start

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

# List all available conditions
python cli.py list-conditions

# Inject a flu spike for January (300 extra visits)
python cli.py add-spike --condition influenza --month 1 --n 300

# Advance time by 6 months (adds new follow-up visits)
python cli.py advance --months 6
```

## Files

| File | Purpose |
|------|---------|
| `models.py` | SQLAlchemy ORM — Patient, Visit, Vital, Diagnosis, Prescription, LabResult |
| `disease_engine.py` | Age/sex/season probability engine, ICD-10 codes, med formularies |
| `generators.py` | Patient and visit history generators |
| `exporters.py` | JSON, FHIR R4 Bundle, plain-text clinical notes |
| `cli.py` | Maintenance CLI |
| `family_medicine.db` | Pre-generated SQLite database (10,000 patients) |

## Disease Coverage (30 conditions)

**Pediatric (0–12):** Otitis media, RSV, febrile illness, strep throat,
conjunctivitis, eczema, well-child visits

**Adolescent (13–17):** Acne, sports physicals, sports injuries, mono, anxiety

**Young Adult (18–35):** Annual physical, influenza, URI, UTI, anxiety,
low back pain, minor laceration, contraception

**Adult (36–65):** Hypertension, Type 2 Diabetes, Hyperlipidemia, GERD,
osteoarthritis, obesity, hypothyroidism, COPD, depression

**Senior (65+):** Annual wellness, polypharmacy review, fall injuries,
COPD exacerbation, depression, hypothyroidism

## Export Formats

- **JSON** — One file per patient, full denormalized bundle
- **FHIR R4** — Standard Bundle with Patient, Encounter, Observation,
  Condition, MedicationRequest resources
- **Plain text** — LLM-ready clinical chart summaries
