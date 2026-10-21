"""The dictation store — saved transcripts of recorded encounters and dictated
notes (design docs/design/media-io-service.md §3, phase 2).

The media service is stateless, so the transcript it produces is persisted
HERE (the hdh DB) the moment recording stops: a ``dictations`` row with the
full text, diarized segments, the signed-in provider, an optional
patient/encounter binding, and a generated summary. **"The last dictation" is
simply the provider's most recent non-voided row** — what lets a provider say
"record the encounter", then later "update the chart now" and have the agent
resolve the right source (phase 4 does the charting).

A dictation is a SOURCE, never the chart: charting it runs through the existing
comprehension and order tools and writes ``visit_notes`` / orders as drafts for
review. Nothing here writes to the chart.

The summary is behind an injectable seam (default = one real Claude call; tests
inject a fake) and it NEVER blocks a save — a failed summary is stored as
``None`` and the transcript is kept regardless. A summary is a convenience, not
a gate on persisting PHI the provider just dictated.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: A transcript -> a short clinical précis, or None when summarization is
#: unavailable (no key, a transport error): a summary never blocks a save.
Summarizer = Callable[[str], "str | None"]


class DictationError(Exception):
    """A dictation cannot be saved — e.g. a bound MRN that names no patient."""


@dataclass(frozen=True)
class NewDictation:
    """The fields of one transcript to persist. Grouped into a record so the
    store and backend stay single-purpose (provider/patient already resolved
    to ids, summary already generated — this is what hits the table)."""

    text: str
    provider_id: int | None = None
    patient_id: int | None = None
    visit_id: int | None = None
    source: str = "dictation"
    segments: list | None = None
    model: str | None = None
    duration_s: float | None = None
    needs_review: bool = False
    summary: str | None = None


@dataclass(frozen=True)
class DictationInput:
    """What a caller (the HTTP endpoint) hands the backend to save: the raw
    transcript plus the patient MRN to bind to. The backend resolves the
    provider from the identity and the patient from the MRN, then summarizes."""

    text: str
    mrn: str | None = None
    visit_id: int | None = None
    source: str = "dictation"
    segments: list | None = None
    stt_model: str | None = None
    duration_s: float | None = None
    needs_review: bool = False


# ── persistence (pure: no LLM, no identity resolution) ───────────────────────


def _table():
    from hdh.core.models import Base

    return Base.metadata.tables["dictations"]


def _speakers(segments: Any) -> list[str]:
    """The distinct speaker labels across a transcript's segments, in order."""
    if not segments:
        return []
    seen: list[str] = []
    for seg in segments:
        label = seg.get("speaker") if isinstance(seg, dict) else None
        if label and label not in seen:
            seen.append(label)
    return seen


def _row_to_dict(row, *, full: bool) -> dict:
    """One dictation row → the JSON-ready payload the API and the agent read."""
    out: dict[str, Any] = {
        "dictation_id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "source": row.source,
        "provider_id": row.provider_id,
        "patient_id": row.patient_id,
        "visit_id": row.visit_id,
        "summary": row.summary,
        "model": row.model,
        "duration_s": row.duration_s,
        "needs_review": bool(row.needs_review),
        "chars": len(row.text or ""),
    }
    if full:
        out["text"] = row.text
        out["segments"] = row.segments
        out["speakers"] = _speakers(row.segments)
    return out


def _resolve_provider_id(session, identity) -> int | None:
    """The signed-in provider's id — from the Identity directly, else mapped
    from its Keycloak subject. None when no provider can be resolved, which the
    callers treat as "no dictation to show" rather than guessing."""
    from hdh.core.identity.accounts import provider_for

    provider_id = getattr(identity, "provider_id", None)
    if provider_id is not None:
        return provider_id
    subject = getattr(identity, "subject", None)
    if subject is None:
        return None
    return provider_for(session, subject)


def _with_mrn(session, payload: dict | None) -> dict | None:
    """Enrich a payload with the bound patient's MRN (so the agent can chart
    against it) — a dictation stores patient_id, but the agent speaks in MRNs."""
    if payload and payload.get("patient_id"):
        from hdh.core.models import Patient

        patient = session.get(Patient, payload["patient_id"])
        payload["mrn"] = patient.mrn if patient else None
    return payload


