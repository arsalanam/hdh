"""Comprehension tools for the care-program agent (master design §2, §13
phase 6): the agent as prime consumer.

A **published inter-module API** consumed via ``build_comprehension_tools``
exactly like the ICD/SNOMED toolsets — guarded, so the agent works
without this module. ``comprehend_note`` is the master design's
specialized-subagent call (the one sanctioned nested LLM use: extraction
IS the subagent); the other tools are plain reads over stored records —
the consumer contract in action: downstream reads the comprehension,
never the raw note.
"""

from __future__ import annotations

import json

from sqlalchemy import select


def _visit_note(session, mrn: str, visit_date: str):
    from datetime import date

    from hdh.core.models import Patient, Visit, VisitNote

    return (
        session.query(VisitNote)
        .join(Visit, VisitNote.visit_id == Visit.id)
        .join(Patient, Visit.patient_id == Patient.id)
        .filter(Patient.mrn == mrn, Visit.visit_date == date.fromisoformat(visit_date))
        .first()
    )


def _mention_payload(item) -> dict:
    return {
        "text": item.mention.text,
        "type": item.mention.mention_type.value,
        "code": (
            {"system": item.code.system, "code": item.code.code, "display": item.code.display}
            if item.code
            else None
        ),
        "assertion": item.assertion.assertion.value,
        "confidence": item.confidence,
    }


def _mention_search_query(tables, text: str, snomed_code: str, mention_type: str):
    """The search_note_mentions select, kept out of the tool-builder closure."""
    from hdh.core.models import Patient, Visit, VisitNote

    mentions_t, records_t = tables["note_mentions"], tables["note_records"]
    query = (
        select(
            mentions_t.c.text,
            mentions_t.c.mention_type,
            mentions_t.c.assertion,
            mentions_t.c.concept_id,
            Patient.mrn,
            Visit.visit_date,
        )
        .join(records_t, mentions_t.c.record_id == records_t.c.id)
        .join(VisitNote, records_t.c.visit_note_id == VisitNote.id)
        .join(Visit, VisitNote.visit_id == Visit.id)
        .join(Patient, Visit.patient_id == Patient.id)
    )
    if text:
        query = query.where(mentions_t.c.text.ilike(f"%{text}%"))
    if snomed_code:
        query = query.where(mentions_t.c.concept_id == f"snomed_ct:{snomed_code}")
    if mention_type:
        query = query.where(mentions_t.c.mention_type == mention_type.lower())
    return query


def _apply_note_impl(session, extractor, mrn: str, note_text: str, visit_date: str, provider: str) -> str:
    """The provider chart-maintenance flow: free text -> comprehension ->
    new visit + reconciled chart rows + the note stored as the visit's
    VisitNote (full provenance) -> verdicts, review items surfaced."""
    from datetime import date, timedelta

    from hdh.core.models import Patient, Provider, Visit, VisitNote
    from hdh.modules.comprehension.applier import apply_to_chart
    from hdh.modules.comprehension.comprehend import ComprehensionError, comprehend_text
    from hdh.modules.comprehension.pipeline import comprehend_note as run_pipeline
    from hdh.modules.comprehension.pipeline import store_record

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        return f"No patient with MRN {mrn}."
    when = None
    if visit_date:
        lowered = visit_date.strip().lower()
        when = date.today() - timedelta(days=1) if lowered == "yesterday" else date.fromisoformat(lowered)
    provider_row = None
    if provider:
        provider_row = session.query(Provider).filter(Provider.name.ilike(f"%{provider}%")).first()
    try:
        extraction = comprehend_text(note_text, extractor)
    except ComprehensionError as err:
        return f"Comprehension failed validation - nothing was charted: {err}"
    note = run_pipeline(session, extraction)
    from hdh.modules.comprehension.applier import VisitTarget

    # same patient + same date = the same encounter: addenda reconcile
    # into the existing visit instead of spawning a duplicate one
    existing = (
        session.query(Visit)
        .filter(Visit.patient_id == patient.id, Visit.visit_date == (when or date.today()))
        .first()
    )
    result = apply_to_chart(
        session,
        patient,
        note,
        target=VisitTarget(
            visit=existing, visit_date=when, provider_id=provider_row.id if provider_row else None
        ),
    )
    stored_note = VisitNote(
        visit_id=result.visit_id, text=note_text, author_id=provider_row.id if provider_row else None
    )
    session.add(stored_note)
    session.flush()
    record_id = store_record(session, stored_note.id, note)
    return json.dumps(
        {
            "visit_id": result.visit_id,
            "created_visit": result.created_visit,
            "visit_date": str(when or date.today()),
            "provider": provider_row.name if provider_row else None,
            "note_record_id": record_id,
            "verdicts": [{"action": v.action, "kind": v.kind, "detail": v.detail} for v in result.verdicts],
            "review_items": [v.detail for v in result.verdicts if v.action == "review"],
        },
        indent=1,
    )


