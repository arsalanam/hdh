"""The LLM half of the funnel: axis extraction via structured output.

Follows the agent pipeline's discipline exactly — a closed JSON schema the
model must satisfy (the VERDICT_SCHEMA pattern), so free text never leaks
into the deterministic stages. Requires the ``[agent]`` extra and an
Anthropic API key; everything downstream of this file runs without either.
"""

from __future__ import annotations

import json
import os

from hdh.modules.icd10cm.service import AXIS_VALUES, AxisExtraction, AxisExtractor

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "string",
            "description": "Canonical clinical search terms (formal anatomy, not lay words)",
        },
        "axes": {
            "type": "object",
            "properties": {
                axis: {"type": "string", "enum": list(values)} for axis, values in AXIS_VALUES.items()
            },
            "additionalProperties": False,
        },
    },
    "required": ["terms", "axes"],
    "additionalProperties": False,
}

PROMPT = """Extract ICD-10-CM search terms and clinical axes from this description.

Description: {description}

Rules:
1. terms: the formal clinical vocabulary a code description would use
   ("inner side of ankle" → "medial malleolus"; "broke" → "fracture").
   Prefer 2-4 core terms; extra qualifiers narrow the search too far.
2. axes: ONLY values the description actually states or clearly implies
   ("first visit" → encounter=initial; "skin intact" → exposure=closed).
   Never guess an unstated axis — leaving it out is the correct answer.
3. Never emit "unspecified": an axis the description doesn't mention is
   OMITTED, not unspecified. Never infer displacement or laterality.
"""


def llm_extractor(model: str | None = None, client=None) -> AxisExtractor:
    """An AxisExtractor backed by Claude structured output.

    ``client`` is injectable for tests; the default is constructed here
    because this factory is only ever called from composition roots.
    """
    from anthropic import Anthropic

    client = client or Anthropic()  # quality: allow(dependency-injection)
    resolved = model or os.environ.get("HDH_AGENT_MODEL", "claude-opus-5")

    def extract(description: str) -> AxisExtraction:
        response = client.beta.messages.create(
            model=resolved,
            max_tokens=500,
            messages=[{"role": "user", "content": PROMPT.format(description=description)}],
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
        text_blocks = [block for block in response.content if block.type == "text"]
        payload = json.loads(text_blocks[0].text)
        return AxisExtraction(payload["terms"], payload.get("axes", {}))

    return extract
