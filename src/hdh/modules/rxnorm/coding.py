"""Composition, not similarity: how a note's words become an RXCUI.

A note says **"Start Lisinopril 10mg once daily"**. The prescribable
concept is called **"Lisinopril 10 MG Oral Tablet"**. Those strings are
not close, and no amount of ranking makes them close — the note omits the
dose form entirely and writes the strength differently. Meanwhile
"Lisinopril" alone matches the *ingredient*, which is a different RXCUI at
a different level of the graph (design rxnorm §5).

But comprehension already extracts what is missing, as typed attributes:
DOSE, ROUTE, FREQUENCY. So resolution is a walk, not a score:

1. lexical search resolves the **name** — one or two words, which is what
   lexical search is good at;
2. the graph walks down to the component matching the extracted
   **strength**;
3. and then to the clinical drug whose **dose form** the route implies;
4. and where a step is ambiguous, the coding stops at the level the
   evidence supports rather than guessing a tablet.

That last rule is refuse-don't-guess in its RxNorm form, and it answers
what would otherwise be a coin toss: *which level do we code at?* The
deepest one the note actually supports, with the reasoning recorded.

**A brand names a branded drug** (§11 Q5). When the note says "Zorbex",
the walk ends at the SBD, because what was prescribed is what the chart
should say — the ingredient stays one graph hop away for any analysis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Words that mean "extended release" in a note. The dose form is not
#: decoration: ER 500 MG is a different product, and a different RXCUI,
#: from 500 MG (§10 Scenario A).
_EXTENDED_RELEASE = ("er", "xr", "sr", "extended release", "extended-release", "sustained release")

#: route → the dose-form words that route implies.
_ROUTE_FORMS = {
    "po": ("oral",),
    "oral": ("oral",),
    "by mouth": ("oral",),
    "iv": ("injection", "intravenous"),
    "im": ("injection", "intramuscular"),
    "sc": ("injection", "subcutaneous"),
    "topical": ("topical", "cream", "ointment"),
    "inhaled": ("inhalation",),
}

#: "2 x 500mg", "500 mg", "10mg", "50-1000" — the strength as written.
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?(mg|mcg|g|ml|unit|units)\b", re.I)
#: "2 x 500mg" — a count of products, which is the DOSE and not the strength.
_QUANTITY_TIMES = re.compile(r"(\d+)\s*[x×]\s*(?=\d)", re.I)


@dataclass(frozen=True)
class DrugCoding:
    """One resolved drug, and why it stopped where it did."""

    rxcui: str
    tty: str  # the level reached: IN, SCDC, SCD, SBD…
    display: str
    score: float
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def level(self) -> str:
        return self.tty


def parse_strength(text: str) -> str | None:
    """The strength a note wrote, normalised to "10 MG".

    "2 x 500mg" is 500 MG of product taken twice — the note's arithmetic is
    not the label's, and coding the 1000 would find a product that may not
    exist (§10 Scenario A).
    """
    if not text:
        return None
    match = _STRENGTH.search(_QUANTITY_TIMES.sub("", text))
    if match is None:
        return None
    first, second, unit = match.group(1), match.group(2), match.group(3).upper()
    if second:  # a paired strength: "50-1000" is one product, not two
        return f"{first}-{second} {unit}"
    return f"{first} {unit}"


def wants_extended_release(text: str) -> bool:
    """Does the note say extended release, in any of the ways notes do?"""
    lowered = f" {(text or '').lower()} "
    return any(f" {token} " in lowered or f" {token}." in lowered for token in _EXTENDED_RELEASE)


def _form_words(route: str | None, text: str) -> tuple[str, ...]:
    """Dose-form words implied by the route, plus release if it is stated."""
    words: list[str] = []
    key = (route or "").strip().lower()
    for candidate, forms in _ROUTE_FORMS.items():
        if key == candidate or (key and candidate in key):
            words.extend(forms)
            break
    if wants_extended_release(text):
        words.append("extended release")
    return tuple(words)


def _strength_matches(display: str, strength: str) -> bool:
    """Is this concept's name carrying the strength we extracted?

    Compared on the numbers rather than the whole string, because a release
    writes "10 MG" where a note writes "10mg" and both mean one thing.
    """
    wanted = re.findall(r"\d+(?:\.\d+)?", strength)
    found = re.findall(r"\d+(?:\.\d+)?", display)
    return bool(wanted) and all(number in found for number in wanted)


def resolve(
    service,
    name: str,
    *,
    strength: str | None = None,
    route: str | None = None,
    raw: str = "",
    minimum_score: float = 0.6,
) -> DrugCoding | None:
    """Code a drug mention at the deepest level its evidence supports.

    ``raw`` is the mention as written, used for the release form: "ER" is
    part of the drug's name in a note and part of the dose form in RxNorm.
    """
    hits = service.normalize(name, {"limit": 5})
    top = next((h for h in hits if h.score >= minimum_score), None)
    if top is None:
        return None  # nothing to build on; an uncoded order is legitimate

    tty = (top.concept.properties.get("tty") or "").upper()
    evidence = [f"name: {top.concept.display!r} ({tty})"]

    # A brand names a branded drug: walk to the SBD rather than across to
    # the clinical one, because what was prescribed is what the chart says.
    if tty in ("BN", "SBD"):
        return _resolve_branded(service, top, strength, route, raw, evidence)

    strength = strength or parse_strength(raw)
    forms = _form_words(route, raw)
    return _resolve_clinical(service, top, strength, forms, evidence)


def _single_ingredient(service, candidates, evidence):
    """A note that names ONE drug means a single-ingredient product.

    Without this, "Blorbizide 10 mg oral tablet" matches the combination
    product too — it really does contain 10 mg of blorbizide in an oral
    tablet — and the two are indistinguishable on strength and form alone.
    Coding a patient to a combination they were not prescribed adds a drug
    to their chart (§10 Scenario C, from the other direction).
    """
    narrowed = [c for c in candidates if len(service.ingredients_of(c.code)) <= 1]
    if narrowed and len(narrowed) < len(candidates):
        evidence.append("single ingredient: the note named one drug")
        return narrowed
    return candidates


def _pick(candidates, strength, forms, evidence, label):
    """Narrow by strength, then by dose form, recording what narrowed it.

    Returns None whenever the evidence does not single one out, because
    the caller's next move is to STOP at the level above rather than pick
    something plausible. "Plausible" is how a chart acquires a dose nobody
    prescribed.
    """
    if strength:
        matching = [c for c in candidates if _strength_matches(c.display, strength)]
        if not matching:
            # The note states a strength this drug does not come in. Falling
            # through to another strength would chart a dose the clinician
            # never wrote — the worst thing this function could do.
            evidence.append(f"no product at {strength} — stopping here rather than substituting")
            return None
        candidates, _ = matching, evidence.append(f"strength: {strength}")
    wanted_release = "extended release" in forms
    if forms:
        matching = [c for c in candidates if all(w in c.display.lower() for w in forms)]
        if matching:
            candidates, _ = matching, evidence.append(f"form: {' '.join(forms)}")
        elif wanted_release:
            # The note asked for extended release and no product has it.
            # The plain form is a DIFFERENT drug, so refuse rather than
            # silently substitute one for the other.
            evidence.append("form not available — stopping here rather than substituting")
            return None
    if not wanted_release:
        # An unqualified note means the immediate-release product. Choosing
        # extended release would add a property the note never stated, and
        # they are different drugs taken differently.
        plain = [c for c in candidates if "extended release" not in c.display.lower()]
        if plain and len(plain) < len(candidates):
            candidates = plain
            evidence.append("immediate release: the note did not say extended")
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        evidence.append(f"{len(candidates)} {label} candidates — ambiguous, stopping here")
    return None


def _resolve_clinical(service, top, strength, forms, evidence) -> DrugCoding:
    """ingredient → component → clinical drug, as far as evidence allows.

    The walk descends ON EVIDENCE, not as far as the graph reaches. A note
    that names a drug and nothing else supports the INGREDIENT and nothing
    else: coding it to a 10 MG oral tablet would invent both a strength and
    a form. That is what "the deepest level the evidence supports" means,
    and the difference between it and "the deepest level reachable" is a
    chart full of doses nobody prescribed (§11 Q3).
    """
    best = top.concept
    if not strength:
        evidence.append("no strength in the note — the ingredient is as deep as the evidence goes")
        return DrugCoding(
            rxcui=best.code,
            tty=(best.properties.get("tty") or "").upper(),
            display=best.display,
            score=top.score,
            evidence=tuple(evidence),
        )
    descendants = service.descendants(best.code)
    by_tty: dict[str, list] = {}
    for concept in descendants:
        by_tty.setdefault((concept.properties.get("tty") or "").upper(), []).append(concept)

    component = _pick(by_tty.get("SCDC", []), strength, (), evidence, "component")
    if component is not None:
        best = component
        clinical_pool = [
            c for c in service.descendants(component.code) if (c.properties.get("tty") or "").upper() == "SCD"
        ]
    else:
        clinical_pool = by_tty.get("SCD", [])

    clinical_pool = _single_ingredient(service, clinical_pool, evidence)
    clinical = _pick(clinical_pool, strength, forms, evidence, "clinical drug")
    if clinical is not None:
        best = clinical
    return DrugCoding(
        rxcui=best.code,
        tty=(best.properties.get("tty") or "").upper(),
        display=best.display,
        score=top.score,
        evidence=tuple(evidence),
    )


def _resolve_branded(service, top, strength, route, raw, evidence) -> DrugCoding:
    """brand → branded drug, at the strength and form the note gives."""
    best = top.concept
    tty = (best.properties.get("tty") or "").upper()
    strength = strength or parse_strength(raw)
    if tty == "BN" and not strength:
        # Symmetric with the clinical walk: a note naming a brand and
        # nothing else supports the BRAND. Descending to one of its
        # products would invent a strength, and brands carry several.
        evidence.append("no strength in the note — the brand is as deep as the evidence goes")
    elif tty == "BN":
        branded = [
            c for c in service.descendants(best.code) if (c.properties.get("tty") or "").upper() == "SBD"
        ]
        chosen = _pick(branded, strength, _form_words(route, raw), evidence, "branded drug")
        if chosen is not None:
            best = chosen
    evidence.append("branded: the note named the brand")
    return DrugCoding(
        rxcui=best.code,
        tty=(best.properties.get("tty") or "").upper(),
        display=best.display,
        score=top.score,
        evidence=tuple(evidence),
    )
