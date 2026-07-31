# Ontology guide (scaffold)

Maps the dataset's ICD-10 diagnosis codes to SNOMED CT concepts. Library-only
for now — no CLI command.

## Usage

```python
from hdh.modules.ontology import snomed_for_icd10, ICD10_TO_SNOMED

snomed_for_icd10("I10")     # ("59621000", "Essential hypertension")
snomed_for_icd10("Z99.99")  # None — unmapped

# Annotate diagnoses in a chart
for visit in patient.visits:
    for dx in visit.diagnoses:
        mapping = snomed_for_icd10(dx.icd10_code)
        if mapping:
            print(dx.icd10_code, "→ SNOMED", *mapping)
```

The starter map covers the dataset's highest-volume diagnoses: hypertension,
T2DM, hyperlipidemia, URI, osteoarthritis, influenza, hypothyroidism, COPD
exacerbation, GERD, anxiety, depression, obesity, UTI, and low back pain.

## Extending

1. **Complete the map** — add the remaining disease-engine codes
   (`hdh list-conditions` shows them all) to `ICD10_TO_SNOMED`.
2. **Wire into FHIR** — append a SNOMED coding alongside the ICD-10 coding in
   `Condition.code.coding` inside `hdh.core.exporters.patient_to_fhir_bundle`
   (guard the import so core keeps working without the module — or better,
   pass a coding-enricher callback into the exporter).
3. **Terminology server** — replace the static dict with lookups against a
   FHIR terminology service (e.g. `tx.fhir.org` `$translate`), with the dict
   as an offline cache.

Verification tip: SNOMED concept IDs here are illustrative for synthetic-data
purposes; validate against an official SNOMED CT release before using the
mappings anywhere that matters.
