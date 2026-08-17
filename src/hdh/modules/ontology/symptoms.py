"""Curated symptom-level ICD-10↔SNOMED coverage (issue #41).

Comprehension links a symptom mention to SNOMED correctly and then
cannot bill it: every `maps_to` edge we have is disease-centric, because
they are derived from the condition catalog and the catalog holds
diagnoses, not complaints. So *every* note mentioning a headache queues a
review item, and real signal drowns in noise.

This module closes that gap the only way the house rules allow — as
**explicit curation**, never inference. Each pairing below was verified
against the loaded ICD-10-CM and SNOMED CT catalogs (billable ICD leaf,
active SNOMED finding/disorder term); anything ambiguous was left out
rather than guessed, because the refuse-don't-guess posture is the
product, not a limitation to engineer away.

Provenance stays honest: these become `maps_to` edges under their own
``CURATED_SYMPTOM`` authority, rebuilt independently of the derived
tiers (design chart-maintenance.md §2.3) — re-running `hdh ontology tag`
can never silently wipe them, and they never masquerade as an official
crosswalk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, insert, select

SYMPTOM_AUTHORITY = "CURATED_SYMPTOM"


@dataclass(frozen=True)
class SymptomMapping:
    """One curated symptom pairing, with the reason it was accepted."""

    icd10_code: str
    snomed_code: str
    display: str
    note: str = ""


class MappingSource(Protocol):
    """Anything that can offer symptom-level ICD↔SNOMED pairings — a
    curated table today, a terminology service later."""

    def mappings(self) -> tuple[SymptomMapping, ...]:
        """Every pairing this source vouches for."""
        ...


# Verified 2026-08-16 against ICD-10-CM FY2026 + SNOMED CT US Edition
# 202603 as loaded in a dev database: ICD side billable, SNOMED side an
# active finding/disorder whose preferred term names the complaint.
CURATED_SYMPTOMS: tuple[SymptomMapping, ...] = (
    # ── constitutional ────────────────────────────────────────────────
    SymptomMapping("R50.9", "386661006", "Fever", "the commonest presenting complaint"),
    SymptomMapping("R53.83", "84229001", "Fatigue", "distinct from R53.1 weakness"),
    SymptomMapping("R53.1", "13791008", "Asthenia", "weakness, not tiredness"),
    SymptomMapping("R68.83", "43724002", "Chill", "chills without documented fever"),
    SymptomMapping("R61", "42984000", "Night sweats", "generalized hyperhidrosis"),
    SymptomMapping("R63.0", "79890006", "Loss of appetite", "SNOMED anorexia = the symptom"),
    SymptomMapping("R63.4", "262285001", "Weight decreased", "abnormal loss, not intentional"),
    SymptomMapping("R63.5", "262286000", "Weight increased", "abnormal gain"),
    SymptomMapping("R52", "22253000", "Pain", "unspecified site — last resort"),
    # ── neurologic ────────────────────────────────────────────────────
    SymptomMapping("R51.9", "25064002", "Headache", "unspecified headache"),
    SymptomMapping("R42", "404640003", "Dizziness", "covers giddiness"),
    SymptomMapping("R55", "271594007", "Syncope", "syncope and collapse"),
    SymptomMapping("R56.9", "91175000", "Seizure", "unspecified convulsions"),
    SymptomMapping("R25.1", "26079004", "Tremor", "unspecified tremor"),
    SymptomMapping("R20.2", "91019004", "Paresthesia", "tingling of skin"),
    SymptomMapping("R26.81", "22631008", "Unsteady when walking", "unsteadiness on feet"),
    SymptomMapping("R29.6", "161898004", "Falls", "repeated falls — a geriatric staple"),
    SymptomMapping("R41.0", "62476001", "Disorientated", "acute disorientation"),
    SymptomMapping("R41.3", "48167000", "Amnesia", "memory loss"),
    SymptomMapping("R47.02", "20301004", "Dysphasia", "language, not swallowing"),
    SymptomMapping("R43.0", "44169009", "Loss of sense of smell", "anosmia"),
    SymptomMapping("G47.00", "193462001", "Insomnia", "not an R code, but the symptom code"),
    # ── cardiorespiratory ─────────────────────────────────────────────
    SymptomMapping("R07.9", "29857009", "Chest pain", "unspecified chest pain"),
    SymptomMapping("R06.02", "267036007", "Dyspnea", "shortness of breath"),
    SymptomMapping("R06.2", "56018004", "Wheezing", ""),
    SymptomMapping("R05.9", "49727002", "Cough", "unspecified cough"),
    SymptomMapping("R00.2", "80313002", "Palpitations", ""),
    SymptomMapping("R00.0", "3424008", "Tachycardia", "unspecified"),
    SymptomMapping("R00.1", "48867003", "Bradycardia", "unspecified"),
    SymptomMapping("R23.0", "3415004", "Cyanosis", ""),
    SymptomMapping("R60.0", "274724004", "Localized edema", ""),
    SymptomMapping("R60.9", "267038008", "Edema", "unspecified"),
    SymptomMapping(
        "R03.0", "24184005", "Blood pressure above reference range", "elevated reading, no diagnosis"
    ),
    # ── gastrointestinal ──────────────────────────────────────────────
    SymptomMapping("R10.9", "21522001", "Abdominal pain", "unspecified site"),
    SymptomMapping("R11.0", "422587007", "Nausea", ""),
    SymptomMapping("R11.10", "422400008", "Vomiting", "unspecified"),
    SymptomMapping("R11.2", "16932000", "Nausea and vomiting", "the paired complaint"),
    SymptomMapping("R12", "16331000", "Heartburn", ""),
    SymptomMapping("R13.10", "40739000", "Dysphagia", "swallowing, not language"),
    SymptomMapping("R14.0", "41931001", "Distension of abdomen", "bloating"),
    SymptomMapping("R19.7", "62315008", "Diarrhea", "unspecified"),
    SymptomMapping("K59.00", "14760008", "Constipation", "not an R code, but the symptom code"),
    # ── genitourinary ─────────────────────────────────────────────────
    SymptomMapping("R30.0", "49650001", "Dysuria", ""),
    SymptomMapping("R31.9", "34436003", "Blood in urine", "hematuria, unspecified"),
    SymptomMapping("R35.0", "300471006", "Finding of frequency of urination", "urinary frequency"),
    SymptomMapping("R80.9", "29738008", "Proteinuria", "unspecified"),
    # ── musculoskeletal ───────────────────────────────────────────────
    SymptomMapping("M54.50", "279039007", "Low back pain", "unspecified"),
    SymptomMapping("M54.2", "81680005", "Neck pain", "cervicalgia"),
    SymptomMapping("M25.50", "57676002", "Pain of joint", "arthralgia, unspecified joint"),
    SymptomMapping("M79.10", "68962001", "Muscle pain", "myalgia — M79.1 is not billable"),
    # ── ENT / skin / other ────────────────────────────────────────────
    SymptomMapping("R07.0", "267102003", "Sore throat", "pain in throat"),
    SymptomMapping("R09.81", "68235000", "Nasal congestion", ""),
    SymptomMapping("R04.0", "249366005", "Bleeding from nose", "epistaxis"),
    SymptomMapping("R49.0", "50219008", "Hoarse", "dysphonia"),
    SymptomMapping("H92.09", "301354004", "Pain of ear", "otalgia, unspecified ear"),
    SymptomMapping("R21", "271807003", "Eruption", "rash and nonspecific skin eruption"),
    SymptomMapping("L29.9", "418290006", "Itching", "pruritus, unspecified"),
    SymptomMapping("R59.9", "30746006", "Lymphadenopathy", "general↔general; R59.0/R59.1 are site-specific"),
    SymptomMapping("R45.0", "424196004", "Feeling nervous", "nervousness"),
    SymptomMapping("R73.09", "80394007", "Hyperglycemia", "abnormal glucose without a diabetes diagnosis"),
)


class CuratedSymptomSource:
    """Implementation #1 of :class:`MappingSource`: the hand-curated table
    above, reviewed like code."""

    def mappings(self) -> tuple[SymptomMapping, ...]:
        """The curated pairings."""
        return CURATED_SYMPTOMS


def record_symptom_edges(session, source: MappingSource | None = None) -> int:
    """Materialize symptom pairings as ``maps_to`` edges where BOTH
    concepts exist in the shared tables.

    Rebuilds ONLY the ``CURATED_SYMPTOM`` authority, so the derived tiers
    (and any future official crosswalk) are untouched — and so this is
    safely re-runnable. ``source`` is injected; defaults to the curated
    table."""
    from hdh.core.models import Base

    active = source or CuratedSymptomSource()
    tables = Base.metadata.tables
    concepts_t, edges_t = tables["ontology_concepts"], tables["ontology_edges"]
    known = {
        row[0]
        for row in session.execute(
            select(concepts_t.c.id).where(concepts_t.c.ontology.in_(("icd10cm", "snomed_ct")))
        )
    }
    session.execute(
        delete(edges_t).where(edges_t.c.edge_type == "maps_to", edges_t.c.authority == SYMPTOM_AUTHORITY)
    )
    rows = [
        {
            "source_id": f"icd10cm:{mapping.icd10_code}",
            "target_id": f"snomed_ct:{mapping.snomed_code}",
            "edge_type": "maps_to",
            "authority": SYMPTOM_AUTHORITY,
            "confidence": 1.0,
            "properties": {"source": "curated_symptom", "note": mapping.note},
        }
        for mapping in active.mappings()
        if f"icd10cm:{mapping.icd10_code}" in known and f"snomed_ct:{mapping.snomed_code}" in known
    ]
    if rows:
        session.execute(insert(edges_t), rows)
    session.commit()
    return len(rows)
