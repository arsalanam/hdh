"""Parser for the CMS tabular XML (icd10cm_tabular_<fy>.xml).

Supplies what the order file cannot: the block (section) level of the
hierarchy, per-code coding-rule notes (Excludes1/2, code-first,
use-additional, includes), and per-family seventh-character definitions —
which matter because the 7th character is an *episode* only where the XML
says so (fracture families) and something else entirely elsewhere
(obstetric codes use fetus digits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

# note text → referenced codes: "(S58.-)", "(M97.4)", "(Z22.-)" …
_CODE_REF = re.compile(r"\(([A-TV-Z][0-9][0-9A-Z](?:\.[0-9A-Z]{1,4})?)(?:\.?-)?\)")

RULE_ELEMENTS = {
    "excludes1": "excludes1",
    "excludes2": "excludes2",
    "codeFirst": "code_first",
    "useAdditionalCode": "use_additional",
    "includes": "includes",
}


@dataclass(frozen=True)
class Block:
    """One section: the hierarchy level between chapter and category."""

    first: str
    last: str
    description: str

    @property
    def range_code(self) -> str:
        return self.first if self.first == self.last else f"{self.first}-{self.last}"

    def contains(self, category: str) -> bool:
        return self.first <= category <= self.last


@dataclass(frozen=True)
class RuleNote:
    """One coding-rule note on a code, with any resolvable code references."""

    edge_type: str  # excludes1 | excludes2 | code_first | use_additional | includes
    text: str
    refs: tuple[str, ...]  # dotted codes referenced in the note text


@dataclass
class TabularData:
    """Everything the load pipeline consumes from the tabular XML."""

    blocks: list[Block] = field(default_factory=list)
    rules: dict[str, list[RuleNote]] = field(default_factory=dict)  # dotted code → notes
    seven_defs: dict[str, dict[str, str]] = field(default_factory=dict)  # category → char → meaning

    def block_for(self, category: str) -> Block | None:
        for block in self.blocks:
            if block.contains(category):
                return block
        return None


def _refs(text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _CODE_REF.finditer(text))


def parse_tabular(path: Path) -> TabularData:
    """Parse the tabular XML into blocks, rule notes, and 7th-char defs."""
    data = TabularData()
    root = ElementTree.parse(path).getroot()
    for section in root.iter("section"):
        section_id = section.get("id", "")
        desc = section.findtext("desc", "").strip()
        first, _, last = section_id.partition("-")
        if first:
            data.blocks.append(Block(first, last or first, desc))
    for diag in root.iter("diag"):
        name = diag.findtext("name", "").strip()
        if not name:
            continue
        for element, edge_type in RULE_ELEMENTS.items():
            for note in diag.findall(f"./{element}/note"):
                text = (note.text or "").strip()
                if text:
                    data.rules.setdefault(name, []).append(RuleNote(edge_type, text, _refs(text)))
        seven = diag.find("./sevenChrDef")
        if seven is not None:
            extensions = {
                ext.get("char", ""): (ext.text or "").strip()
                for ext in seven.findall("./extension")
                if ext.get("char")
            }
            if extensions:
                data.seven_defs[name] = extensions
    return data