def build_comprehension_tools(session, extractor=None) -> list:
    """The agent's comprehension toolset (``extractor`` injectable for
    tests; None = the real LLM extractor)."""
    from anthropic import beta_tool

    from hdh.core.models import Base, tool_guard

    guard = tool_guard(session)
    tables = Base.metadata.tables
    if "note_records" not in tables:
        return []  # comprehension entities not bootstrapped
    from hdh.core.models import VisitNote

    if session.query(VisitNote).first() is None:
        return []  # no stored notes — don't offer tools that can only fail

    @beta_tool
    @guard
    def comprehend_note(mrn: str, visit_date: str) -> str:
        """Comprehend a stored visit note into a coded structured record: every clinical mention with its span, ontology code, assertion (negated/historical/family...), and confidence. Use when asked what a note SAYS, to code a note, or to compare a note against the chart. This runs the full pipeline (slow, LLM-backed) — prefer get_note_record when a stored record already exists.

        Args:
            mrn: The patient's MRN, e.g. MRN12345678.
            visit_date: The visit date, YYYY-MM-DD.
        """
        from hdh.modules.comprehension.comprehend import ComprehensionError, comprehend_text
        from hdh.modules.comprehension.pipeline import comprehend_note as run_pipeline

        stored = _visit_note(session, mrn, visit_date)
        if stored is None:
            return f"No stored note for {mrn} on {visit_date}."
        try:
            active_extractor = extractor
            if active_extractor is None:
                from hdh.modules.comprehension.extract import llm_extractor

                active_extractor = llm_extractor()
            extraction = comprehend_text(stored.text, active_extractor)
        except ComprehensionError as err:
            return f"Comprehension failed validation: {err}"
        note = run_pipeline(session, extraction)
        return json.dumps(
            {
                "mentions": [_mention_payload(m) for m in note.mentions],
                "relations": [
                    {
                        "kind": r.kind.value,
                        "source": note.extraction.mentions[r.source_id].text,
                        "target": note.extraction.mentions[r.target_id].text,
                        "inferred": r.inferred,
                    }
                    for r in note.extraction.relations
                ],
                "needs_review": note.needs_review,
            },
            indent=1,
        )

    @beta_tool
    @guard
    def get_note_record(mrn: str, visit_date: str) -> str:
        """The STORED comprehension record for a visit note (fast, no reprocessing): coded mentions with assertions and confidence, as written by `hdh comprehend --store`.

        Args:
            mrn: The patient's MRN.
            visit_date: The visit date, YYYY-MM-DD.
        """
        stored = _visit_note(session, mrn, visit_date)
        if stored is None:
            return f"No stored note for {mrn} on {visit_date}."
        records_t, mentions_t = tables["note_records"], tables["note_mentions"]
        record = session.execute(
            select(records_t).where(records_t.c.visit_note_id == stored.id).order_by(records_t.c.id.desc())
        ).first()
        if record is None:
            return "No stored comprehension record — run comprehend_note (or `hdh comprehend --store`)."
        rows = session.execute(
            select(mentions_t).where(mentions_t.c.record_id == record.id).order_by(mentions_t.c.start)
        ).all()
        return json.dumps(
            {
                "record_id": record.id,
                "status": str(record.status),
                "pipeline_version": record.pipeline_version,
                "mentions": [
                    {
                        "text": row.text,
                        "type": str(row.mention_type),
                        "assertion": row.assertion,
                        "concept_id": row.concept_id,
                        "code": (row.properties or {}).get("code"),
                        "confidence": row.confidence,
                    }
                    for row in rows
                ],
            },
            indent=1,
        )

    @beta_tool
    @guard
    def search_note_mentions(
        text: str = "", snomed_code: str = "", mention_type: str = "", limit: int = 20
    ) -> str:
        """Search every STORED note mention across all comprehended notes — "which notes ever mention X?" grounded in spans, not memory. Filter by text substring, SNOMED code, and/or mention type.

        Args:
            text: Substring of the mention text (optional).
            snomed_code: Exact SNOMED concept id, e.g. 73211009 (optional).
            mention_type: problem | medication | lab_vital | procedure | allergy (optional).
            limit: Maximum rows.
        """
        query = _mention_search_query(tables, text, snomed_code, mention_type)
        rows = session.execute(query.limit(limit)).all()
        if not rows:
            return "No stored mentions match."
        return json.dumps(
            [
                {
                    "mrn": row.mrn,
                    "visit_date": str(row.visit_date),
                    "text": row.text,
                    "type": str(row.mention_type),
                    "assertion": row.assertion,
                    "concept_id": row.concept_id,
                }
                for row in rows
            ],
            indent=1,
        )

    @beta_tool
    @guard
    def apply_note(mrn: str, note_text: str, visit_date: str = "", provider: str = "") -> str:
        """Add a free-text clinical note to a patient's chart: comprehends the note and applies every entity with a reconciliation verdict (new / confirmed / review). If the patient already has a visit on that date the note reconciles into it (addenda attach, never duplicate a visit); otherwise the visit is created. Review items are NOT written - report them to the user for resolution. Use when a provider asks to chart a note or record an encounter they dictate.

        Args:
            mrn: The patient's MRN.
            note_text: The full note text, verbatim as provided.
            visit_date: Encounter date - YYYY-MM-DD or the word "yesterday" (default today).
            provider: Provider name to attribute the visit to, e.g. "Dr. Priya Sharma".
        """
        active_extractor = extractor
        if active_extractor is None:
            from hdh.modules.comprehension.extract import llm_extractor

            active_extractor = llm_extractor()
        return _apply_note_impl(session, active_extractor, mrn, note_text, visit_date, provider)

    return [comprehend_note, get_note_record, search_note_mentions, apply_note]
