"""Curated symptom coverage (issue #41).

The table itself is data, so it gets a data test; the writer is
behavior, so it gets DI'd fakes; and the point of the whole exercise —
a complaint that used to queue for review now charts — gets an
end-to-end test through the comprehension applier.

The synthetic SNOMED fixture stands in for the licensed catalog, and one
hand-inserted ICD concept row stands in for the ICD-10-CM catalog: the
edge writer only cares that both concepts resolve."""

import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from hdh.core.models import Patient, Sex, get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.ontology.symptoms import (
    CURATED_SYMPTOMS,
    SYMPTOM_AUTHORITY,
    CuratedSymptomSource,
    SymptomMapping,
    record_symptom_edges,
)

SNOMED_FIXTURES = Path(__file__).parent / "fixtures" / "snomed"
sys.path.insert(0, str(SNOMED_FIXTURES))
import fixture_ids as fx  # noqa: E402

COMPLAINT_ICD = "R99.9"  # stands in for a curated symptom code in fixtures


class _FixtureSymptoms:
    """A MappingSource over concepts the fixture actually has."""

    def mappings(self) -> tuple[SymptomMapping, ...]:
        return (SymptomMapping(COMPLAINT_ICD, fx.BLORBITIS, "Blorbitis", "test pairing"),)


@pytest.fixture()
def world(tmp_path):
    from hdh.core.models import Base
    from hdh.modules.snomed.loader import run_load

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "symptoms.db"))
    session = get_session(engine)
    run_load(session, SNOMED_FIXTURES)
    # the ICD side: one concept row, standing in for the loaded catalog
    session.execute(
        insert(Base.metadata.tables["ontology_concepts"]),
        [
            {
                "id": f"icd10cm:{COMPLAINT_ICD}",
                "ontology": "icd10cm",
                "code": COMPLAINT_ICD,
                "kind": "leaf",
                "display": "Synthetic complaint",
                "is_billable": True,
            }
        ],
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _edges(session, authority: str | None = None) -> list:
    from hdh.core.models import Base

    edges_t = Base.metadata.tables["ontology_edges"]
    query = select(edges_t.c.source_id, edges_t.c.target_id, edges_t.c.authority).where(
        edges_t.c.edge_type == "maps_to"
    )
    if authority:
        query = query.where(edges_t.c.authority == authority)
    return session.execute(query).all()


# ── the curated table as data ────────────────────────────────────────


def test_curated_table_is_wellformed():
    """Every pairing is one-to-one and fully specified — a duplicate on
    either side would make the billing view ambiguous."""
    icd_codes = [m.icd10_code for m in CURATED_SYMPTOMS]
    snomed_codes = [m.snomed_code for m in CURATED_SYMPTOMS]
    assert len(icd_codes) == len(set(icd_codes)), "duplicate ICD-10 code"
    assert len(snomed_codes) == len(set(snomed_codes)), "duplicate SNOMED concept"
    assert len(CURATED_SYMPTOMS) >= 50, "the curated set should cover the primary-care staples"
    for mapping in CURATED_SYMPTOMS:
        assert mapping.icd10_code and mapping.display
        assert mapping.snomed_code.isdigit(), f"{mapping.icd10_code}: SNOMED ids are numeric"
    with pytest.raises(AttributeError):  # frozen contract
        CURATED_SYMPTOMS[0].icd10_code = "X00"  # type: ignore[misc]


def test_curated_source_is_the_default_implementation():
    assert CuratedSymptomSource().mappings() == CURATED_SYMPTOMS


# ── the edge writer as behavior ──────────────────────────────────────


def test_edges_written_only_for_resolvable_pairs_and_idempotent(world):
    assert record_symptom_edges(world, _FixtureSymptoms()) == 1
    written = _edges(world, SYMPTOM_AUTHORITY)
    assert written == [(f"icd10cm:{COMPLAINT_ICD}", f"snomed_ct:{fx.BLORBITIS}", SYMPTOM_AUTHORITY)]

    # re-running rebuilds this tier in place — never accumulates
    assert record_symptom_edges(world, _FixtureSymptoms()) == 1
    assert len(_edges(world, SYMPTOM_AUTHORITY)) == 1

    # the real curated table resolves nothing here (no ICD/SNOMED catalog)
    assert record_symptom_edges(world) == 0


def test_symptom_rebuild_never_touches_other_authorities(world):
    from hdh.core.models import Base

    record_symptom_edges(world, _FixtureSymptoms())
    world.execute(
        insert(Base.metadata.tables["ontology_edges"]),
        [
            {
                "source_id": f"icd10cm:{COMPLAINT_ICD}",
                "target_id": f"snomed_ct:{fx.CHRONIC_BLORBITIS}",
                "edge_type": "maps_to",
                "authority": "DERIVED_NORMALIZE",
                "confidence": 0.7,
                "properties": {},
            }
        ],
    )
    world.commit()
    record_symptom_edges(world, _FixtureSymptoms())  # rebuild our tier only
    assert ("DERIVED_NORMALIZE",) in [(row.authority,) for row in _edges(world)]
    assert len(_edges(world, SYMPTOM_AUTHORITY)) == 1


# ── the point of the exercise ────────────────────────────────────────


def test_complaint_charts_instead_of_queueing_for_review(world):
    """Before coverage the applier refuses (no billing mapping); after it,
    the same note charts the complaint."""
    from hdh.modules.comprehension.applier import VisitTarget, apply_to_chart
    from hdh.modules.comprehension.comprehend import comprehend_text
    from hdh.modules.comprehension.extract import stub_extractor
    from hdh.modules.comprehension.pipeline import comprehend_note

    note = "Patient reports blorbitis this week."
    raw = {"mentions": [{"type": "problem", "text": "blorbitis", "occurrence": 1, "attributes": []}]}
    patient = Patient(
        mrn="MRN00SYMPTM",
        first_name="Comp",
        last_name="Laint",
        date_of_birth=date(1980, 2, 2),
        sex=Sex.FEMALE,
    )
    world.add(patient)
    world.commit()

    def apply_once():
        comprehended = comprehend_note(world, comprehend_text(note, stub_extractor(raw)))
        return apply_to_chart(world, patient, comprehended, target=VisitTarget(visit_date=date(2026, 3, 3)))

    before = apply_once()
    assert [v.action for v in before.verdicts] == ["review"]
    assert "no ICD billing mapping" in before.verdicts[0].detail

    record_symptom_edges(world, _FixtureSymptoms())
    after = apply_once()
    assert [v.action for v in after.verdicts] == ["new"]
    assert COMPLAINT_ICD in after.verdicts[0].detail
    assert not after.needs_review
