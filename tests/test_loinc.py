"""LOINC: OntologyService #3, and the coding of real orders.

The fixture is FABRICATED — LOINC is licensed, so these codes are invented
and the names are nonsense words, exactly as the SNOMED fixture is. What
it reproduces faithfully is the shape of a release, which is what the
loader has to understand.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

from hdh.core.models import (
    Patient,
    RequestOrigin,
    RequestStatus,
    ServiceKind,
    ServiceRequest,
    Sex,
    Visit,
    VisitType,
    get_engine,
    get_session,
)
from hdh.core.ontology import get_ontology_service
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.loinc.loader import LoincLoadError, run_load

FIXTURE = Path(__file__).parent / "fixtures" / "loinc"
sys.path.insert(0, str(FIXTURE))
# NOT `fixture_ids`: tests/fixtures/snomed already owns that module name,
# and both directories end up on sys.path — whichever imports first wins
# and the other suite silently gets the wrong constants.
import loinc_ids as fx  # noqa: E402


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    """A database with the fabricated release in it."""
    bootstrap_schema()
    engine = get_engine(str(tmp_path_factory.mktemp("loinc") / "loinc.db"))
    session = get_session(engine)
    report = run_load(session, FIXTURE)
    yield session, report
    session.close()
    engine.dispose()


@pytest.fixture
def service(loaded):
    session, _report = loaded
    return get_ontology_service("loinc", session)


# ── the loader ───────────────────────────────────────────────────────────


def test_a_release_loads_codes_terms_and_the_tree(loaded):
    _session, report = loaded
    assert report.concepts == 11  # 6 orderable codes + 5 interior Part nodes
    assert report.terms == 32
    assert report.hierarchy_rows == 11


def test_a_retired_code_is_not_loaded(service):
    """A coder must not quietly assign a code the release withdrew, and a
    chart holding one cannot say whether it was current when written."""
    assert service.lookup(fx.RETIRED) is None
    assert service.lookup(fx.BLORBIUM_SERUM) is not None


def test_related_names_become_searchable_terms(service):
    """RELATEDNAMES2 is where the clinician's vocabulary lives. Without it
    a lab funnel matches nothing but formal names — and hand-curating the
    alternative is what issue #54 argued against."""
    synonyms = {s.lower() for s in service.synonyms(fx.ZONKOCYTES)}
    assert {"zonk count", "zc", "zonkocyte"} <= synonyms


def test_pointing_the_loader_at_the_wrong_place_fails_loudly(tmp_path):
    session = get_session(get_engine(str(tmp_path / "empty.db")))
    with pytest.raises(LoincLoadError, match="Loinc.csv"):
        run_load(session, tmp_path)
    session.close()


# ── the funnel ───────────────────────────────────────────────────────────


def test_the_funnel_finds_a_code_by_its_common_name(service):
    hits = service.normalize("Zonk count")
    assert hits and hits[0].concept.code == fx.ZONKOCYTES


def test_an_abbreviation_in_the_release_resolves(service):
    """ "QXT" is in RELATEDNAMES2, so LOINC ships the abbreviation table we
    would otherwise have curated by hand."""
    hits = service.normalize("QXT")
    assert hits and hits[0].concept.code == fx.QUIXATE


def test_the_specimen_axis_decides_between_identical_names(service):
    """A bare "blorbium" matches the serum and the urine code equally on
    text, and they are different tests. Unqualified means blood; a caller
    who meant urine says so."""
    default = service.normalize("Blorbium", {"limit": 2})
    assert default[0].concept.code == fx.BLORBIUM_SERUM

    urine = service.normalize("Blorbium", {"system": "urine", "limit": 2})
    assert urine[0].concept.code == fx.BLORBIUM_URINE


def test_an_unknown_test_name_resolves_to_nothing(service):
    """Refuse-don't-guess: an uncoded request is legitimate state, and a
    wrong LOINC on a chart is worse than no LOINC at all."""
    assert service.normalize("whole-body vibe assessment") == ()


# ── the hierarchy ────────────────────────────────────────────────────────


def test_ancestors_walk_the_multiaxial_path_nearest_first(service):
    """LOINC's interior nodes are Parts, and Parts are absent from
    Loinc.csv — load only the numbered codes and the tree is there with
    nothing standing on it."""
    assert [c.code for c in service.ancestors(fx.BLORBIUM_SERUM)] == [
        fx.CHEM_ANALYTES,
        fx.CHEMISTRY,
    ]


