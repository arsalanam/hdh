"""Stage 2: whole-note extraction behind one injectable protocol.

The extractor returns RAW dict output (the §7 shape, offsets optional);
:mod:`validate` turns it into a typed Extraction or a rejection whose
reasons become the next attempt's feedback. ``stub_extractor`` serves
tests and offline runs; ``llm_extractor`` follows the icd10cm llm.py
discipline exactly — closed JSON schema via structured output, the
``[agent]`` extra required, everything downstream runs without it.

The LLM emits ``text`` + ``occurrence`` and NEVER character offsets —
the validator's deterministic locator derives spans (design §7).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from hdh.modules.comprehension.contracts import (
    ATTRIBUTE_LEGALITY,
    AttributeKind,
    MentionType,
    Section,
)

# (note_text, sections, feedback-from-last-rejection) -> raw extraction dict
Extractor = Callable[[str, tuple[Section, ...], str | None], dict]


def stub_extractor(raw: dict) -> Extractor:
    """A fixed extraction — tests and offline demos, zero LLM."""

    def extract(_note: str, _sections: tuple[Section, ...], _feedback: str | None) -> dict:
        return raw

    return extract


_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [t.value for t in MentionType]},
        "text": {"type": "string", "description": "VERBATIM substring of the note"},
        "occurrence": {"type": "integer", "description": "nth appearance of text in the note (1 = first)"},
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [k.value for k in AttributeKind]},
                    "text": {"type": "string"},
                    "occurrence": {"type": "integer"},
                },
                "required": ["kind", "text", "occurrence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["type", "text", "occurrence", "attributes"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "mentions": {"type": "array", "items": _ITEM_SCHEMA},
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["treats"]},
                    "source": {"type": "integer"},
                    "target": {"type": "integer"},
                    "inferred": {"type": "boolean"},
                },
                "required": ["kind", "source", "target", "inferred"],
                "additionalProperties": False,
            },
        },
        "shared_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "occurrence": {"type": "integer"},
                },
                "required": ["text", "occurrence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mentions", "relations", "shared_triggers"],
    "additionalProperties": False,
}

_LEGALITY_LINES = "\n".join(
    f"  {kind.value}: only on {sorted(t.value for t in types)}" for kind, types in ATTRIBUTE_LEGALITY.items()
)

PROMPT = """Extract every clinical mention from this note. You FIND and TYPE mentions; you never assign codes and never judge negation/certainty.

Note:
---
{note}
---

Rules:
1. text must be a VERBATIM substring of the note (copy exactly, including case);
   occurrence says which appearance (1 = first). Never paraphrase.
2. Span the MINIMAL head phrase that names the entity — not the sentence.
   A modifier joins the text only when it changes the concept ("Chronic kidney
   disease, stage 3a" is one mention; "severe headache" is "headache" with a
   severity attribute).
3. Every list item is its own mention. Parenthetical codes like "(I48.91)" are
   plain text — never extract or repeat them.
4. attributes are typed sub-spans, verbatim, near their mention:
{legality}
   Keep value and unit SEPARATE whenever the text allows ("98.5F" in the
   note -> value "98.5", unit "F"; never fold the unit into the value).
   control is for disease-control phrasing on problems ("well controlled",
   "worsening"); status_word is for order verbs on medications/procedures
   ("Start", "Continue") — never mix them.
   Emit each relation at most once.
5. relations: ONLY "treats" (medication/procedure -> problem or lab_vital),
   with inferred=true unless the text states the link ("for HTN").
   source/target are 0-based indexes into your mentions array.
6. shared_triggers: a negation/uncertainty word that governs a LIST
   ("No fever, chills, or sweats" -> record "No" once); do not mark the
   mentions themselves.
7. Mention every entity everywhere it appears — the same disease in the
   history and the assessment is TWO mentions. This includes the family
   history clause, which is easy to skip after a long "History of:" list:
   in "Family history: sister: breast cancer" extract "breast cancer"
   (the condition, never the relative).
{feedback}"""


def llm_extractor(model: str | None = None, client=None) -> Extractor:
    """An Extractor backed by Claude structured output (icd10cm llm.py
    pattern; ``client`` injectable for tests)."""
    from anthropic import Anthropic

    client = client or Anthropic()  # quality: allow(dependency-injection)
    resolved = model or os.environ.get("HDH_AGENT_MODEL", "claude-opus-5")

    def extract(note: str, _sections: tuple[Section, ...], feedback: str | None) -> dict:
        feedback_block = (
            f"\nYour previous attempt was rejected — fix ALL of these and try again:\n{feedback}\n"
            if feedback
            else ""
        )
        response = client.beta.messages.create(
            model=resolved,
            max_tokens=4000,
            messages=[
                {
                    "role": "user",
                    "content": PROMPT.format(note=note, legality=_LEGALITY_LINES, feedback=feedback_block),
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
        text_blocks = [block for block in response.content if block.type == "text"]
        return json.loads(text_blocks[0].text)

    return extract
