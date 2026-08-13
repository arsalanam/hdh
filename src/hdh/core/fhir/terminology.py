"""FHIR terminology constants — the honestly config-shaped fragments.

Lookups live here as data; construction logic stays in the emitters
(design fhir-emitters.md §4).
"""

SYSTEMS = {
    "mrn": "urn:family-medicine-mrn",
    "icd10": "http://hl7.org/fhir/sid/icd-10",
    "snomed": "http://snomed.info/sct",
    "loinc": "http://loinc.org",
    "cvx": "http://hl7.org/fhir/sid/cvx",
    "obs-category": "http://terminology.hl7.org/CodeSystem/observation-category",
    "interpretation": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
    "condition-clinical": "http://terminology.hl7.org/CodeSystem/condition-clinical",
    "allergy-clinical": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
}

ENCOUNTER_CLASS = {"acute": "AMB", "follow_up": "AMB", "preventive": "AMB", "urgent": "EMER"}

# (LOINC, display, Vital attribute or None for composite BP, unit)
VITALS_PANEL = (
    ("55284-4", "BP", None, "mm[Hg]"),
    ("8867-4", "Heart rate", "heart_rate", "/min"),
    ("9279-1", "Respiratory rate", "respiratory_rate", "/min"),
    ("8310-5", "Body temperature", "temperature_f", "degF"),
    ("59408-5", "SpO2", "oxygen_sat", "%"),
    ("29463-7", "Body weight", "weight_kg", "kg"),
    ("8302-2", "Body height", "height_cm", "cm"),
    ("39156-5", "BMI", "bmi", "kg/m2"),
)

CONDITION_CLINICAL_STATUS = {"ACTIVE": "active", "RESOLVED": "resolved", "REMISSION": "remission"}
