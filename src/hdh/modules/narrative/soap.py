"""
SOAP note presentation for the narrative module.

The deterministic renderer lives in hdh.core.notes (stored notes are core
chart data now); this module keeps presentation helpers and the optional
LLM polish.
"""

from hdh.core.models import Patient
from hdh.core.notes import visit_to_soap

__all__ = ["visit_to_soap", "patient_soap_notes", "polish_with_llm"]


def patient_soap_notes(patient: Patient, last_n: int | None = None) -> list[str]:
    """SOAP notes for a patient's visits (chronological); last_n limits to the most recent."""
    visits = patient.visits[-last_n:] if last_n else patient.visits
    return [visit_to_soap(v, patient) for v in visits]


def polish_with_llm(note: str, model: str = "claude-opus-5", client=None) -> str:
    """Rewrite a templated SOAP note as natural clinical prose using Claude."""
    import anthropic

    # Default factory for the optional LLM path; callers may pass a client.
    client = client or anthropic.Anthropic()  # quality: allow(dependency-injection)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=(
            "You rewrite templated SOAP notes from a SYNTHETIC dataset as natural, "
            "realistic clinical prose. Keep the S/O/A/P structure, all clinical "
            "values, codes, and dates exactly as given. Output only the note."
        ),
        messages=[{"role": "user", "content": note}],
    )
    if response.stop_reason == "refusal":
        return note
    return next((b.text for b in response.content if b.type == "text"), note)
