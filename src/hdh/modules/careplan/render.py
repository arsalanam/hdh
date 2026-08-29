"""S4c of `interactive-care-planning.md`: the plan as a clinician reads it.

The terminal rendering in :mod:`hdh.modules.careplan.review` is for
iterating. This one is for judging — the plan laid out the way a clinician
reads a care plan, so that a question the scores cannot answer becomes
answerable by looking.

**What the layout is arguing.** Traceability governs 21 of 24 verdicts on
the cohort. So the single most important thing this page can show is *an
element that cites nothing*, and it is styled to be the loudest thing on
it — a warning chip, not a quiet gap. Everything else is arranged to keep
the clinical shape intact: a concern, the goals under it, the interventions
under each, and what never made it in.

**What was withheld is shown as prominently as what was kept**, and split
by cause. A problem triage deferred was never offered to the model; a
candidate the model dropped was offered and declined. Merging them would
hide that a problem was never considered at all.

The page is a rendering of **synthetic** data and says so permanently, at
the top, in a form that survives being screenshotted. Nothing hdh produces
should be mistakable for a real patient record.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Fonts come from the system stack rather than a CDN. A published page
#: blocks external requests, so a linked webfont does not degrade — it
#: silently falls back, and the design that was reviewed is not the design
#: that ships.
STYLE = """
:root {
  --ink: #12161c; --ink-soft: #4a5361; --ink-faint: #6b7480;
  --ground: #fbfaf8; --card: #ffffff; --rule: #e3e2de;
  --accent: #1f4b73; --accent-soft: #eaf1f8;
  --warn: #8a4b12; --warn-soft: #fdf1e3; --warn-rule: #e0b184;
  --good: #2f5d3f; --good-soft: #ebf3ed;
  --shadow: 0 1px 2px rgba(18,22,28,.06), 0 4px 12px rgba(18,22,28,.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #e8e6e1; --ink-soft: #a9b0b9; --ink-faint: #838b95;
    --ground: #14171b; --card: #1b1f24; --rule: #2b3038;
    --accent: #8fbde6; --accent-soft: #1c2833;
    --warn: #e6b075; --warn-soft: #2b2015; --warn-rule: #6b4a22;
    --good: #8fc7a1; --good-soft: #17241b;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.2);
  }
}
:root[data-theme="dark"] {
  --ink: #e8e6e1; --ink-soft: #a9b0b9; --ink-faint: #838b95;
  --ground: #14171b; --card: #1b1f24; --rule: #2b3038;
  --accent: #8fbde6; --accent-soft: #1c2833;
  --warn: #e6b075; --warn-soft: #2b2015; --warn-rule: #6b4a22;
  --good: #8fc7a1; --good-soft: #17241b;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.2);
}

body {
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 16px; line-height: 1.55;
}
.sheet { max-width: 54rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }

.synthetic {
  display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap;
  background: var(--warn-soft); border: 1px solid var(--warn-rule);
  border-radius: .4rem; padding: .6rem .85rem; margin-bottom: 2rem;
  color: var(--warn); font-size: .82rem;
}
.synthetic b { letter-spacing: .08em; text-transform: uppercase; }

h1 {
  font-family: ui-serif, Georgia, "Times New Roman", serif;
  font-size: 2rem; line-height: 1.15; margin: 0 0 .35rem;
  text-wrap: balance; font-weight: 600;
}
.who { color: var(--ink-soft); font-size: .95rem; margin: 0 0 2rem; }
.who span + span::before { content: " · "; color: var(--ink-faint); }

h2 {
  font-family: ui-serif, Georgia, serif; font-size: 1.05rem; font-weight: 600;
  letter-spacing: .02em; margin: 2.75rem 0 .9rem; padding-bottom: .4rem;
  border-bottom: 1px solid var(--rule);
}

.concern {
  background: var(--card); border: 1px solid var(--rule); border-radius: .5rem;
  box-shadow: var(--shadow); padding: 1.1rem 1.25rem; margin-bottom: 1.1rem;
}
.concern > header { display: flex; gap: .7rem; align-items: baseline; flex-wrap: wrap; }
.concern h3 {
  font-size: 1.06rem; font-weight: 600; margin: 0; flex: 1 1 18rem; text-wrap: balance;
}
.kind {
  font-size: .68rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--accent); background: var(--accent-soft);
  border-radius: 2rem; padding: .18rem .6rem; white-space: nowrap;
}

.goal { margin: 1rem 0 0 0; padding: .85rem 0 0; border-top: 1px dashed var(--rule); }
.goal > p { margin: 0; font-weight: 550; }
.target { color: var(--ink-soft); font-weight: 400; }

ul.actions { list-style: none; margin: .6rem 0 0; padding: 0; }
ul.actions li {
  position: relative; padding: .3rem 0 .3rem 1.15rem; color: var(--ink-soft);
}
ul.actions li::before {
  content: ""; position: absolute; left: .25rem; top: .95em;
  width: .35rem; height: .35rem; border-radius: 50%; background: var(--accent);
}
.role {
  font-size: .72rem; color: var(--ink-faint); text-transform: uppercase;
  letter-spacing: .06em; margin-left: .4rem;
}

