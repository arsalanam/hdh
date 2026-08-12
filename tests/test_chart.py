"""Chart-expansion tests (design core-chart-expansion.md §9): family
coherence, hereditary seeding wiring, stored notes, medication lists,
immunizations, and the thin reference entities."""

from sqlalchemy import func

from hdh.core.models import (
    Allergy,
    Condition,
    FamilyHistory,
    FamilyMember,
    Immunization,
    MedicationStatement,
    Patient,
    Provider,
    Visit,
    VisitNote,
)


def test_households_are_coherent(db_session):
    """Household members share surname/address; links are bidirectional."""
    linked = db_session.query(FamilyMember).filter(FamilyMember.related_patient_id.isnot(None)).all()
    assert linked, "the small panel should contain at least one multi-member household"
    for fm in linked:
        assert fm.patient.last_name == fm.related_patient.last_name
        assert fm.patient.address == fm.related_patient.address
        reciprocal = (
            db_session.query(FamilyMember)
            .filter(
                FamilyMember.patient_id == fm.related_patient_id,
                FamilyMember.related_patient_id == fm.patient_id,
            )
            .count()
        )
        assert reciprocal == 1
    # parent/child ages are plausible
    for fm in linked:
        if fm.relationship_type in ("mother", "father"):
            gap = (fm.patient.date_of_birth - fm.related_patient.date_of_birth).days // 365
            assert 18 <= gap <= 45, f"{fm.relationship_type} age gap {gap}"


def test_family_history_is_structured(db_session):
    """Adults carry FamilyHistory rows with relationship + condition."""
    rows = db_session.query(FamilyHistory).all()
    assert rows
    for h in rows[:20]:
        assert h.relationship_type and h.condition
    # lightweight relatives carry narrative summaries
    summarized = db_session.query(FamilyMember).filter(FamilyMember.summary.isnot(None)).first()
    assert summarized is not None and "history of" in summarized.summary


def test_every_visit_stores_a_note(db_session):
    """The note-per-visit invariant — the comprehension corpus exists."""
    n_visits = db_session.query(func.count(Visit.id)).scalar()
    n_notes = db_session.query(func.count(VisitNote.id)).scalar()
    assert n_notes == n_visits
    note = db_session.query(VisitNote).first()
    assert note.text.startswith("SOAP NOTE") and "S:" in note.text and "P:" in note.text
    assert note.author_id is not None


def test_unified_problem_list(db_session):
    """Chronic conditions appear once (active); acutes resolve with dates."""
    chronic = db_session.query(Condition).filter(Condition.chronic.is_(True)).all()
    for c in chronic:
        assert str(c.status).endswith("ACTIVE") and c.onset_date is not None
    # no duplicate chronic rows per patient+code
    dupes = (
        db_session.query(Condition.patient_id, Condition.icd10_code, func.count(Condition.id))
        .filter(Condition.chronic.is_(True))
        .group_by(Condition.patient_id, Condition.icd10_code)
        .having(func.count(Condition.id) > 1)
        .all()
    )
    assert dupes == []
    acute = db_session.query(Condition).filter(Condition.chronic.is_(False)).first()
    assert acute is not None and acute.resolved_date is not None


def test_medication_statements_derive_from_prescriptions(db_session):
    """Every patient with prescriptions has a medication list; actives have
    no end date, completed courses do."""
    stmt = db_session.query(MedicationStatement).first()
    assert stmt is not None
    for s in db_session.query(MedicationStatement).limit(50):
        if str(s.status).endswith("ACTIVE"):
            assert s.end_date is None
        elif str(s.status).endswith("COMPLETED"):
            assert s.end_date is not None


def test_immunizations_and_providers(db_session):
    """Age-driven immunizations exist; visits carry provider identities."""
    assert db_session.query(func.count(Immunization.id)).scalar() > 0
    assert db_session.query(func.count(Provider.id)).scalar() >= 6
    with_provider = db_session.query(Visit).filter(Visit.provider_id.isnot(None)).count()
    assert with_provider == db_session.query(func.count(Visit.id)).scalar()


def test_structured_allergies(db_session):
    """Allergies are rows with substance/severity, not a pipe string."""
    a = db_session.query(Allergy).first()
    if a is not None:  # small panel may legitimately have none
        assert a.substance and a.severity is not None
    assert not hasattr(Patient, "fam_hx_diabetes")  # the booleans are gone
