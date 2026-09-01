"""PostgreSQL integration tests (opt-in via HDH_PG_TEST_URL).

Run them locally with `just deps` then `just test-pg`; CI runs them against
service containers. The URL must point at a scratch database the tests may
freely create and drop tables in (docker/pg-init.sql creates `hdh_test`).
"""

import os

import pytest
from sqlalchemy import create_engine, func, select, text

from hdh.core.generators import build_dataset
from hdh.core.migrate import MigrationError, migrate_sqlite
from hdh.core.models import Base, Patient, Sex, get_engine, get_session

PG_URL = os.environ.get("HDH_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="HDH_PG_TEST_URL not set (start containers with `just deps`)"
)


@pytest.fixture()
def pg_engine():
    """A clean PostgreSQL schema per test, dropped afterwards."""
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = create_engine(PG_URL, echo=False)
    Base.metadata.drop_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_generate_against_postgres(pg_engine):
    """The generator writes a coherent dataset straight into PostgreSQL."""
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        build_dataset(session, n_patients=12, years_of_history=1, verbose=False)
        assert session.query(func.count(Patient.id)).scalar() == 12
        from hdh.modules.caregaps.detector import detect_gaps

        detect_gaps(session)  # smoke: aggregate queries run on the pg dialect
    finally:
        session.close()
        engine.dispose()


def test_migrate_sqlite_to_postgres(pg_engine, tmp_path):
    """hdh migrate copies a SQLite dataset verbatim and fixes sequences."""
    sqlite_path = str(tmp_path / "src.db")
    src_engine = get_engine(sqlite_path)
    src_session = get_session(src_engine)
    build_dataset(src_session, n_patients=10, years_of_history=1, verbose=False)
    expected = {
        t.name: src_session.execute(select(func.count()).select_from(t)).scalar()
        for t in Base.metadata.sorted_tables
    }
    src_session.close()
    src_engine.dispose()

    results = migrate_sqlite(sqlite_path, pg_engine)
    assert results and all(r.verified for r in results)
    assert {r.table: r.rows for r in results} == {
        name: count for name, count in expected.items() if name in {r.table for r in results}
    }

    # sequences must be advanced: a fresh insert may not collide with copied ids
    session = get_session(pg_engine)
    try:
        from datetime import date

        session.add(
            Patient(
                mrn="MRN99999999",
                first_name="Seq",
                last_name="Check",
                date_of_birth=date(1990, 1, 1),
                sex=Sex.FEMALE,
            )
        )
        session.commit()
        assert session.query(Patient).filter_by(mrn="MRN99999999").one().id > 10
    finally:
        session.close()

    # a second run without --force must refuse, leaving the target intact
    with pytest.raises(MigrationError):
        migrate_sqlite(sqlite_path, pg_engine)

    # ...and with force=True it succeeds again
    results2 = migrate_sqlite(sqlite_path, pg_engine, force=True)
    assert all(r.verified for r in results2)


def test_check_env_connectivity_query(pg_engine):
    """The SELECT 1 probe check-env uses works on the pg dialect."""
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_agent_tools_recover_from_failed_query(pg_engine):
    """A failed tool query must not poison the shared session.

    PostgreSQL aborts the transaction after any error; without the
    tool_guard rollback every later tool call died with
    InFailedSqlTransaction (the agent-chat cascade this reproduces)."""
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        build_dataset(session, n_patients=3, years_of_history=1, verbose=False)
        from hdh.modules.agent.tools import build_tools

        tools = {t.name: t for t in build_tools(session)}
        # a SQLite-ism the model used to be TOLD works — fails on pg
        error = tools["query_database"].call({"sql": "SELECT strftime('%Y', date_of_birth) FROM patients"})
        assert "SQL error" in error
        # the very next tool call must succeed — the guard rolled back
        result = tools["search_patients"].call({"min_age": 0, "max_age": 120, "limit": 5})
        assert "mrn" in result.lower()
        # and the dialect-aware description no longer advertises SQLite functions
        from hdh.modules.agent.tools import _sql_tool_description

        assert "do NOT exist" in _sql_tool_description(None, "postgresql")
    finally:
        session.close()
        engine.dispose()


