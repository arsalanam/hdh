"""Personal, identity-aware patient search (AU4 follow-on).

Now that the agent runs as a signed-in provider, search can answer "my
patients", "who did I see recently", and "my visits this quarter" — not just
name/age/ICD. The provider scope defaults to the signed-in identity and can
be pointed at a named colleague (every clinician may view).

Visits carry ``provider_id`` here by hand: the generator does not yet assign
visit providers (that is the #156 named-clinician re-baseline), so on
generated data these views light up only once it does — the logic is proven
against explicit fixtures.
"""

from datetime import date

import pytest

from hdh.core.identity import Identity
from hdh.core.models import Patient, Provider, Sex, Visit, VisitType, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema

AS_OF = "2026-08-31"


@pytest.fixture()
def clinic(tmp_path):
    """Two providers and three patients with dated, provider-stamped visits.

    Chen saw P1 twice in Aug and P2 once back in May; Okafor saw P2 and P3 in
    Aug. Yields (session, chen_identity, ids)."""
    pytest.importorskip("anthropic")
    bootstrap_schema()
    engine = get_engine(str(tmp_path / "clinic.db"))
    session = get_session(engine)

    chen = Provider(identifier="HDH-CHEN", name="Grace Chen")
    okafor = Provider(identifier="HDH-OKAFOR", name="Ada Okafor")
    session.add_all([chen, okafor])
    session.flush()

    def patient(mrn, first, last):
        p = Patient(mrn=mrn, first_name=first, last_name=last, date_of_birth=date(1970, 1, 1), sex=Sex.FEMALE)
        session.add(p)
        session.flush()
        return p

    p1, p2, p3 = patient("MRN1", "Pat", "One"), patient("MRN2", "Pat", "Two"), patient("MRN3", "Pat", "Three")

    def visit(patient, provider, when):
        session.add(
            Visit(
                patient_id=patient.id,
                provider_id=provider.id,
                visit_date=when,
                visit_type=VisitType.FOLLOW_UP,
                chief_complaint="review",
            )
        )

    visit(p1, chen, date(2026, 8, 1))
    visit(p1, chen, date(2026, 8, 20))
    visit(p2, chen, date(2026, 5, 10))
    visit(p2, okafor, date(2026, 8, 25))
    visit(p3, okafor, date(2026, 8, 15))
    session.commit()

    chen_identity = Identity("sub-chen", "dr.chen", frozenset(["clinician"]), provider_id=chen.id)
    yield session, chen_identity, {"chen": chen.id, "okafor": okafor.id}
    session.close()
    engine.dispose()


def _tool(session, identity, name):
    from hdh.modules.agent.tools import build_tools

    return next(t for t in build_tools(session, identity=identity) if t.name == name)


def _mrns(payload) -> set[str]:
    import json

    return {row["mrn"] for row in json.loads(payload)}


# ── search_patients: personal scoping ────────────────────────────────────


def test_seen_by_me_scopes_to_the_signed_in_provider(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call({"seen_by_me": True, "as_of": AS_OF})
    assert _mrns(out) == {"MRN1", "MRN2"}  # Chen saw P1 and P2; P3 (Okafor only) excluded


def test_seen_within_days_keeps_only_the_recent(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call(
        {"seen_by_me": True, "seen_within_days": 30, "as_of": AS_OF}
    )
    # P1's last Chen visit is Aug 20 (within 30d of Aug 31); P2's is May 10 (not)
    assert _mrns(out) == {"MRN1"}


def test_sort_by_last_seen_orders_and_annotates(clinic):
    import json

    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call(
        {"seen_by_me": True, "sort": "last_seen", "as_of": AS_OF}
    )
    rows = json.loads(out)
    assert [r["mrn"] for r in rows] == ["MRN1", "MRN2"]  # Aug 20 before May 10
    assert rows[0]["last_seen"] == "2026-08-20"


def test_a_named_provider_can_be_scoped_to(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call({"provider": "Okafor", "as_of": AS_OF})
    assert _mrns(out) == {"MRN2", "MRN3"}  # Okafor's panel, viewed by Chen


def test_no_signed_in_provider_returns_a_helpful_message(clinic):
    session, _chen, _ = clinic
    tool = _tool(session, None, "search_patients")  # eval/system context: no identity
    out = tool.call({"seen_by_me": True})
    assert "No signed-in provider" in out and "hdh login" in out


def test_an_unknown_provider_name_is_reported(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call({"provider": "Nobody"})
    assert "No provider matches" in out


def test_plain_search_still_works_unscoped(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "search_patients").call({"name": "Pat"})
    assert _mrns(out) == {"MRN1", "MRN2", "MRN3"}


# ── provider_visits: the caseload by period ──────────────────────────────


def test_provider_visits_this_month(clinic):
    import json

    session, chen, _ = clinic
    out = json.loads(_tool(session, chen, "provider_visits").call({"period": "this_month", "as_of": AS_OF}))
    assert out["provider"] == "Grace Chen"
    assert out["from"] == "2026-08-01" and out["to"] == "2026-08-31"
    assert out["total"] == 2  # Chen's two August visits (both P1)
    assert out["by_type"] == {"FOLLOW_UP": 2}


def test_provider_visits_last_month_is_honestly_empty(clinic):
    import json

    session, chen, _ = clinic
    out = json.loads(_tool(session, chen, "provider_visits").call({"period": "last_month", "as_of": AS_OF}))
    assert out["from"] == "2026-07-01" and out["total"] == 0


def test_provider_visits_this_quarter_spans_the_months(clinic):
    import json

    session, chen, _ = clinic
    out = json.loads(_tool(session, chen, "provider_visits").call({"period": "this_quarter", "as_of": AS_OF}))
    # Q3 = Jul–Sep: Chen's Aug visits count, the May one (Q2) does not
    assert out["period"] == "Q3 2026"
    assert out["from"] == "2026-07-01" and out["to"] == "2026-09-30"
    assert out["total"] == 2


def test_provider_visits_can_target_a_colleague(clinic):
    import json

    session, chen, _ = clinic
    out = json.loads(
        _tool(session, chen, "provider_visits").call(
            {"provider": "Okafor", "period": "this_month", "as_of": AS_OF}
        )
    )
    assert out["provider"] == "Ada Okafor" and out["total"] == 2  # P2 + P3 in Aug


def test_unknown_period_is_reported(clinic):
    session, chen, _ = clinic
    out = _tool(session, chen, "provider_visits").call({"period": "fortnight", "as_of": AS_OF})
    assert "Unknown period" in out


# ── period arithmetic (pure) ─────────────────────────────────────────────


def test_period_range_boundaries():
    from hdh.modules.agent.tools import _period_range

    ref = date(2026, 8, 31)
    assert _period_range("this_month", ref)[:3] == (date(2026, 8, 1), date(2026, 8, 31), "August 2026")
    assert _period_range("last_month", ref)[:3] == (date(2026, 7, 1), date(2026, 7, 31), "July 2026")
    assert _period_range("this_quarter", ref)[:3] == (date(2026, 7, 1), date(2026, 9, 30), "Q3 2026")
    # last quarter across a year boundary
    assert _period_range("last_quarter", date(2026, 2, 15))[:3] == (
        date(2025, 10, 1),
        date(2025, 12, 31),
        "Q4 2025",
    )
