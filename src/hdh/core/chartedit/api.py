"""The one sanctioned way to change a chart row (design §3.2).

Everything else — the CLI, the agent tools, comprehension's review
resolution — is a thin client of :func:`apply_edits`. That is what makes
the audit trail complete: there is no code path that mutates a chart row
without writing its event in the same transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hdh.core.chartedit.contracts import Actor, ChartEdit, EditAction, EditOutcome
from hdh.core.chartedit.entities import VISIT_OWNED, spec_for


def _jsonable(value: Any) -> Any:
    """Audit diffs are JSON: enums by value, dates as ISO text."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # enum member
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class _Change:
    """What to record about one landed change — the audit payload,
    grouped so the writer keeps a single responsibility."""

    action: str
    reason: str
    before: dict
    after: dict


def _audit(session, actor: Actor, spec, row, change: _Change):
    """Append one event. Never updated, never deleted."""
    from hdh.core.models import ChartAuditEvent

    event = ChartAuditEvent(
        actor_name=actor.name,
        actor_source=actor.source,
        provider_id=actor.provider_id,
        patient_id=spec.patient_id(row),
        entity=spec.entity,
        row_id=row.id,
        action=change.action,
        reason=change.reason,
        before=change.before or None,
        after=change.after or None,
    )
    session.add(event)
    session.flush()
    return event


def _amend(session, actor: Actor, spec, row, edit: ChartEdit) -> EditOutcome:
    if row.voided_at is not None:
        return EditOutcome(edit, False, f"{spec.describe(row)} is voided — voided rows are not amended")
    unknown = set(edit.changes) - spec.amendable_fields
    if unknown:
        allowed = ", ".join(sorted(spec.amendable_fields))
        return EditOutcome(
            edit, False, f"not amendable on {spec.entity}: {', '.join(sorted(unknown))} (allowed: {allowed})"
        )
    if not edit.changes:
        return EditOutcome(edit, False, "no changes given")
    before, after = {}, {}
    for field, raw in edit.changes.items():
        try:
            value = spec.coerce(field, raw)
        except (ValueError, KeyError) as err:
            return EditOutcome(edit, False, f"{field}: {err}")
        current = getattr(row, field)
        if current == value:
            continue
        before[field] = _jsonable(current)
        after[field] = _jsonable(value)
        setattr(row, field, value)
    if not after:
        return EditOutcome(edit, False, f"{spec.describe(row)} already matches — nothing to change")
    event = _audit(session, actor, spec, row, _Change("amend", edit.reason, before, after))
    changed = ", ".join(f"{k}: {before[k]!r} → {after[k]!r}" for k in after)
    return EditOutcome(edit, True, f"{spec.describe(row)} — {changed}", event.id, before, after)


def _void(session, actor: Actor, spec, row, edit: ChartEdit, now: datetime) -> EditOutcome:
    if row.voided_at is not None:
        return EditOutcome(edit, False, f"{spec.describe(row)} was already voided at {row.voided_at}")
    label = spec.describe(row)
    row.voided_at = now
    voided = _Change("void", edit.reason, {"voided_at": None}, {"voided_at": _jsonable(now)})
    event = _audit(session, actor, spec, row, voided)
    cascaded = _cascade_void(session, actor, spec, row, edit, now) if spec.entity == "Visit" else 0
    suffix = f" (+{cascaded} owned rows)" if cascaded else ""
    return EditOutcome(edit, True, f"voided {label}{suffix}", event.id)


def _cascade_void(session, actor: Actor, spec, visit, edit: ChartEdit, now: datetime) -> int:
    """Voiding a visit voids what it owns — the encounter did not happen,
    so neither did its vitals, orders, results or recorded problems."""
    from sqlalchemy import select

    count = 0
    for entity in VISIT_OWNED:
        owned_spec = spec_for(entity)
        model = owned_spec._model()
        rows = session.execute(
            select(model).where(model.visit_id == visit.id).execution_options(include_voided=True)
        ).scalars()
        for row in rows:
            if row.voided_at is not None:
                continue
            row.voided_at = now
            why = f"cascade: {edit.reason}" if edit.reason else f"cascade from voided Visit #{visit.id}"
            _audit(
                session,
                actor,
                owned_spec,
                row,
                _Change("void", why, {"voided_at": None}, {"voided_at": _jsonable(now)}),
            )
            count += 1
    return count


