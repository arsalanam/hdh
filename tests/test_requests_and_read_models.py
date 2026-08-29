"""A request is an intent; the chart records what happened.

`docs/design/requests-and-read-models.md`. The measured reason this exists:
every request-shaped column in the schema was empty — `lab_results.request_id`
0 of 8,309, `prescriptions.request_id` 0 of 2,175, `service_requests.end_date`
0 of 1,705, and 0 requests COMPLETED. Not six oversights but one missing
idea: the generator wrote chart rows directly, so the request layer was
never on the path between an intention and a fact.
"""

from __future__ import annotations

from datetime import date

import pytest

from hdh.core.fulfilment import OPEN_STATUSES, fulfil, is_open, may_write_chart


class _Request:
    """Enough of a ServiceRequest for the rules to read."""

    def __init__(self, **kwargs):
        self.voided_at = None
        self.end_date = None
        self.status = "ACTIVE"
        self.requested_date = date(2026, 1, 1)
        self.detail = None
        self.__dict__.update(kwargs)


TODAY = date(2026, 6, 1)


# ── the four ways a request is not actionable ────────────────────────────


def test_an_open_request_is_open():
    verdict = is_open(_Request(), TODAY)
    assert verdict and verdict.reason == "open"


def test_a_closed_request_cannot_be_acted_upon():
    """`end_date` is not a clinical date. It is the end of the request's own
    life, stamped by whatever served it — and a served request may not be
    acted on again."""
    verdict = is_open(_Request(end_date=date(2026, 3, 1)), TODAY)
    assert not verdict
    assert "2026-03-01" in verdict.reason


def test_a_voided_request_never_happened():
    assert not is_open(_Request(voided_at="x"), TODAY)


def test_a_revoked_request_is_refused_by_status():
    verdict = is_open(_Request(status="REVOKED"), TODAY)
    assert not verdict and "revoked" in verdict.reason


def test_a_future_request_is_not_yet_actionable():
    verdict = is_open(_Request(requested_date=date(2027, 1, 1)), TODAY)
    assert not verdict and "future" in verdict.reason


def test_every_refusal_names_its_own_cause():
    """A refusal without a cause is indistinguishable from a system that did
    not work — the same rule the care-plan grader follows."""
    for request in (
        _Request(voided_at="x"),
        _Request(end_date=date(2026, 3, 1)),
        _Request(status="REVOKED"),
        _Request(requested_date=date(2027, 1, 1)),
    ):
        verdict = is_open(request, TODAY)
        assert not verdict
        assert len(verdict.reason) > 15, verdict.reason


def test_only_draft_and_active_are_open():
    assert OPEN_STATUSES == {"DRAFT", "ACTIVE"}


# ── the chart guard ──────────────────────────────────────────────────────


def test_the_chart_may_not_be_written_from_a_closed_request():
    verdict = may_write_chart(_Request(end_date=date(2026, 3, 1)), TODAY)
    assert not verdict
    assert "chart may not be written" in verdict.reason


def test_the_chart_may_be_written_from_an_open_one():
    assert may_write_chart(_Request(), TODAY)


def test_the_agent_cannot_create_chart_rows(monkeypatch):
    """The guarantee the request layer is for: **the agent places requests;
    the agent does not write the chart.**

    Its chart tools amend and void rows that already exist. Nothing in that
    surface creates one, so a fact can only enter the chart through a
    fulfilment — where `may_write_chart` applies. This pins the property so
    it cannot erode by someone adding a create tool without noticing what it
    costs.
    """
    from hdh.modules.agent.chart_tools import build_chart_tools

    # `.name`, not `__name__`. A BetaFunctionTool carries the former and
    # leaves the latter None, so the first version of this test collected an
    # empty set and asserted it held no creators — passing without ever
    # looking at a tool. The guard below makes that failure impossible to
    # repeat: no names means the test itself is broken, not that the
    # property holds.
    names = {tool.name for tool in build_chart_tools(session=None)}
    assert names and all(names), "no tool names — this absence test would be vacuous"

    creators = {n for n in names if "create" in n or "add" in n or "new" in n}
    assert not creators, f"the agent gained a chart-creating tool: {creators}"