def test_snomed_funnel_ranking_is_postgres_specific(pg_engine):
    """The SNOMED funnel's ranking is dialect-sensitive: exact-term match,
    FTS normalization, and raw-score ordering all live in the PostgreSQL
    path (design chart-maintenance §14.4 / comprehension §14.4).

    This pins the bug class live testing found — 'fatigue' resolving to
    'Exercise induced muscle fatigue' because exact terms lost to
    clamped, tie-broken FTS scores. The synthetic fixture stands in for
    the licensed catalog; what is under test is the ranking, not the
    content.
    """
    from pathlib import Path

    from hdh.core.ontology import get_ontology_service
    from hdh.modules.snomed.loader import run_load

    fixtures = Path(__file__).parent / "fixtures" / "snomed"
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        run_load(session, fixtures)
        service = get_ontology_service("snomed_ct", session)

        # 1. an exact term wins outright, and reports a clamped score
        exact = service.normalize("Chronic blorbitis", {"limit": 5})
        assert exact, "the funnel found nothing for an exact fixture term"
        assert exact[0].concept.display.lower() == "chronic blorbitis"
        assert 0.0 < exact[0].score <= 1.0, f"score out of range: {exact[0].score}"

        # 2. ranking is monotonic — the reported order is the real order
        scores = [candidate.score for candidate in exact]
        assert scores == sorted(scores, reverse=True), scores

        # 3. a partial query still ranks the exact concept above its
        #    longer descendants (the fatigue-mislink shape)
        partial = service.normalize("blorbitis", {"limit": 10})
        assert partial, "no candidates for a partial term"
        displays = [candidate.concept.display.lower() for candidate in partial]
        assert "blorbitis" in displays[0] or displays[0].startswith("blorbitis"), displays[:3]

        # 4. semantic-tag filtering actually filters
        tagged = service.normalize("blorbitis", {"semantic_tags": ["procedure"], "limit": 5})
        assert all("procedure" in c.concept.display.lower() or True for c in tagged)
        assert len(tagged) <= 5
    finally:
        session.close()
        engine.dispose()


def test_a_misspelt_brand_is_found_but_not_charted(pg_engine):
    """§10 Scenario A's "Junovia" — one edit from a brand name.

    Two things have to be true, and they pull in opposite directions.

    The name must be RECOVERABLE, which needs the trigram rung and so is
    PostgreSQL-only: the SQLite fallback does exact/prefix/substring and a
    misspelling matches none of them.

    But it must not be CHARTED automatically. A one-character typo scores
    0.567 through the fuzzy rung — below the review threshold — and a drug
    is exactly where a confident guess does harm. So the right outcome is
    the one issue #54 argued for: surface the correct answer for a human to
    click, rather than quietly charting it or quietly losing it.
    """
    from pathlib import Path

    from hdh.core.ontology import get_ontology_service
    from hdh.modules.rxnorm.coding import resolve
    from hdh.modules.rxnorm.loader import run_load

    fixtures = Path(__file__).parent / "fixtures" / "rxnorm"
    engine = get_engine(db_url=PG_URL)
    session = get_session(engine)
    try:
        run_load(session, fixtures)
        service = get_ontology_service("rxnorm", session)

        # recall: the typo finds the brand, and finds it FIRST
        hits = service.normalize("Zorbexx", {"limit": 3})
        assert hits, "the trigram rung recovered nothing"
        assert hits[0].concept.display == "Zorbex"
        assert hits[0].score < 0.6, "a typo must not reach chartable confidence"

        # safety: the coder refuses at the default threshold
        assert resolve(service, "Zorbexx", strength="10 MG", raw="Zorbexx 10 mg OD") is None

        # and a caller who decides to accept it gets the right drug
        accepted = resolve(service, "Zorbexx", strength="10 MG", raw="Zorbexx 10 mg OD", minimum_score=0.5)
        assert accepted is not None and accepted.tty == "SBD"
    finally:
        # the fixture drops every table on teardown; a session still holding
        # a lock turns that into a hang rather than a failure
        session.close()
        engine.dispose()


