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

from hdh.modules.careplan import usage
from hdh.modules.careplan.caching import cached_text
from hdh.modules.careplan.context import CarePlanContext
from hdh.modules.careplan.prompts import prompt_set
from hdh.modules.careplan.stratify import RiskFlag
from hdh.modules.careplan.triage import Topic

#: How many chunks each node retrieves PER CORPUS before asking the model
#: to choose, and the ceiling across all of them. Hits become prompt
#: tokens, so the cap is the token budget rather than a retrieval opinion.
CANDIDATES_PER_CORPUS = 3
MAX_CANDIDATES = 6

#: How many concerns one topic may produce.
#:
#: A topic names one subject, so a second concern about it is the
#: duplication node 6 exists to remove — and not making it is cheaper than
#: merging it. Measured: fanning out over eight topics without this cap
#: produced sixteen concerns and **88 interventions** against a burden
#: limit of 8, which is not a care plan, it is a reading of the chart.
MAX_CONCERNS_PER_TOPIC = 1

#: And how far each later node may fan out, for the same reason.
#:
#: Without these, eleven topics became eleven concerns, and eleven concerns
#: became **60 interventions** — every one individually reasonable, the
#: whole unusable. Reconciliation merges what is duplicated; it cannot merge
#: what is merely too much, and §7 is explicit that it must not truncate,
#: because choosing which care to drop is the decision this system is least
#: qualified to make. So the fan-out is bounded where the items are created
#: rather than after.
MAX_GOALS_PER_CONCERN = 2
MAX_INTERVENTIONS_PER_GOAL = 2

#: Which corpora each node retrieves from (design §6.3). The nodes ask
#: different questions and should not be handed the same shelf: a goal
#: template is not a medication-risk statement, and a plan built only from
#: the latter can only ever be about drugs.
#:
#: Corpora named here that are not ingested simply return nothing, so this
#: tuple can name a corpus before it exists without breaking a run.
CONCERN_CORPORA = ("med_safety", "condition_guidelines", "gravity_sdoh")
GOAL_CORPORA = ("condition_guidelines", "mcc_ecare_plan")
INTERVENTION_CORPORA = ("condition_guidelines", "med_safety", "gravity_sdoh")


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

    def __post_init__(self) -> None:
        # A checkpoint round-trip returns sequences as lists — msgpack has no
        # tuple, so a frozen dataclass declared with `tuple[str, ...]` comes
        # back holding a list. The type survives and the annotation lies,
        # equality stops working, and the damage only appears after a resume.
        # Coercing on construction makes the round trip faithful.
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs or ()))


@dataclass(frozen=True)
class GoalDraft:
    """A proposed goal, bound to the concern it answers.

    ``concern_index`` is assigned by the loop that generated it, never
    chosen by the model — a goal cannot outlive its reason.
    """

    statement: str
    concern_index: int
    target_value: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A checkpoint round-trip returns sequences as lists — msgpack has no
        # tuple, so a frozen dataclass declared with `tuple[str, ...]` comes
        # back holding a list. The type survives and the annotation lies,
        # equality stops working, and the damage only appears after a resume.
        # Coercing on construction makes the round trip faithful.
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs or ()))


@dataclass(frozen=True)
class InterventionDraft:
    """A proposed action, bound to the goal it serves, with an owner.

    ``goal_index`` is assigned by the loop, on the same principle as
    :class:`GoalDraft`.
    """

    statement: str
    goal_index: int
    intervention_type: str = "monitoring"
    owner_role: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # A checkpoint round-trip returns sequences as lists — msgpack has no
        # tuple, so a frozen dataclass declared with `tuple[str, ...]` comes
        # back holding a list. The type survives and the annotation lies,
        # equality stops working, and the damage only appears after a resume.
        # Coercing on construction makes the round trip faithful.
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs or ()))


