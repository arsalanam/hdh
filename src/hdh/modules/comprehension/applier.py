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
from datetime import timedelta
from typing import Any

from hdh.modules.comprehension.contracts import Assertion, AttributeKind, MentionType
from hdh.modules.comprehension.pipeline import (
    REVIEW_THRESHOLD,
    ComprehendedMention,
    ComprehendedNote,
)


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
    created: list[tuple[str, Any]] = field(default_factory=list)  # (entity, row) for the audit pass

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
    elif provider_id is not None and visit.provider_id is None:
        # reconciling into an existing unattributed visit: the note names
        # its author, so record it — but never overwrite an attribution
        # the chart already has
        visit.provider_id = provider_id
    result = ApplyResult(visit_id=visit.id, created_visit=created)

    _apply_conditions(session, patient, visit, note, result)
    _apply_medications(session, visit, note, result)
    _apply_vitals(session, visit, note, result)
    _apply_allergies(session, patient, note, result)
    _apply_requests(session, patient, visit, note, result)
    if dry_run:
        session.rollback()
    else:
        _audit_creations(session, result, provider_id)
        session.commit()
    return result


def _audit_creations(session, result: ApplyResult, provider_id: int | None) -> None:
    """Record how these entries arrived (design §7 Q2): comprehension's
    own writes belong in the chart's history, so `hdh chart history` can
    answer "who put this here?" with "the note, via the pipeline"."""
    from hdh.core.chartedit import record_creation
    from hdh.core.chartedit.contracts import Actor
    from hdh.core.models import EditSource, Provider

    if not result.created:
        return
    session.flush()
    name = "comprehension"
    if provider_id is not None:
        provider = session.get(Provider, provider_id)
        if provider is not None:
            name = provider.name
    actor = Actor(name=name, source=EditSource.PIPELINE, provider_id=provider_id)
    for entity, row in result.created:
        record_creation(session, actor, entity, row, reason=f"charted from note (visit #{result.visit_id})")


#: Control phrasing a note uses → whether the condition is controlled.
#: Longest key first when matching, so "not well controlled" cannot be read
#: as "well controlled" with a stray word in front of it.
_CONTROL_PHRASES: dict[str, bool] = {
    "not well controlled": False,
    "poorly controlled": False,
    "badly controlled": False,
    "uncontrolled": False,
    "not controlled": False,
    "out of control": False,
    "worsening": False,
    "deteriorating": False,
    "well controlled": True,
    "well-controlled": True,
    "well treated": True,
    "well managed": True,
    "controlled": True,
    "stable": True,
    "improving": True,
}

#: Words a control-qualified SNOMED concept has to actually contain before
#: we will accept it as a refinement (guard 3 of §10.0).
_CONTROL_WORDS = ("controlled", "control", "uncontrolled")


def _control_state(item: ComprehendedMention) -> bool | None:
    """Does the note say this condition is controlled? None = it does not say."""
    text = (_attr(item, AttributeKind.CONTROL) or "").strip().lower()
    if not text:
        return None
    for phrase in sorted(_CONTROL_PHRASES, key=len, reverse=True):
        if phrase in text:
            return _CONTROL_PHRASES[phrase]
    return None


def _control_refinement(session, base_code: str, base_display: str, controlled: bool):
    """A control-qualified SNOMED concept for this condition, or None.

    An ENRICHMENT, never a substitute (design §10.0). SNOMED's coverage is
    uneven — diabetes and asthma have control-qualified disorders,
    hypertension has separate findings that are not subtypes at all, and
    most conditions have nothing — so the flag carries the meaning and
    this only sharpens the code when the vocabulary allows.

    Three guards, because two are not enough: searching "uncontrolled
    essential hypertension" returns *Benign essential hypertension*, which
    is a real subtype, genuinely subsumed, and completely wrong.
    """
    from hdh.core.ontology import get_ontology_service

    prefix = "Well controlled" if controlled else "Uncontrolled"
    service = get_ontology_service("snomed_ct", session)
    hits = service.normalize(
        f"{prefix} {base_display}", {"semantic_tags": ["disorder", "finding"], "limit": 1}
    )
    if not hits:
        return None
    best = hits[0]
    if best.score < REVIEW_THRESHOLD:
        return None  # guard 1: not confident
    display = (best.concept.display or "").lower()
    if not any(word in display for word in _CONTROL_WORDS):
        return None  # guard 3: not actually about control
    if not service.subsumes(base_code, best.concept.code):
        return None  # guard 2: not a refinement of THIS condition
    return best.concept


