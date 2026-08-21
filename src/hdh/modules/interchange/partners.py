"""Mock partners: a lab and a pharmacy that behave plausibly.

The rider on design §9 Q4 is the whole difficulty here. Seeded random
values inside a reference range are easy and useless: a CKD-4 patient
whose creatinine comes back normal produces a chart that contradicts
itself, which is worse than no lab at all in a dataset people learn from.

So the lab is **condition-aware** — and it gets there by READING the
catalog the generator already uses, never by deciding anything clinical
itself. The order carries the patient's diagnosis codes (as a real
requisition does); the catalog says which conditions shift which test;
``generate_lab`` applies the shift. The mock has no clinical opinions of
its own, and must not grow any: it is not a second disease engine.
"""

from __future__ import annotations

import random

from hdh.modules.interchange.contracts import InboundResult, OutboundOrder

#: Order text → the tests a partner would actually run. Matching is on the
#: lower-cased display, longest key first, so "comprehensive metabolic
#: panel" is not swallowed by "metabolic panel".
PANELS: dict[str, tuple[str, ...]] = {
    "comprehensive metabolic panel": ("Sodium", "Potassium", "Creatinine", "Glucose", "BUN", "ALT"),
    "basic metabolic panel": ("Sodium", "Potassium", "Creatinine", "Glucose", "BUN"),
    "metabolic panel": ("Sodium", "Potassium", "Creatinine", "Glucose"),
    "renal function panel": ("Creatinine", "BUN", "eGFR"),
    "lipid panel": ("Total Cholesterol", "LDL", "HDL", "Triglycerides"),
    "complete blood count": ("Hemoglobin", "WBC", "Platelets"),
    "cbc": ("Hemoglobin", "WBC", "Platelets"),
    "bmp": ("Sodium", "Potassium", "Creatinine", "Glucose", "BUN"),
    "cmp": ("Sodium", "Potassium", "Creatinine", "Glucose", "BUN", "ALT"),
    "thyroid panel": ("TSH",),
}

#: Qualitative tests: the catalog gives them a 0–0 range because there is
#: no number to give. Before `value_text` existed these were unstorable
#: (design §3), so the mock returns the words a lab actually reports.
QUALITATIVE: dict[str, tuple[str, str]] = {
    "Influenza A/B": ("negative", "positive"),
    "Monospot": ("negative", "positive"),
    "Rapid Strep": ("negative", "positive"),
}

#: How often a qualitative test comes back positive when the patient is
#: actually carrying the condition it tests for.
_POSITIVE_WHEN_INDICATED = 0.75


def _category(icd10: str) -> str:
    """The ICD-10 CATEGORY — the disease, without the stage or variant.

    Matching on the full code does not work, and the CKD case is exactly
    why: the catalog profile starts at N18.31 (stage 3a) while a patient
    who has progressed carries N18.4. Both are chronic kidney disease and
    both shift creatinine. The characters after the dot are severity,
    site or complication; the category is what the patient HAS.
    """
    return icd10.split(".")[0].strip().upper()


def _catalog_index() -> dict[str, tuple]:
    """test name → (baseline spec, {ICD-10 category: the SHIFTED spec}).

    Built from the SAME catalog the generator samples from, so the mock
    cannot drift away from the clinical content it is imitating. Two
    details do the real work.

    **Shifts are keyed by LOINC, not by test name.** The catalog calls the
    diabetic glucose "Glucose (stat)" and the panel's glucose "Glucose",
    and both are LOINC 2345-7 — the same measurement under two labels. Key
    on the name and a type-2 diabetic's metabolic panel comes back with a
    perfectly healthy glucose.

    **The patient's diagnoses choose the spec.** One test name carries
    different specs in different conditions: creatinine appears in the
    annual-physical profile with no shift and in the CKD profile with
    +0.8. Keeping only the first would hand a CKD-4 patient a healthy
    creatinine — the precise failure the §9 Q4 rider is about.
    """
    from hdh.core.conditions import default_catalog

    catalog = default_catalog()
    baseline: dict[str, object] = {}
    shifted_by_loinc: dict[str, dict[str, object]] = {}
    loinc_of: dict[str, str] = {}

    for name in catalog.names():
        profile = catalog.get(name)
        # A staged condition reaches several codes; they share a category,
        # which is why the category is what we match on.
        categories = {
            _category(code)
            for code in {profile.icd10_code}
            | {stage.icd10_code for stage in (profile.staging.stages if profile.staging else ())}
            if code
        }
        for spec in profile.labs:
            loinc_of.setdefault(spec.test_name, spec.loinc_code)
            if spec.condition_shift:
                for category in categories:
                    shifted_by_loinc.setdefault(spec.loinc_code, {}).setdefault(category, spec)
            # prefer an UNSHIFTED spec as the baseline, but fall back to the
            # shifted one for tests that only ever appear with a condition
            if not spec.condition_shift or spec.test_name not in baseline:
                baseline[spec.test_name] = spec

    return {
        test_name: (baseline[test_name], shifted_by_loinc.get(loinc_of[test_name], {}))
        for test_name in baseline
    }


