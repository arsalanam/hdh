"""Core synthetic data generation engine.

This package is intentionally self-contained: it must never import from
``hdh.modules``. Feature modules depend on the core, not the other way around.
"""

from .models import (
    Base, Patient, ChronicCondition, Visit, Vital, Diagnosis,
    Prescription, LabResult, Sex, VisitType, LabStatus,
    get_engine, get_session,
)
from .disease_engine import CONDITIONS, ConditionProfile, pick_condition, comorbidity_seeds
from .generators import build_dataset, generate_patient, generate_visit_history
from .exporters import (
    patient_to_json, patient_to_fhir_bundle, patient_to_text,
    export_json, export_fhir, export_text,
)
