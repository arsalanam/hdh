"""Node 1, intake: the chart, compacted into what a plan needs.

Design §7. This is the only node that reads the whole chart, and it is
deliberately narrow about what it keeps: everything here becomes prompt
context downstream, so a field that no rule and no retrieval query uses is
tokens spent for nothing.

Nothing in this module calls an LLM. Intake and stratify are the
"deterministic first" half of §7, and they stay that way — a flag that
fires from a rule can be explained; a flag that fires from a model cannot
be re-derived tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from hdh.core.medications import ONGOING_WINDOW_DAYS, is_current_row

#: Drug classes that lower glucose, for the med-safety rules. Matched
#: case-insensitively against `Prescription.drug_class`, which the
#: condition packs author (`Sulfonylurea`, `Biguanide`, `DPP-4 inhibitor`).
GLUCOSE_LOWERING = ("sulfonylurea", "biguanide", "dpp-4", "sglt2", "glp-1", "insulin")

#: Classes whose harm depends on someone noticing it in time.
DELAYED_RESCUE_CLASSES = ("sulfonylurea", "insulin", "anticoagulant", "opioid", "benzodiazepine")

#: How far back a prescription still counts as an active medication.
#:
#: Re-exported from `hdh.core.medications`, which owns the rule. It used to
#: be restated here, and a restated constant is how `careplan` and
#: `caregaps` came to agree on the window while disagreeing about whether a
#: finished course counts at all (#115).
MEDICATION_WINDOW_DAYS = ONGOING_WINDOW_DAYS


@dataclass(frozen=True)
class MedicationView:
    """One active medication, as a plan needs to see it."""

    name: str
    drug_class: str
    dose: str
    started: date | None

    def is_class(self, *needles: str) -> bool:
        """Case-insensitive substring match on the class."""
        lowered = (self.drug_class or "").lower()
        return any(n in lowered for n in needles)


@dataclass(frozen=True)
class ProblemView:
    """One chronic problem, as a plan needs to see it."""

    icd10: str
    description: str
    controlled: bool | None
    onset: date | None


@dataclass(frozen=True)
class SocialView:
    """What the chart can say about circumstances, and how it knows.

    ``lives_alone`` is ``None`` when the chart cannot say — which is not the
    same as ``False``. A medication risk that depends on someone being
    nearby is understated by assuming company, so an unknown is carried
    forward as an unknown and the rules decline rather than guess.
    """

    lives_alone: bool | None
    lives_alone_basis: str
    smoker: bool | None
    marital_status: str | None


@dataclass(frozen=True)
class CarePlanContext:
    """The compacted chart a plan is generated from."""

    mrn: str
    age: int
    sex: str
    as_of: date | None = None
    problems: tuple[ProblemView, ...] = ()
    medications: tuple[MedicationView, ...] = ()
    social: SocialView | None = None
    risk_score: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # See the note in `generate.ConcernDraft`: a checkpoint round-trip
        # returns these as lists, and the whole chart would quietly change
        # shape on resume.
        for name in ("problems", "medications", "notes"):
            object.__setattr__(self, name, tuple(getattr(self, name) or ()))

    def medications_in_class(self, *needles: str) -> tuple[MedicationView, ...]:
        """Active medications whose class matches any of ``needles``."""
        return tuple(m for m in self.medications if m.is_class(*needles))

    @property
    def uncontrolled(self) -> tuple[ProblemView, ...]:
        """Chronic problems the chart says are not controlled."""
        return tuple(p for p in self.problems if p.controlled is False)


def _lives_alone(session, patient) -> tuple[bool | None, str]:
    """Derive whether the patient lives alone, and say how.

    hdh generates patients as households and links co-resident members with
    ``FamilyMember.related_patient_id``; a single-person household produces
    none. That makes the inference sound *here* and worth labelling as an
    inference, because in a real chart living arrangements are something
    somebody asks about and records, not something derivable from a table.
    """
    from hdh.core.models import FamilyMember

    linked = (
        session.query(FamilyMember)
        .filter(
            FamilyMember.patient_id == patient.id,
            FamilyMember.related_patient_id.isnot(None),
        )
        .count()
    )
    if linked:
        return False, f"{linked} co-resident household member(s) recorded"
    return True, "no co-resident household members recorded"


def _risk_score(session, mrn: str) -> float | None:
    """The risk module's score, when that module is installed and trained.

    Optional by design: the plan is weaker without it, not broken, and a
    module that is not installed must never be an error (§7).
    """
    try:
        from hdh.modules.risk import model as risk_model

        rows = risk_model.score(session, mrn=mrn, top=1)
    except (ImportError, FileNotFoundError):
        return None
    except Exception:  # noqa: BLE001 — a scoring failure must not stop a plan
        return None
    if not rows:
        return None
    value = rows[0].get("risk_score") if isinstance(rows[0], dict) else None
    return float(value) if value is not None else None


def build_context(session, patient, as_of: date | None = None) -> CarePlanContext:
    """Node 1: read the chart, keep what a plan uses.

    ``as_of`` defaults to the dataset's latest visit rather than today —
    the same anchor `caregaps` uses, because a synthetic chart generated
    last year is not stale, it is simply dated.
    """
    from hdh.modules.caregaps import reference_date

    as_of = as_of or reference_date(session)
    problems = tuple(
        ProblemView(
            icd10=condition.icd10_code,
            description=condition.description,
            controlled=condition.controlled,
            onset=condition.onset_date,
        )
        for condition in patient.conditions
        if condition.chronic
    )

    seen: set[str] = set()
    medications: list[MedicationView] = []
    for visit in sorted(patient.visits, key=lambda v: v.visit_date, reverse=True):
        for rx in visit.prescriptions:
            # Per prescription, not per visit. A course ends when its
            # duration runs out — a five-day antibiotic is not an active
            # medication eleven months later (#115) — and a long course
            # written before the window can still be running, which a
            # visit-level date filter would have dropped.
            if not is_current_row(rx, as_of, started=visit.visit_date):
                continue
            key = (rx.drug_name or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            medications.append(
                MedicationView(
                    name=rx.drug_name,
                    drug_class=rx.drug_class or "",
                    dose=rx.dose or "",
                    started=visit.visit_date,
                )
            )

    alone, basis = _lives_alone(session, patient)
    return CarePlanContext(
        mrn=patient.mrn,
        age=patient.age,
        as_of=as_of,
        sex=str(patient.sex).split(".")[-1],
        problems=problems,
        medications=tuple(medications),
        social=SocialView(
            lives_alone=alone,
            lives_alone_basis=basis,
            smoker=patient.smoker,
            marital_status=patient.marital_status,
        ),
        risk_score=_risk_score(session, patient.mrn),
    )