def _update_control(session, row, item: ComprehendedMention, result: ApplyResult) -> None:
    """Carry a note's control assertion onto a problem the chart already has.

    Silent when the note says nothing about control: absence of a phrase is
    not an assertion that the condition is uncontrolled, so an unchanged flag
    is the correct outcome and must not be written as one.
    """
    from hdh.core.chartedit import record_update
    from hdh.core.chartedit.contracts import Actor
    from hdh.core.models import EditSource

    controlled = _control_state(item)
    if controlled is None or bool(row.controlled) == controlled:
        return
    before = row.controlled
    row.controlled = controlled
    session.flush()
    record_update(
        session,
        Actor(name="comprehension", source=EditSource.PIPELINE, provider_id=None),
        "Condition",
        row,
        {"controlled": (before, controlled)},
        reason="control state asserted by a note",
    )
    result.verdicts.append(
        Verdict(
            "updated",
            "condition",
            f"{item.mention.text!r}: controlled {before} → {controlled}",
        )
    )


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
            # Referencing the problem is not the same as learning nothing
            # about it. Control is the thing a follow-up note is FOR — "now
            # uncontrolled" is news about a problem the chart already has —
            # and writing it only on creation meant the flag could be set
            # once, on the day a problem was first charted, and never again.
            _update_control(session, existing[0], item, result)
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
        # What the note says about CONTROL. The flag is primary — it works
        # for every condition — and a control-qualified SNOMED concept
        # sharpens the code only where the vocabulary has one (§10.0).
        snomed_code, snomed_display = item.code.code, item.code.display
        controlled = _control_state(item)
        if controlled is not None:
            row.controlled = controlled
            refined = _control_refinement(session, item.code.code, item.code.display, controlled)
            if refined is not None:
                snomed_code, snomed_display = refined.code, refined.display
                row.description = refined.display
        if hasattr(row, "snomed_code") and hasattr(row, "snomed_display"):
            row.snomed_code = snomed_code
            row.snomed_display = snomed_display
        session.add(row)
        result.created.append(("Condition", row))
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
        prescription = Prescription(
            visit_id=visit.id,
            drug_name=drug,
            drug_class="",
            dose=_attr(item, AttributeKind.DOSE) or "",
            frequency=_attr(item, AttributeKind.FREQUENCY) or "",
            is_new="start" in status_word,
        )
        session.add(prescription)
        result.created.append(("Prescription", prescription))
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
        if item.mention.mention_type is not MentionType.LAB_VITAL:
            continue
        if item.code is None:
            # The surface did not resolve to a LOINC code. Losing it
            # silently would break the refuse-don't-guess contract — a
            # provider writing "B/P 152/94" would see the reading simply
            # vanish — so it goes to a human like any other unresolvable
            # entity. (The alias table is a documented placeholder until
            # the LOINC module lands; master design §12.)
            result.verdicts.append(
                Verdict(
                    "review",
                    "vitals",
                    f"{item.mention.text!r}: no LOINC code for this surface — "
                    "add an alias or map it manually",
                )
            )
            continue
        raw = _attr(item, AttributeKind.VALUE) or ""
        if item.code.code == "55284-4":
            match = _BP_RE.search(raw)
            if match:
                values["bp_systolic"], values["bp_diastolic"] = int(match.group(1)), int(match.group(2))
            continue
        target = _VITAL_COLUMNS.get(item.code.code)
        if target is None:
            # A coded lab rather than a vitals-panel measurement. There is
            # no LabResult reconciliation pass yet, so say so instead of
            # dropping it without trace.
            result.verdicts.append(
                Verdict(
                    "skipped", "lab", f"{item.mention.text!r} ({item.code.code}): labs are not charted yet"
                )
            )
            continue
        column, cast = target
        number = _NUM_RE.search(raw)
        if number:
            values[column] = cast(float(number.group(0)))
    if not values:
        return
    if visit.vitals is not None:
        result.verdicts.append(Verdict("confirmed", "vitals", "visit already has a vitals row"))
        return
    vital = Vital(visit_id=visit.id, **values)
    session.add(vital)
    result.created.append(("Vital", vital))
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
        allergy = Allergy(
            patient_id=patient.id,
            substance=substance,
            reaction=_attr(item, AttributeKind.REACTION),
            severity=severity,
            noted_date=note_date(note) or date_type.today(),
        )
        session.add(allergy)
        result.created.append(("Allergy", allergy))
        added_this_run.add(substance.lower())
        result.verdicts.append(Verdict("new", "allergy", substance))


