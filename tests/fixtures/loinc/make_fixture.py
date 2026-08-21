"""Regenerate the fabricated LOINC fixture.

LOINC is licensed, so the fixture is INVENTED: the codes below are not
real LOINC numbers and the names are deliberately nonsense words, exactly
as the SNOMED fixture is. What it reproduces faithfully is the SHAPE of a
release — the column names, the semicolon-separated RELATEDNAMES2, the
six axes, the dotted PATH_TO_ROOT — because that shape is what the loader
has to understand.

    uv run python tests/fixtures/loinc/make_fixture.py
"""

import csv
from pathlib import Path

HERE = Path(__file__).parent

# (LOINC_NUM, LONG_COMMON_NAME, SHORTNAME, DisplayName, RELATEDNAMES2,
#  COMPONENT, PROPERTY, TIME, SYSTEM, SCALE, METHOD, CLASS, STATUS)
ROWS = [
    (
        "11111-1",
        "Blorbium [Moles/volume] in Serum or Plasma",
        "Blorbium SerPl",
        "Blorbium",
        "Blorb;Blorbium level;Serum blorbium",
        "Blorbium",
        "SCnc",
        "Pt",
        "Ser/Plas",
        "Qn",
        "",
        "CHEM",
        "ACTIVE",
    ),
    (
        "11111-2",
        "Blorbium [Moles/volume] in Urine",
        "Blorbium Ur",
        "Blorbium (U)",
        "Urine blorbium;Ur blorb",
        "Blorbium",
        "SCnc",
        "Pt",
        "Urine",
        "Qn",
        "",
        "CHEM",
        "ACTIVE",
    ),
    (
        "22222-2",
        "Quixate [Mass/volume] in Serum or Plasma",
        "Quixate SerPl",
        "Quixate",
        "QXT;Quix;Quixate level",
        "Quixate",
        "MCnc",
        "Pt",
        "Ser/Plas",
        "Qn",
        "",
        "CHEM",
        "ACTIVE",
    ),
    (
        "33333-3",
        "Fleeble panel - Serum or Plasma",
        "Fleeble Pnl SerPl",
        "Fleeble panel",
        "Fleeble panel;FLP",
        "Fleeble panel",
        "-",
        "Pt",
        "Ser/Plas",
        "-",
        "",
        "CHEM",
        "ACTIVE",
    ),
    (
        "44444-4",
        "Zonkocyte count [#/volume] in Blood",
        "Zonkocytes Bld",
        "Zonkocytes",
        "Zonk count;ZC;Zonkocyte",
        "Zonkocytes",
        "NCnc",
        "Pt",
        "Bld",
        "Qn",
        "",
        "HEM/BC",
        "ACTIVE",
    ),
    (
        "55555-5",
        "Grimble virus [Presence] in Nasal swab",
        "Grimble Nasal Ql",
        "Grimble",
        "Grimble;Grimble virus;GRV",
        "Grimble virus",
        "PrThr",
        "Pt",
        "Nasal",
        "Ord",
        "Probe.amp.tar",
        "MICRO",
        "ACTIVE",
    ),
    # a retired code: the loader must leave it out, so a coder cannot
    # quietly assign something the release has withdrawn
    (
        "99999-9",
        "Obsolete blorbium assay",
        "Obs blorb",
        "Obsolete blorbium",
        "Old blorbium",
        "Blorbium",
        "SCnc",
        "Pt",
        "Ser/Plas",
        "Qn",
        "",
        "CHEM",
        "DEPRECATED",
    ),
]

HIERARCHY = [
    # PATH_TO_ROOT, SEQUENCE, IMMEDIATE_PARENT, CODE, CODE_TEXT
    ("LP-CHEM", 1, "", "LP-CHEM", "Chemistry"),
    ("LP-CHEM.LP-ANALYTE", 2, "LP-CHEM", "LP-ANALYTE", "Chemistry analytes"),
    ("LP-CHEM.LP-ANALYTE.11111-1", 3, "LP-ANALYTE", "11111-1", "Blorbium SerPl"),
    ("LP-CHEM.LP-ANALYTE.11111-2", 3, "LP-ANALYTE", "11111-2", "Blorbium Ur"),
    ("LP-CHEM.LP-ANALYTE.22222-2", 3, "LP-ANALYTE", "22222-2", "Quixate SerPl"),
    ("LP-CHEM.LP-PANEL", 2, "LP-CHEM", "LP-PANEL", "Chemistry panels"),
    ("LP-CHEM.LP-PANEL.33333-3", 3, "LP-PANEL", "33333-3", "Fleeble Pnl"),
    ("LP-HEM", 1, "", "LP-HEM", "Hematology"),
    ("LP-HEM.44444-4", 2, "LP-HEM", "44444-4", "Zonkocytes Bld"),
    ("LP-MICRO", 1, "", "LP-MICRO", "Microbiology"),
    ("LP-MICRO.55555-5", 2, "LP-MICRO", "55555-5", "Grimble Nasal"),
]

TABLE_HEADER = [
    "LOINC_NUM",
    "COMPONENT",
    "PROPERTY",
    "TIME_ASPCT",
    "SYSTEM",
    "SCALE_TYP",
    "METHOD_TYP",
    "CLASS",
    "STATUS",
    "SHORTNAME",
    "LONG_COMMON_NAME",
    "DisplayName",
    "RELATEDNAMES2",
]


def main() -> None:
    table_dir = HERE / "LoincTable"
    tree_dir = HERE / "AccessoryFiles" / "MultiAxialHierarchy"
    table_dir.mkdir(parents=True, exist_ok=True)
    tree_dir.mkdir(parents=True, exist_ok=True)

    with (table_dir / "Loinc.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(TABLE_HEADER)
        for (
            num,
            long_name,
            short,
            display,
            related,
            component,
            prop,
            time,
            system,
            scale,
            method,
            klass,
            status,
        ) in ROWS:
            writer.writerow(
                [
                    num,
                    component,
                    prop,
                    time,
                    system,
                    scale,
                    method,
                    klass,
                    status,
                    short,
                    long_name,
                    display,
                    related,
                ]
            )

    with (tree_dir / "MultiAxialHierarchy.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["PATH_TO_ROOT", "SEQUENCE", "IMMEDIATE_PARENT", "CODE", "CODE_TEXT"])
        writer.writerows(HIERARCHY)

    print(f"wrote {len(ROWS)} rows and {len(HIERARCHY)} hierarchy paths under {HERE}")


if __name__ == "__main__":
    main()
