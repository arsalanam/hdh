# Ontology module (scaffold)

Maps the dataset's ICD-10 diagnosis codes to SNOMED CT concepts.

Current state: a starter `ICD10_TO_SNOMED` dictionary covering the dataset's
highest-volume diagnoses, plus `snomed_for_icd10()`.

## Planned extensions

- Cover all ~30 disease-engine conditions.
- Add SNOMED codings to the FHIR `Condition` resources emitted by
  `hdh.core.exporters` (as an additional `coding` entry).
- Optional: resolve codes via a terminology server (e.g. tx.fhir.org) instead
  of a static map.

See CONTRIBUTING.md for how modules hook into the core.