def note_date(note: ComprehendedNote) -> date_type | None:
    """The note's own date when the header carries one."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", note.extraction.note_text[:80])
    return date_type.fromisoformat(match.group(1)) if match else None


# ── the fifth pass: what the plan ASKED FOR ──────────────────────────────
#
# The chart could record what happened and never what was requested, so a
# plan line was comprehended correctly and then dropped on the floor
# (design service-requests-and-interchange.md §5).
#
# Two sources feed it, because the extraction schema has no mention type
# for either of the last two rows of §5's table. A referral and a return
# visit are not clinical ENTITIES — they are instructions — so they are
# read from the plan text by rule, the same way segmentation and NegEx are.

_FOLLOW_UP_RE = re.compile(
    r"(?:follow[\s-]?up|return|rtc)\b[^.]{0,24}?\bin\s+(\d+)\s*(day|week|month|year)s?",
    re.IGNORECASE,
)
_REFERRAL_RE = re.compile(r"refer(?:ral)?\s+(?:to|for)\s+([A-Za-z][A-Za-z /&'-]{2,40})", re.IGNORECASE)
_INTERVAL_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

#: Which mention types become which kind of order when they appear in the
#: PLAN. The section is what makes this safe: the same LAB_VITAL type is a
#: RESULT in the objective section and a REQUEST in the plan.
_PLAN_ORDER_KINDS = {
    MentionType.MEDICATION: "MEDICATION",
    MentionType.LAB_VITAL: "LAB",
    MentionType.PROCEDURE: "PROCEDURE",
}

#: An order for something the note says the patient does NOT have, or
#: might have, is not an order.
_NOT_ORDERABLE = frozenset({Assertion.NEGATED, Assertion.HYPOTHETICAL, Assertion.FAMILY_HISTORY})


#: Status words that mean an order was PLACED. A note without SOAP
#: headers has no plan section to key on, and these are what it says
#: instead — "continued Metformin", "added Januvia", "asked for a repeat
#: HbA1c". Deliberately excludes words that describe a drug the patient is
#: merely ON: "taking", "on", "reports".
_ORDERING_WORDS = frozenset(
    {
        "start",
        "started",
        "starting",
        "begin",
        "began",
        "continue",
        "continued",
        "continuing",
        "add",
        "added",
        "increase",
        "increased",
        "decrease",
        "decreased",
        "titrate",
        "order",
        "ordered",
        "prescribe",
        "prescribed",
        "refill",
        "refilled",
        "repeat",
        "renew",
        "renewed",
    }
)


def _has_plan_section(note: ComprehendedNote) -> bool:
    from hdh.modules.comprehension.contracts import SectionKind

    return any(section.kind is SectionKind.PLAN for section in note.extraction.sections)


def _plan_text(note: ComprehendedNote) -> str:
    """The note's plan, or the whole note when it has no sections.

    Most real notes are prose: no "P:" header, so `segment()` returns one
    UNKNOWN section and the plan text is empty — which silently cost the
    referral and the return visit, because their regexes had nothing to
    read (design rxnorm §10 Scenario A). A note that HAS a plan still uses
    only its plan, so nothing changes for the structured case.
    """
    from hdh.modules.comprehension.contracts import SectionKind

    text = note.extraction.note_text
    parts = [
        text[section.span.start : section.span.end]
        for section in note.extraction.sections
        if section.kind is SectionKind.PLAN
    ]
    return " ".join(parts) if parts else text


def _in_plan(note: ComprehendedNote, item: ComprehendedMention) -> bool:
    """Is this mention something the note ORDERED?

    The section answers it when there is one, and that is the safer
    signal: the same LAB_VITAL is a result in the objective and a request
    in the plan.

    When a note has no sections at all, the status word answers instead —
    "continued", "added", "ordered" say what the section cannot. Without
    this, an unstructured note produces no orders whatsoever, which is a
    silent and total loss rather than a partial one.
    """
    from hdh.modules.comprehension.contracts import SectionKind

    if _has_plan_section(note):
        return note.extraction.section_of(item.mention).kind is SectionKind.PLAN
    status = (_attr(item, AttributeKind.STATUS_WORD) or "").strip().lower()
    return any(word in _ORDERING_WORDS for word in re.split(r"[^a-z]+", status) if word)


def _treated_condition_id(session, patient, note: ComprehendedNote, item: ComprehendedMention):
    """The TREATS target, persisted at last (design §2).

    Comprehension already derives "lisinopril FOR hypertension" and used to
    discard it after the FHIR export. Resolving it to the problem-list row
    is what makes an order answer "why".
    """
    from hdh.core.models import Condition

    from .contracts import RelationKind

    targets = [
        relation.target_id
        for relation in note.extraction.relations
        if relation.kind is RelationKind.TREATS and relation.source_id == item.mention.id
    ]
    # snomed_code is a schema-registry extension the ontology module adds,
    # so it is not always there — same guard the condition pass uses.
    if not hasattr(Condition, "snomed_code"):
        return None
    for target_id in targets:
        target = next((m for m in note.mentions if m.mention.id == target_id), None)
        if target is None or target.code is None:
            continue
        row = (
            session.query(Condition)
            .filter(
                Condition.patient_id == patient.id,
                Condition.snomed_code == target.code.code,
            )
            .first()
        )
        if row is not None:
            return row.id
    return None


def _existing_request(visit, kind_name: str, display: str):
    """Same order already on this visit? Asking twice is not two orders."""
    wanted = display.strip().lower()
    for request in visit.service_requests:
        if request.kind.name == kind_name and (request.display or "").strip().lower() == wanted:
            return request
    return None


def _new_request(patient, visit, kind_name: str, display: str, **fields):
    from hdh.core.models import RequestOrigin, RequestStatus, ServiceKind, ServiceRequest

    return ServiceRequest(
        patient_id=patient.id,
        visit_id=visit.id,
        requester_id=visit.provider_id,
        kind=ServiceKind[kind_name],
        # DRAFT, not ACTIVE: a comprehended order has not been released by
        # anyone yet. `hdh orders release` is the human act that sends it.
        status=RequestStatus.DRAFT,
        origin=RequestOrigin.COMPREHENSION,
        display=display,
        requested_date=visit.visit_date,
        **fields,
    )


def _apply_requests(session, patient, visit, note: ComprehendedNote, result: ApplyResult) -> None:
    """Turn the plan into orders (design §5).

    An UNCODED request is deliberately fine here, which is the one place
    this pass differs from the condition pass. A problem with no billing
    code cannot be charted faithfully, so it goes to review; but a request
    is real before anyone codes it — that is the point of ordering it — and
    its `display` is verbatim from the note, so nothing is being guessed.
    The LOINC and RxNorm modules fill the code in later (§2, §7).
    """
    plan = _plan_text(note)
    follow_up_days = _parse_follow_up(plan)
    due = visit.visit_date + timedelta(days=follow_up_days) if follow_up_days else None

    for item in note.mentions:
        kind_name = _PLAN_ORDER_KINDS.get(item.mention.mention_type)
        if kind_name is None or not _in_plan(note, item):
            continue
        if item.assertion.assertion in _NOT_ORDERABLE:
            result.verdicts.append(
                Verdict(
                    "skipped",
                    "request",
                    f"{item.mention.text!r}: {item.assertion.assertion.value} — not an order",
                )
            )
            continue
        display = item.mention.text.strip()
        if not display:
            continue
        if _existing_request(visit, kind_name, display):
            result.verdicts.append(
                Verdict("confirmed", "request", f"{display}: already ordered on this visit")
            )
            continue

        fields: dict[str, Any] = {}
        if item.code is not None and item.confidence >= REVIEW_THRESHOLD:
            fields["code_system"], fields["code"] = item.code.system, item.code.code
        if kind_name == "MEDICATION":
            # The verbatim direction line is what a pharmacy actually reads
            # (design §3), so keep the note's own words rather than a
            # reassembled dose/frequency string.
            fields["sig"] = _sig_for(item)
            fields["route"] = _attr(item, AttributeKind.ROUTE)
            fields["reason_condition_id"] = _treated_condition_id(session, patient, note, item)
        elif kind_name == "LAB" and due is not None:
            # "basic metabolic panel to be drawn before that visit" — the
            # follow-up is what "that visit" refers to (§5).
            fields["occurrence_date"] = due

        request = _new_request(patient, visit, kind_name, display, **fields)
        session.add(request)
        result.created.append(("ServiceRequest", request))
        coded = "" if fields.get("code") else " (uncoded)"
        result.verdicts.append(Verdict("new", "request", f"{kind_name.lower()}: {display}{coded}"))

    _apply_referrals(session, patient, visit, plan, result)
    _apply_follow_up(session, patient, visit, follow_up_days, due, result)


def _sig_for(item: ComprehendedMention) -> str:
    """The directions as written: name, then whatever qualified it."""
    parts = [item.mention.text.strip()]
    for kind in (AttributeKind.DOSE, AttributeKind.ROUTE, AttributeKind.FREQUENCY, AttributeKind.DURATION):
        value = _attr(item, kind)
        if value:
            parts.append(value)
    return " ".join(parts)


def _parse_follow_up(plan: str) -> int | None:
    """ "Follow up in 90 days" / "Return in 3 months" → days, or None.

    "Follow up as needed" deliberately yields None: PRN is the absence of
    a scheduled return, not a return with no date.
    """
    match = _FOLLOW_UP_RE.search(plan)
    if match is None:
        return None
    return int(match.group(1)) * _INTERVAL_DAYS[match.group(2).lower()]


def _apply_referrals(session, patient, visit, plan: str, result: ApplyResult) -> None:
    for match in _REFERRAL_RE.finditer(plan):
        target = match.group(1).strip().rstrip(".").strip()
        if not target:
            continue
        if _existing_request(visit, "REFERRAL", target):
            result.verdicts.append(Verdict("confirmed", "request", f"referral to {target}: already ordered"))
            continue
        request = _new_request(patient, visit, "REFERRAL", target, detail={"specialty": target})
        session.add(request)
        result.created.append(("ServiceRequest", request))
        result.verdicts.append(Verdict("new", "request", f"referral: {target}"))


def _apply_follow_up(session, patient, visit, days: int | None, due, result: ApplyResult) -> None:
    if days is None:
        return
    display = f"Follow-up visit in {days} days"
    if visit.follow_up_request is not None:
        result.verdicts.append(Verdict("confirmed", "request", f"{display}: already ordered on this visit"))
        return
    request = _new_request(patient, visit, "FOLLOW_UP", display, occurrence_date=due)
    session.add(request)
    result.created.append(("ServiceRequest", request))
    result.verdicts.append(Verdict("new", "request", display))
