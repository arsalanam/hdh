"""Institution, location, and the shared address/contact tables.

The institution half of the thin-entity plan (core-chart-expansion §3): an
organisation, its locations, the specialty clinics each location runs (with
their own phone and weekly hours), and the shared address/contact tables that
serve both owners through a real FK with a one-owner CHECK. Plus the
``location_id`` link that lets a visit or procedure say where it happened.
"""

from datetime import date, time

import pytest
from sqlalchemy.exc import IntegrityError

from hdh.core.models import (
    Address,
    Contact,
    Location,
    LocationSpecialty,
    Organization,
    Patient,
    Procedure,
    Sex,
    Visit,
    VisitType,
    get_engine,
    get_session,
)
from hdh.core.schema_registry import bootstrap_schema


@pytest.fixture()
def session(tmp_path):
    from hdh.core.models import Base

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "inst.db"))
    Base.metadata.create_all(engine)
    s = get_session(engine)
    yield s
    s.close()
    engine.dispose()


# ── the shared address/contact one-owner rule ────────────────────────────


def test_address_and_contact_require_exactly_one_owner(session):
    org = Organization(name="X")
    session.add(org)
    loc = Location(organization=org, name="L")
    session.add(loc)
    session.flush()

    for kwargs in ({}, {"organization_id": org.id, "location_id": loc.id}):
        session.add(Address(use="physical", line="1", **kwargs))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    for kwargs in ({}, {"organization_id": org.id, "location_id": loc.id}):
        session.add(Contact(system="phone", value="x", **kwargs))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_a_single_owner_is_accepted_and_navigable(session):
    org = Organization(name="Practice")
    session.add(org)
    session.flush()
    org.addresses.append(Address(use="billing", line="1 Main", city="Springfield"))
    org.contacts.append(Contact(system="phone", use="main", value="555-0100", rank=1))
    session.commit()

    assert [a.line for a in org.addresses] == ["1 Main"]
    assert org.contacts[0].value == "555-0100"
    # the reverse navigation resolves to the owner
    assert org.addresses[0].organization is org and org.addresses[0].location is None


# ── location specialties and their weekly hours ──────────────────────────


def test_seed_organization_is_complete_and_idempotent(session):
    from hdh.core.generators import seed_organization, seed_providers

    seed_providers(session)
    org = seed_organization(session)
    session.commit()

    assert org.tax_id and org.npi
    assert len(org.addresses) == 1 and org.addresses[0].use == "billing"
    assert any(c.system == "phone" for c in org.contacts)

    locations = session.query(Location).all()
    assert len(locations) == 2
    for location in locations:
        assert len(location.addresses) == 1 and location.addresses[0].use == "physical"
        assert location.contacts and location.contacts[0].system == "phone"
        assert location.specialties, f"{location.name} has no specialty clinics"
        for clinic in location.specialties:
            assert clinic.phone
            # Monday–Friday, five rows, opening at 08:00
            assert len(clinic.hours) == 5
            assert {h.day_of_week for h in clinic.hours} == {0, 1, 2, 3, 4}
            assert min(h.opens for h in clinic.hours) == time(8, 0)

    # idempotent: a second call neither duplicates nor raises
    again = seed_organization(session)
    assert again.id == org.id
    assert session.query(Organization).count() == 1
    assert session.query(Location).count() == 2


def test_a_location_cannot_offer_the_same_specialty_twice(session):
    from hdh.core.models import Specialty

    org = Organization(name="P")
    session.add(org)
    session.flush()
    loc = Location(organization_id=org.id, name="Clinic")
    spec = Specialty(code="FM", name="Family Medicine")
    session.add_all([loc, spec])
    session.flush()

    session.add(LocationSpecialty(location_id=loc.id, specialty_id=spec.id, phone="1"))
    session.flush()
    session.add(LocationSpecialty(location_id=loc.id, specialty_id=spec.id, phone="2"))
    with pytest.raises(IntegrityError):
        session.flush()


# ── the service-location link on visits and procedures ───────────────────


def test_a_visit_and_procedure_record_where_they_happened(session):
    org = Organization(name="P")
    session.add(org)
    session.flush()
    loc = Location(organization_id=org.id, name="North Clinic")
    patient = Patient(
        mrn="MRN1", first_name="A", last_name="B", date_of_birth=date(1980, 1, 1), sex=Sex.FEMALE
    )
    session.add_all([loc, patient])
    session.flush()

    visit = Visit(
        patient_id=patient.id, visit_date=date(2026, 8, 1), visit_type=VisitType.FOLLOW_UP, location_id=loc.id
    )
    session.add(visit)
    session.flush()
    proc = Procedure(patient_id=patient.id, visit_id=visit.id, description="ECG", location_id=loc.id)
    session.add(proc)
    session.commit()

    assert visit.location.name == "North Clinic"
    assert proc.location.name == "North Clinic"
    # nullable: a visit without a known location is still valid
    unknown = Visit(patient_id=patient.id, visit_date=date(2026, 8, 2), visit_type=VisitType.ACUTE)
    session.add(unknown)
    session.commit()
    assert unknown.location is None