def apply_edits(
    session,
    actor: Actor,
    edits: Sequence[ChartEdit],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[EditOutcome, ...]:
    """Apply chart edits, auditing every one that lands.

    Refusals are outcomes, not exceptions: a bad field name or a missing
    reason comes back as ``applied=False`` with the explanation, so CLI
    and agent report the same words. ``dry_run`` computes every outcome
    and rolls the transaction back — nothing written, audit included."""
    stamp = now or datetime.utcnow()
    outcomes: list[EditOutcome] = []
    for edit in edits:
        try:
            spec = spec_for(edit.entity)
        except ValueError as err:
            outcomes.append(EditOutcome(edit, False, str(err)))
            continue
        row = spec.load(session, edit.row_id)
        if row is None:
            outcomes.append(EditOutcome(edit, False, f"no {edit.entity} #{edit.row_id}"))
            continue
        if spec.reason_required and not edit.reason.strip():
            outcomes.append(EditOutcome(edit, False, f"{spec.entity} changes require a reason — none given"))
            continue
        if edit.action is EditAction.AMEND:
            outcomes.append(_amend(session, actor, spec, row, edit))
        else:
            outcomes.append(_void(session, actor, spec, row, edit, stamp))
    if dry_run:
        session.rollback()
        return tuple(
            EditOutcome(o.edit, o.applied, f"[dry run] {o.detail}", None, o.before, o.after) for o in outcomes
        )
    session.commit()
    return tuple(outcomes)


def record_creation(session, actor: Actor, entity: str, row, reason: str = "") -> int | None:
    """Audit a row that another writer just created (design §7 Q2: the
    comprehension applier's own writes are part of a chart's history, so
    ``hdh chart history`` shows *how* each entry arrived)."""
    spec = spec_for(entity)
    if spec.patient_id(row) is None:
        return None
    event = _audit(session, actor, spec, row, _Change("create", reason, {}, {"created": spec.describe(row)}))
    return event.id


def record_update(
    session,
    actor: Actor,
    entity: str,
    row,
    changes: Mapping[str, tuple[Any, Any]],
    reason: str = "",
) -> int | None:
    """Audit fields another writer just changed on an existing row.

    The sibling of :func:`record_creation`, and needed for the same reason.
    ``apply_edits`` is the route for a REQUESTED edit and commits as part of
    its contract; a writer that owns its own transaction — the comprehension
    applier, which must be able to roll a whole note back for ``--dry-run``
    — cannot use it, and without this would mutate a chart leaving no trace
    of who changed what.

    ``changes`` maps field name to ``(before, after)``, so one note that
    revises several fields of a row is one event rather than several.
    """
    spec = spec_for(entity)
    if spec.patient_id(row) is None or not changes:
        return None
    event = _audit(
        session,
        actor,
        spec,
        row,
        _Change(
            "amend",
            reason,
            {field: _jsonable(before) for field, (before, _after) in changes.items()},
            {field: _jsonable(after) for field, (_before, after) in changes.items()},
        ),
    )
    return event.id


def history(session, patient_id: int, limit: int = 50) -> list:
    """The audit trail for one chart, newest first."""
    from sqlalchemy import select

    from hdh.core.models import ChartAuditEvent

    return list(
        session.execute(
            select(ChartAuditEvent)
            .where(ChartAuditEvent.patient_id == patient_id)
            .order_by(ChartAuditEvent.occurred_at.desc(), ChartAuditEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def purge_visit(session, visit_id: int) -> dict[str, int]:
    """ADMIN ONLY (design §7 Q1): really delete a visit and everything it
    owns, audit rows included. This is the supported form of the by-hand
    cleanup we do on dev databases — never exposed to the agent, and not
    a clinical operation. Returns per-table delete counts."""
    from sqlalchemy import delete, select

    from hdh.core.models import ChartAuditEvent, Visit

    visit = session.execute(
        select(Visit).where(Visit.id == visit_id).execution_options(include_voided=True)
    ).scalar_one_or_none()
    if visit is None:
        raise ValueError(f"no Visit #{visit_id}")
    counts: dict[str, int] = {}
    for entity in VISIT_OWNED:
        model = spec_for(entity)._model()
        counts[entity] = session.execute(delete(model).where(model.visit_id == visit_id)).rowcount
    for extra in ("VisitNote", "Procedure"):
        from hdh.core import models as core_models

        model = getattr(core_models, extra, None)
        if model is not None:
            counts[extra] = session.execute(delete(model).where(model.visit_id == visit_id)).rowcount
    counts["ChartAuditEvent"] = session.execute(
        delete(ChartAuditEvent).where(ChartAuditEvent.entity == "Visit", ChartAuditEvent.row_id == visit_id)
    ).rowcount
    counts["Visit"] = session.execute(delete(Visit).where(Visit.id == visit_id)).rowcount
    session.commit()
    return counts
