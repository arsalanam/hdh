"""A recorded encounter carries its location (AU4 location half, #169).

Now that a provider is linked to the site(s) they practise at, a new Visit
created from a note is stamped with the recording provider's primary
location — so vitals, the encounter, and procedures under it can say where
they happened. A provider with no location link leaves it unknown rather than
guessing, and an existing located visit is never overwritten.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

from hdh.core.models import (
    Location,
    Organization,
    Patient,
    Provider,
    ProviderLocation,
    Sex,
    Visit,
    VisitType,
    get_engine,
    get_session,
    primary_location_id,
)
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
from hdh.modules.comprehension.comprehend import comprehend_text
from hdh.modules.comprehension.extract import stub_extractor
from hdh.modules.comprehension.pipeline import comprehend_note

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
if str(SNOMED_FIXTURES) not in sys.path:
    sys.path.insert(0, str(SNOMED_FIXTURES))

EMPTY_NOTE = {"mentions": [], "relations": [], "shared_triggers": []}


@pytest.fixture()
def world(tmp_path):
    """SNOMED fixture (so comprehension runs) + a patient. Providers and
    locations are built per-test to keep each one's linkage explicit."""
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "loc.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    patient = Patient(
        mrn="MRN1", first_name="A", last_name="B", date_of_birth=date(1970, 1, 1), sex=Sex.FEMALE
    )
    session.add(patient)
    session.commit()
    yield session, patient
    session.close()
    engine.dispose()


def _empty_note(session):
    return comprehend_note(session, comprehend_text("2026-08-15 encounter.", stub_extractor(EMPTY_NOTE)))


def _provider_at(session, name, *, location_name, is_primary=True):
    """A provider linked to a (new) location as their home site."""
    org = session.query(Organization).first() or Organization(name="Practice")
    session.add(org)
    session.flush()
    location = Location(organization_id=org.id, name=location_name)
    provider = Provider(identifier=f"NPI-{name}", name=name)
    session.add_all([location, provider])
    session.flush()
    session.add(ProviderLocation(provider_id=provider.id, location_id=location.id, is_primary=is_primary))
    session.commit()
    return provider, location


# ── the resolver ─────────────────────────────────────────────────────────


def test_primary_location_id_prefers_the_primary_then_none(world):
    session, _patient = world
    provider, location = _provider_at(session, "Dr. Home", location_name="Home Clinic")
    assert primary_location_id(session, provider.id) == location.id
    # a provider with no link, and the None provider, both resolve to None
    lone = Provider(identifier="NPI-LONE", name="Dr. Lone")
    session.add(lone)
    session.flush()
    assert primary_location_id(session, lone.id) is None
    assert primary_location_id(session, None) is None


# ── the encounter picks up the location ──────────────────────────────────


def test_a_new_encounter_is_stamped_with_the_providers_location(world):
    session, patient = world
    provider, location = _provider_at(session, "Dr. Grace Chen", location_name="North Clinic")
    result = apply_to_chart(session, patient, _empty_note(session), VisitTarget(provider_id=provider.id))
    assert result.created_visit
    visit = session.get(Visit, result.visit_id)
    assert visit.provider_id == provider.id
    assert visit.location_id == location.id  # where the provider practises
    assert visit.location.name == "North Clinic"


def test_no_provider_link_leaves_the_location_unknown(world):
    session, patient = world
    lone = Provider(identifier="NPI-LONE", name="Dr. Lone")
    session.add(lone)
    session.commit()
    result = apply_to_chart(session, patient, _empty_note(session), VisitTarget(provider_id=lone.id))
    visit = session.get(Visit, result.visit_id)
    assert visit.provider_id == lone.id and visit.location_id is None  # unknown, not guessed


def test_reconciling_backfills_a_missing_location_but_never_overwrites(world):
    session, patient = world
    provider, location = _provider_at(session, "Dr. Grace Chen", location_name="North Clinic")
    other = Location(organization_id=location.organization_id, name="Elsewhere")
    session.add(other)
    session.flush()

    # an existing visit with NO location → backfilled from the provider
    bare = Visit(patient_id=patient.id, visit_date=date(2026, 8, 1), visit_type=VisitType.FOLLOW_UP)
    session.add(bare)
    session.commit()
    apply_to_chart(session, patient, _empty_note(session), VisitTarget(visit=bare, provider_id=provider.id))
    session.refresh(bare)
    assert bare.location_id == location.id

    # an existing visit that ALREADY has a location → left untouched
    located = Visit(
        patient_id=patient.id,
        visit_date=date(2026, 8, 2),
        visit_type=VisitType.FOLLOW_UP,
        location_id=other.id,
    )
    session.add(located)
    session.commit()
    apply_to_chart(
        session, patient, _empty_note(session), VisitTarget(visit=located, provider_id=provider.id)
    )
    session.refresh(located)
    assert located.location_id == other.id  # not overwritten with the provider's site


# ── the generator seeding ─────────────────────────────────────────────────


def test_seed_provider_locations_links_by_specialty(world):
    session, _patient = world
    from hdh.core.generators import seed_organization, seed_provider_locations, seed_providers

    providers = seed_providers(session)
    seed_organization(session)
    seed_provider_locations(session, providers)
    session.commit()

    # every provider is linked, with exactly one primary
    for provider in providers:
        links = session.query(ProviderLocation).filter_by(provider_id=provider.id).all()
        assert links, f"{provider.name} has no location"
        assert sum(1 for link in links if link.is_primary) == 1

    # an FM provider's primary is the North clinic (the lowest-id FM site)
    fm = next(p for p in providers if p.specialty and p.specialty.code == "FM")
    primary = session.get(Location, primary_location_id(session, fm.id))
    assert primary.name == "North Family Medicine Clinic"

    # idempotent
    before = session.query(ProviderLocation).count()
    seed_provider_locations(session, providers)
    assert session.query(ProviderLocation).count() == before