def _tests_for(display: str) -> tuple[str, ...]:
    """Which tests an order asks for — a panel, or a single named test."""
    text = display.strip().lower()
    for key in sorted(PANELS, key=len, reverse=True):
        if key in text:
            return PANELS[key]
    index = _catalog_index()
    for test_name in sorted(index, key=len, reverse=True):
        if test_name.lower() in text:
            return (test_name,)
    return ()  # not something this lab can run


class MockLabPartner:
    """A lab that answers with results the patient could plausibly have."""

    name = "mock-lab"

    def __init__(self, rng: random.Random | None = None) -> None:
        # Seeded so a round trip is reproducible, like `hdh generate --seed`.
        self._rng = rng or random.Random(0)

    def handles(self, order: OutboundOrder) -> bool:
        return order.kind == "lab"

    def fulfil(self, order: OutboundOrder) -> tuple[InboundResult, ...]:
        """Run whatever tests the order names, shaped by the patient.

        Returns () for anything this lab has no spec for — saying "I
        cannot do that" is better than inventing a value.
        """
        from hdh.core.generators import generate_lab

        index = _catalog_index()
        results: list[InboundResult] = []
        for test_name in _tests_for(order.display):
            entry = index.get(test_name)
            if entry is None:
                continue
            baseline, shifted = entry
            # THE RIDER: the patient's own problem list decides which spec
            # answers. Nothing here judges what the patient has — the
            # requisition said, and the catalog knows what that means for
            # this test.
            categories = {_category(code) for code in order.diagnoses}
            match = next((shifted[c] for c in sorted(shifted) if c in categories), None)
            spec = match or baseline
            indicated = match is not None
            if test_name in QUALITATIVE:
                results.append(self._qualitative(order, spec, test_name, indicated))
                continue
            state = random.getstate()
            try:
                random.seed(self._rng.random())
                row = generate_lab(visit_id=0, spec=spec, has_condition=indicated)
            finally:
                random.setstate(state)
            results.append(
                InboundResult(
                    request_id=order.request_id,
                    kind="lab",
                    # the name the ORDER used: the panel asked for "Glucose",
                    # and "Glucose (stat)" is the catalog's internal variant
                    # for the same LOINC, not something a lab would report
                    name=test_name,
                    value=row.value,
                    unit=spec.unit,
                    reference_low=spec.ref_low,
                    reference_high=spec.ref_high,
                    abnormal=str(row.status).split(".")[-1].lower(),
                    loinc_code=spec.loinc_code,
                )
            )
        return tuple(results)

    def _qualitative(self, order: OutboundOrder, spec, test_name: str, indicated: bool) -> InboundResult:
        negative, positive = QUALITATIVE[test_name]
        hit = indicated and self._rng.random() < _POSITIVE_WHEN_INDICATED
        return InboundResult(
            request_id=order.request_id,
            kind="lab",
            name=test_name,
            value=None,  # there is no number, and there never was
            value_text=positive if hit else negative,
            unit=None,
            abnormal="high" if hit else "normal",
            loinc_code=spec.loinc_code,
        )


class MockPharmacyPartner:
    """A pharmacy that dispenses what was ordered, and says what it gave."""

    name = "mock-pharmacy"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random(1)

    def handles(self, order: OutboundOrder) -> bool:
        return order.kind == "medication"

    def fulfil(self, order: OutboundOrder) -> tuple[InboundResult, ...]:
        """Dispense what was ordered, and report the label it printed."""
        return (
            InboundResult(
                request_id=order.request_id,
                kind="dispense",
                name=order.display,
                detail={
                    # The sig is what the pharmacy actually reads and prints
                    # on the label, which is why §3 adopted it.
                    "sig": order.sig or "",
                    "days_supply": self._rng.choice([30, 60, 90]),
                },
            ),
        )


def build_partners(seed: int | None = None) -> dict[str, MockLabPartner | MockPharmacyPartner]:
    """Every partner this build knows about, by name."""
    rng = random.Random(seed if seed is not None else 0)
    return {p.name: p for p in (MockLabPartner(rng), MockPharmacyPartner(rng))}
