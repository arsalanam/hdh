"""Voided rows disappear from the chart (design chart-maintenance.md §3.3).

Voiding is only meaningful if readers stop seeing the row — otherwise a
"voided" condition still shows up in exports, cohort queries and the
comprehension applier's reconciliation. Rather than teach every reader
about ``voided_at``, one loader criterion is installed on the Session:
ORM reads exclude voided rows, and anything that genuinely needs them
(the audit trail, the admin purge path, voiding something twice) opts in
explicitly with ``execution_options(include_voided=True)``.

Two documented limits, both inherent to where the filter sits:

- **Core-table reads** via ``session.execute(select(table))`` bypass the
  ORM and therefore this filter — those callers see ``voided_at`` and can
  filter it themselves.
- **Rows already in a session's identity map** stay reachable through
  that same session: the criterion applies when a query runs, while
  ``session.get()`` answers from cache (and its refresh is a column load,
  which this filter skips by design). Fresh sessions — every CLI
  invocation, every agent tool call, every request — are unaffected;
  within one long-lived session, ``expunge_all()`` drops the cache.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

INCLUDE_VOIDED = "include_voided"


def voidable_models() -> tuple[type, ...]:
    """The entities that carry ``voided_at`` (imported lazily: this module
    is installed *from* models.py)."""
    from hdh.core import models

    return (
        models.Condition,
        models.Visit,
        models.Vital,
        models.Prescription,
        models.LabResult,
        models.Allergy,
    )


def _hide_voided(state) -> None:
    if not state.is_select or state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get(INCLUDE_VOIDED, False):
        return
    for model in voidable_models():
        state.statement = state.statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.voided_at.is_(None),
                include_aliases=True,
            )
        )


def install() -> None:
    """Register the filter once, for every Session in the process."""
    if not event.contains(Session, "do_orm_execute", _hide_voided):
        event.listen(Session, "do_orm_execute", _hide_voided)