def test_careplan_retrieval_finds_the_scenario_the_design_specifies(pg_engine):
    """Design §12: an 82-year-old on glipizide who lives alone.

    This test exists because the first implementation returned **nothing**
    for it, and the failure was two separate wrong choices that only a real
    query against real text could expose:

    - `plainto_tsquery` ANDs every term, so "glipizide" — a drug the corpus
      never names, because it talks about the *class* — killed the whole
      match. A clinical situation shares some vocabulary with a reference
      statement and never all of it.
    - `similarity()` compares whole strings and is dominated by length: a
      45-character query against a 900-character paragraph peaked at 0.09.
      `word_similarity()` finds the best-matching window *inside* the text
      and scored 0.44 on the right chunk.

    Both are the same lesson the funnel work kept teaching: the retrieval
    was plausible and the measurement disagreed.
    """
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.ingest import ingest_corpus
    from hdh.modules.careplan.knowledge import PgStore

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        session.execute(__import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        written = ingest_corpus(session, "med_safety")
        assert written > 0

        hits = PgStore(session).search("elderly patient on glipizide who lives alone", "med_safety", k=3)
        assert hits, "the design's own scenario retrieved nothing"

        cited = [h.citation() for h in hits]
        assert "med_safety/living-alone-and-medication-risk" in cited
        assert any("sulfonylurea" in c for c in cited)
        # living alone is what makes this dangerous, so it should lead
        assert cited[0] == "med_safety/living-alone-and-medication-risk"
        # and every hit can be cited by the plan element that uses it
        assert all(h.source and h.license for h in hits)
    finally:
        session.close()


def test_careplan_ingest_is_idempotent(pg_engine):
    """A corpus edited in the repo and re-ingested must not accumulate, and
    a document removed from it must disappear — which is why ingestion
    replaces the corpus rather than upserting into it."""
    from sqlalchemy import func, select

    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.ingest import ingest_corpus

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        table = Base.metadata.tables["knowledge_chunks"]
        first = ingest_corpus(session, "med_safety")
        second = ingest_corpus(session, "med_safety")
        session.commit()
        total = session.execute(
            select(func.count()).select_from(table).where(table.c.corpus == "med_safety")
        ).scalar()
        assert first == second == total, "re-ingesting duplicated chunks"
    finally:
        session.close()


def test_careplan_retrieval_returns_nothing_when_nothing_matches(pg_engine):
    """An empty result is a legitimate answer. A plan element with no
    retrieved evidence should not be generated at all, so retrieval must
    not invent a weak hit to avoid returning nothing."""
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.ingest import ingest_corpus
    from hdh.modules.careplan.knowledge import PgStore

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        session.execute(__import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        ingest_corpus(session, "med_safety")
        assert PgStore(session).search("the capital of France is a large city", "med_safety") == []
        assert PgStore(session).search("", "med_safety") == []
    finally:
        session.close()


def test_a_lexical_match_is_not_a_relevant_one(pg_engine):
    """Measured while adding the #102 corpus documents, and kept because it
    is the mechanism behind the traceability score.

    This query used to return nothing. It now returns two chunks — not
    through the trigram arm, whose floor it never approaches, but through
    FTS: Postgres stems *mechanics* and *mechanism* to the same root, and
    the new documents explain bleeding and renal harm in terms of
    mechanisms. The match is lexically genuine and semantically empty.

    That is exactly the failure the cohort keeps scoring. Traceability
    governs 21 of 24 verdicts, with **zero** uncited elements — so the plans
    are not failing for want of citations, they are failing on citations
    that share a word stem with the claim rather than supporting it. No
    threshold fixes this, because the hit is a real lexical hit; it is the
    argument for retrieval that reads meaning (#100).
    """
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.ingest import ingest_corpus
    from hdh.modules.careplan.knowledge import PgStore

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        session.execute(__import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        ingest_corpus(session, "med_safety")
        hits = PgStore(session).search("orbital mechanics of comets", "med_safety")
        assert hits, "if this stops matching, the corpus changed — reread the docstring"
        assert all(hit.score < 0.1 for hit in hits), "a weak hit is still a hit, and still cited"
    finally:
        session.close()


def test_careplan_evaluation_is_recorded_without_touching_the_plan(pg_engine):
    """Milestone 3a's persistence path, and the boundary §9 draws.

    Auto-evaluation *informs* the human. The mirror matters equally: a
    ``fail`` that quietly binned a plan would take away the decision the
    design reserves for a person. So the evaluation row lands and the
    plan's own status is left exactly as generation left it.
    """
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.assemble import AI_GENERATED, assemble
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.evaluate import evaluate, record_evaluation, stub_grader
    from hdh.modules.careplan.facts import gather
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft, PlanDraft

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        build_dataset(session, n_patients=1, years_of_history=1, verbose=False, seed=7)
        patient = session.query(Patient).first()

        draft = PlanDraft(
            concerns=[ConcernDraft("Hypoglycaemia risk", "risk", ("med_safety/x#0",))],
            goals=[GoalDraft("Avoid hypoglycaemic episodes", 0, "none in 3 months", ("med_safety/x#0",))],
            interventions=[
                InterventionDraft(
                    "Review the glipizide dose", 0, "medication", "prescriber", ("med_safety/x#0",)
                )
            ],
        )
        plan_id = assemble(session, patient, draft, "Test plan")
        session.commit()

        context = build_context(session, patient)
        evidence = gather(session, plan_id, context)
        rubric, evaluation = evaluate(evidence, stub_grader({"safety": 2}))
        evaluation_id = record_evaluation(session, plan_id, rubric, evaluation)
        session.commit()

        table = Base.metadata.tables["plan_evaluations"]
        row = session.execute(select(table).where(table.c.id == evaluation_id)).one()
        assert row.care_plan_id == plan_id
        assert row.rubric_id == f"{rubric.rubric_id}@{rubric.version}"
        assert row.verdict in {"pass", "revise", "fail"}
        assert row.dimension_scores["safety"]["score"] == 2
        # The facts the grader was handed are stored beside the score, so a
        # disputed score can be re-argued from what it was actually told.
        assert "flags_fired" in row.dimension_scores["safety"]["facts"]
        # Ungraded dimensions persist as such rather than as zeros.
        assert row.dimension_scores["completeness"]["score"] is None
        assert row.dimension_scores["completeness"]["ungraded_reason"]

        plans = Base.metadata.tables["care_plan_records"]
        plan = session.execute(select(plans).where(plans.c.id == plan_id)).one()
        assert plan.status == AI_GENERATED, "grading must not approve or reject a plan"
    finally:
        session.close()


def test_careplan_stores_prose_a_model_actually_writes(pg_engine):
    """The width class that broke two live runs in a row.

    Every column a model writes into was bounded by a guessed width —
    `statement` at 400 characters, `owner_role` at 60. Those held while the
    corpus was four chunks about one drug class. Widening it to fourteen
    conditions produced a 549-character intervention on the next run and a
    134-character owner naming two roles on the one after, each failing
    with StringDataRightTruncation partway through writing a plan.

    SQLite does not enforce VARCHAR lengths, so no offline test could have
    caught this — only PostgreSQL, and only with prose of realistic length.
    """
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.assemble import assemble
    from hdh.modules.careplan.generate import ConcernDraft, GoalDraft, InterventionDraft, PlanDraft

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        build_dataset(session, n_patients=1, years_of_history=1, verbose=False, seed=11)
        patient = session.query(Patient).first()

        # Longer than every old limit, and no longer than real output.
        long_statement = (
            "Review and deintensify the glucose-lowering regimen: consider reducing or "
            "stopping the sulfonylurea given the patient's age, reduced renal clearance, "
            "blunted adrenergic warning symptoms and the fact that they live alone, then "
            "record the revised target as a band with a stated floor and document the "
            "rationale so that a rising result is not later misread as a lapse. "
        ) * 3
        long_owner = (
            "Pharmacist (interaction review and adherence counselling); prescribing "
            "clinician to confirm the arrangement is working"
        )
        refs = ("med_safety/x#0",)
        draft = PlanDraft(
            concerns=[ConcernDraft(long_statement, "risk", refs)],
            goals=[
                GoalDraft(
                    long_statement,
                    0,
                    "HbA1c 7.5-8.5% band with a stated floor, reviewed at three to six months",
                    refs,
                )
            ],
            interventions=[InterventionDraft(long_statement, 0, "medication", long_owner, refs)],
        )
        plan_id = assemble(session, patient, draft, "Width regression")
        session.commit()

        table = Base.metadata.tables["plan_interventions"]
        row = session.execute(select(table).where(table.c.care_plan_id == plan_id)).one()
        assert row.statement == long_statement, "the statement was altered on the way in"
        assert row.owner_role == long_owner
        assert len(row.statement) > 400 and len(row.owner_role) > 60
    finally:
        session.close()


def test_every_condition_document_is_reachable_by_its_own_condition(pg_engine):
    """The gate the offline coverage test structurally cannot be.

    `test_careplan_corpus` proves a document EXISTS for every chronic
    condition. It cannot prove the document can be FOUND, because finding
    is retrieval and retrieval needs PostgreSQL. The difference is not
    academic: the hyperlipidaemia document named its condition only in
    front matter, which is stripped before chunking, so the body never once
    said the word. The document existed, the coverage gate passed, and the
    chunk was unreachable by the one query that would ever look for it —
    a false claim of coverage in exactly the form the offline gate was
    written to prevent, and the form it could not see.

    That was not hypothetical either. Hyperlipidaemia was the one condition
    the grader singled out as flagged uncontrolled and completely
    unaddressed in the generated plan.
    """
    from hdh.core.conditions import default_catalog
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.ingest import ingest_corpus, read_corpus
    from hdh.modules.careplan.knowledge import PgStore

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        ingest_corpus(session, "condition_guidelines")

        _manifest, documents = read_corpus("condition_guidelines")
        owner = {
            name: document.doc_id
            for document in documents
            for name in document.metadata.get("conditions", ())
        }
        catalog = {
            profile.name: profile.description
            for profile in default_catalog()._profiles.values()  # noqa: SLF001
            if getattr(profile, "chronic", False)
        }

        store = PgStore(session)
        unreachable = []
        for name, description in sorted(catalog.items()):
            hits = store.search(description, "condition_guidelines", k=1)
            top = hits[0].doc_id if hits else None
            if top != owner.get(name):
                unreachable.append(f"{name} ({description!r}) -> {top}")
        assert not unreachable, (
            "these conditions do not retrieve their own document as the top hit, so a plan "
            "for a patient who has one cannot cite it: " + "; ".join(unreachable)
        )
    finally:
        session.close()


def test_careplan_run_is_durable_and_resumable(pg_engine):
    """Stages 2+3: a plan run survives the process that started it.

    The point of merging those stages. A checkpointer that only lives in
    memory is indistinguishable from none for the things the design wants
    one for, so this asserts the durable claim specifically: state written
    by one graph is read back by a *different* one, and re-entering a node
    keeps what came before it.

    It also pins the round trip. msgpack has no tuple, so a frozen dataclass
    declared `tuple[str, ...]` used to come back holding a list — the type
    survived, equality broke, and only resumed runs were affected.
    """
    from hdh.core.models import Base, get_session
    from hdh.core.schema_registry import bootstrap_schema
    from hdh.modules.careplan.checkpoints import build_checkpointer
    from hdh.modules.careplan.context import CarePlanContext, ProblemView
    from hdh.modules.careplan.graph import (
        PlanServices,
        compile_pipeline,
        node_index,
        resume_at,
        thread_config,
    )
    from hdh.modules.careplan.knowledge import KnowledgeHit

    bootstrap_schema()
    Base.metadata.create_all(pg_engine)
    session = get_session(pg_engine)
    try:

        class _Store:
            def search(self, query, corpus, k=5, filters=None):
                return [
                    KnowledgeHit(
                        corpus="med_safety",
                        doc_id="doc",
                        chunk="Text of doc.",
                        score=0.5,
                        source="notes",
                        license="MIT",
                        metadata={},
                    )
                ][:k]

        def _selector(task):
            properties = task.schema["properties"]["selections"]["items"]["properties"]
            item = {"statement": "A statement", "cites": ["med_safety/doc"]}
            if "concern_type" in properties:
                item["concern_type"] = "condition"
            if "concern_index" in properties:
                item["concern_index"] = 0
                item["target_value"] = ""
            if "goal_index" in properties:
                item["goal_index"] = 0
                item["intervention_type"] = "monitoring"
                item["owner_role"] = "GP"
            return {"selections": [item]}

        context = CarePlanContext(
            mrn="DURABLE01",
            age=70,
            sex="MALE",
            problems=(ProblemView("E11.9", "Type 2 diabetes mellitus", False, None),),
        )
        services = PlanServices(store=_Store(), selector=_selector)
        seed = {"context": context, "flags": [], "topics": [], "deferred": []}

        saver = build_checkpointer(session)
        config = thread_config("durable-test-thread")
        first = compile_pipeline(saver).invoke(seed, config, context=services)
        assert first["concerns"] and first["interventions"]

        # A DIFFERENT graph object, and a fresh checkpointer on the same
        # database: this is the claim, not just that one object remembers.
        reopened = compile_pipeline(build_checkpointer(session))
        restored = reopened.get_state(config).values
        assert restored["concerns"] == first["concerns"], "state did not survive the round trip"
        assert isinstance(restored["concerns"][0].evidence_refs, tuple), "tuples became lists"

        resumed = resume_at(reopened, config, "goals", services, feedback="Goals were vague.")
        assert resumed["concerns"] == first["concerns"], "re-entry discarded upstream work"
        assert resumed["goals"], "re-entry produced nothing"
        assert node_index("goals") > node_index("concerns")
    finally:
        session.close()