def test_descendants_sweep_the_subtree(service):
    codes = {c.code for c in service.descendants(fx.CHEMISTRY)}
    assert {fx.BLORBIUM_SERUM, fx.QUIXATE, fx.FLEEBLE_PANEL} <= codes
    assert fx.ZONKOCYTES not in codes  # hematology, a different branch


def test_subsumption_follows_the_path(service):
    assert service.subsumes(fx.CHEMISTRY, fx.BLORBIUM_SERUM)
    assert not service.subsumes(fx.CHEMISTRY, fx.ZONKOCYTES)
    assert not service.subsumes(fx.BLORBIUM_SERUM, fx.BLORBIUM_SERUM)  # strict


# ── coding real orders ───────────────────────────────────────────────────


def _order(session, patient, visit, display: str) -> ServiceRequest:
    row = ServiceRequest(
        patient_id=patient.id,
        visit_id=visit.id,
        kind=ServiceKind.LAB,
        status=RequestStatus.ACTIVE,
        origin=RequestOrigin.CLINICIAN,
        display=display,
        requested_date=date(2026, 8, 21),
    )
    session.add(row)
    session.flush()
    return row


def test_the_coder_fills_in_what_a_request_was_created_without(loaded):
    """§2 made `code` nullable because a request is real before anyone
    codes it. This is the other half — and it goes through the audited
    path, so the chart can say the code came from the LOINC module rather
    than from the clinician who ordered the test."""
    from argparse import Namespace

    from hdh.core.chartedit import history
    from hdh.modules.loinc.cli import run_cli

    session, _report = loaded
    patient = Patient(
        mrn="MRN-LOINC-1",
        first_name="Cod",
        last_name="Ing",
        date_of_birth=date(1960, 1, 1),
        sex=Sex.FEMALE,
    )
    session.add(patient)
    session.flush()
    visit = Visit(patient_id=patient.id, visit_date=date(2026, 8, 21), visit_type=VisitType.FOLLOW_UP)
    session.add(visit)
    session.flush()
    known = _order(session, patient, visit, "Zonk count")
    unknown = _order(session, patient, visit, "Whole-body vibe assessment")
    session.commit()
    # ids BEFORE the expunge below: a detached row cannot refresh itself
    known_id, unknown_id, patient_id = known.id, unknown.id, patient.id

    run_cli(
        session,
        Namespace(loinc_cmd="code", request=None, mrn="MRN-LOINC-1", min_score=0.6, dry_run=False),
    )

    session.expunge_all()
    coded = session.get(ServiceRequest, known_id)
    assert (coded.code, coded.code_system) == (fx.ZONKOCYTES, "loinc")
    # the one it could not ground stays uncoded rather than guessed
    assert session.get(ServiceRequest, unknown_id).code is None

    events = [
        event
        for event in history(session, patient_id, limit=50)
        if event.entity == "ServiceRequest" and event.action.value == "amend"
    ]
    assert events and events[0].actor_name == "loinc-module"


def test_comprehension_falls_back_to_the_funnel_for_unaliased_tests(loaded):
    """§7's actual complaint: "B/P" and "Tmax" resolved to NOTHING because
    they were absent from a hardcoded dict. The vitals table still wins
    where it applies — those codes are the contract with the chart's vitals
    columns — but everything else now reaches term search."""
    from hdh.modules.comprehension.contracts import Mention, MentionType, Span
    from hdh.modules.comprehension.normalize import MentionNormalizer

    session, _report = loaded
    normalizer = MentionNormalizer(session)

    def code_for(text: str):
        mention = Mention(
            id=1,
            mention_type=MentionType.LAB_VITAL,
            span=Span(0, len(text)),
            text=text,
            section_id=1,
        )
        return normalizer.candidates(mention)

    # the vitals contract still wins, exactly and deterministically
    assert code_for("HR")[0].code == "8867-4"

    # and a test the dict never knew about now resolves
    found = code_for("Zonk count")
    assert found and found[0].code == fx.ZONKOCYTES
    assert found[0].in_shared_tables is True  # LOINC IS in the shared tables
