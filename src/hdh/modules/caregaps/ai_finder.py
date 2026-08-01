"""AI-based care-gap finder: LLM chart review with clinical reasoning.

Instead of fixed rules, each patient's chart is reviewed by a Claude model
that identifies clinically meaningful gaps — including kinds no rule in
detector.py expresses (missing condition-specific monitoring, guideline
medication gaps, screening due by age). Output is schema-enforced JSON, so
every finding maps cleanly onto the same ``CareGap`` record the rule engine
produces (``source="ai"``).

Costs and caveats: each reviewed chart is one model call (roughly a few
cents); results are not deterministic; and this is an educational
demonstration, not a validated clinical quality measure. Without ``--mrn``
it reviews only the ``sample`` most complex patients (most chronic
conditions) rather than the whole panel.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from sqlalchemy import func

from hdh.core.models import ChronicCondition, Patient

from .detector import CareGap, reference_date

DEFAULT_MODEL = "claude-opus-5"
CHART_CHAR_CAP = 12_000  # bound per-patient review cost

REVIEW_PROMPT = """You are a primary-care quality reviewer examining ONE patient chart from a
SYNTHETIC family-medicine dataset (no real patients). Treat the AS-OF DATE
as today, and reason explicitly about elapsed time:

1. Find the most recent visit date. If it requested a follow-up interval
   ("Return in N days") that has lapsed before the AS-OF DATE with no later
   visit, that is a missed_follow_up gap.
2. For each active chronic condition, check when its monitoring was last
   done relative to the AS-OF DATE (diabetes: HbA1c ~every 6 months;
   hyperlipidemia: lipid panel yearly; hypertension: BP recheck per plan;
   COPD: spirometry/review yearly). Stale monitoring is a gap.
3. If the last preventive/annual visit is more than a year before the
   AS-OF DATE, that is overdue_preventive.
4. Flag guideline medication gaps visible in the chart (e.g. diabetes +
   hyperlipidemia with no statin on the med list) and polypharmacy concerns.

Only use facts that are IN the chart — never invent visits or results — but
DO report the absence of expected care: an expected test or visit missing
relative to the AS-OF DATE is exactly what a gap is. Use snake_case
gap_type labels, keep each description and recommendation under 30 words,
and report at most the 6 most important gaps. Return an empty list only if
care is genuinely up to date.
"""

GAPS_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap_type": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "description": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
                "required": ["gap_type", "severity", "description", "recommendation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["gaps"],
    "additionalProperties": False,
}

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _parse_gaps_json(text: str) -> dict:
    """Parse the reviewer's JSON; salvage complete gap objects if truncated.

    A response cut off by max_tokens is invalid JSON — rather than silently
    reporting zero gaps, recover every complete finding and warn.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    tail = text.rfind("},")
    if tail != -1:
        try:
            salvaged = json.loads(text[: tail + 1] + "]}")
            print(
                f"⚠  AI review was truncated — salvaged {len(salvaged.get('gaps', []))} complete finding(s)"
            )
            return salvaged
        except ValueError:
            pass
    print("⚠  AI review returned unparseable output — reporting no gaps")
    return {"gaps": []}


def _clip_chart(chart: str, cap: int) -> str:
    """Bound chart size while keeping what matters for gap review.

    Charts are chronological, so a naive head-truncation would discard the
    most recent visits — exactly the part recency-based gaps live in. Keep
    the header (demographics, problem list) and the tail (latest visits).
    """
    if len(chart) <= cap:
        return chart
    head = chart[: cap // 4]
    tail = chart[-(cap - len(head)) :]
    omitted = len(chart) - len(head) - len(tail)
    return f"{head}\n...[{omitted:,} chars of older visits omitted]...\n{tail}"


@dataclass
class AIGapFinder:
    """LLM chart reviewer implementing the GapFinder interface.

    ``review(chart_text, as_of) -> {"gaps": [...]}`` is injectable, so tests
    (and offline demos) run without an API key.
    """

    model: str | None = None
    review: Callable | None = None

    name: ClassVar[str] = "ai"
    description: ClassVar[str] = "LLM chart review — clinical reasoning, costs tokens"

    def find(self, session, *, mrn=None, limit=None, as_of=None, sample=5) -> list[CareGap]:
        """Review one patient (mrn) or the ``sample`` most complex patients."""
        as_of = as_of or reference_date(session)
        patients = self._select_patients(session, mrn, sample)
        if not patients:
            return []

        gaps: list[CareGap] = []
        for patient in patients:
            gaps.extend(self._review_patient(patient, as_of))

        gaps.sort(key=lambda g: (_SEVERITY_RANK.get(g.severity, 3), g.mrn))
        return gaps[:limit] if limit else gaps

    def _select_patients(self, session, mrn: str | None, sample: int) -> list:
        """One named patient, or the highest-complexity patients to review."""
        if mrn:
            patient = session.query(Patient).filter(Patient.mrn == mrn).first()
            return [patient] if patient else []
        return (
            session.query(Patient)
            .join(ChronicCondition)
            .group_by(Patient.id)
            .order_by(func.count(ChronicCondition.id).desc())
            .limit(sample)
            .all()
        )

    def _review_patient(self, patient, as_of: date) -> list[CareGap]:
        """Run one chart through the reviewer and map findings to CareGaps."""
        from hdh.core.exporters import patient_to_text

        chart = _clip_chart(patient_to_text(patient), CHART_CHAR_CAP)
        result = (self.review or self._llm_review)(chart, as_of)
        findings = result.get("gaps") or []
        return [
            CareGap(
                mrn=patient.mrn,
                patient_name=f"{patient.first_name} {patient.last_name}",
                age=patient.age,
                gap_type=str(finding.get("gap_type", "ai_identified")),
                severity=str(finding.get("severity", "medium")),
                description=(f"{finding.get('description', '')} → {finding.get('recommendation', '')}").strip(
                    " →"
                ),
                overdue_days=0,  # AI findings are qualitative; rules quantify lateness
                source="ai",
            )
            for finding in findings
        ]

    def _llm_review(self, chart: str, as_of: date) -> dict:
        """Default reviewer: one schema-enforced Claude call per chart."""
        import anthropic

        # Default factory; inject `review` (tests) or a client-bound callable instead.
        client = anthropic.Anthropic()  # quality: allow(dependency-injection)
        message = client.messages.create(
            model=self.model or os.environ.get("HDH_AGENT_MODEL", DEFAULT_MODEL),
            max_tokens=4000,
            system=REVIEW_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": GAPS_SCHEMA}},
            messages=[{"role": "user", "content": f"AS-OF DATE: {as_of}\n\nCHART:\n{chart}"}],
        )
        text = "\n".join(b.text for b in message.content if b.type == "text")
        return _parse_gaps_json(text)
