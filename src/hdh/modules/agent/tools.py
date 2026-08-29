"""
Database tools exposed to the care-program agent.

Each tool returns a plain string (JSON or formatted text) — the agent reads
these as tool results. All data is synthetic; there is no PHI here.
"""

import json
import logging
from collections.abc import Mapping
from datetime import date

from sqlalchemy import func, text

from hdh.core.models import Base, Condition, Patient

log = logging.getLogger("hdh.agent")


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


def _sql_tool_description(tables: tuple[str, ...] | None, dialect: str = "sqlite") -> str:
    """The query_database tool description, with an intent-scoped schema.

    Dialect-aware date guidance: telling the model julianday()/strftime()
    "work" while it queries PostgreSQL produces a guaranteed first-query
    failure (and, before the tool_guard, an aborted transaction)."""
    if dialect == "postgresql":
        date_note = (
            "Dates are native DATE columns — use date arithmetic, "
            "AGE(), EXTRACT(), and casts like '2026-01-01'::date "
            "(julianday()/strftime() do NOT exist here)."
        )
    else:
        date_note = "Dates are ISO 'YYYY-MM-DD' text (julianday()/strftime() work)."
    return f"""Run a read-only SQL SELECT against the synthetic {dialect} database.

        Schema (one line per table):
{_schema_summary(tables)}

        Joins: conditions.patient_id -> patients.id (the unified problem
        list: chronic=1 rows are ongoing conditions, others are encounter
        diagnoses; conditions.visit_id links to the recording visit);
        vitals, prescriptions, lab_results, visit_notes, procedures join via
        visit_id -> visits.id; allergies, family_history, immunizations,
        medication_statements join via patient_id. Enum columns store names:
        visits.visit_type in ('ACUTE','FOLLOW_UP','PREVENTIVE','URGENT'),
        lab_results.status in ('NORMAL','HIGH','LOW','CRITICAL'),
        conditions.status in ('ACTIVE','RESOLVED','REMISSION'), patients.sex
        in ('MALE','FEMALE'). conditions.controlled is 0/1.
        ontology_concepts.path is ICD-10-CM only; use the icd tools for
        hierarchy questions. {date_note} Results are capped at 200 rows.

        Args:
            sql: A single SELECT statement (no writes, no multiple statements).
        """


def _search_patient_rows(session, name: str, min_age: int, max_age: int, icd10_prefix: str, limit: int):
    """The search_patients query, kept out of the tool-builder closure."""
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
            q.join(Condition)
            .filter(Condition.chronic.is_(True), Condition.icd10_code.like(f"{icd10_prefix}%"))
            .distinct()
        )
    return [
        {
            "mrn": p.mrn,
            "name": f"{p.first_name} {p.last_name}",
            "age": p.age,
            "sex": str(p.sex).split(".")[-1],
            "chronic_conditions": [f"[{c.icd10_code}] {c.description}" for c in p.conditions if c.chronic],
        }
        for p in q.limit(limit).all()
    ]


def build_tools(session, tables: tuple[str, ...] | None = None, include: set[str] | None = None):
    """Build the agent's tool functions bound to an open DB session.

    ``tables`` narrows the schema embedded in query_database's description;
    ``include`` narrows which tools are returned. Both default to everything
    (the simple engine uses the full set).
    """
    from anthropic import beta_tool

    from hdh.core.models import tool_guard

    guard = tool_guard(session)

    @beta_tool
    @guard
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
    @guard
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
        rows = _search_patient_rows(session, name, min_age, max_age, icd10_prefix, limit)
        return json.dumps(rows, indent=2) if rows else "No matching patients."

    @beta_tool
    @guard
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
    @guard
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
            session.rollback()  # a failed SELECT must not poison the shared transaction (PG)
            return f"SQL error: {e}"
        return json.dumps(rows, indent=2, default=str) if rows else "Query returned no rows."

    dialect = session.get_bind().dialect.name if session is not None else "sqlite"
    query_database.__doc__ = _sql_tool_description(tables, dialect)
    query_database = beta_tool(guard(query_database))

    @beta_tool
    @guard
    def dataset_stats() -> str:
        """Get overall dataset statistics: patient, visit, diagnosis, prescription, and lab counts."""
        from hdh.core.models import Condition as Dx
        from hdh.core.models import LabResult, Prescription, Visit

        stats = {
            "patients": session.query(func.count(Patient.id)).scalar(),
            "visits": session.query(func.count(Visit.id)).scalar(),
            "diagnoses": session.query(func.count(Dx.id)).scalar(),
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
    all_tools.extend(_ontology_tools(session))
    all_tools.extend(_chart_tools(session))
    if include is None:
        return all_tools
    return [tool for tool in all_tools if tool.name in include]


def _chart_tools(session) -> list:
    """Chart maintenance (amend / void / audit trail) — core, so these are
    always available; the agent proposes and hdh.core.chartedit decides."""
    from hdh.modules.agent.chart_tools import build_chart_tools

    return build_chart_tools(session)


#: Optional toolsets, in the order the agent sees them. Each builder is
#: responsible for returning [] when its catalog is not loaded.
_ONTOLOGY_BUILDERS: tuple[tuple[str, str], ...] = (
    ("hdh.modules.icd10cm.agent_tools", "build_icd_tools"),
    ("hdh.modules.snomed.agent_tools", "build_snomed_tools"),
    ("hdh.modules.loinc.agent_tools", "build_loinc_tools"),
    ("hdh.modules.rxnorm.agent_tools", "build_rxnorm_tools"),
    ("hdh.modules.comprehension.agent_tools", "build_comprehension_tools"),
    ("hdh.modules.careplan.agent_tools", "build_careplan_tools"),
)


def _ontology_tools(session) -> list:
    """Coding tools via each ontology module's published API — optional:
    the agent runs fine without the modules or their catalogs.

    Three things used to look identical here, because one ``except
    Exception: continue`` covered all of them: a module that is not
    installed (expected), a catalog that is not loaded (expected, and the
    builders already handle it themselves by returning ``[]``), and a
    builder that RAISED (a bug). The third is now loud. It has to be —
    a crashing toolset and an absent one are indistinguishable from the
    outside, and the agent just answers worse.
    """
    from importlib import import_module

    tools: list = []
    for module_path, builder_name in _ONTOLOGY_BUILDERS:
        try:
            module = import_module(module_path)
        except ImportError:
            continue  # the module is not installed — the one silent case
        try:
            tools.extend(getattr(module, builder_name)(session))
        except Exception:  # noqa: BLE001 — a broken toolset must not break the agent...
            log.warning(
                "%s.%s raised while building the agent's tools — those tools are "
                "MISSING from this session, not merely unavailable",
                module_path,
                builder_name,
                exc_info=True,  # ...but it must not be silent either
            )
    return tools
