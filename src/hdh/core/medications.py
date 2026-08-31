"""Is a patient on this drug *now*?

One definition, because three places were answering it and two of them were
wrong in the same way (issue #115).

The generator has known since the prescribing work that a course with a
stated duration ends when it ends. The two modules that *read* the chart did
not: both took every prescription written in the last 365 days, so a
five-day antibiotic finished in March still counted as an active medication
in December.

That is not a rounding error in a display. It inflates the medication list a
care plan reasons over, and it inflates the polypharmacy flag — which is
graded, acted on, and was reading "10 medications" for a patient who was not
on ten. Every plan built on that list inherited the mistake.

**The rule.** A prescription with a duration is current until that duration
runs out. One without a duration is an ongoing medication, and falls back to
a window — because a repeat prescription written two years ago and never
renewed is not evidence the patient is still taking it either.

`ONGOING_WINDOW_DAYS` lives here rather than in each caller for the same
reason the rule does: `careplan` and `caregaps` must not be able to disagree
about whether a patient is on five drugs.
"""

from __future__ import annotations

from datetime import date, timedelta

#: How long a prescription with no stated duration counts as current.
#:
#: Not a clinical constant — a reporting one. Without any window, "active
#: medications" becomes every prescription ever written and polypharmacy
#: fires on everybody; with too short a window, a stable repeat looks
#: stopped. A year is what the modules already agreed on.
ONGOING_WINDOW_DAYS = 365


def is_current(
    started: date | None,
    duration_days: int | None,
    as_of: date,
    *,
    window_days: int = ONGOING_WINDOW_DAYS,
) -> bool:
    """Was this prescription still running on ``as_of``?

    ``started`` of ``None`` is not current: a prescription that cannot say
    when it began cannot support a claim about now, and guessing would put
    an unknowable into a medication list that gets reasoned over.
    """
    if started is None:
        return False
    if started > as_of:
        # Written for a future date. Not an error — a post-dated repeat is
        # real — but not something the patient is taking yet.
        return False
    if duration_days:
        return started + timedelta(days=int(duration_days)) >= as_of
    return started >= as_of - timedelta(days=window_days)


def is_current_row(prescription, as_of: date, *, started: date | None = None, **kwargs) -> bool:
    """:func:`is_current` for a prescription row or dict.

    ``started`` is passed in because a ``Prescription`` has no date of its
    own — it is dated by the visit that wrote it, and only the caller holds
    that. Making it explicit beats reaching back through the relationship
    from here and issuing a query per row.
    """
    if isinstance(prescription, dict):
        duration = prescription.get("duration_days")
    else:
        duration = getattr(prescription, "duration_days", None)
    return is_current(started, duration, as_of, **kwargs)
