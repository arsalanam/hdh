"""
Database tools exposed to the care-program agent.

Each tool returns a plain string (JSON or formatted text) — the agent reads
these as tool results. All data is synthetic; there is no PHI here.
"""

import json
from collections.abc import Mapping
from datetime import date

from sqlalchemy import func, text

from hdh.core.models import Base, ChronicCondition, Patient


def _schema_summary(tables: tuple[str, ...] | None = None) -> str:
    """One line per table from the live ORM metadata — never drifts from models.py.

    ``tables`` limits the summary to the named tables (selective schema
    revealing: the executor only sees what its intent needs).
    """
    lines = [
        f"  {table.name}({', '.join(c.name for c in table.columns)})"
        for table in Base.metadata.sorted_tables
        if tables is None or table.name in tables
    ]
    return "\n".join(lines)


def clip_tool_results(tool_response: Mapping | None, cap: int) -> Mapping | None:
    """Truncate oversized tool results before they re-enter model context.

    Mutates the runner's pending tool-result message in place: each result
    longer than ``cap`` characters is cut and annotated so the model knows to
    fetch less next time. This bounds the context growth of long tool loops.
    """
    if tool_response is None:
        return None
    blocks = tool_response.get("content") or []
    if isinstance(blocks, str):
        return tool_response
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("content"), str):
            text = block["content"]
            if len(text) > cap:
                block["content"] = (
                    text[:cap] + f"\n...[truncated {len(text) - cap:,} chars — request fewer rows/fields "
                    "or refine the query instead of re-fetching]"
                )
    return tool_response


def _sql_tool_description(tables: tuple[str, ...] | None) -> str:
    """The query_database tool description, with an intent-scoped schema."""
    return f"""Run a read-only SQL SELECT against the synthetic SQLite database.

        Schema (one line per table):
{_schema_summary(tables)}

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


def build_tools(session, tables: tuple[str, ...] | None = None, include: set[str] | None = None):
    """Build the agent's tool functions bound to an open DB session.

    ``tables`` narrows the schema embedded in query_database's description;
    ``include`` narrows which tools are returned. Both default to everything
    (the simple engine uses the full set).
    """
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

    query_database.__doc__ = _sql_tool_description(tables)
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

    all_tools: list = [
        get_patient_chart,
        search_patients,
        get_care_gaps,
        get_risk_scores,
        query_database,
        dataset_stats,
    ]
    # ICD-10-CM coding tools via the icd10cm module's published API —
    # optional: the agent runs fine without the module or its catalog
    try:
        from hdh.modules.icd10cm.agent_tools import build_icd_tools

        all_tools.extend(build_icd_tools(session))
    except Exception:  # noqa: BLE001 — absent module/catalog must never break the agent
        pass
    if include is None:
        return all_tools
    return [tool for tool in all_tools if tool.name in include]
