"""A saved care plan leaves the building as FHIR.

The flat CarePlan comes from the schema hint core reads; everything that
makes it a *plan* — status, goals, activities, what was deferred, what each
element cites — comes from the module's enricher, because a care plan is
four tables and a declared emitter maps one row.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("fhir.resources")

from hdh.modules.careplan.fhir import CITATION_URL, STATUS_MAP  # noqa: E402


@pytest.fixture()
def planned(tmp_path):
    """A patient with one saved, approved care plan."""
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft
    from hdh.modules.careplan.persist import persist_reviewed_plan

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=4, years_of_history=2, verbose=False, seed=13, as_of=date(2026, 8, 14))
    patient = session.query(Patient).first()
    values = {
        "concerns": [ConcernDraft("Polypharmacy — 6 active drugs", "risk", ("med_safety/dup",))],
        "goals": [GoalDraft("Reduce to 4 agents", 0, "4 agents", ("med_safety/dup",))],
        "interventions": [
            InterventionDraft("Structured medication review", 0, "service", "GP", ("med_safety/dup",))
        ],
        "deferred": ["Essential hypertension — controlled"],
    }
    plan_id = persist_reviewed_plan(session, patient, values).plan_id
    yield session, patient, plan_id
    session.close()
    engine.dispose()


def _care_plans(patient) -> list[dict]:
    from hdh.core.exporters import patient_to_fhir_bundle

    bundle = patient_to_fhir_bundle(patient)
    return [
        entry["resource"]
        for entry in bundle.get("entry") or []
        if entry["resource"].get("resourceType") == "CarePlan"
    ]


# ── the resource exists at all ───────────────────────────────────────────


def test_a_saved_plan_appears_in_the_bundle(planned):
    _session, patient, _plan_id = planned
    plans = _care_plans(patient)
    assert len(plans) == 1


def test_the_plan_is_anchored_to_the_patient(planned):
    _session, patient, _plan_id = planned
    assert _care_plans(patient)[0]["subject"]["reference"] == f"Patient/{patient.mrn}"


# ── status is the part a receiver acts on ────────────────────────────────


def test_an_unapproved_plan_is_not_active(planned):
    """A plan nobody approved must not arrive looking actionable."""
    _session, patient, _plan_id = planned
    assert _care_plans(patient)[0]["status"] == "draft"


def test_approval_makes_it_active(planned):
    from hdh.modules.careplan.persist import decide

    session, patient, plan_id = planned
    decide(session, plan_id, True, "reviewed with the patient")
    assert _care_plans(patient)[0]["status"] == "active"


def test_rejection_makes_it_revoked(planned):
    from hdh.modules.careplan.persist import decide

    session, patient, plan_id = planned
    decide(session, plan_id, False, "burden too high")
    assert _care_plans(patient)[0]["status"] == "revoked"


def test_every_hdh_status_maps_to_a_legal_fhir_status():
    """The FHIR value set is not ours to extend, so an unmapped status would
    fail validation at the receiver rather than here."""
    legal = {"draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown"}
    assert set(STATUS_MAP.values()) <= legal


def test_an_unknown_status_degrades_to_draft():
    """Never to active. An unrecognised status is not permission to act."""
    assert STATUS_MAP.get("something-new", "draft") == "draft"


# ── the plan's content ───────────────────────────────────────────────────


def test_interventions_become_activities(planned):
    _session, patient, _plan_id = planned
    plan = _care_plans(patient)[0]
    assert plan["activity"]
    assert "Structured medication review" in plan["activity"][0]["detail"]["description"]


def test_an_activity_carries_the_goal_it_serves(planned):
    """A FHIR activity with no goal is a task nobody can justify."""
    _session, patient, _plan_id = planned
    detail = _care_plans(patient)[0]["activity"][0]["detail"]
    assert "Reduce to 4 agents" in detail["description"]


def test_a_measurable_target_survives_the_export(planned):
    """`goals_with_target` is what `goal_quality` is graded on — losing it
    on export would export a weaker plan than the one that was written."""
    _session, patient, _plan_id = planned
    assert "target: 4 agents" in _care_plans(patient)[0]["activity"][0]["detail"]["description"]


def test_the_owner_role_travels_as_text_not_a_reference(planned):
    """A role is not a Practitioner. Inventing a reference to a practitioner
    nobody named would be a confident approximation."""
    _session, patient, _plan_id = planned
    detail = _care_plans(patient)[0]["activity"][0]["detail"]
    assert "Owner: GP" in detail["description"]
    assert not detail.get("performer")


def test_citations_travel_as_extensions(planned):
    """Exporting the claim without its support is the one thing this module
    refuses to do anywhere else."""
    _session, patient, _plan_id = planned
    extensions = _care_plans(patient)[0]["activity"][0].get("extension") or []
    assert any(e["url"] == CITATION_URL and e["valueString"] == "med_safety/dup" for e in extensions)


def test_what_triage_deferred_is_exported_too(planned):
    """A receiving system that cannot see what was set aside is reading a
    filtered plan, exactly as a human reviewer would be."""
    _session, patient, _plan_id = planned
    notes = " ".join(n["text"] for n in _care_plans(patient)[0].get("note") or [])
    assert "Deferred by triage: Essential hypertension" in notes


def test_the_concerns_are_visible(planned):
    _session, patient, _plan_id = planned
    notes = " ".join(n["text"] for n in _care_plans(patient)[0].get("note") or [])
    assert "Polypharmacy" in notes


# ── a patient with no plan still exports ─────────────────────────────────


def test_a_patient_without_a_plan_exports_cleanly(tmp_path):
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, Patient, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "empty.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=3, years_of_history=1, verbose=False, seed=5, as_of=date(2026, 8, 14))
    try:
        assert _care_plans(session.query(Patient).first()) == []
    finally:
        session.close()
        engine.dispose()


# ── a superseded plan, and the link between the two ──────────────────────


@pytest.fixture()
def superseded(planned):
    """#1 approved, then amended into #2 which supersedes it."""
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft
    from hdh.modules.careplan.persist import amend_plan, decide, persist_reviewed_plan

    session, patient, _first = planned
    values = {
        "concerns": [
            ConcernDraft("Polypharmacy", "risk", ("med_safety/dup",)),
            ConcernDraft("Falls risk", "risk", ("guidelines/falls",)),
        ],
        "goals": [
            GoalDraft("Reduce to 4 agents", 0, "4 agents", ("med_safety/dup",)),
            GoalDraft("No falls in 6 months", 1, "0", ("guidelines/falls",)),
        ],
        "interventions": [
            InterventionDraft("Medication review", 0, "service", "GP", ("med_safety/dup",)),
            InterventionDraft("Falls assessment", 1, "referral", "GP", ("guidelines/falls",)),
        ],
    }
    old_id = persist_reviewed_plan(session, patient, values).plan_id
    decide(session, old_id, True, "signed off")
    new_id = amend_plan(session, old_id, {1}, "falls handled by the falls service").plan_id
    return session, patient, old_id, new_id