# ── fulfilment moves status and date together ────────────────────────────


class _Session:
    def flush(self):
        pass


def test_fulfilment_stamps_the_date_as_well_as_the_status():
    """The importer previously set COMPLETED without stamping when, which
    left every served request looking open to anything reading dates. The
    two move in one place so a caller cannot do half of it."""
    from hdh.core.models import RequestStatus

    request = _Request()
    assert fulfil(_Session(), request, TODAY)
    assert request.status is RequestStatus.COMPLETED
    assert request.end_date == TODAY


def test_a_request_cannot_be_fulfilled_twice():
    """A second fulfilment means something upstream lost track, and allowing
    it silently would hide that."""
    request = _Request()
    assert fulfil(_Session(), request, TODAY)
    again = fulfil(_Session(), request, TODAY)
    assert not again and "cannot fulfil" in again.reason


def test_fulfilment_records_a_note_when_given_one():
    request = _Request()
    fulfil(_Session(), request, TODAY, note="results imported")
    assert request.detail["fulfilment"] == "results imported"


# ── the generator now puts requests on the path ──────────────────────────


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("gen") / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=8, years_of_history=3, verbose=False, seed=11)
    yield session
    session.close()
    engine.dispose()


@pytest.mark.parametrize("entity", ["LabResult", "Procedure", "MedicationDispense"])
def test_every_read_model_row_names_the_request_it_fulfils(generated, entity):
    """Was 0 of 8,309 for labs. A fact that cannot say what asked for it is
    a fact that appeared from nowhere."""
    from sqlalchemy import func, select

    import hdh.core.models as models

    model = getattr(models, entity)
    total = generated.execute(select(func.count()).select_from(model)).scalar()
    orphans = generated.execute(
        select(func.count()).select_from(model).where(model.request_id.is_(None))
    ).scalar()
    assert total, f"the fixture generated no {entity}"
    assert not orphans, f"{orphans} of {total} {entity} rows have no request"


def test_served_requests_carry_the_date_they_closed(generated):
    """Status and `end_date` move together, so a served request never looks
    open to something reading dates."""
    from sqlalchemy import func, select

    from hdh.core.models import RequestStatus, ServiceRequest

    completed = generated.execute(
        select(func.count())
        .select_from(ServiceRequest)
        .where(ServiceRequest.status == RequestStatus.COMPLETED)
    ).scalar()
    dated = generated.execute(
        select(func.count())
        .select_from(ServiceRequest)
        .where(
            ServiceRequest.status == RequestStatus.COMPLETED,
            ServiceRequest.end_date.isnot(None),
        )
    ).scalar()
    assert completed and completed == dated


def test_a_follow_up_is_fulfilled_only_with_evidence(generated):
    """A visit can answer several follow-ups and `Visit.request_id` names
    one, so the rest stay open rather than being marked fulfilled with
    nothing to point at."""
    from sqlalchemy import func, select

    from hdh.core.models import RequestStatus, ServiceKind, ServiceRequest, Visit

    fulfilled = generated.execute(
        select(func.count())
        .select_from(ServiceRequest)
        .where(
            ServiceRequest.kind == ServiceKind.FOLLOW_UP,
            ServiceRequest.status == RequestStatus.COMPLETED,
        )
    ).scalar()
    answering = generated.execute(
        select(func.count()).select_from(Visit).where(Visit.request_id.isnot(None))
    ).scalar()
    assert fulfilled == answering, "a follow-up was closed with no visit to show for it"


def test_referrals_stay_unfulfilled(generated):
    """Decided deliberately: a referral is answered by a letter, an
    appointment or a decline, none of which the chart models. Leaving it
    open is truer than inventing an outcome row for it."""
    from sqlalchemy import func, select

    from hdh.core.models import RequestStatus, ServiceKind, ServiceRequest

    closed = generated.execute(
        select(func.count())
        .select_from(ServiceRequest)
        .where(
            ServiceRequest.kind == ServiceKind.REFERRAL,
            ServiceRequest.status == RequestStatus.COMPLETED,
        )
    ).scalar()
    assert not closed