def save_dictation(session, rec: NewDictation) -> int:
    """Insert one dictation and return its id. Pure persistence — the caller
    (the backend) computes the summary and resolves provider/patient first."""
    from sqlalchemy import insert

    table = _table()
    return session.execute(
        insert(table).values(
            provider_id=rec.provider_id,
            patient_id=rec.patient_id,
            visit_id=rec.visit_id,
            source=rec.source,
            text=rec.text,
            segments=rec.segments,
            summary=rec.summary,
            model=rec.model,
            duration_s=rec.duration_s,
            needs_review=bool(rec.needs_review),
            created_at=datetime.now(),
        )
    ).inserted_primary_key[0]


def get_dictation(
    session, dictation_id: int, *, provider_id: int | None = None, full: bool = True
) -> dict | None:
    """One dictation by id (non-voided). Scoped to ``provider_id`` when given,
    so one provider cannot read another's dictation by guessing an id."""
    from sqlalchemy import select

    table = _table()
    query = select(table).where(table.c.id == dictation_id, table.c.voided_at.is_(None))
    if provider_id is not None:
        query = query.where(table.c.provider_id == provider_id)
    row = session.execute(query).first()
    return _row_to_dict(row, full=full) if row else None


def latest_dictation(
    session, *, provider_id: int | None = None, patient_id: int | None = None, full: bool = True
) -> dict | None:
    """The provider's most recent non-voided dictation — "the last dictation".
    Narrowed to ``patient_id`` when a chart is open (design decision §13.2:
    per provider, scoped to the bound patient when one is present)."""
    from sqlalchemy import select

    table = _table()
    query = select(table).where(table.c.voided_at.is_(None))
    if provider_id is not None:
        query = query.where(table.c.provider_id == provider_id)
    if patient_id is not None:
        query = query.where(table.c.patient_id == patient_id)
    query = query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)
    row = session.execute(query).first()
    return _row_to_dict(row, full=full) if row else None


# ── the summary seam (injectable; default = one real Claude call) ────────────


def llm_summarizer(model: str | None = None, client=None) -> Summarizer:
    """A Summarizer backed by one Claude call — a concise clinical précis of a
    transcript for quick recall (extract.py pattern; ``client`` injectable for
    tests). Any failure returns None; the transcript is still saved."""
    import anthropic

    client = client or anthropic.Anthropic()  # quality: allow(dependency-injection)
    resolved = model or os.environ.get("HDH_AGENT_MODEL", "claude-opus-5")

    def summarize(text: str) -> str | None:
        snippet = (text or "").strip()
        if not snippet:
            return None
        try:
            message = client.messages.create(
                model=resolved,
                max_tokens=400,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Summarize this clinical encounter transcript in 2-4 sentences for a "
                            "provider's quick recall: the chief complaint, the salient findings, and any "
                            "plan or follow-up mentioned. State only what the transcript says — add "
                            "nothing and diagnose nothing. Output only the summary.\n\n" + snippet
                        ),
                    }
                ],
            )
            text_out = "\n".join(
                getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text"
            ).strip()
            return text_out or None
        except Exception:  # noqa: BLE001 — a summary is best-effort; never block the save
            return None

    return summarize


# ── the composition-root facade for the HTTP endpoints ───────────────────────


