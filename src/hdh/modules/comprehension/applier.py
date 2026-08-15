"""Stage 6, half one: the internal chart applier (design §10.3).

The comprehended note updates hdh DIRECTLY through the ORM — the FHIR
Bundle is the interchange artifact (assemble.py), never the update
mechanism (review decision §13 Q5). Every entity gets a reconciliation
**verdict** before anything is written:

  new        — not on the chart → row created
  confirmed  — already on the chart and the note agrees → referenced, never duplicated
  review     — the note and the chart disagree, or the entity cannot be
               applied faithfully (e.g. a problem with no ICD billing
               mapping) → NOTHING is written; a checkpoint, not a guess

All writes happen in one transaction; a review verdict never blocks the
clean entries but does mark the record ``needs_review``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Any

from hdh.modules.comprehension.contracts import Assertion, AttributeKind, MentionType
from hdh.modules.comprehension.pipeline import ComprehendedMention, ComprehendedNote


@dataclass(frozen=True)
class Verdict:
    """One reconciliation decision, explained."""

    action: str  # "new" | "confirmed" | "review" | "skipped"
    kind: str  # "condition" | "medication" | "vitals" | "lab" | "allergy" | "visit"
    detail: str


@dataclass(frozen=True)
class VisitTarget:
    """Where the note lands: an existing visit to reconcile against, or
    the parameters of the NEW visit to create (the fluent-note path)."""

    visit: Any | None = None  # an existing Visit ORM row
    visit_date: date_type | None = None
    provider_id: int | None = None


@dataclass
class ApplyResult:
    """Everything the applier did (and refused to do)."""

    visit_id: int
    created_visit: bool
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return any(v.action == "review" for v in self.verdicts)


def _attr(item: ComprehendedMention, kind: AttributeKind) -> str | None:
    for attribute in item.mention.attributes:
        if attribute.kind is kind:
            return attribute.text
    return None


def _icd_for_snomed(session, snomed_code: str) -> str | None:
    """The billing view: reverse maps_to lookup (icd10cm → snomed edges
    recorded by `hdh ontology tag`)."""
    from sqlalchemy import select

    from hdh.core.models import Base

    edges_t = Base.metadata.tables["ontology_edges"]
    row = session.execute(
        select(edges_t.c.source_id).where(
            edges_t.c.edge_type == "maps_to", edges_t.c.target_id == f"snomed_ct:{snomed_code}"
        )
    ).first()
    return row.source_id.split(":", 1)[1] if row else None


def apply_to_chart(
    session,
    patient,
    note: ComprehendedNote,
    target: VisitTarget | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply a comprehended note to the patient's chart.

    ``target.visit`` reconciles against an existing encounter; without
    one a NEW Visit is created (the fluent-note path) dated
    ``target.visit_date`` (default today) and attributed to
    ``target.provider_id``. ``dry_run`` computes every verdict and then
    rolls the whole transaction back — repeatable testing against an
    unchanged chart."""
    from hdh.core.models import Visit, VisitType

    target = target or VisitTarget()
    visit, visit_date, provider_id = target.visit, target.visit_date, target.provider_id
    created = False
    if visit is None:
        problem = next((m for m in note.mentions if m.mention.mention_type is MentionType.PROBLEM), None)
        visit = Visit(
            patient_id=patient.id,
            visit_date=visit_date or date_type.today(),
            visit_type=VisitType.FOLLOW_UP,
            chief_complaint=problem.mention.text if problem else "Note-derived encounter",
            provider_id=provider_id,
        )
        session.add(visit)
        session.flush()
        created = True
    result = ApplyResult(visit_id=visit.id, created_visit=created)

    _apply_conditions(session, patient, visit, note, result)
    _apply_medications(session, visit, note, result)
    _apply_vitals(session, visit, note, result)
    _apply_allergies(session, patient, note, result)
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result


def _apply_conditions(session, patient, visit, note: ComprehendedNote, result: ApplyResult) -> None:
    from hdh.core.models import Condition, ConditionStatus

    added_this_run: set[str] = set()
    for item in note.mentions:
        if item.mention.mention_type is not MentionType.PROBLEM:
            continue
        assertion = item.assertion.assertion
        if assertion in (Assertion.NEGATED, Assertion.FAMILY_HISTORY, Assertion.HYPOTHETICAL):
            result.verdicts.append(
                Verdict("skipped", "condition", f"{item.mention.text!r}: assertion {assertion.value}")
            )
            continue
        if item.code is None or item.code.system != "snomed_ct":
            result.verdicts.append(
                Verdict("review", "condition", f"{item.mention.text!r}: no SNOMED code — cannot apply")
            )
            continue
        if item.code.code in added_this_run:
            result.verdicts.append(
                Verdict("confirmed", "condition", f"{item.mention.text!r}: already applied from this note")
            )
            continue
        existing = [
            c
            for c in patient.conditions
            if (getattr(c, "snomed_code", None) == item.code.code)
            or c.description.lower() in item.mention.text.lower()
            or item.mention.text.lower() in c.description.lower()
        ]
        if existing:
            result.verdicts.append(
                Verdict(
                    "confirmed",
                    "condition",
                    f"{item.mention.text!r} ≡ chart {existing[0].icd10_code} — referenced, not duplicated",
                )
            )
            continue
        icd10 = _icd_for_snomed(session, item.code.code)
        if icd10 is None:
            result.verdicts.append(
                Verdict(
                    "review",
                    "condition",
                    f"{item.mention.text!r} (snomed {item.code.code}): no ICD billing mapping "
                    "in maps_to — run `hdh ontology tag` or map manually",
                )
            )
            continue
        row = Condition(
            patient_id=patient.id,
            visit_id=visit.id,
            icd10_code=icd10,
            description=item.code.display,
            chronic=False,
            status=ConditionStatus.ACTIVE,
            onset_date=visit.visit_date,
        )
        if hasattr(row, "snomed_code") and hasattr(row, "snomed_display"):
            row.snomed_code = item.code.code
            row.snomed_display = item.code.display
        session.add(row)
        added_this_run.add(item.code.code)
        result.verdicts.append(
            Verdict("new", "condition", f"{item.mention.text!r} → {icd10} / snomed {item.code.code}")
        )


