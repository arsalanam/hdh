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


RULES: tuple[SafetyRule, ...] = (
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
