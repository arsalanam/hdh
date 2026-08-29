"""S4b of `interactive-care-planning.md`: care planning, conversationally.

The agent starts a plan for one patient, and the graph stops after every
node that made a judgement. The clinician reads what was proposed, keeps
what is right, and sends back what is not — in words, in the same session.

These tools add **no rules of their own**. Each is a call into
:mod:`hdh.modules.careplan.review` plus a rendering, which is the same
shape the chart tools take against ``chartedit``: a change made by talking
and a change made at the terminal go through one path, so they cannot
diverge.

Two guarantees hold here, and both are pinned by tests:

**Nothing in this pack writes the chart.** #121 established that a fact
enters the chart only as the outcome of a fulfilment. A care plan is a plan
*about* a chart, not a fact in one; these tools create care-plan rows and
nothing else.

**Nothing here edits a rubric.** ``show_care_plan_rubric`` is read-only and
there is deliberately no counterpart that writes. A system able to rewrite
the standard it is graded against has no grade — so rubric changes stay
human edits to ``rubrics/*.json``, reviewed like any other change and then
measured on the cohort, where the noise floor is 0.207.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hdh.modules.careplan import review
from hdh.modules.careplan.graph import PlanServices, compile_pipeline, thread_config
from hdh.modules.careplan.review import Pause


#: One open review per patient. A stable thread id means a plan survives the
#: agent losing its place mid-conversation — the checkpointer already keeps
#: the state, and this is what lets the tools find it again.
def _thread_for(mrn: str) -> str:
    return f"careplan-review-{mrn.strip().upper()}"


@dataclass
class _Desk:
    """The reviews open in this session, and what runs them.

    Built once per tool pack rather than per call: the graph and the
    checkpointer each open resources, and a factory that made them per tool
    call would open one per sentence the clinician says.
    """

    session: Any
    graph: Any = None
    services: PlanServices | None = None
    open_reviews: set[str] = field(default_factory=set)

    def ready(self) -> tuple[Any, PlanServices]:
        """The compiled graph and the services to run it, built on first use.

        Lazily, because building either one opens a resource — a retrieval
        store and a checkpointer — and a session that never mentions care
        planning should not pay for them.
        """
        if self.graph is None or self.services is None:
            from hdh.modules.careplan.checkpoints import build_checkpointer

            services = PlanServices().resolved(self.session)
            checkpointer = build_checkpointer(self.session)
            self.services = PlanServices(
                store=services.store,
                selector=services.selector,
                grader=services.grader,
                checkpointer=checkpointer,
            )
            self.graph = compile_pipeline(checkpointer=checkpointer, review=True)
        return self.graph, self.services


def _render(mrn: str, pause: Pause) -> str:
    """A pause as the clinician reads it, with what to do next spelled out.

    The next actions are named in the body rather than left to the model to
    infer. A reviewer who is not told that rejecting is available will
    approve things they would have sent back.
    """
    lines = [f"care plan for {mrn}", *review.summarise(pause)]
    if pause.finished:
        lines.append("")
        lines.append("Nothing left to review. Grade it, or write it to the chart.")
    else:
        lines.append("")
        lines.append(
            f"Approve to run {pause.next_node}, amend to keep only some of these, "
            f"or reject with a reason to have {pause.node} done again."
        )
    return "\n".join(lines)


def _numbers(raw: str, limit: int) -> list[int]:
    """Parse "1,3,4" into indices, refusing what does not exist.

    Refusing beats clamping: a reviewer who typed 7 when there are 5 items
    meant something, and silently keeping the first five would look like it
    worked.
    """
    picked = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"{part!r} is not an item number")
        index = int(part)
        if not 1 <= index <= limit:
            raise ValueError(f"there is no item {index} — this stage has {limit}")
        picked.append(index - 1)
    return picked


def _staged_key(pause: Pause) -> str:
    """The state channel holding the items under review at this pause."""
    for key in pause.proposed:
        if not key.startswith("dropped_"):
            return key
    raise ValueError(f"{pause.node} proposed nothing that can be amended")


def _rubric_text(name: str) -> str:
    """Every rubric on disk, or one, as a reader can follow it.

    Leads with what governs the verdict. The lowest dimension decides and
    the mean is only reported — the fact a reader most often assumes the
    other way round, and the one that makes a 4.8 average with a 2 in it
    make sense.
    """
    from hdh.modules.careplan.rubric import load_rubrics

    rubrics = load_rubrics()
    wanted = [r for r in rubrics if not name or r.rubric_id == name]
    if not wanted:
        have = ", ".join(sorted(r.rubric_id for r in rubrics))
        return f"no rubric {name!r} — on disk: {have}"

    lines: list[str] = []
    for rubric in wanted:
        lines.append(f"{rubric.rubric_id} — {rubric.title}")
        lines.append(
            f"  scored {rubric.scale_min}-{rubric.scale_max}; "
            f"revise below {rubric.revise_below}, fail below {rubric.fail_below}. "
            f"The LOWEST dimension governs the verdict."
        )
        for dimension in rubric.dimensions:
            lines.append(f"  {dimension.id} — {dimension.title}")
            lines.append(f"    asks: {dimension.question}")
            for score in sorted(dimension.anchors):
                lines.append(f"      {score}: {dimension.anchors[score]}")
    return "\n".join(lines)


def _seed_for(session, mrn: str) -> dict | str:
    """The state a plan starts from, or the reason there is none.

    Returns the refusal as text rather than raising, because the caller is
    a tool: the agent has to be able to tell the user why nothing happened,
    and an exception would end the turn instead.
    """
    from hdh.core.models import Patient
    from hdh.modules.careplan.context import build_context

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        return f"no patient {mrn}"
    context = build_context(session, patient)
    if not context.problems:
        return f"{mrn} has no problems on the chart — there is nothing to plan for"
    return {"context": context}


def _amend(desk: _Desk, mrn: str, keep: str) -> str:
    """Keep only the numbered items in the stage under review, then go on.

    Every refusal here is returned rather than raised: the caller is a tool,
    and the agent has to be able to tell the user which number did not exist.
    """
    graph, services = desk.ready()
    config = thread_config(_thread_for(mrn))
    pause = review.where(graph, config)
    if not pause.started:
        return f"no care plan in progress for {mrn}"
    if pause.finished:
        return "the plan is complete — there is no stage under review"

    try:
        channel = _staged_key(pause)
        items = list(pause.proposed.get(channel) or [])
        kept = [items[i] for i in _numbers(keep, len(items))]
    except ValueError as err:
        return str(err)

    try:
        after = review.edit(graph, config, services, **{channel: kept})
    except review.ReviewError as err:
        return str(err)
    headline = f"kept {len(kept)} of {len(items)} {channel}"
    return headline + "\n\n" + _render(mrn, after)


def _write_page(desk: _Desk, mrn: str, path: str) -> str:
    """Render the plan under review to a file, and say what is missing.

    The count of uncited elements comes back in the tool result rather than
    living only on the page: it is the number the plan is graded on, and the
    agent should be able to say it out loud without the user opening
    anything.
    """
    from hdh.modules.careplan.render import Framing, uncited, view_from_state, write_plan_html

    graph, _services = desk.ready()
    pause = review.where(graph, thread_config(_thread_for(mrn)))
    if not pause.started:
        return f"no care plan in progress for {mrn} — start one first"

    context = pause.values.get("context")
    view = view_from_state(
        mrn,
        pause.values,
        Framing(age=getattr(context, "age", None), sex=getattr(context, "sex", "") or ""),
    )
    written = write_plan_html(
        path or f"care-plan-{mrn.lower()}.html",
        view,
        generated_note=(
            f"Paused after {pause.node}." if not pause.finished else "Complete — every stage reviewed."
        ),
    )
    missing = uncited(view)
    tail = (
        f" {len(missing)} element(s) cite nothing and are flagged on the page."
        if missing
        else " Every element cites something."
    )
    return f"wrote {written}.{tail}"


def build_careplan_tools(session, *, services: PlanServices | None = None, graph=None) -> list:
    """The agent's care-planning toolset.

    Returns ``[]`` when the agent extra is not installed, matching every
    other optional pack: the agent runs fine without it.

    ``services`` and ``graph`` are injection seams, for the same reason
    :class:`PlanServices` exists at all — the pack's own behaviour has to be
    testable without a PostgreSQL retrieval store, a checkpointer and an API
    key between the test and the thing it is checking.
    """
    try:
        from anthropic import beta_tool
    except ImportError:
        return []

    from hdh.core.models import tool_guard

    guard = tool_guard(session)
    desk = _Desk(session=session, graph=graph, services=services)

    @beta_tool
    @guard
    def start_care_plan(mrn: str, restart: bool = False) -> str:
        """Begin a care plan for ONE patient and stop at the first thing needing review. The plan pauses after each stage the model judged — concerns, then goals, then interventions — so the clinician can steer it before the next stage is built. Show the user everything this returns, including what was deferred or dropped.

        Args:
            mrn: The patient's medical record number.
            restart: Discard an in-progress plan for this patient and begin again.
        """
        graph, services = desk.ready()
        config = thread_config(_thread_for(mrn))

        if not restart:
            existing = review.where(graph, config)
            if existing.started and not existing.finished:
                return (
                    f"a care plan for {mrn} is already open, paused after "
                    f"{existing.node}. Continue it, or start again with "
                    f"restart=true — which discards what is there."
                )

        seed = _seed_for(session, mrn)
        if isinstance(seed, str):
            return seed

        pause = review.begin(graph, config, seed, services)
        desk.open_reviews.add(mrn)
        return _render(mrn, pause)

    @beta_tool
    @guard
    def show_care_plan(mrn: str) -> str:
        """Show where a care plan has got to and what is waiting for review, without changing anything. Use this when the user asks what the plan looks like so far.

        Args:
            mrn: The patient's medical record number.
        """
        graph, _services = desk.ready()
        pause = review.where(graph, thread_config(_thread_for(mrn)))
        if not pause.started:
            return f"no care plan in progress for {mrn} — start one first"
        return _render(mrn, pause)

    @beta_tool
    @guard
    def approve_care_plan_stage(mrn: str) -> str:
        """Accept the stage currently under review as it stands and build the next one. Only call this once the user has actually said the stage is right.

        Args:
            mrn: The patient's medical record number.
        """
        graph, services = desk.ready()
        config = thread_config(_thread_for(mrn))
        try:
            pause = review.approve(graph, config, services)
        except review.ReviewError as err:
            return str(err)
        return _render(mrn, pause)

    @beta_tool
    @guard
    def amend_care_plan_stage(mrn: str, keep: str) -> str:
        """Keep only some of the items in the stage under review, then build the next stage. Use this when the stage is broadly right but an item does not belong — dropping one is an amendment, not a rejection. Numbers are the ones shown in the stage.

        Args:
            mrn: The patient's medical record number.
            keep: Item numbers to keep, comma separated, e.g. "1,2,4". Empty keeps none.
        """
        return _amend(desk, mrn, keep)

    @beta_tool
    @guard
    def reject_care_plan_stage(mrn: str, feedback: str) -> str:
        """Send the stage under review back to be done again, with a reason the model must address. Use this when the stage itself is wrong rather than one item in it. The stages after it have not been built yet, so nothing downstream is wasted.

        Args:
            mrn: The patient's medical record number.
            feedback: What is wrong and what would be right. Required — without it the stage comes back the same.
        """
        graph, services = desk.ready()
        config = thread_config(_thread_for(mrn))
        try:
            pause = review.reject(graph, config, services, feedback=feedback)
        except review.ReviewError as err:
            return str(err)
        return _render(mrn, pause)

    @beta_tool
    @guard
    def show_care_plan_rubric(name: str = "") -> str:
        """Show a care-plan rubric: its dimensions, what each one asks, and what each score from 1 to 5 means. Read-only — rubrics are changed by editing their files, never from a conversation.

        Args:
            name: Rubric name, or empty for every rubric on disk.
        """
        return _rubric_text(name)

    @beta_tool
    @guard
    def write_care_plan_page(mrn: str, path: str = "") -> str:
        """Write the care plan so far as a self-contained HTML page a clinician can read, and return where it was written. Use this when the user wants to review the plan properly rather than in the chat, or wants to show it to someone. Elements that cite nothing are marked prominently, because that is what the plan is graded on.

        Args:
            mrn: The patient's medical record number.
            path: Where to write it. Defaults to care-plan-<mrn>.html in the working directory.
        """
        return _write_page(desk, mrn, path)

    return [
        start_care_plan,
        show_care_plan,
        approve_care_plan_stage,
        amend_care_plan_stage,
        reject_care_plan_stage,
        show_care_plan_rubric,
        write_care_plan_page,
    ]