def test_a_superseded_plan_is_not_active(superseded):
    """It was approved, so it would otherwise export as `active` and a
    receiving system would act on a plan that has been replaced."""
    _session, patient, _old, _new = superseded
    revoked = [p for p in _care_plans(patient) if p["status"] == "revoked"]
    assert len(revoked) == 1


def test_the_successor_points_at_what_it_replaced(superseded):
    _session, patient, _old, _new = superseded
    successor = next(p for p in _care_plans(patient) if p.get("replaces"))
    assert successor["replaces"][0]["reference"].startswith("CarePlan/")


def test_the_replaces_reference_resolves_inside_the_bundle(superseded):
    """The reference was `CarePlan/<row id>` while resource ids are
    content-hashed, so it pointed at nothing. A dangling reference in an
    exported bundle is worse than no reference: a receiver cannot tell it
    from a resource it failed to fetch."""
    _session, patient, _old, _new = superseded
    plans = _care_plans(patient)
    ids = {p["id"] for p in plans}
    successor = next(p for p in plans if p.get("replaces"))
    target = successor["replaces"][0]["reference"].split("/", 1)[1]
    assert target in ids, f"replaces points at {target}, which is not in the bundle"


def test_the_superseded_plan_is_the_one_pointed_at(superseded):
    """Not merely *a* resource in the bundle — the right one."""
    _session, patient, _old, _new = superseded
    plans = _care_plans(patient)
    successor = next(p for p in plans if p.get("replaces"))
    target = successor["replaces"][0]["reference"].split("/", 1)[1]
    assert next(p for p in plans if p["id"] == target)["status"] == "revoked"