def dictation_backend(*, db_path: str, model: str | None = None, summarize: Summarizer | None = None):
    """The backend the agent_api endpoints inject (default built in create_app).

    A request-scoped session per call (this mirrors ``note_ingest`` — the app is
    its own composition root). ``summarize`` is injectable for tests; the default
    is the real one-shot summarizer, built lazily so no Anthropic client is
    constructed until a save actually needs one.
    """
    from types import SimpleNamespace

    def _session():
        from hdh.core.models import get_engine, get_session

        return get_session(get_engine(db_path))  # quality: allow(dependency-injection)

    def _summarizer() -> Summarizer:
        return summarize or llm_summarizer(model=model)

    def save(request: DictationInput, *, identity: Any = None) -> dict:
        from hdh.core.models import Patient

        session = _session()
        try:
            provider_id = _resolve_provider_id(session, identity)
            patient_id = None
            if request.mrn:
                patient = session.query(Patient).filter(Patient.mrn == request.mrn).first()
                if patient is None:
                    raise DictationError(f"no patient with MRN {request.mrn}")
                patient_id = patient.id
            dictation_id = save_dictation(
                session,
                NewDictation(
                    text=request.text,
                    provider_id=provider_id,
                    patient_id=patient_id,
                    visit_id=request.visit_id,
                    source=request.source,
                    segments=request.segments,
                    model=request.stt_model,
                    duration_s=request.duration_s,
                    needs_review=request.needs_review,
                    summary=_summarizer()(request.text),
                ),
            )
            session.commit()
            saved = get_dictation(session, dictation_id, full=False)
            if saved is not None:
                saved["mrn"] = request.mrn
            return saved or {}
        finally:
            session.close()

    def current(*, identity: Any = None, mrn: str | None = None) -> dict | None:
        from hdh.core.models import Patient

        session = _session()
        try:
            provider_id = _resolve_provider_id(session, identity)
            if provider_id is None:
                return None  # no provider resolved → refuse to guess whose dictation
            patient_id = None
            if mrn:
                patient = session.query(Patient).filter(Patient.mrn == mrn).first()
                if patient is None:
                    return None  # a bound MRN that names no patient has no dictation
                patient_id = patient.id
            return _with_mrn(
                session, latest_dictation(session, provider_id=provider_id, patient_id=patient_id, full=True)
            )
        finally:
            session.close()

    def get(dictation_id: int, *, identity: Any = None) -> dict | None:
        session = _session()
        try:
            provider_id = _resolve_provider_id(session, identity)
            return _with_mrn(
                session, get_dictation(session, dictation_id, provider_id=provider_id, full=True)
            )
        finally:
            session.close()

    return SimpleNamespace(save=save, current=current, get=get)


# ── the agent tool ───────────────────────────────────────────────────────────


def build_dictation_tools(session, *, identity=None) -> list:
    """The agent's one dictation tool: resolve the provider's most recent
    recorded conversation / dictation so "update the chart now" has a source.
    Guarded — absent until the ``dictations`` table is bootstrapped."""
    from anthropic import beta_tool

    from hdh.core.models import Base, tool_guard

    # The tool cannot run without a session, and it needs the table registered;
    # absent either, it is not offered (mirrors the other registry toolsets,
    # which stay out of the `build_tools(None)` always-on baseline).
    if session is None or "dictations" not in Base.metadata.tables:
        return []

    guard = tool_guard(session)

    @beta_tool
    @guard
    def use_last_dictation(mrn: str = "") -> str:
        """Retrieve the most recent recorded conversation or dictation the signed-in provider saved — its transcript, diarized segments, bound patient (MRN), and summary — so you can act on "update the chart now" or "chart the last conversation". This returns the SOURCE text only; charting it still goes through the normal tools (apply_note for history, the order tools for medications/labs/procedures) as drafts for review — never auto-committed. If an MRN is given, only a dictation bound to that patient is returned.

        Args:
            mrn: Restrict to a dictation bound to this patient's MRN, e.g. MRN12345678 (optional).
        """
        from hdh.core.models import Patient

        provider_id = _resolve_provider_id(session, identity)
        if provider_id is None:
            return "No signed-in provider could be resolved, so there is no 'last dictation' to use."
        patient_id = None
        if mrn:
            patient = session.query(Patient).filter(Patient.mrn == mrn).first()
            if patient is None:
                return f"No patient with MRN {mrn}."
            patient_id = patient.id
        latest = _with_mrn(
            session, latest_dictation(session, provider_id=provider_id, patient_id=patient_id, full=True)
        )
        if latest is None:
            scope = f" bound to {mrn}" if mrn else ""
            return f"No recorded dictation found{scope}. Ask the provider to record or upload one first."
        return json.dumps(latest, indent=1)

    return [use_last_dictation]
