"""Chart-maintenance tools for the agent (design chart-maintenance.md §3.5).

The agent proposes; :func:`hdh.core.chartedit.apply_edits` validates,
applies and audits. These tools add no rules of their own — they are the
conversational surface of the same core the CLI drives, which is why a
change made by talking to the agent and a change made at the terminal
land in one trail with one shape.

The guardrails are contracts, not hopes:

1. **one row per call** — no predicates, no bulk operations; an agent
   cannot void "all conditions where…";
2. **a reason is required** for clinical rows, enforced by the core;
3. **preview before write** — ``dry_run`` is offered and the tool
   descriptions tell the model to use it for anything it did not itself
   just create;
4. **never delete** — void only; real deletion is an admin CLI path that
   is not exposed here at all;
5. **audit is not optional** — the core writes the event in the same
   transaction as the change, so there is no path that mutates silently.
"""

from __future__ import annotations

import json


def _outcome_payload(outcomes) -> str:
    return json.dumps(
        [
            {
                "applied": outcome.applied,
                "detail": outcome.detail,
                "audit_id": outcome.audit_id,
                "before": dict(outcome.before),
                "after": dict(outcome.after),
            }
            for outcome in outcomes
        ],
        indent=1,
    )


def build_chart_tools(session) -> list:
    """The agent's chart-maintenance toolset."""
    from anthropic import beta_tool

    from hdh.core.models import tool_guard

    guard = tool_guard(session)

    @beta_tool
    @guard
    def amend_chart_entry(entity: str, row_id: int, changes: str, reason: str, dry_run: bool = False) -> str:
        """Correct fields on ONE existing chart row (Condition, Prescription, Vital, LabResult, Allergy, Visit). Every change is recorded in the patient's audit trail with your reason. Call with dry_run=true first and show the user the preview unless you created the row yourself moments ago. Cannot create rows and cannot delete them.

        Args:
            entity: Condition | Prescription | Vital | LabResult | Allergy | Visit.
            row_id: The id of the row to change.
            changes: JSON object of field/value pairs, e.g. {"status": "resolved"}.
            reason: Why this change is being made — required for clinical rows.
            dry_run: Compute the outcome and write nothing.
        """
        from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

        try:
            parsed = json.loads(changes)
        except json.JSONDecodeError as err:
            return f"changes must be a JSON object of field/value pairs: {err}"
        if not isinstance(parsed, dict) or not parsed:
            return 'changes must be a non-empty JSON object, e.g. {"status": "resolved"}'
        edit = ChartEdit(entity, row_id, EditAction.AMEND, parsed, reason)
        return _outcome_payload(apply_edits(session, _agent_actor(session, reason), [edit], dry_run=dry_run))

    @beta_tool
    @guard
    def void_chart_entry(entity: str, row_id: int, reason: str, dry_run: bool = False) -> str:
        """Mark ONE chart row entered-in-error so it stops appearing in the chart. Voiding a Visit also voids the vitals, prescriptions, labs and conditions it owns. Nothing is deleted — the row and its audit trail remain. Call with dry_run=true first and report the preview to the user before applying.

        Args:
            entity: Condition | Prescription | Vital | LabResult | Allergy | Visit.
            row_id: The id of the row to void.
            reason: Why — required, e.g. "entered in error", "duplicate encounter".
            dry_run: Compute the outcome and write nothing.
        """
        from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

        edit = ChartEdit(entity, row_id, EditAction.VOID, {}, reason)
        return _outcome_payload(apply_edits(session, _agent_actor(session, reason), [edit], dry_run=dry_run))

    @beta_tool
    @guard
    def chart_history(mrn: str, limit: int = 25) -> str:
        """Who changed what on this patient's chart, and why — newest first. Includes entries created by note comprehension (source "pipeline"), so this answers "where did this diagnosis come from?". Read-only.

        Args:
            mrn: The patient's MRN.
            limit: Maximum events to return.
        """
        from hdh.core.chartedit import history
        from hdh.core.models import Patient

        patient = session.query(Patient).filter(Patient.mrn == mrn).first()
        if patient is None:
            return f"No patient with MRN {mrn}."
        events = history(session, patient.id, limit=limit)
        if not events:
            return f"No recorded changes for {mrn}."
        return json.dumps(
            [
                {
                    "occurred_at": event.occurred_at.isoformat(timespec="minutes"),
                    "action": getattr(event.action, "value", event.action),
                    "entity": event.entity,
                    "row_id": event.row_id,
                    "actor": event.actor_name,
                    "source": getattr(event.actor_source, "value", event.actor_source),
                    "reason": event.reason,
                    "before": event.before,
                    "after": event.after,
                }
                for event in events
            ],
            indent=1,
        )

    return [amend_chart_entry, void_chart_entry, chart_history]


def _agent_actor(session, reason: str):
    """Attribution for an agent-made change: the provider named in the
    reason when there is one, else the agent itself (design §7 Q3 — real
    user accounts arrive with authentication)."""
    import re

    from hdh.core.chartedit import Actor
    from hdh.core.models import EditSource, Provider

    haystack = reason.lower()
    for provider in session.query(Provider).all():
        # "Dr. Priya Sharma, MD" → match either the full name or just the
        # surname, since a provider writes "Dr. Sharma confirmed…"
        bare = provider.name.replace("Dr. ", "").split(",")[0].strip()
        candidates = {bare, bare.split()[-1]} if bare else set()
        if any(
            len(name) > 2 and re.search(rf"\b{re.escape(name.lower())}\b", haystack) for name in candidates
        ):
            return Actor(name=provider.name, source=EditSource.AGENT, provider_id=provider.id)
    return Actor(name="agent", source=EditSource.AGENT)
