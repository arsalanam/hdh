"""
Database tools exposed to the care-program agent.

Each tool returns a plain string (JSON or formatted text) — the agent reads
these as tool results. All data is synthetic; there is no PHI here.
"""

import json
from datetime import date

from sqlalchemy import func, text

from hdh.core.models import Base, ChronicCondition, Patient


def _schema_summary() -> str:
    """One line per table from the live ORM metadata — never drifts from models.py."""
    lines = [
        f"  {table.name}({', '.join(c.name for c in table.columns)})" for table in Base.metadata.sorted_tables
    ]
    return "\n".join(lines)


def build_tools(session):
    """Build the agent's tool functions bound to an open DB session."""
    from anthropic import beta_tool

    @beta_tool
    def get_patient_chart(mrn: str) -> str:
        """Retrieve a patient's full clinical chart as plain text.

        Args:
            mrn: The patient's medical record number, e.g. MRN12345678.
        """
        from hdh.core.exporters import patient_to_text

        p = session.query(Patient).filter(Patient.mrn == mrn).first()
        if not p:
            return f"No patient found with MRN {mrn}"
        return patient_to_text(p)

    @beta_tool
    def search_patients(
        name: str = "", min_age: int = 0, max_age: int = 120, icd10_prefix: str = "", limit: int = 20
    ) -> str:
        """Search patients by name, age range, and/or chronic-condition ICD-10 code.

        Args:
            name: Substring match on first or last name (optional).
            min_age: Minimum age in years.
            max_age: Maximum age in years.
            icd10_prefix: ICD-10 code prefix of a chronic condition, e.g. "E11" (optional).
            limit: Maximum number of patients to return.
        """
        from datetime import timedelta

        today = date.today()
        q = session.query(Patient)
        if name:
            like = f"%{name}%"
            q = q.filter(Patient.first_name.ilike(like) | Patient.last_name.ilike(like))
        q = q.filter(
            Patient.date_of_birth <= today - timedelta(days=min_age * 365),
            Patient.date_of_birth >= today - timedelta(days=(max_age + 1) * 365),
        )
        if icd10_prefix:
            q = (
                q.join(ChronicCondition)
                .filter(ChronicCondition.icd10_code.like(f"{icd10_prefix}%"))
                .distinct()
            )
        rows = []
        for p in q.limit(limit).all():
            rows.append(
                {
                    "mrn": p.mrn,
                    "name": f"{p.first_name} {p.last_name}",
                    "age": p.age,
                    "sex": str(p.sex).split(".")[-1],
                    "chronic_conditions": [f"[{c.icd10_code}] {c.description}" for c in p.chronic_conditions],
                }
            )
        return json.dumps(rows, indent=2) if rows else "No matching patients."

    @beta_tool
    def get_care_gaps(mrn: str = "", limit: int = 25) -> str:
        """List care gaps: overdue preventive visits, uncontrolled chronic conditions without follow-up, missed follow-ups, and senior polypharmacy.

        Args:
            mrn: Restrict to one patient (optional; empty = whole population).
            limit: Maximum number of gaps to return, ranked by severity.
        """
        from hdh.modules.caregaps import detect_gaps

        gaps = detect_gaps(session, mrn=mrn or None, limit=limit)
        return json.dumps([g.to_dict() for g in gaps], indent=2) if gaps else "No care gaps found."

    @beta_tool
    def get_risk_scores(mrn: str = "", top: int = 20) -> str:
        """Get ML risk-stratification scores (probability of urgent visit or critical lab within 180 days), highest risk first.

        Args:
            mrn: Score one patient (optional; empty = top-N riskiest).
            top: Number of highest-risk patients to return.
        """
        try:
            from hdh.modules.risk import model as risk_model

            rows = risk_model.score(session, mrn=mrn or None, top=top)
        except FileNotFoundError:
            return "No trained risk model found. Ask the operator to run `hdh risk train` first."
        except ImportError:
            return "Risk module not installed (pip install hdh[risk])."
        return json.dumps(rows, indent=2) if rows else "No results."

    def query_database(sql: str) -> str:
        # Docstring (the tool description Claude sees) is set dynamically below
        # so the embedded schema always matches the live ORM metadata.
        stripped = sql.strip().rstrip(";")
        if not stripped.lower().startswith("select") or ";" in stripped:
            return "Error: only a single SELECT statement is allowed."
        try:
            result = session.execute(text(stripped))
            cols = list(result.keys())
            rows = [dict(zip(cols, r, strict=False)) for r in result.fetchmany(200)]
        except Exception as e:
            return f"SQL error: {e}"
        return json.dumps(rows, indent=2, default=str) if rows else "Query returned no rows."

    query_database.__doc__ = f"""Run a read-only SQL SELECT against the synthetic SQLite database.

        Schema (one line per table):
{_schema_summary()}

        Joins: visits.patient_id -> patients.id; chronic_conditions.patient_id
        -> patients.id; vitals, diagnoses, prescriptions, and lab_results join
        via visit_id -> visits.id. Enum columns store names: visits.visit_type
        in ('ACUTE','FOLLOW_UP','PREVENTIVE','URGENT'), lab_results.status in
        ('NORMAL','HIGH','LOW','CRITICAL'), patients.sex in ('MALE','FEMALE').
        chronic_conditions.controlled is 0/1. Dates are ISO 'YYYY-MM-DD' text
        (julianday()/strftime() work). Results are capped at 200 rows.

        Args:
            sql: A single SELECT statement (no writes, no multiple statements).
        """
    query_database = beta_tool(query_database)

    @beta_tool
    def dataset_stats() -> str:
        """Get overall dataset statistics: patient, visit, diagnosis, prescription, and lab counts."""
        from hdh.core.models import Diagnosis, LabResult, Prescription, Visit

        stats = {
            "patients": session.query(func.count(Patient.id)).scalar(),
            "visits": session.query(func.count(Visit.id)).scalar(),
            "diagnoses": session.query(func.count(Diagnosis.id)).scalar(),
            "prescriptions": session.query(func.count(Prescription.id)).scalar(),
            "lab_results": session.query(func.count(LabResult.id)).scalar(),
        }
        return json.dumps(stats, indent=2)

    return [get_patient_chart, search_patients, get_care_gaps, get_risk_scores, query_database, dataset_stats]