@dataclass
class PlanDraft:
    """The whole proposal, before anything is persisted."""

    concerns: list[ConcernDraft] = field(default_factory=list)
    goals: list[GoalDraft] = field(default_factory=list)
    interventions: list[InterventionDraft] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    #: Chart problems triage set aside. Recorded rather than discarded: a
    #: plan that quietly addressed six of fifteen would be indistinguishable
    #: from one that missed nine.
    deferred: list[str] = field(default_factory=list)


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


def goal_schema() -> dict:
    """The shape a goal must answer in, under the active prompt set.

    ``target_value`` is always offered; whether it is *required* comes from
    the set. Required does not mean non-empty — an empty string is the
    honest answer when nothing retrieved supports a number, and the
    instruction says so. The requirement forces a decision, not a figure.
    """
    required = ["concern_index"]
    if prompt_set().requires_goal_target:
        required.append("target_value")
    return _selection_schema(
        {"concern_index": {"type": "integer"}, "target_value": {"type": "string"}},
        required,
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
            prompt_set()
            .text("selection_envelope")
            .format(instruction=task.instruction, situation=task.situation, menu=menu)
        )
        response = client.beta.messages.create(
            model=resolved,
            max_tokens=2000,
            messages=[{"role": "user", "content": cached_text(prompt)}],
            output_config={"format": {"type": "json_schema", "schema": dict(task.schema)}},
        )
        # The stage is read from the schema rather than passed in: only a
        # goal carries concern_index and only an intervention goal_index, so
        # the shape of the answer names the node that asked for it. Passing
        # it would have meant changing the Selector signature, which is the
        # seam every fake selector depends on.
        usage.record(response, _stage_of(task.schema))
        blocks = [block for block in response.content if block.type == "text"]
        return json.loads(blocks[0].text)

    return select


def _stage_of(schema: Mapping[str, Any]) -> str:
    """Which node a selection task came from, from the shape it demands."""
    try:
        properties = schema["properties"]["selections"]["items"]["properties"]
    except (KeyError, TypeError):
        return "selection"
    if "goal_index" in properties:
        return "interventions"
    if "concern_index" in properties:
        return "goals"
    return "concerns"


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


def _candidates(store, query: str, corpora: Sequence[str]) -> tuple[Candidate, ...]:
    """Retrieve from each corpus in turn, by quota rather than by score.

    Pooling every hit and sorting would be the obvious thing and would be
    wrong: :class:`PgStore` scores with ``ts_rank`` when full-text matches
    and falls back to ``word_similarity`` when it does not, and those two
    are not calibrated against each other — trigram scores land around
    0.2-0.5 where ts_rank lands an order of magnitude lower. A corpus
    answered by the fallback would systematically outrank one answered by
    full text, which is a ranking of retrieval modes rather than of
    relevance.

    A quota per corpus avoids comparing the incomparable, and guarantees
    the node sees each shelf it was pointed at. Duplicates are dropped by
    citation, since the same chunk can be reached from two corpora only if
    something has gone wrong upstream.
    """
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for corpus in corpora:
        for hit in store.search(query, corpus, k=CANDIDATES_PER_CORPUS):
            ref = hit.citation()
            if ref in seen:
                continue
            seen.add(ref)
            candidates.append(Candidate(ref=ref, text=hit.chunk))
            if len(candidates) >= MAX_CANDIDATES:
                return tuple(candidates)
    return tuple(candidates)


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


#: How a grader's objection is presented to the node that has to answer it.
#:
#: Framed as a critique of a previous attempt rather than as a new
#: instruction, because the node's job has not changed — only its evidence
#: about how it did. Stated as the last thing before the candidates so it is
#: not buried behind the situation.
def _instruct(instruction: str, feedback: str) -> str:
    """The node's instruction, with any critique of the last attempt."""
    if not feedback.strip():
        return instruction
    preamble = prompt_set().text("feedback_preamble").format(feedback=feedback.strip())
    return instruction + "\n\n" + preamble


# ── the nodes ────────────────────────────────────────────────────────────