def _apply_medications(session, visit, note: ComprehendedNote, result: ApplyResult) -> None:
    from hdh.core.models import Prescription

    added_this_run: set[str] = set()
    for item in note.mentions:
        if item.mention.mention_type is not MentionType.MEDICATION:
            continue
        drug = item.code.display if item.code else item.mention.text
        existing = [rx for rx in visit.prescriptions if rx.drug_name.lower().startswith(drug.lower())]
        if drug.lower() in added_this_run:
            result.verdicts.append(
                Verdict("confirmed", "medication", f"{drug}: already applied from this note")
            )
            continue
        if existing:
            result.verdicts.append(Verdict("confirmed", "medication", f"{drug}: already on this visit"))
            continue
        status_word = (_attr(item, AttributeKind.STATUS_WORD) or "").lower()
        session.add(
            Prescription(
                visit_id=visit.id,
                drug_name=drug,
                drug_class="",
                dose=_attr(item, AttributeKind.DOSE) or "",
                frequency=_attr(item, AttributeKind.FREQUENCY) or "",
                is_new="start" in status_word,
            )
        )
        added_this_run.add(drug.lower())
        result.verdicts.append(
            Verdict("new", "medication", f"{drug} {_attr(item, AttributeKind.DOSE) or ''}".strip())
        )


_BP_RE = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# LOINC → Vital column (+int coercion)
_VITAL_COLUMNS = {
    "8867-4": ("heart_rate", int),
    "9279-1": ("respiratory_rate", int),
    "8310-5": ("temperature_f", float),
    "59408-5": ("oxygen_sat", int),
    "29463-7": ("weight_kg", float),
    "8302-2": ("height_cm", float),
    "39156-5": ("bmi", float),
    "72514-3": ("pain_scale", int),
}


def _apply_vitals(session, visit, note: ComprehendedNote, result: ApplyResult) -> None:
    from hdh.core.models import Vital

    values: dict[str, object] = {}
    for item in note.mentions:
        if item.mention.mention_type is not MentionType.LAB_VITAL or item.code is None:
            continue
        raw = _attr(item, AttributeKind.VALUE) or ""
        if item.code.code == "55284-4":
            match = _BP_RE.search(raw)
            if match:
                values["bp_systolic"], values["bp_diastolic"] = int(match.group(1)), int(match.group(2))
            continue
        target = _VITAL_COLUMNS.get(item.code.code)
        if target is None:
            continue  # a lab, not a vital — labs reconcile against LabResult rows in a later pass
        column, cast = target
        number = _NUM_RE.search(raw)
        if number:
            values[column] = cast(float(number.group(0)))
    if not values:
        return
    if visit.vitals is not None:
        result.verdicts.append(Verdict("confirmed", "vitals", "visit already has a vitals row"))
        return
    session.add(Vital(visit_id=visit.id, **values))
    result.verdicts.append(Verdict("new", "vitals", ", ".join(sorted(values))))


def _apply_allergies(session, patient, note: ComprehendedNote, result: ApplyResult) -> None:
    from hdh.core.models import Allergy, AllergySeverity

    added_this_run: set[str] = set()
    for item in note.mentions:
        if item.mention.mention_type is not MentionType.ALLERGY:
            continue
        substance = item.mention.text
        if substance.lower() in added_this_run:
            result.verdicts.append(
                Verdict("confirmed", "allergy", f"{substance}: already applied from this note")
            )
            continue
        if any(a.substance.lower() == substance.lower() for a in patient.allergies):
            result.verdicts.append(Verdict("confirmed", "allergy", f"{substance}: already charted"))
            continue
        severity_text = (_attr(item, AttributeKind.SEVERITY) or "").lower()
        severity = next((s for s in AllergySeverity if s.value in severity_text), None)
        session.add(
            Allergy(
                patient_id=patient.id,
                substance=substance,
                reaction=_attr(item, AttributeKind.REACTION),
                severity=severity,
                noted_date=note_date(note) or date_type.today(),
            )
        )
        added_this_run.add(substance.lower())
        result.verdicts.append(Verdict("new", "allergy", substance))


def note_date(note: ComprehendedNote) -> date_type | None:
    """The note's own date when the header carries one."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", note.extraction.note_text[:80])
    return date_type.fromisoformat(match.group(1)) if match else None
