"""Node 2, stratify: deterministic flags, no LLM.

Design §7. Rules, not judgement — every flag here can be re-derived
tomorrow from the same chart, explained to a clinician who disagrees, and
tested. That is the reason this node exists before the generating ones
rather than being folded into them.

**Each rule cites a corpus document rather than restating it.** The rule
decides *whether* it fires; the corpus says *why it matters*, in prose the
plan can quote and a reader can check. Splitting them that way means the
explanation can be corrected without touching code, and a rule can never
drift from the reasoning it claims — there is a test that every citation
resolves to a document that exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from hdh.modules.careplan.context import (
    DELAYED_RESCUE_CLASSES,
    GLUCOSE_LOWERING,
    CarePlanContext,
)

#: Distinct active drugs at or above which polypharmacy is worth raising.
POLYPHARMACY_THRESHOLD = 5

#: Ages the med-safety rules treat as older-adult. Two, because the
#: thresholds in this area genuinely differ: risk from hypoglycaemia rises
#: from around 65, while deintensification is usually discussed later.
OLDER_ADULT = 65
DEINTENSIFICATION_AGE = 75


@dataclass(frozen=True)
class RiskFlag:
    """One deterministic finding, with what triggered it and what explains it."""

    rule_id: str
    kind: str  # medication_safety | sdoh | disease_control | burden
    statement: str
    basis: str  # what in THIS chart made it fire
    cites: str  # "corpus/doc_id" — the prose that explains why it matters

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "statement": self.statement,
            "basis": self.basis,
            "cites": self.cites,
        }


@dataclass(frozen=True)
class SafetyRule:
    """A rule: when it fires, what it says, and what explains it."""

    rule_id: str
    kind: str
    cites: str
    fires: Callable[[CarePlanContext], str | None]
    statement: str

    def evaluate(self, context: CarePlanContext) -> RiskFlag | None:
        """The flag, or None. ``fires`` returns the basis text or None."""
        basis = self.fires(context)
        if basis is None:
            return None
        return RiskFlag(
            rule_id=self.rule_id,
            kind=self.kind,
            statement=self.statement,
            basis=basis,
            cites=self.cites,
        )


# ── the rules ────────────────────────────────────────────────────────────


def _sulfonylurea_in_older_adult(context: CarePlanContext) -> str | None:
    if context.age < OLDER_ADULT:
        return None
    drugs = context.medications_in_class("sulfonylurea")
    if not drugs:
        return None
    return f"{drugs[0].name} at age {context.age}"


def _delayed_rescue_living_alone(context: CarePlanContext) -> str | None:
    """Amplifies a medication risk when nobody is there to notice it.

    Declines on an unknown. If the chart cannot say whether the patient
    lives alone, this does not fire — but neither does it record that the
    risk is absent, which is the reason ``lives_alone`` is tri-state rather
    than a boolean defaulting to False.
    """
    social = context.social
    if social is None or social.lives_alone is not True:
        return None
    drugs = context.medications_in_class(*DELAYED_RESCUE_CLASSES)
    if not drugs:
        return None
    names = ", ".join(d.name for d in drugs)
    return f"{names}; {social.lives_alone_basis}"


def _deintensification_candidate(context: CarePlanContext) -> str | None:
    if context.age < DEINTENSIFICATION_AGE:
        return None
    drugs = context.medications_in_class(*GLUCOSE_LOWERING)
    if not drugs:
        return None
    return f"{len(drugs)} glucose-lowering agent(s) at age {context.age}"


def _polypharmacy(context: CarePlanContext) -> str | None:
    count = len(context.medications)
    if count < POLYPHARMACY_THRESHOLD:
        return None
    return f"{count} distinct active medications"


def _uncontrolled_chronic(context: CarePlanContext) -> str | None:
    problems = context.uncontrolled
    if not problems:
        return None
    return ", ".join(f"{p.description} ({p.icd10})" for p in problems)


#: Classes where a second concurrent agent is a prescribing question rather
#: than normal practice.
#:
#: Deliberately a list rather than "any repeated class". Antibiotics repeat
#: because courses follow one another, and dual antiplatelet therapy is
#: standard after a stent — flagging either would train a reader to ignore
#: the flag, which costs more than the rule is worth. The generator makes
#: the same exemption for antiplatelets.
DUPLICATE_WATCH: tuple[str, ...] = (
    "nsaid",
    "statin",
    "ppi",
    "ssri",
    "anticoagulant",
    "sulfonylurea",
    "biguanide",
    "ace inhibitor",
    "arb",
    "benzodiazepine",
    "anxiolytic",
)

#: What raises the bleeding risk of an NSAID from a caution to a hazard.
BLEEDING_RISK_CLASSES: tuple[str, ...] = ("anticoagulant",)

#: ICD-10 prefixes where an NSAID is a problem in its own right. CKD stages
#: 3-5 and heart failure; N18.1/N18.2 are deliberately absent because the
#: concern is meaningful impairment rather than any CKD code at all.
NSAID_UNSAFE_ICD: tuple[str, ...] = ("N18.3", "N18.4", "N18.5", "N18.6", "N18.9", "I50")


def _class_family(drug_class: str | None) -> str:
    """The family a class belongs to, for comparing two drugs.

    Needed because the formulary spells one family several ways: `NSAID` and
    `COX-2 NSAID`, `Statin` and `Statin (high-intensity)`,
    `Anticoagulant (DOAC)` and `Anticoagulant (VKA)`. Comparing the strings
    exactly makes them different classes, and a patient ends up on two of
    something with nothing noticing — which is exactly how the two NSAIDs in
    #102 got past both the generator's guard and every rule here.
    """
    lowered = (drug_class or "").lower()
    for family in DUPLICATE_WATCH:
        if family in lowered:
            return family
    return lowered.split("(")[0].strip()


def _nsaid_with_bleeding_risk(context: CarePlanContext) -> str | None:
    """An NSAID alongside something that already impairs haemostasis."""
    nsaids = context.medications_in_class("nsaid")
    if not nsaids:
        return None
    risky = context.medications_in_class(*BLEEDING_RISK_CLASSES)
    if not risky:
        return None
    # Both drugs are named. "A drug interaction was found" is not something
    # a clinician can act on or disagree with.
    return (
        f"{', '.join(d.name for d in nsaids)} with {', '.join(f'{d.name} ({d.drug_class})' for d in risky)}"
    )


def _duplicate_class_therapy(context: CarePlanContext) -> str | None:
    """Two active drugs in one family, where that is not standard practice."""
    families: dict[str, list[str]] = {}
    for medication in context.medications:
        family = _class_family(medication.drug_class)
        if family in DUPLICATE_WATCH:
            families.setdefault(family, []).append(medication.name)

    duplicated = {family: names for family, names in families.items() if len(set(names)) > 1}
    if not duplicated:
        return None
    return "; ".join(
        f"{family}: {', '.join(sorted(set(names)))}" for family, names in sorted(duplicated.items())
    )


def _nsaid_in_renal_or_cardiac_impairment(context: CarePlanContext) -> str | None:
    """An NSAID where the kidneys or the heart already cannot absorb it."""
    nsaids = context.medications_in_class("nsaid")
    if not nsaids:
        return None
    problems = [
        problem
        for problem in context.problems
        if any((problem.icd10 or "").startswith(code) for code in NSAID_UNSAFE_ICD)
    ]
    if not problems:
        return None
    return (
        f"{', '.join(d.name for d in nsaids)} with "
        f"{', '.join(f'{p.description} ({p.icd10})' for p in problems)}"
    )


RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        rule_id="nsaid-with-anticoagulant",
        kind="medication_safety",
        cites="med_safety/nsaid-bleeding-risk",
        fires=_nsaid_with_bleeding_risk,
        statement="An NSAID is prescribed alongside an anticoagulant — bleeding risk",
    ),
    SafetyRule(
        rule_id="duplicate-class-therapy",
        kind="medication_safety",
        cites="med_safety/duplicate-class-therapy",
        fires=_duplicate_class_therapy,
        statement="Two active medications in the same class, with no added benefit",
    ),
    SafetyRule(
        rule_id="nsaid-in-renal-or-cardiac-impairment",
        kind="medication_safety",
        cites="med_safety/nsaid-in-ckd-and-heart-failure",
        fires=_nsaid_in_renal_or_cardiac_impairment,
        statement="An NSAID is prescribed with impaired renal function or heart failure",
    ),
    SafetyRule(
        rule_id="sulfonylurea-in-older-adult",
        kind="medication_safety",
        cites="med_safety/sulfonylurea-older-adults",
        fires=_sulfonylurea_in_older_adult,
        statement="Risk of hypoglycaemia from a sulfonylurea in an older adult",
    ),
    SafetyRule(
        rule_id="delayed-rescue-living-alone",
        kind="sdoh",
        cites="med_safety/living-alone-and-medication-risk",
        fires=_delayed_rescue_living_alone,
        statement="A medication whose harm depends on someone noticing, in a patient who lives alone",
    ),
    SafetyRule(
        rule_id="deintensification-candidate",
        kind="medication_safety",
        cites="med_safety/glycaemic-targets-older-adults",
        fires=_deintensification_candidate,
        statement="Glycaemic target and agents worth reviewing for deintensification",
    ),
    SafetyRule(
        rule_id="polypharmacy",
        kind="burden",
        cites="med_safety/living-alone-and-medication-risk",
        fires=_polypharmacy,
        statement="Polypharmacy — regimen complexity worth reviewing",
    ),
    SafetyRule(
        rule_id="uncontrolled-chronic",
        kind="disease_control",
        cites="med_safety/glycaemic-targets-older-adults",
        fires=_uncontrolled_chronic,
        statement="Chronic condition recorded as not controlled",
    ),
)


class Stratifier:
    """Node 2. Injectable rule set, so a specialty can supply its own."""

    name: ClassVar[str] = "rules"

    def __init__(self, rules: tuple[SafetyRule, ...] = RULES) -> None:
        self._rules = rules

    def flags(self, context: CarePlanContext) -> list[RiskFlag]:
        """Every rule that fires, in declaration order.

        Declaration order rather than a severity sort: severity here would
        be a number nobody measured, and the ordering that matters —
        which concern leads the plan — is decided downstream with
        retrieval behind it.
        """
        return [flag for rule in self._rules if (flag := rule.evaluate(context)) is not None]


def stratify(context: CarePlanContext) -> list[RiskFlag]:
    """Node 2 with the shipped rule set."""
    return Stratifier().flags(context)
