"""Core synthetic data generation engine.

This package is intentionally self-contained: it must never import from
``hdh.modules``. Feature modules depend on the core, not the other way around.
"""

from .conditions import (
    ConditionCatalog,
    ConditionProfile,
    ConditionSource,
    SamplingContext,
    build_catalog,
    default_catalog,
)
from .disease_engine import FamilyMedicineCorePack
from .exporters import (
    export_fhir,
    export_json,
    export_text,
    patient_to_fhir_bundle,
    patient_to_json,
    patient_to_text,
)
from .generators import build_dataset, generate_patient, generate_visit_history
from .models import (
    Base,
    Condition,
    LabResult,
    LabStatus,
    Patient,
    Prescription,
    Sex,
    Visit,
    VisitType,
    Vital,
    get_engine,
    get_session,
)

__all__ = [
    "ConditionCatalog",
    "ConditionProfile",
    "ConditionSource",
    "SamplingContext",
    "build_catalog",
    "default_catalog",
    "FamilyMedicineCorePack",
    "export_fhir",
    "export_json",
    "export_text",
    "patient_to_fhir_bundle",
    "patient_to_json",
    "patient_to_text",
    "build_dataset",
    "generate_patient",
    "generate_visit_history",
    "Base",
    "Condition",
    "LabResult",
    "LabStatus",
    "Patient",
    "Prescription",
    "Sex",
    "Visit",
    "VisitType",
    "Vital",
    "get_engine",
    "get_session",
]