.cites { margin-top: .4rem; display: flex; gap: .35rem; flex-wrap: wrap; }
.ref {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .72rem;
  color: var(--good); background: var(--good-soft);
  border-radius: .25rem; padding: .12rem .45rem; word-break: break-all;
}
/* The point of the whole page. Traceability governs 21 of 24 verdicts, so
   an element with nothing behind it is the loudest thing here. */
.ref.none {
  color: var(--warn); background: var(--warn-soft);
  border: 1px solid var(--warn-rule); font-weight: 600;
}

.withheld { background: var(--card); border: 1px solid var(--rule); border-radius: .5rem; }
.withheld > div { padding: .85rem 1.25rem; border-bottom: 1px solid var(--rule); }
.withheld > div:last-child { border-bottom: 0; }
.withheld h4 {
  margin: 0 0 .3rem; font-size: .72rem; text-transform: uppercase;
  letter-spacing: .09em; color: var(--ink-faint); font-weight: 600;
}
.withheld p { margin: .15rem 0; color: var(--ink-soft); font-size: .92rem; }

table.grade { width: 100%; border-collapse: collapse; font-size: .92rem; }
table.grade th, table.grade td {
  text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--rule);
}
table.grade th { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--ink-faint); font-weight: 600; }
table.grade td.n { text-align: right; font-variant-numeric: tabular-nums; }
tr.governs td { background: var(--warn-soft); color: var(--warn); font-weight: 600; }
.scroll { overflow-x: auto; }

.empty { color: var(--ink-faint); font-style: italic; }
footer { margin-top: 3.5rem; padding-top: 1rem; border-top: 1px solid var(--rule);
  color: var(--ink-faint); font-size: .8rem; }
"""

NO_CITATION = "cites nothing"


@dataclass(frozen=True)
class PlanView:
    """Everything the page shows, already flattened.

    A view rather than the raw state, so the template does no thinking: what
    belongs under what is decided once, here, and the HTML only lays it out.
    """

    mrn: str
    age: int | None = None
    sex: str = ""
    rubric: str = ""
    concerns: Sequence[Any] = ()
    goals: Sequence[Any] = ()
    interventions: Sequence[Any] = ()
    deferred: Sequence[str] = ()
    dropped: Mapping[str, Sequence[str]] | None = None
    scores: Mapping[str, int] | None = None
    verdict: str = ""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _refs(item: Any) -> str:
    """Citation chips, or a loud one saying there are none."""
    refs = tuple(getattr(item, "evidence_refs", ()) or ())
    if not refs:
        return f'<div class="cites"><span class="ref none">{NO_CITATION}</span></div>'
    chips = "".join(f'<span class="ref">{_esc(ref)}</span>' for ref in refs)
    return f'<div class="cites">{chips}</div>'


def _interventions_for(view: PlanView, goal_index: int) -> list[Any]:
    return [i for i in view.interventions if getattr(i, "goal_index", -1) == goal_index]


def _goals_for(view: PlanView, concern_index: int) -> list[tuple[int, Any]]:
    return [
        (index, goal)
        for index, goal in enumerate(view.goals)
        if getattr(goal, "concern_index", -1) == concern_index
    ]


def _goal_block(view: PlanView, index: int, goal: Any) -> str:
    target = getattr(goal, "target_value", "") or ""
    target_html = f' <span class="target">— target {_esc(target)}</span>' if target else ""
    actions = []
    for intervention in _interventions_for(view, index):
        role = getattr(intervention, "owner_role", "") or ""
        role_html = f'<span class="role">{_esc(role)}</span>' if role else ""
        actions.append(
            f"<li>{_esc(getattr(intervention, 'statement', ''))}{role_html}{_refs(intervention)}</li>"
        )
    listed = (
        f'<ul class="actions">{"".join(actions)}</ul>'
        if actions
        else '<p class="empty">No interventions were proposed for this goal.</p>'
    )
    return (
        f'<div class="goal"><p>{_esc(getattr(goal, "statement", ""))}{target_html}</p>'
        f"{_refs(goal)}{listed}</div>"
    )


def _concern_block(view: PlanView, index: int, concern: Any) -> str:
    kind = getattr(concern, "concern_type", "") or ""
    kind_html = f'<span class="kind">{_esc(kind)}</span>' if kind else ""
    goals = _goals_for(view, index)
    inner = (
        "".join(_goal_block(view, gi, goal) for gi, goal in goals)
        if goals
        else '<p class="empty">No goals were set against this concern.</p>'
    )
    return (
        f'<article class="concern"><header>'
        f"<h3>{_esc(getattr(concern, 'statement', ''))}</h3>{kind_html}</header>"
        f"{_refs(concern)}{inner}</article>"
    )


def _withheld_block(view: PlanView) -> str:
    """What never reached the plan, split by cause.

    Deferred and dropped are different absences: triage removed a problem
    before the model saw it, the model declined a candidate it was offered.
    A reviewer told only "some things are missing" cannot tell whether a
    problem was rejected or never considered.
    """
    sections = []
    if view.deferred:
        rows = "".join(f"<p>{_esc(item)}</p>" for item in view.deferred)
        sections.append("<div><h4>Deferred by triage — never offered to the model</h4>" + rows + "</div>")
    for channel, entries in (view.dropped or {}).items():
        if not entries:
            continue
        stage = channel.replace("dropped_", "")
        rows = "".join(f"<p>{_esc(item)}</p>" for item in entries)
        sections.append(f"<div><h4>Declined at {_esc(stage)} — offered and refused</h4>{rows}</div>")
    if not sections:
        return '<p class="empty">Nothing was deferred or declined.</p>'
    return f'<div class="withheld">{"".join(sections)}</div>'


def _grade_block(view: PlanView) -> str:
    """The scores, with the dimension that decided the verdict marked.

    The lowest dimension governs and the mean is only reported. A table that
    showed an average without saying which row produced the verdict would
    reproduce the misreading the rubric exists to prevent.
    """
    if not view.scores:
        return ""
    lowest = min(view.scores.values())
    rows = []
    for dimension, score in sorted(view.scores.items(), key=lambda kv: kv[1]):
        governs = score == lowest
        mark = " (governs the verdict)" if governs else ""
        rows.append(
            f'<tr class="{"governs" if governs else ""}">'
            f"<td>{_esc(dimension)}{mark}</td>"
            f'<td class="n">{score}</td></tr>'
        )
    mean = round(sum(view.scores.values()) / len(view.scores), 2)
    verdict = f" — {_esc(view.verdict)}" if view.verdict else ""
    return (
        f"<h2>Grade{verdict}</h2>"
        f'<div class="scroll"><table class="grade">'
        f"<thead><tr><th>dimension</th><th class='n'>score</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        f"<footer>Mean {mean}, reported only. The lowest dimension decides.</footer>"
    )


def plan_html(view: PlanView, *, generated_note: str = "") -> str:
    """The whole page: content only, for the artifact wrapper to host."""
    who = [f"<span>{_esc(view.mrn)}</span>"]
    if view.age is not None:
        who.append(f"<span>{_esc(view.age)} years</span>")
    if view.sex:
        who.append(f"<span>{_esc(view.sex.lower())}</span>")
    if view.rubric:
        who.append(f"<span>rubric: {_esc(view.rubric)}</span>")

    concerns = (
        "".join(_concern_block(view, i, c) for i, c in enumerate(view.concerns))
        if view.concerns
        else '<p class="empty">No concerns were proposed.</p>'
    )
    note = f"<footer>{_esc(generated_note)}</footer>" if generated_note else ""

    return f"""<title>Care Plan {_esc(view.mrn)}</title>
