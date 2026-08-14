"""RF2 Snapshot reading: file discovery, row streaming, SCTID validation.

RF2 files are UTF-8, tab-separated, first line a header, one component
per row. This parser is deliberately dumb — column access by header
name, no interpretation; meaning (preferred terms, is-a inversion,
semantic tags) belongs to the build stage. Works identically on the
committed synthetic fixture and a real US Edition extract (design
snomed-module.md §2, §9).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# ── Well-known SCTIDs (identifiers, not content) ─────────────────────────────

ROOT_CONCEPT = "138875005"
IS_A = "116680003"

FSN_TYPE = "900000000000003001"
SYNONYM_TYPE = "900000000000013009"

US_ENGLISH_REFSET = "900000000000509007"
PREFERRED = "900000000000548007"
ACCEPTABLE = "900000000000549004"

# Well-known defining-attribute types → stable snake_case names; anything
# else falls back to the type concept's own preferred term at build time.
ATTRIBUTE_NAMES = {
    "363698007": "finding_site",
    "260686004": "method",
    "405813007": "procedure_site_direct",
    "363704007": "procedure_site",
    "246075003": "causative_agent",
    "116676008": "associated_morphology",
    "424226004": "using_device",
    "424361007": "using_substance",
    "363701004": "direct_substance",
    "260507000": "access",
    "246454002": "occurrence",
    "370135005": "pathological_process",
    "116678009": "has_focus",
    "363589002": "associated_procedure",
}

# The four Snapshot files v1 consumes (design §2), by glob prefix.
RF2_PATTERNS = {
    "concepts": "sct2_Concept_Snapshot*",
    "descriptions": "sct2_Description_Snapshot*",
    "relationships": "sct2_Relationship_Snapshot*",
    "language": "der2_cRefset_LanguageSnapshot*",
}


class Rf2Error(Exception):
    """A structural RF2 problem (missing file, bad header, invalid SCTID)."""


def find_rf2_files(source_dir: Path) -> dict[str, Path]:
    """Locate the four Snapshot files anywhere under source_dir.

    Real releases nest them (``Snapshot/Terminology/...``); the fixture
    keeps them flat — recursive glob serves both. Missing or ambiguous
    files fail loudly before any parsing starts."""
    found: dict[str, Path] = {}
    for key, pattern in RF2_PATTERNS.items():
        matches = sorted(source_dir.rglob(pattern + ".txt"))
        if not matches:
            raise Rf2Error(f"no {pattern}.txt under {source_dir} — is this an RF2 Snapshot directory?")
        if len(matches) > 1:
            raise Rf2Error(f"multiple {pattern} files under {source_dir}: {[m.name for m in matches]}")
        found[key] = matches[0]
    return found


def iter_rows(path: Path) -> Iterator[dict[str, str]]:
    """Stream one RF2 file as header-keyed dicts (no type coercion)."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        header_line = fh.readline().rstrip("\r\n")
        if not header_line or "\t" not in header_line:
            raise Rf2Error(f"{path.name}: missing tab-separated header")
        header = header_line.split("\t")
        for lineno, line in enumerate(fh, start=2):
            line = line.rstrip("\r\n")
            if not line:
                continue
            values = line.split("\t")
            if len(values) != len(header):
                raise Rf2Error(f"{path.name}:{lineno}: {len(values)} fields, header has {len(header)}")
            yield dict(zip(header, values, strict=True))


# ── SCTID check-digit validation (Verhoeff) ──────────────────────────────────

_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_check_digit(digits: str) -> str:
    """The Verhoeff check digit for a digit string (SCTID's last digit)."""
    c = 0
    for i, digit in enumerate(reversed(digits)):
        c = _D[c][_P[(i + 1) % 8][int(digit)]]
    return str(_INV[c])


def is_valid_sctid(sctid: str) -> bool:
    """Structural SCTID check: 6–18 digits, no leading zero, Verhoeff-valid."""
    if not sctid.isdigit() or not 6 <= len(sctid) <= 18 or sctid[0] == "0":
        return False
    c = 0
    for i, digit in enumerate(reversed(sctid)):
        c = _D[c][_P[i % 8][int(digit)]]
    return c == 0


def make_sctid(item: int, partition: str) -> str:
    """Compose a valid SCTID (fixture generation): item + partition + check.

    Partition identifiers (short form): '00' concept, '01' description,
    '02' relationship."""
    body = f"{item}{partition}"
    return body + verhoeff_check_digit(body)
