"""Requests are intents; the chart records what happened.

`docs/design/requests-and-read-models.md`. A request changes nothing about
a patient. A read model is written **only** as the outcome of a fulfilment,
and the fulfilment closes the request.

A lab order is the case everyone already understands: ordering an HbA1c
changes nothing, the result enters the chart when the lab reports back, and
the order closes at that moment. Nobody would accept a system where placing
the order wrote a value. This module is that rule, made available to every
other kind.

**The measured reason it exists.** Every request-shaped column in the schema
was empty — `lab_results.request_id` 0 of 8,309, `prescriptions.request_id`
0 of 2,175, `service_requests.end_date` 0 of 1,705, and 0 requests in
COMPLETED. Not six oversights: one missing idea. The generator wrote chart
rows directly, so the request layer was never on the path between an
intention and a fact.

**What this buys is mostly safety.** The agent places requests; the agent
does not write the chart. A request is cheap because it changes nothing,
evaluable because :func:`is_open` runs against it first, and the chart moves
only on evidence of service — evidence carrying its own origin, so an
agent-initiated fact stays distinguishable from a clinician's for as long as
the row exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: Statuses a request may still be acted upon in.
OPEN_STATUSES = frozenset({"DRAFT", "ACTIVE"})


@dataclass(frozen=True)
class Verdict:
    """Whether something may proceed, and why not when it may not.

    ``reason`` is populated even when allowed. A refusal without a cause is
    indistinguishable from a system that did not work, and an approval
    without one cannot be audited.
    """

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _status_name(request) -> str:
    status = getattr(request, "status", None)
    return getattr(status, "name", str(status or "")).upper()


def is_open(request, as_of: date | None = None) -> Verdict:
    """May this request still be acted upon?

    Four clauses, and the refusal names which one failed:

    - **voided** — entered in error; it never happened
    - **closed** — ``end_date`` is set, meaning the request has been served
      and its life is over (`medication-orders-and-refills.md` §7 Q2). The
      chart may not be modified on the basis of a closed request.
    - **status** — revoked or already completed
    - **future** — requested for a date that has not arrived

    ``end_date`` is deliberately *not* a clinical date. It is the end of the
    request's own life, stamped by whatever served it.
    """
    if getattr(request, "voided_at", None) is not None:
        return Verdict(False, "the request was voided — entered in error")

    end = getattr(request, "end_date", None)
    if end is not None:
        return Verdict(False, f"the request closed on {end} and cannot be acted upon")

    status = _status_name(request)
    if status and status not in OPEN_STATUSES:
        return Verdict(False, f"the request is {status.lower()}, not open")

    if as_of is not None:
        requested = getattr(request, "requested_date", None)
        if requested is not None and requested > as_of:
            return Verdict(False, f"the request is dated {requested}, which is in the future")

    return Verdict(True, "open")


def fulfil(session, request, when: date, *, note: str = "") -> Verdict:
    """Close a request because it has been served.

    Status and date move **together**. The importer previously set
    ``status = COMPLETED`` without stamping when, which left every served
    request looking open to anything reading dates — so the two are done in
    one place rather than by each caller remembering both.

    Refuses to close what is not open, rather than closing it twice: a
    second fulfilment of one request means something upstream lost track,
    and silently allowing it would hide that.
    """
    from hdh.core.models import RequestStatus

    verdict = is_open(request, when)
    if not verdict.ok:
        return Verdict(False, f"cannot fulfil: {verdict.reason}")

    request.status = RequestStatus.COMPLETED
    request.end_date = when
    if note:
        detail = dict(getattr(request, "detail", None) or {})
        detail["fulfilment"] = note
        request.detail = detail
    session.flush()
    return Verdict(True, f"fulfilled on {when}")


def may_write_chart(request, as_of: date | None = None) -> Verdict:
    """Guard for anything about to write a read model from a request.

    The rule in one call: nothing may amend the chart on the basis of a
    request that is not open. Separate from :func:`is_open` only to give
    the intent a name at the call site — a reader should be able to see
    that a write was gated, not infer it.
    """
    verdict = is_open(request, as_of)
    if verdict.ok:
        return verdict
    return Verdict(False, f"the chart may not be written from this request: {verdict.reason}")
