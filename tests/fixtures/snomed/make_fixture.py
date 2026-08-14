"""Generate the synthetic RF2 fixture (design snomed-module.md §9).

SNOMED CT is licensed — this fixture is a FABRICATED mini clinical world
with structurally perfect RF2: real column layouts, valid SCTID check
digits (Verhoeff) and partition identifiers, a coherent is-a DAG, and a
procedure with method/site attributes echoing the thrombectomy shape.
The only real identifiers are well-known structural SCTIDs (the root
concept and RF2 metadata concepts), which are published identifiers, not
licensed content. Nothing here appears in any real SNOMED release.

Regenerate with:  uv run python tests/fixtures/snomed/make_fixture.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3] / "src"))

from hdh.modules.snomed.loader.rf2 import (  # noqa: E402
    FSN_TYPE,
    IS_A,
    PREFERRED,
    ROOT_CONCEPT,
    SYNONYM_TYPE,
    US_ENGLISH_REFSET,
    make_sctid,
)

OUT = Path(__file__).parent
RELEASE = "20260301"
MODULE = make_sctid(900001, "00")  # fabricated module concept

# Attribute type SCTIDs (well-known identifiers; displays fabricated)
METHOD = "260686004"
PROC_SITE_DIRECT = "405813007"
FINDING_SITE = "363698007"

_next_item = 100000


def sid() -> str:
    """A fresh fabricated concept SCTID with a valid check digit."""
    global _next_item
    _next_item += 1
    return make_sctid(_next_item, "00")


# ── The mini clinical world ──────────────────────────────────────────────────

ROOT = ROOT_CONCEPT
FINDING = sid()  # Clinical finding (finding)
PROCEDURE = sid()  # Procedure (procedure)
BODY = sid()  # Body structure (body structure)
QUALIFIER = sid()  # Qualifier value (qualifier value)

DISORDER_FLENUM = sid()  # Disorder of flenum (disorder)
BLORBITIS = sid()  # Blorbitis (disorder)
ACUTE_BLORBITIS = sid()  # Acute blorbitis (disorder)
CHRONIC_BLORBITIS = sid()  # Chronic blorbitis (disorder)
SEVERE_ACUTE = sid()  # Severe acute blorbitis (disorder)
FLENUM_BLORBITIS = sid()  # Blorbitis of flenum (disorder) — TWO parents (DAG!)

REMOVAL_PROC = sid()  # Removal procedure (procedure)
FLENUMECTOMY = sid()  # Flenumectomy (procedure) — method+site attributes
FLENUM_STRUCT = sid()  # Flenum structure (body structure)
REMOVAL_ACTION = sid()  # Removal - action (qualifier value)

RETIRED = sid()  # Glimmerpox (disorder) — inactive, must not load

# (sctid, fsn, preferred term, extra synonyms, parents, active)
CONCEPTS = [
    (ROOT, "Fabricated root concept (root)", "Fabricated root", [], [], True),
    (FINDING, "Clinical finding (finding)", "Clinical finding", [], [ROOT], True),
    (PROCEDURE, "Procedure (procedure)", "Procedure", [], [ROOT], True),
    (BODY, "Body structure (body structure)", "Body structure", [], [ROOT], True),
    (QUALIFIER, "Qualifier value (qualifier value)", "Qualifier value", [], [ROOT], True),
    (METHOD, "Method (attribute)", "Method", [], [QUALIFIER], True),
    (
        PROC_SITE_DIRECT,
        "Procedure site - Direct (attribute)",
        "Procedure site - Direct",
        [],
        [QUALIFIER],
        True,
    ),
    (FINDING_SITE, "Finding site (attribute)", "Finding site", [], [QUALIFIER], True),
    (DISORDER_FLENUM, "Disorder of flenum (disorder)", "Disorder of flenum", [], [FINDING], True),
    (
        BLORBITIS,
        "Blorbitis (disorder)",
        "Blorbitis",
        ["Blorb inflammation", "Glimmer fever"],
        [FINDING],
        True,
    ),
    (ACUTE_BLORBITIS, "Acute blorbitis (disorder)", "Acute blorbitis", [], [BLORBITIS], True),
    (
        CHRONIC_BLORBITIS,
        "Chronic blorbitis (disorder)",
        "Chronic blorbitis",
        ["Longstanding blorb inflammation"],
        [BLORBITIS],
        True,
    ),
    (
        SEVERE_ACUTE,
        "Severe acute blorbitis (disorder)",
        "Severe acute blorbitis",
        [],
        [ACUTE_BLORBITIS],
        True,
    ),
    (
        FLENUM_BLORBITIS,
        "Blorbitis of flenum (disorder)",
        "Blorbitis of flenum",
        [],
        [BLORBITIS, DISORDER_FLENUM],  # the DAG node
        True,
    ),
    (REMOVAL_PROC, "Removal procedure (procedure)", "Removal procedure", [], [PROCEDURE], True),
    (
        FLENUMECTOMY,
        "Flenumectomy (procedure)",
        "Flenumectomy",
        ["Removal of flenum"],
        [REMOVAL_PROC],
        True,
    ),
    (FLENUM_STRUCT, "Flenum structure (body structure)", "Flenum structure", [], [BODY], True),
    (REMOVAL_ACTION, "Removal - action (qualifier value)", "Removal - action", [], [QUALIFIER], True),
    (RETIRED, "Glimmerpox (disorder)", "Glimmerpox", [], [FINDING], False),
]

# (source, type, destination, group) — the thrombectomy shape + a finding site
ATTRIBUTES = [
    (FLENUMECTOMY, METHOD, REMOVAL_ACTION, 1),
    (FLENUMECTOMY, PROC_SITE_DIRECT, FLENUM_STRUCT, 1),
    (FLENUM_BLORBITIS, FINDING_SITE, FLENUM_STRUCT, 0),
]


def build_files() -> dict[str, list[list[str]]]:
    """Assemble the four RF2 snapshot row sets."""
    concept_rows, description_rows, language_rows, relationship_rows = [], [], [], []
    desc_item, rel_item, ref_item = 200000, 300000, 400000

    def add_description(concept: str, term: str, type_id: str, preferred: bool, active: str = "1"):
        nonlocal desc_item, ref_item
        desc_item += 1
        did = make_sctid(desc_item, "01")
        description_rows.append(
            [did, RELEASE, active, MODULE, concept, "en", type_id, term, "900000000000448009"]
        )
        if preferred:
            ref_item += 1
            language_rows.append(
                [
                    f"{ref_item:08d}-aaaa-bbbb-cccc-dddddddddddd",
                    RELEASE,
                    "1",
                    MODULE,
                    US_ENGLISH_REFSET,
                    did,
                    PREFERRED,
                ]
            )

    for sctid, fsn, pt, synonyms, parents, active in CONCEPTS:
        concept_rows.append([sctid, RELEASE, "1" if active else "0", MODULE, "900000000000074008"])
        add_description(sctid, fsn, FSN_TYPE, preferred=True)
        add_description(sctid, pt, SYNONYM_TYPE, preferred=True)
        for synonym in synonyms:
            add_description(sctid, synonym, SYNONYM_TYPE, preferred=False)
        for parent in parents:
            rel_item += 1
            relationship_rows.append(
                [
                    make_sctid(rel_item, "02"),
                    RELEASE,
                    "1",
                    MODULE,
                    sctid,
                    parent,
                    "0",
                    IS_A,
                    "900000000000011006",
                    "900000000000451002",
                ]
            )

    for source, type_id, destination, group in ATTRIBUTES:
        rel_item += 1
        relationship_rows.append(
            [
                make_sctid(rel_item, "02"),
                RELEASE,
                "1",
                MODULE,
                source,
                destination,
                str(group),
                type_id,
                "900000000000011006",
                "900000000000451002",
            ]
        )

    # An inactive relationship that must be ignored by the loader
    rel_item += 1
    relationship_rows.append(
        [
            make_sctid(rel_item, "02"),
            RELEASE,
            "0",
            MODULE,
            SEVERE_ACUTE,
            CHRONIC_BLORBITIS,
            "0",
            IS_A,
            "900000000000011006",
            "900000000000451002",
        ]
    )

    return {
        f"sct2_Concept_Snapshot_FAB1000000_{RELEASE}.txt": [
            ["id", "effectiveTime", "active", "moduleId", "definitionStatusId"],
            *concept_rows,
        ],
        f"sct2_Description_Snapshot_FAB1000000_{RELEASE}.txt": [
            [
                "id",
                "effectiveTime",
                "active",
                "moduleId",
                "conceptId",
                "languageCode",
                "typeId",
                "term",
                "caseSignificanceId",
            ],
            *description_rows,
        ],
        f"sct2_Relationship_Snapshot_FAB1000000_{RELEASE}.txt": [
            [
                "id",
                "effectiveTime",
                "active",
                "moduleId",
                "sourceId",
                "destinationId",
                "relationshipGroup",
                "typeId",
                "characteristicTypeId",
                "modifierId",
            ],
            *relationship_rows,
        ],
        f"der2_cRefset_LanguageSnapshot_FAB1000000_{RELEASE}.txt": [
            [
                "id",
                "effectiveTime",
                "active",
                "moduleId",
                "refsetId",
                "referencedComponentId",
                "acceptabilityId",
            ],
            *language_rows,
        ],
    }


def main() -> None:
    """Write the four fixture files (LF endings, UTF-8, tab-separated)."""
    for name, rows in build_files().items():
        path = OUT / name
        path.write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {name}: {len(rows) - 1} rows")
    key_names = {
        "ROOT": ROOT,
        "BLORBITIS": BLORBITIS,
        "ACUTE_BLORBITIS": ACUTE_BLORBITIS,
        "CHRONIC_BLORBITIS": CHRONIC_BLORBITIS,
        "SEVERE_ACUTE": SEVERE_ACUTE,
        "FLENUM_BLORBITIS": FLENUM_BLORBITIS,
        "DISORDER_FLENUM": DISORDER_FLENUM,
        "FLENUMECTOMY": FLENUMECTOMY,
        "FLENUM_STRUCT": FLENUM_STRUCT,
        "REMOVAL_ACTION": REMOVAL_ACTION,
        "REMOVAL_PROC": REMOVAL_PROC,
        "RETIRED": RETIRED,
    }
    (OUT / "fixture_ids.py").write_text(
        '"""SCTIDs of the fixture\'s key concepts (generated by make_fixture.py)."""\n\n'
        + "\n".join(f'{name} = "{value}"' for name, value in key_names.items())
        + "\n",
        encoding="utf-8",
    )
    print("wrote fixture_ids.py")


if __name__ == "__main__":
    main()