def propose_concerns(
    store,
    context,
    flags,
    selector: Selector,
    topics: Sequence[Topic] | None = None,
    feedback: str = "",
) -> tuple[list[ConcernDraft], list[str]]:
    """Node 3. One retrieval and one selection **per topic**.

    This used to retrieve once for the whole patient — the entire chart as a
    single query, six candidates back — and it could not span a long problem
    list. Measured on a ten-problem chart, the blended query returned six
    chunks covering five conditions, all scoring between 0.0065 and 0.0112:
    weak, flat, and missing the one condition the chart recorded as
    uncontrolled. Asked one topic at a time, the same corpus returned the
    right document at rank 1 fourteen times out of fourteen.

    Per topic rather than one selection over a pooled menu, because that is
    what nodes 4 and 5 already do — a goal is chosen per concern, an
    intervention per goal — and node 3 was the odd one out. It also
    guarantees each triaged topic is considered on its own evidence instead
    of competing for space in one prompt.

    ``topics`` defaults to triaging the context, so callers that do not care
    about deferral keep working.
    """
    from hdh.modules.careplan.triage import triage

    if topics is None:
        topics, _deferred = triage(context, flags)
    described = situation(context, flags)
    drafts: list[ConcernDraft] = []
    dropped: list[str] = []

    for topic in topics:
        candidates = _candidates(store, topic.query, CONCERN_CORPORA)
        if not candidates:
            dropped.append(f"{topic.label[:50]} — nothing retrieved, no concern can be supported")
            continue
        answer = selector(
            SelectionTask(
                instruction=_instruct(prompt_set().text("concerns"), feedback),
                situation=f"{described}\n\nTOPIC: {topic.label}\nWHY: {topic.basis}",
                candidates=candidates,
                schema=CONCERN_SCHEMA,
            )
        )
        _kept(
            answer.get("selections", [])[:MAX_CONCERNS_PER_TOPIC],
            {c.ref for c in candidates},
            drafts,
            dropped,
            lambda item, cites, t=topic: ConcernDraft(
                statement=item["statement"],
                concern_type=item.get("concern_type", "risk"),
                evidence_refs=cites,
                basis=t.basis,
            ),
        )

    if not drafts and not dropped:
        dropped.append("no topics to plan from — the chart records no problems and no flags fired")
    return drafts, dropped


def propose_goals(
    store, context, concerns, selector: Selector, feedback: str = ""
) -> tuple[list[GoalDraft], list[str]]:
    """Node 4. One pass per concern, so a goal cannot outlive its reason."""
    drafts: list[GoalDraft] = []
    dropped: list[str] = []
    for index, concern in enumerate(concerns):
        candidates = _candidates(store, concern.statement, GOAL_CORPORA)
        if not candidates:
            dropped.append(f"goal for {concern.statement[:40]!r} — nothing retrieved")
            continue
        answer = selector(
            SelectionTask(
                instruction=_instruct(prompt_set().text("goals"), feedback),
                situation=f"{situation(context, ())}\n\nCONCERN: {concern.statement}",
                candidates=candidates,
                schema=goal_schema(),
            )
        )
        _kept(
            answer.get("selections", [])[:MAX_GOALS_PER_CONCERN],
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


def propose_interventions(store, context, goals, selector: Selector, feedback: str = ""):
    """Node 5. One pass per goal; each intervention names an owner."""
    drafts: list[InterventionDraft] = []
    dropped: list[str] = []
    for index, goal in enumerate(goals):
        candidates = _candidates(store, goal.statement, INTERVENTION_CORPORA)
        if not candidates:
            dropped.append(f"intervention for {goal.statement[:40]!r} — nothing retrieved")
            continue
        answer = selector(
            SelectionTask(
                instruction=_instruct(prompt_set().text("interventions"), feedback),
                situation=f"{situation(context, ())}\n\nGOAL: {goal.statement}",
                candidates=candidates,
                schema=INTERVENTION_SCHEMA,
            )
        )
        _kept(
            answer.get("selections", [])[:MAX_INTERVENTIONS_PER_GOAL],
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
