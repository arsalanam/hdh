"""Nodes 3-5 and 7: constrained generation, and writing the plan.

Design §7. Everything here follows one rule, and it is the reason this is
retrieval rather than a fine-tune:

    The model SELECTS and PHRASES. It never invents a clinical claim.

Each node retrieves candidates first, hands the model those candidates,
and takes back a choice plus wording. A selection naming a candidate that
was not offered is dropped — not corrected, not queried, dropped — because
a plan element with no evidence behind it is exactly what a care plan must
not contain.

That check is deterministic and runs after every node. The design (§8)
makes the same argument about the graph invariant: the parts that must not
be wrong are not asked of the model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.stratify import RiskFlag

#: How many chunks each node retrieves before asking the model to choose.
CANDIDATES_PER_NODE = 4


@dataclass(frozen=True)
class Candidate:
    """One retrieved option the model may select, and its citation."""

    ref: str
    text: str


@dataclass(frozen=True)
class SelectionTask:
    """What the model is asked: an instruction, a situation, and a menu."""

    instruction: str
    situation: str
    candidates: tuple[Candidate, ...]
    schema: Mapping[str, Any]


#: Given a task, return JSON matching its schema. Injectable for the same
#: reason `Extractor` is: the graph has to run in CI with no API key.
Selector = Callable[[SelectionTask], dict]


@dataclass(frozen=True)
class ConcernDraft:
    """A proposed health concern, before it is written."""

    statement: str
    concern_type: str
    evidence_refs: tuple[str, ...]
    basis: str = ""


@dataclass(frozen=True)
class GoalDraft:
    statement: str
    concern_index: int
    target_value: str = ""
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterventionDraft:
    statement: str
    goal_index: int
    intervention_type: str = "monitoring"
    owner_role: str = ""
    evidence_refs: tuple[str, ...] = ()


@dataclass
class PlanDraft:
    """The whole proposal, before anything is persisted."""

    concerns: list[ConcernDraft] = field(default_factory=list)
    goals: list[GoalDraft] = field(default_factory=list)
    interventions: list[InterventionDraft] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


# ── the schemas the model must answer in ─────────────────────────────────


def _selection_schema(extra: Mapping[str, Any], required: Sequence[str]) -> dict:
    """A selection response: a list of items, each citing what it came from."""
    return {
        "type": "object",
        "properties": {
            "selections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                        **extra,
                    },
                    "required": ["statement", "cites", *required],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["selections"],
        "additionalProperties": False,
    }


CONCERN_SCHEMA = _selection_schema(
    {"concern_type": {"type": "string", "enum": ["condition", "risk", "sdoh", "functional"]}},
    ["concern_type"],
)
GOAL_SCHEMA = _selection_schema(
    {"concern_index": {"type": "integer"}, "target_value": {"type": "string"}},
    ["concern_index"],
)
INTERVENTION_SCHEMA = _selection_schema(
    {
        "goal_index": {"type": "integer"},
        "intervention_type": {
            "type": "string",
            "enum": ["medication", "service", "referral", "education", "monitoring"],
        },
        "owner_role": {"type": "string"},
    },
    ["goal_index", "intervention_type"],
)


# ── selectors ────────────────────────────────────────────────────────────


def stub_selector(responses: Sequence[dict]) -> Selector:
    """A fixed sequence of answers — tests and offline demos, zero LLM."""
    queue = list(responses)

    def select(_task: SelectionTask) -> dict:
        return queue.pop(0) if queue else {"selections": []}

    return select


def llm_selector(model: str | None = None, client=None) -> Selector:
    """A Selector backed by Claude structured output."""
    from anthropic import Anthropic

    client = client or Anthropic()  # quality: allow(dependency-injection)
    resolved = model or os.environ.get("HDH_AGENT_MODEL", "claude-opus-5")

    def select(task: SelectionTask) -> dict:
        menu = "\n\n".join(f"[{c.ref}]\n{c.text}" for c in task.candidates)
        prompt = (
            f"{task.instruction}\n\n"
            f"PATIENT SITUATION\n{task.situation}\n\n"
            f"CANDIDATES — you may only select from these, and every item you "
            f"return must cite at least one by its [reference]:\n\n{menu}\n\n"
            "Do not propose anything the candidates do not support. Returning "
            "fewer items, or none, is a valid and expected answer."
        )
        response = client.beta.messages.create(
            model=resolved,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": dict(task.schema)}},
        )
        blocks = [block for block in response.content if block.type == "text"]
        return json.loads(blocks[0].text)

    return select


# ── retrieval helpers ────────────────────────────────────────────────────


def situation(context: CarePlanContext, flags: Sequence[RiskFlag]) -> str:
    """The patient in a paragraph — what retrieval and selection both see."""
    drugs = ", ".join(f"{m.name} ({m.drug_class})" for m in context.medications) or "none recorded"
    problems = ", ".join(p.description for p in context.problems) or "none recorded"
    lines = [
        f"{context.age}-year-old, sex {context.sex.lower()}.",
        f"Problems: {problems}.",
        f"Medications: {drugs}.",
    ]
    if context.social and context.social.lives_alone is True:
        lines.append("Lives alone.")
    for flag in flags:
        lines.append(f"Flagged: {flag.statement} ({flag.basis}).")
    return "\n".join(lines)


def _candidates(store, query: str, corpus: str = "med_safety") -> tuple[Candidate, ...]:
    hits = store.search(query, corpus, k=CANDIDATES_PER_NODE)
    return tuple(Candidate(ref=hit.citation(), text=hit.chunk) for hit in hits)


def _kept(items: Sequence[dict], offered: set[str], drafts: list, dropped: list[str], build) -> None:
    """Keep only selections that cite something actually offered.

    A citation the menu did not contain is a claim with no evidence behind
    it, however plausible the sentence is. It is dropped and recorded —
    silently discarding it would hide a model doing the one thing this
    design forbids.
    """
    for item in items:
        cites = tuple(c for c in item.get("cites", ()) if c in offered)
        if not cites:
            dropped.append(f"{item.get('statement', '?')[:60]} — cited nothing that was offered")
            continue
        drafts.append(build(item, cites))


# ── the nodes ────────────────────────────────────────────────────────────


def propose_concerns(store, context, flags, selector: Selector) -> tuple[list[ConcernDraft], list[str]]:
    """Node 3. Concerns, each citing the chunk it came from."""
    described = situation(context, flags)
    candidates = _candidates(store, described)
    drafts: list[ConcernDraft] = []
    dropped: list[str] = []
    if not candidates:
        return drafts, ["no knowledge retrieved — no concern can be supported"]
    answer = selector(
        SelectionTask(
            instruction=(
                "Select the health concerns this patient's situation supports. "
                "Phrase each in one sentence a clinician would recognise."
            ),
            situation=described,
            candidates=candidates,
            schema=CONCERN_SCHEMA,
        )
    )
    _kept(
        answer.get("selections", []),
        {c.ref for c in candidates},
        drafts,
        dropped,
        lambda item, cites: ConcernDraft(
            statement=item["statement"],
            concern_type=item.get("concern_type", "risk"),
            evidence_refs=cites,
        ),
    )
    return drafts, dropped


def propose_goals(store, context, concerns, selector: Selector) -> tuple[list[GoalDraft], list[str]]:
    """Node 4. One pass per concern, so a goal cannot outlive its reason."""
    drafts: list[GoalDraft] = []
    dropped: list[str] = []
    for index, concern in enumerate(concerns):
        candidates = _candidates(store, concern.statement)
        if not candidates:
            dropped.append(f"goal for {concern.statement[:40]!r} — nothing retrieved")
            continue
        answer = selector(
            SelectionTask(
                instruction=(
                    "Select goals that answer this concern. State each as an "
                    "outcome for the patient, not an action for the clinician."
                ),
                situation=f"{situation(context, ())}\n\nCONCERN: {concern.statement}",
                candidates=candidates,
                schema=GOAL_SCHEMA,
            )
        )
        _kept(
            answer.get("selections", []),
            {c.ref for c in candidates},
            drafts,
            dropped,
            lambda item, cites, i=index: GoalDraft(
                statement=item["statement"],
                concern_index=i,
                target_value=item.get("target_value", ""),
                evidence_refs=cites,
            ),
        )
    return drafts, dropped


def propose_interventions(store, context, goals, selector: Selector):
    """Node 5. One pass per goal; each intervention names an owner."""
    drafts: list[InterventionDraft] = []
    dropped: list[str] = []
    for index, goal in enumerate(goals):
        candidates = _candidates(store, goal.statement)
        if not candidates:
            dropped.append(f"intervention for {goal.statement[:40]!r} — nothing retrieved")
            continue
        answer = selector(
            SelectionTask(
                instruction=(
                    "Select interventions that serve this goal. Name the role "
                    "responsible for each. Removing or reducing a medication is "
                    "a valid intervention."
                ),
                situation=f"{situation(context, ())}\n\nGOAL: {goal.statement}",
                candidates=candidates,
                schema=INTERVENTION_SCHEMA,
            )
        )
        _kept(
            answer.get("selections", []),
            {c.ref for c in candidates},
            drafts,
            dropped,
            lambda item, cites, i=index: InterventionDraft(
                statement=item["statement"],
                goal_index=i,
                intervention_type=item.get("intervention_type", "monitoring"),
                owner_role=item.get("owner_role", ""),
                evidence_refs=cites,
            ),
        )
    return drafts, dropped
