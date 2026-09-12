"""
Database tools exposed to the care-program agent.

Each tool returns a plain string (JSON or formatted text) — the agent reads
these as tool results. All data is synthetic; there is no PHI here.
"""

import calendar
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, text

from hdh.core.models import Base, Condition, Patient

log = logging.getLogger("hdh.agent")


def _semantic_schema(tables: tuple[str, ...] | None = None) -> str:
    """Columns AND meaning, per table, from the schema registry (#93).

    What this replaces is the point. The columns were always generated from
    live ORM metadata and could not drift; the sentence explaining what a
    table is *for* was a hand-written paragraph in this file, about tables
    this module does not own. It went four behind — `service_requests`,
    `note_records`, `rejected_results` and `care_plan_records` appeared in
    the column list with nothing saying what they were — and it went behind
    in the only way it could, one module at a time.

    So the meaning now travels with the entity that declares it, and this
    function renders rather than remembers.
    """
    from hdh.core.schema_registry import table_semantics

    meanings = table_semantics()
    blocks: list[str] = []
    for table in Base.metadata.sorted_tables:
        if tables is not None and table.name not in tables:
            continue
        lines = [f"  {table.name}({', '.join(c.name for c in table.columns)})"]
        meaning = meanings.get(table.name)
        if meaning:
            if meaning.get("purpose"):
                lines.append(f"      {meaning['purpose']}")
            if meaning.get("also_called"):
                lines.append(f"      also called: {', '.join(meaning['also_called'])}")
            if meaning.get("use_when"):
                lines.append(f"      use when: {meaning['use_when']}")
            for column, note in (meaning.get("columns") or {}).items():
                lines.append(f"      .{column}: {note}")
            for join in meaning.get("joins") or []:
                lines.append(f"      join: {join}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


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


def _table_catalog() -> str:
    """Every table with a one-line purpose — the compact map the SQL tool
    carries so the model knows what EXISTS without paying for every column on
    every loop turn. Columns arrive on demand via ``describe_table``.

    This replaces embedding the full column schema (~4k tokens, re-billed each
    turn). Measured: without a catalog the model spent queries discovering the
    schema at runtime — an 8,463-char ``SELECT … FROM information_schema`` —
    because the tables it needed were not in view. The catalog puts every
    table in view cheaply.
    """
    from hdh.core.schema_registry import table_semantics

    meanings = table_semantics()
    lines = []
    for table in Base.metadata.sorted_tables:
        purpose = (meanings.get(table.name) or {}).get("purpose", "")
        lines.append(f"  {table.name}" + (f" — {purpose}" if purpose else ""))
    return "\n".join(lines)


def _sql_tool_description(tables: tuple[str, ...] | None, dialect: str = "sqlite") -> str:
    """The query_database tool description: a compact table CATALOG, not every
    column. The model calls ``describe_table(name)`` for a table's columns on
    demand, so the full schema no longer rides in context every turn and the
    model does not burn queries introspecting it.

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
    relevant = f"\n        Most relevant for this request: {', '.join(tables)}.\n" if tables else ""
    return f"""Run a read-only SQL SELECT against the synthetic {dialect} database.

        Tables (call describe_table(name) for a table's columns and meaning
        BEFORE you query it — do not guess column names, and do not SELECT from
        information_schema):
{_table_catalog()}
{relevant}
        A table shown with no purpose is unannotated: describe_table still
        returns its columns, but say what you are assuming about it rather than
        guessing at its meaning. Enum columns store NAMES, not numbers — compare to the string.
        Anything joining via visit_id reaches the patient through
        visits.patient_id. {date_note} Results are capped at 200 rows.

        Args:
            sql: A single SELECT statement (no writes, no multiple statements).
        """


@dataclass(frozen=True)
class PatientSearch:
    """The criteria for one patient search — grouped so the query helper reads
    as one thing rather than a ten-argument signature.

    ``scope_provider_id`` restricts to patients that provider has seen (the
    "seen by me / by Dr. X" filter); ``seen_within_days`` keeps only those
    whose most recent qualifying visit is recent; ``sort`` orders by name or
    by that last-seen date. ``as_of`` anchors "recent" — it defaults to today
    but a caller can pin it to the dataset's latest date so the filter is
    meaningful on a historical synthetic cohort.
    """

    name: str = ""
    min_age: int = 0
    max_age: int = 120
    icd10_prefix: str = ""
    limit: int = 20
    scope_provider_id: int | None = None
    seen_within_days: int = 0
    sort: str = "name"  # "name" | "last_seen"
    as_of: date | None = None


def _last_seen_subquery(session, provider_id: int | None):
    """Per-patient most-recent non-voided visit date, optionally scoped to one
    provider. This is what "last seen" and the recency filter both read."""
    from hdh.core.models import Visit

    q = session.query(Visit.patient_id, func.max(Visit.visit_date).label("last_seen")).filter(
        Visit.voided_at.is_(None)
    )
    if provider_id is not None:
        q = q.filter(Visit.provider_id == provider_id)
    return q.group_by(Visit.patient_id).subquery()


def _search_patient_rows(session, criteria: PatientSearch):
    """The search_patients query, kept out of the tool-builder closure.

    Beyond name/age/ICD, the search can be made personal: scoped to a
    provider's own patients, filtered to who was seen recently, and sorted by
    last-seen date — the point of the signed-in-provider work.
    """
    today = criteria.as_of or date.today()
    need_last_seen = (
        criteria.scope_provider_id is not None
        or criteria.seen_within_days > 0
        or criteria.sort == "last_seen"
    )
    q = session.query(Patient)
    last_seen_col = None
    if need_last_seen:
        ls = _last_seen_subquery(session, criteria.scope_provider_id)
        last_seen_col = ls.c.last_seen
        # inner join when scoped to a provider (only their patients); outer
        # otherwise, so an unscoped recency sort still lists the never-seen.
        q = (
            q.join(ls, ls.c.patient_id == Patient.id)
            if criteria.scope_provider_id is not None
            else q.outerjoin(ls, ls.c.patient_id == Patient.id)
        )
        if criteria.seen_within_days > 0:
            q = q.filter(last_seen_col >= today - timedelta(days=criteria.seen_within_days))
    if criteria.name:
        like = f"%{criteria.name}%"
        q = q.filter(Patient.first_name.ilike(like) | Patient.last_name.ilike(like))
    q = q.filter(
        Patient.date_of_birth <= today - timedelta(days=criteria.min_age * 365),
        Patient.date_of_birth >= today - timedelta(days=(criteria.max_age + 1) * 365),
    )
    if criteria.icd10_prefix:
        q = (
            q.join(Condition)
            .filter(Condition.chronic.is_(True), Condition.icd10_code.like(f"{criteria.icd10_prefix}%"))
            .distinct()
        )
    if criteria.sort == "last_seen" and last_seen_col is not None:
        q = q.order_by(last_seen_col.desc())
    else:
        q = q.order_by(Patient.last_name)
    if need_last_seen:  # carry the joined date out as a second column
        q = q.add_columns(last_seen_col)
    rows = []
    for result in q.limit(criteria.limit).all():
        p, seen = (result[0], result[1]) if need_last_seen else (result, None)
        row = {
            "mrn": p.mrn,
            "name": f"{p.first_name} {p.last_name}",
            "age": p.age,
            "sex": str(p.sex).split(".")[-1],
            "chronic_conditions": [f"[{c.icd10_code}] {c.description}" for c in p.conditions if c.chronic],
        }
        if need_last_seen:
            row["last_seen"] = seen.isoformat() if seen else None
        rows.append(row)
    return rows


def _provider_scope(session, identity, provider_name: str = ""):
    """Resolve which provider a personal view is about.

    A name (case-insensitive substring) looks up a colleague — every clinician
    may view, so this is allowed, just not the default. With no name it is the
    signed-in provider (AU4). Returns ``(provider_id, label, error)``; ``error``
    is a ready-to-return message when there is nobody to scope to or the name
    is ambiguous.
    """
    from hdh.core.models import Provider

    if provider_name:
        matches = session.query(Provider).filter(Provider.name.ilike(f"%{provider_name}%")).limit(6).all()
        if not matches:
            return None, "", f"No provider matches '{provider_name}'."
        if len(matches) > 1:
            names = ", ".join(f"{m.name} (#{m.id})" for m in matches)
            return None, "", f"'{provider_name}' matches several providers — be more specific: {names}"
        return matches[0].id, matches[0].name, None
    provider_id = getattr(identity, "provider_id", None)
    if provider_id is None and identity is not None:
        from hdh.core.identity.accounts import provider_for

        provider_id = provider_for(session, identity.subject)
    if provider_id is None:
        return (
            None,
            "",
            (
                "No signed-in provider to scope to. Sign in with `hdh login`, or pass "
                "provider=<name> to search a specific provider's patients."
            ),
        )
    provider = session.get(Provider, provider_id)
    return provider_id, (provider.name if provider else f"provider #{provider_id}"), None


def _period_range(period: str, as_of: date):
    """Translate a named period into an inclusive ``[start, end]`` range,
    relative to ``as_of``. Returns ``(start, end, label, error)``."""

    def month_range(year: int, month: int):
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    p = (period or "this_month").strip().lower()
    year, month = as_of.year, as_of.month
    if p in ("this_month", "last_month"):
        if p == "last_month":
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        start, end = month_range(year, month)
        return start, end, f"{start:%B %Y}", None
    if p in ("this_quarter", "last_quarter"):
        quarter = (month - 1) // 3
        if p == "last_quarter":
            quarter, year = (3, year - 1) if quarter == 0 else (quarter - 1, year)
        start, _ = month_range(year, quarter * 3 + 1)
        _, end = month_range(year, quarter * 3 + 3)
        return start, end, f"Q{quarter + 1} {year}", None
    if p in ("this_week", "last_week"):
        monday = as_of - timedelta(days=as_of.weekday() + (7 if p == "last_week" else 0))
        return monday, monday + timedelta(days=6), f"week of {monday:%Y-%m-%d}", None
    if p == "ytd":
        return date(year, 1, 1), as_of, f"{year} YTD", None
    return (
        None,
        None,
        "",
        f"Unknown period '{period}'. Use this_week, last_week, this_month, "
        "last_month, this_quarter, last_quarter, or ytd.",
    )


def _provider_visit_rows(session, provider_id: int, start: date, end: date, limit: int) -> dict:
    """A provider's non-voided visits in ``[start, end]``: a total, a
    breakdown by visit type, and the most recent ``limit`` visits."""
    from hdh.core.models import Visit

    visits = (
        session.query(Visit)
        .filter(
            Visit.provider_id == provider_id,
            Visit.voided_at.is_(None),
            Visit.visit_date >= start,
            Visit.visit_date <= end,
        )
        .order_by(Visit.visit_date.desc())
        .all()
    )
    by_type: dict[str, int] = {}
    for v in visits:
        key = str(v.visit_type).split(".")[-1]
        by_type[key] = by_type.get(key, 0) + 1
    rows = [
        {
            "date": v.visit_date.isoformat(),
            "mrn": v.patient.mrn,
            "patient": f"{v.patient.first_name} {v.patient.last_name}",
            "visit_type": str(v.visit_type).split(".")[-1],
            "chief_complaint": v.chief_complaint,
        }
        for v in visits[:limit]
    ]
    return {"total": len(visits), "by_type": by_type, "visits": rows}


def _personal_search_tools(session, identity, guard) -> list:
    """The identity-aware reads, built together: ``search_patients`` (name/age/
    ICD plus seen-by-me, recency and last-seen sorting) and ``provider_visits``
    (a provider's caseload for a period). Kept out of ``build_tools`` so its
    schema-shaped signatures — which the model fills and so cannot be collapsed
    into a structure — do not bloat the composition root."""
    from anthropic import beta_tool

    @beta_tool
    @guard
    def search_patients(  # quality: allow(no-god-class) — each arg is a distinct search facet the model fills
        name: str = "",
        min_age: int = 0,
        max_age: int = 120,
        icd10_prefix: str = "",
        seen_by_me: bool = False,
        provider: str = "",
        seen_within_days: int = 0,
        sort: str = "name",
        as_of: str = "",
        limit: int = 20,
    ) -> str:
        """Search patients by name, age, and/or chronic condition — and, for the signed-in provider, by who they have seen and how recently.

        Args:
            name: Substring match on first or last name (optional).
            min_age: Minimum age in years.
            max_age: Maximum age in years.
            icd10_prefix: ICD-10 code prefix of a chronic condition, e.g. "E11" (optional).
            seen_by_me: Restrict to patients the signed-in provider has seen (their own panel).
            provider: A provider name to scope to instead of the signed-in one (optional; a clinician viewing a colleague's patients).
            seen_within_days: Keep only patients last seen within this many days (0 = no recency filter).
            sort: "name" (default) or "last_seen" (most recently seen first; adds a last_seen date to each row).
            as_of: Reference date YYYY-MM-DD for "recent"/sorting (default today; pin it to anchor onto historical data).
            limit: Maximum number of patients to return.
        """
        scope_provider_id = None
        if seen_by_me or provider:
            scope_provider_id, _label, error = _provider_scope(session, identity, provider)
            if error:
                return error
        criteria = PatientSearch(
            name=name,
            min_age=min_age,
            max_age=max_age,
            icd10_prefix=icd10_prefix,
            limit=limit,
            scope_provider_id=scope_provider_id,
            seen_within_days=seen_within_days,
            sort=sort,
            as_of=date.fromisoformat(as_of) if as_of else None,
        )
        rows = _search_patient_rows(session, criteria)
        return json.dumps(rows, indent=2) if rows else "No matching patients."

    @beta_tool
    @guard
    def provider_visits(
        period: str = "this_month", provider: str = "", as_of: str = "", limit: int = 50
    ) -> str:
        """List a provider's visits for a period (this/last month, quarter, week, or YTD) — the signed-in provider's caseload by default.

        Args:
            period: this_week, last_week, this_month, last_month, this_quarter, last_quarter, or ytd.
            provider: A provider name to report on instead of the signed-in one (optional).
            as_of: Reference date YYYY-MM-DD the period is measured from (default today; pin it to reach historical visits).
            limit: Maximum number of visits to list (the total and per-type counts always reflect the whole period).
        """
        provider_id, label, error = _provider_scope(session, identity, provider)
        if error:
            return error
        reference = date.fromisoformat(as_of) if as_of else date.today()
        start, end, period_label, period_error = _period_range(period, reference)
        if period_error:
            return period_error
        result = _provider_visit_rows(session, provider_id, start, end, limit)
        return json.dumps(
            {
                "provider": label,
                "period": period_label,
                "from": start.isoformat(),
                "to": end.isoformat(),
                **result,
            },
            indent=2,
        )

    return [search_patients, provider_visits]


def build_tools(
    session, tables: tuple[str, ...] | None = None, include: set[str] | None = None, identity=None
):
    """Build the agent's tool functions bound to an open DB session.

    ``tables`` narrows the schema embedded in query_database's description;
    ``include`` narrows which tools are returned. Both default to everything
    (the simple engine uses the full set).

    ``identity`` is the signed-in actor (AU4). It reaches the write tools
    so they enforce permissions and attribute to the person; None is a
    system/eval context, where writes proceed under a system actor.
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
    def describe_table(table_name: str) -> str:
        """Get the columns and meaning of ONE database table. Call this before writing SQL against a table — it gives the exact column names so you never guess or introspect the schema yourself.

        Args:
            table_name: A table from the query_database catalog, e.g. "lab_results".
        """
        known = {t.name for t in Base.metadata.sorted_tables}
        if table_name not in known:
            return f"Unknown table '{table_name}'. Known tables: {', '.join(sorted(known))}"
        return _semantic_schema((table_name,))

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
        get_care_gaps,
        get_risk_scores,
        query_database,
        describe_table,
        dataset_stats,
    ]
    all_tools.extend(_personal_search_tools(session, identity, guard))
    all_tools.extend(_ontology_tools(session, identity))
    all_tools.extend(_chart_tools(session, identity))
    if include is None:
        return all_tools
    # describe_table travels with query_database — it is how the model reads the
    # schema, so an intent that can run SQL can always inspect a table's columns.
    wanted = set(include)
    if "query_database" in wanted:
        wanted.add("describe_table")
    return [tool for tool in all_tools if tool.name in wanted]


def _chart_tools(session, identity=None) -> list:
    """Chart maintenance (amend / void / audit trail) — core, so these are
    always available; the agent proposes and hdh.core.chartedit decides."""
    from hdh.modules.agent.chart_tools import build_chart_tools

    return build_chart_tools(session, identity=identity)


#: Optional toolsets, in the order the agent sees them. Each builder is
#: responsible for returning [] when its catalog is not loaded.
_ONTOLOGY_BUILDERS: tuple[tuple[str, str], ...] = (
    ("hdh.modules.icd10cm.agent_tools", "build_icd_tools"),
    ("hdh.modules.snomed.agent_tools", "build_snomed_tools"),
    ("hdh.modules.loinc.agent_tools", "build_loinc_tools"),
    ("hdh.modules.rxnorm.agent_tools", "build_rxnorm_tools"),
    ("hdh.modules.comprehension.agent_tools", "build_comprehension_tools"),
    ("hdh.modules.careplan.agent_tools", "build_careplan_tools"),
    ("hdh.modules.agent.refill_tools", "build_refill_tools"),
)


def _ontology_tools(session, identity=None) -> list:
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
            tools.extend(getattr(module, builder_name)(session, identity=identity))
        except Exception:  # noqa: BLE001 — a broken toolset must not break the agent...
            log.warning(
                "%s.%s raised while building the agent's tools — those tools are "
                "MISSING from this session, not merely unavailable",
                module_path,
                builder_name,
                exc_info=True,  # ...but it must not be silent either
            )
    return tools
