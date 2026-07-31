"""Ontology mapping (scaffold): ICD-10 → SNOMED CT.

Provides a starter mapping for the dataset's most common diagnoses and a
lookup helper. Extend ``ICD10_TO_SNOMED`` (or replace it with a full
terminology-server lookup) to cover more codes — see README.md in this
directory.
"""

# SNOMED CT concept IDs for the dataset's highest-volume ICD-10 codes.
ICD10_TO_SNOMED = {
    "I10":     ("59621000",  "Essential hypertension"),
    "E11.9":   ("44054006",  "Diabetes mellitus type 2"),
    "E78.5":   ("55822004",  "Hyperlipidemia"),
    "J06.9":   ("54150009",  "Upper respiratory infection"),
    "M19.90":  ("396275006", "Osteoarthritis"),
    "J11.1":   ("6142004",   "Influenza"),
    "E03.9":   ("40930008",  "Hypothyroidism"),
    "J44.1":   ("195951007", "Acute exacerbation of chronic obstructive airways disease"),
    "K21.9":   ("235595009", "Gastroesophageal reflux disease"),
    "F41.9":   ("48694002",  "Anxiety"),
    "F32.9":   ("35489007",  "Depressive disorder"),
    "E66.9":   ("414916001", "Obesity"),
    "N39.0":   ("68566005",  "Urinary tract infection"),
    "M54.5":   ("279039007", "Low back pain"),
}


def snomed_for_icd10(icd10_code: str):
    """Return (snomed_id, display) for an ICD-10 code, or None if unmapped."""
    return ICD10_TO_SNOMED.get(icd10_code)