<style>{STYLE}</style>
<main class="sheet">
  <div class="synthetic">
    <b>Synthetic record</b>
    <span>Generated by hdh for testing. Not a real patient and not clinical advice.</span>
  </div>

  <h1>Care plan</h1>
  <p class="who">{"".join(who)}</p>

  <h2>Concerns, goals and interventions</h2>
  {concerns}

  <h2>What did not make it in</h2>
  {_withheld_block(view)}

  {_grade_block(view)}
  {note}
</main>
"""


@dataclass(frozen=True)
class Framing:
    """What the page shows that plan state does not hold.

    Who the patient is, which rubric applies, and how it graded. They travel
    together because they are all answers to "what am I looking at" — plan
    state answers "what does it say".
    """

    age: int | None = None
    sex: str = ""
    rubric: str = ""
    scores: Mapping[str, int] | None = None
    verdict: str = ""


def view_from_state(mrn: str, values: Mapping[str, Any], framing: Framing | None = None) -> PlanView:
    """A view over plan state, whether from a live pause or a checkpoint."""
    framing = framing or Framing()
    dropped = {
        key: list(values.get(key) or ()) for key in values if key.startswith("dropped_") and values.get(key)
    }
    return PlanView(
        mrn=mrn,
        age=framing.age,
        sex=framing.sex,
        rubric=framing.rubric,
        concerns=list(values.get("concerns") or ()),
        goals=list(values.get("goals") or ()),
        interventions=list(values.get("interventions") or values.get("raw_interventions") or ()),
        deferred=list(values.get("deferred") or ()),
        dropped=dropped,
        scores=framing.scores,
        verdict=framing.verdict,
    )


def write_plan_html(path, view: PlanView, *, generated_note: str = "") -> str:
    """Write the page and return where it went."""
    import pathlib

    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan_html(view, generated_note=generated_note), encoding="utf-8")
    return str(target)


def uncited(view: PlanView) -> list[str]:
    """Every element that cites nothing, as text.

    Exposed because it is the question the page exists to answer, and a
    caller should be able to ask it without parsing HTML.
    """
    missing: list[str] = []
    groups: Iterable[tuple[str, Sequence[Any]]] = (
        ("concern", view.concerns),
        ("goal", view.goals),
        ("intervention", view.interventions),
    )
    for label, items in groups:
        for item in items:
            if not tuple(getattr(item, "evidence_refs", ()) or ()):
                missing.append(f"{label}: {getattr(item, 'statement', '')}")
    return missing
