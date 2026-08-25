"""Care plan, milestone 1: the plan graph as a schema module.

The design (§5, §8) makes one strong claim about this data model — that the
four-part graph is enforced by **foreign keys**, not by a validator and
certainly not by asking the model nicely:

> Orphans are structurally impossible to persist.

These tests exist to hold that claim. Everything else here — a plan
lifecycle, provenance on every content row — is secondary to it.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from hdh.core.models import Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema

PLAN_TABLES = (
    "care_plan_records",
    "health_concerns",
    "plan_goals",
    "plan_interventions",
    "plan_outcomes",
    "plan_evaluations",
)


@pytest.fixture
def world(tmp_path):
    """A chart with one patient, and the careplan tables registered."""
    from hdh.core.models import Base

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "careplan.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    # SQLite does not enforce foreign keys unless asked, and the whole point
    # of this module's design is that it does
    session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
    patient = Patient(
        mrn="MRN-PLAN",
        first_name="Care",
        last_name="Planned",
        date_of_birth=date(1948, 3, 2),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    yield session, patient
    session.close()
    engine.dispose()


def _entity(name):
    from hdh.core.models import Base

    return Base.metadata.tables[name]


# ── the module registers at all ──────────────────────────────────────────


def test_the_six_entities_register():
    """The registry's new-entity path, which is what §5 says this module
    exercises — six tables that core knows nothing about."""
    from hdh.core.models import Base

    bootstrap_schema()
    missing = [t for t in PLAN_TABLES if t not in Base.metadata.tables]
    assert not missing, f"careplan entities did not register: {missing}"


def test_core_still_has_no_idea_this_module_exists():
    """The dependency rule: `hdh.core` never imports a module. If the plan
    tables were reachable from core's own imports this would pass for the
    wrong reason, so it checks the source, not the runtime."""
    import pathlib

    core = pathlib.Path("src/hdh/core")
    offenders = [
        f"{path.relative_to(core)}:{i}"
        for path in core.rglob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "hdh.modules.careplan" in line
    ]
    assert not offenders, f"core imports the careplan module: {offenders}"


# ── the graph invariant, which is the whole point ────────────────────────


def _plan(session, patient):
    from sqlalchemy import insert

    session.execute(
        insert(_entity("care_plan_records")),
        [
            {
                "patient_id": patient.id,
                "title": "Diabetes and social isolation",
                "status": "draft",
                "created_at": datetime(2026, 8, 25, 9, 0),
            }
        ],
    )
    session.flush()
    return session.execute(__import__("sqlalchemy").select(_entity("care_plan_records").c.id)).scalar()


def _concern(session, plan_id, statement="Hypoglycaemia risk on a sulfonylurea"):
    from sqlalchemy import insert, select

    session.execute(
        insert(_entity("health_concerns")),
        [
            {
                "care_plan_id": plan_id,
                "concern_type": "risk",
                "statement": statement,
                "source": "ai",
                "evidence_refs": {"chunks": ["med_safety/beers#sulfonylurea"]},
            }
        ],
    )
    session.flush()
    return session.execute(
        select(_entity("health_concerns").c.id).order_by(_entity("health_concerns").c.id.desc())
    ).scalar()


def _goal(session, plan_id, concern_id):
    from sqlalchemy import insert, select

    session.execute(
        insert(_entity("plan_goals")),
        [
            {
                "care_plan_id": plan_id,
                "concern_id": concern_id,
                "statement": "No hypoglycaemic episode requiring assistance",
                "measure_system": "loinc",
                "measure_code": "4548-4",
                "target_value": "7.5-8.0%",
                "target_due": date(2026, 11, 25),
                "expressed_by": "clinician",
                "status": "active",
                "source": "ai",
            }
        ],
    )
    session.flush()
    return session.execute(
        select(_entity("plan_goals").c.id).order_by(_entity("plan_goals").c.id.desc())
    ).scalar()


def test_a_goal_cannot_exist_without_a_concern(world):
    """§5: 'every goal points at a concern'. A goal with no concern is a
    recommendation with no reason — the exact thing a generated plan drifts
    into, and the reason this is a NOT NULL foreign key rather than a
    post-hoc validator."""
    session, patient = world
    plan_id = _plan(session, patient)
    from sqlalchemy import insert

    with pytest.raises(IntegrityError):
        session.execute(
            insert(_entity("plan_goals")),
            [
                {
                    "care_plan_id": plan_id,
                    "concern_id": None,
                    "statement": "Improve glycaemic control",
                    "expressed_by": "clinician",
                    "status": "active",
                    "source": "ai",
                }
            ],
        )
        session.flush()
    session.rollback()


def test_an_intervention_cannot_exist_without_a_goal(world):
    """§5: 'every intervention points at a goal'. An intervention with no
    goal is an instruction nobody can say the purpose of."""
    session, patient = world
    plan_id = _plan(session, patient)
    from sqlalchemy import insert

    with pytest.raises(IntegrityError):
        session.execute(
            insert(_entity("plan_interventions")),
            [
                {
                    "care_plan_id": plan_id,
                    "goal_id": None,
                    "intervention_type": "medication",
                    "statement": "Switch glipizide to a DPP-4 inhibitor",
                    "source": "ai",
                }
            ],
        )
        session.flush()
    session.rollback()


def test_an_outcome_cannot_exist_without_a_goal(world):
    """§5: 'every outcome points at a goal'. An outcome with no goal is a
    measurement of nothing in particular."""
    session, patient = world
    plan_id = _plan(session, patient)
    from sqlalchemy import insert

    with pytest.raises(IntegrityError):
        session.execute(
            insert(_entity("plan_outcomes")),
            [
                {
                    "care_plan_id": plan_id,
                    "goal_id": None,
                    "measure": "HbA1c",
                    "achievement_status": "in_progress",
                }
            ],
        )
        session.flush()
    session.rollback()


def test_a_goal_cannot_point_at_a_concern_that_does_not_exist(world):
    """NOT NULL is only half of it: a plausible-looking id that references
    nothing is exactly what a generated plan produces when it invents a
    cross-reference."""
    session, patient = world
    plan_id = _plan(session, patient)
    from sqlalchemy import insert

    with pytest.raises(IntegrityError):
        session.execute(
            insert(_entity("plan_goals")),
            [
                {
                    "care_plan_id": plan_id,
                    "concern_id": 9999,
                    "statement": "Improve glycaemic control",
                    "expressed_by": "clinician",
                    "status": "active",
                    "source": "ai",
                }
            ],
        )
        session.flush()
    session.rollback()


def test_the_whole_chain_persists_when_it_is_intact(world):
    """The positive case: concern → goal → intervention → outcome, each
    traceable upward, all four written."""
    from sqlalchemy import func, insert, select

    session, patient = world
    plan_id = _plan(session, patient)
    concern_id = _concern(session, plan_id)
    goal_id = _goal(session, plan_id, concern_id)

    session.execute(
        insert(_entity("plan_interventions")),
        [
            {
                "care_plan_id": plan_id,
                "goal_id": goal_id,
                "intervention_type": "monitoring",
                "statement": "Continuous glucose monitoring with caregiver alerts",
                "owner_role": "nurse",
                "schedule": "continuous",
                "source": "ai",
            }
        ],
    )
    session.execute(
        insert(_entity("plan_outcomes")),
        [
            {
                "care_plan_id": plan_id,
                "goal_id": goal_id,
                "measure": "HbA1c",
                "measure_system": "loinc",
                "measure_code": "4548-4",
                "observed_value": "7.8%",
                "observed_at": date(2026, 11, 20),
                "achievement_status": "achieved",
            }
        ],
    )
    session.commit()

    for table in ("health_concerns", "plan_goals", "plan_interventions", "plan_outcomes"):
        count = session.execute(select(func.count()).select_from(_entity(table))).scalar()
        assert count == 1, f"{table} did not persist"

    # and the chain is walkable in the direction that matters: why is this
    # intervention here?
    joined = session.execute(
        select(_entity("health_concerns").c.statement).select_from(
            _entity("plan_interventions")
            .join(
                _entity("plan_goals"), _entity("plan_interventions").c.goal_id == _entity("plan_goals").c.id
            )
            .join(
                _entity("health_concerns"),
                _entity("plan_goals").c.concern_id == _entity("health_concerns").c.id,
            )
        )
    ).scalar()
    assert joined == "Hypoglycaemia risk on a sulfonylurea"


# ── provenance and lifecycle ─────────────────────────────────────────────


def test_every_content_row_records_whether_a_human_or_the_model_wrote_it(world):
    """`source` is not optional on concerns, goals or interventions. A plan
    that cannot say which parts were proposed is not reviewable, and review
    is the entire safety story (§10)."""
    for table in ("health_concerns", "plan_goals", "plan_interventions"):
        column = _entity(table).c.source
        assert column.nullable is False, f"{table}.source must be required"
        assert {"ai", "human"} <= set(column.type.enums)


def test_the_lifecycle_covers_rejection_and_revision(world):
    """§5's status chain has to include the unhappy paths, or a rejected
    plan has nowhere to sit."""
    states = set(_entity("care_plan_records").c.status.type.enums)
    assert {"draft", "ai_generated", "auto_evaluated", "pending_review"} <= states
    assert {"user_edited", "approved", "rejected"} <= states


def test_an_intervention_can_point_at_the_order_it_became(world):
    """A medication or referral a plan proposes is a *proposal*, and
    `ServiceRequest` already models exactly that — DRAFT until a human
    releases it. Linking rather than duplicating is why the orders module
    existing matters here."""
    column = _entity("plan_interventions").c.request_id
    assert column.foreign_keys, "plan interventions should be able to reference a ServiceRequest"
    assert next(iter(column.foreign_keys)).target_fullname == "service_requests.id"
    assert column.nullable, "an education or monitoring intervention has no order"


def test_a_measure_carries_its_vocabulary_not_just_a_code(world):
    """The design said `measure_loinc`. Every code the chart stores now
    pairs with its system (`Prescription`, `ServiceRequest`), and naming a
    column after one vocabulary is the only place hdh would bake one in."""
    for table in ("plan_goals", "plan_outcomes"):
        columns = _entity(table).c
        assert "measure_system" in columns and "measure_code" in columns
        assert "measure_loinc" not in columns


# ── the PostgreSQL requirement, stated rather than degraded ──────────────


def test_a_postgresql_requirement_refuses_with_a_reason(world):
    """ARCHITECTURE §4a: an advanced module says what it cannot do here.

    The failure names the feature in the user's terms, the dialect it
    actually found, and how to get a PostgreSQL database — because the
    alternative this replaces was silently doing a quarter of the job.
    """
    from hdh.core.dialect import DatabaseFeatureError, is_postgresql, require_postgresql

    session, _patient = world
    assert not is_postgresql(session)

    with pytest.raises(DatabaseFeatureError) as err:
        require_postgresql(session, "Care-plan knowledge retrieval")
    message = str(err.value)
    assert "Care-plan knowledge retrieval" in message
    assert "sqlite" in message
    assert "just deps" in message


def test_the_check_passes_silently_where_the_feature_exists():
    """It must be cheap and quiet on the supported path — a guard that
    logged or warned on success would be noise on every call."""
    from types import SimpleNamespace

    from hdh.core.dialect import require_postgresql

    pg = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    require_postgresql(pg, "anything")  # no raise, no output


def test_the_plan_data_model_itself_stays_portable(world):
    """The requirement is scoped to the capability that needs it, not the
    whole module. Six tables of plan graph are ordinary SQL and work
    anywhere — which is why these tests run on SQLite at all."""
    from sqlalchemy import func, select

    session, patient = world
    plan_id = _plan(session, patient)
    concern_id = _concern(session, plan_id)
    _goal(session, plan_id, concern_id)
    session.commit()
    assert session.execute(select(func.count()).select_from(_entity("plan_goals"))).scalar() == 1
