"""Pluggable care-gap finders: one interface, interchangeable strategies.

``GapFinder`` is the contract; the ``FINDERS`` registry maps a name to an
implementation, and the CLI's ``--finder`` flag selects one. Two ship today:

  rules  Deterministic rule engine (detector.py) — fast, free, reproducible;
         the same input always yields the same gaps.
  ai     LLM chart review (ai_finder.py) — clinical reasoning that can catch
         gaps no rule expresses (a diabetic with no recent HbA1c, statin
         absent despite diabetes + hyperlipidemia); costs API tokens and is
         not deterministic.

To plug in your own: implement the protocol and add it to ``FINDERS`` —
nothing else changes, the CLI picks it up automatically.
"""

from typing import ClassVar, Protocol

from .ai_finder import AIGapFinder
from .detector import CareGap, detect_gaps


class GapFinder(Protocol):
    """The interface every care-gap finder implements."""

    name: ClassVar[str]
    description: ClassVar[str]

    def find(self, session, *, mrn=None, limit=None, as_of=None, sample=5) -> list[CareGap]:
        """Return care gaps, most severe first; ``sample`` bounds AI-style
        finders when scanning without a specific patient."""
        ...


class RuleBasedGapFinder:
    """The deterministic rule engine, adapted to the GapFinder interface."""

    name: ClassVar[str] = "rules"
    description: ClassVar[str] = "Deterministic rules — fast, free, reproducible"

    def find(self, session, *, mrn=None, limit=None, as_of=None, sample=5) -> list[CareGap]:
        """Run the four detector rules (sample is ignored: rules scan everyone)."""
        return detect_gaps(session, mrn=mrn, limit=limit, as_of=as_of)


FINDERS: dict[str, GapFinder] = {
    "rules": RuleBasedGapFinder(),
    "ai": AIGapFinder(),
}


def get_finder(name: str) -> GapFinder:
    """Look up a registered finder by name."""
    try:
        return FINDERS[name]
    except KeyError:
        available = ", ".join(sorted(FINDERS))
        raise ValueError(f"Unknown gap finder '{name}'. Available: {available}") from None
