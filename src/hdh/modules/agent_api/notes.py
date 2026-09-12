"""Uploaded notes → chart (design agentic-ui-module §8).

A provider uploads a note — handwritten, scanned, or (via #87) dictated — to
save themselves typing. It is ALWAYS a note: transcribed to text, run through
the SAME comprehension path a typed note takes, and charted as history with
reconciliation verdicts (an ambiguous value reaches the review queue, never a
confident guess). It is NEVER a lab-result feed (those are LOINC-coded, via
``interchange``) or an order (in-chart → Pharmacy). The upload saves typing;
it is not a new write path.

``ingest`` is injectable so the HTTP surface is testable without a vision
model — tests pass a fake that returns verdicts for a file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: (mrn, filename, content_type, data, identity) → result dict.
IngestFn = Callable[[str, str, str, bytes, object], dict]


class NoteError(Exception):
    """The upload cannot become a note — unknown patient, or media we do not
    turn into text here (e.g. audio, which is the speech front door #87)."""


def transcribe(filename: str, content_type: str, data: bytes, *, model=None) -> str:
    """A file → the text the comprehension pipeline reads.

    Plain text decodes directly; an image or PDF goes through a vision pass
    (the model reads the page, marking anything illegible rather than guessing).
    Audio is NOT handled here — dictation is the speech front door (#87), which
    brings its own transcription.
    """
    content_type = (content_type or "").lower()
    if content_type.startswith("text/") or content_type == "application/json":
        return data.decode("utf-8", errors="replace")
    if content_type.startswith("audio/"):
        raise NoteError("audio dictation is the speech front door (#87), not this upload path")
    if content_type.startswith("image/") or content_type == "application/pdf":
        return _vision_transcribe(content_type, data, model=model)
    raise NoteError(f"unsupported media type for a note: {content_type or 'unknown'}")


def _vision_transcribe(content_type: str, data: bytes, *, model) -> str:
    """Read a scanned or handwritten note to text with a vision pass."""
    import base64

    import anthropic

    from hdh.modules.agent.agent import DEFAULT_MODEL

    payload = base64.standard_b64encode(data).decode()
    media: Any
    if content_type == "application/pdf":
        media = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": payload},
        }
    else:
        media = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": payload}}
    instruction = {
        "type": "text",
        "text": (
            "Transcribe this clinical note to plain text, exactly as written. Mark anything ambiguous or "
            "illegible as [illegible] rather than guessing. Output only the transcription — no preamble."
        ),
    }
    # The default transcriber; the whole pre-pass is injectable via the `ingest`
    # seam (tests use a fake), so constructing the client here is intentional.
    content: list[Any] = [media, instruction]
    client = anthropic.Anthropic()  # quality: allow(dependency-injection)
    message = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": content}],
    )
    return "\n".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text")


def note_ingest(*, db_path: str, model: str | None = None) -> IngestFn:
    """The default ingest: transcribe → comprehend → chart, attributed to the
    signed-in provider. A fresh session per request (this app is its own root).
    """

    def ingest(mrn: str, filename: str, content_type: str, data: bytes, identity) -> dict:
        from hdh.core.identity.accounts import provider_for
        from hdh.core.models import Patient, get_engine, get_session
        from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
        from hdh.modules.comprehension.comprehend import comprehend_text
        from hdh.modules.comprehension.extract import llm_extractor
        from hdh.modules.comprehension.pipeline import comprehend_note

        text = transcribe(filename, content_type, data, model=model)
        session = get_session(get_engine(db_path))  # quality: allow(dependency-injection)
        try:
            patient = session.query(Patient).filter(Patient.mrn == mrn).first()
            if patient is None:
                raise NoteError(f"no patient with MRN {mrn}")
            provider_id = getattr(identity, "provider_id", None)
            if provider_id is None and identity is not None:
                provider_id = provider_for(session, identity.subject)
            note = comprehend_note(session, comprehend_text(text, llm_extractor(model=model)))
            result = apply_to_chart(session, patient, note, VisitTarget(provider_id=provider_id))
            return {
                "mrn": mrn,
                "visit_id": result.visit_id,
                "created_visit": result.created_visit,
                "needs_review": result.needs_review,
                "chars": len(text),
                "verdicts": [
                    {"action": v.action, "kind": v.kind, "detail": v.detail} for v in result.verdicts
                ],
            }
        finally:
            session.close()

    return ingest
