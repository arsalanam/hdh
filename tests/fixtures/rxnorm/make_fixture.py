"""Regenerate the fabricated RxNorm fixture.

RxNorm is redistributable only under UMLS terms, so the fixture is
INVENTED: these RXCUIs are not real and the drug names are nonsense words,
exactly as the SNOMED and LOINC fixtures are. What it reproduces
faithfully is the SHAPE of a release — pipe-delimited RRF with a trailing
separator, the term types, and the relationship directions — because that
shape is what the loader has to understand.

The graph it builds is the one §4 of the design draws, twice over: a
single-ingredient drug with a brand, and a two-ingredient combination,
which is where the compositional walk gets hard.

    uv run python tests/fixtures/rxnorm/make_fixture.py
"""

from pathlib import Path

HERE = Path(__file__).parent

# RXCUI, TTY, STR  — SAB is RXNORM throughout unless noted
CONCEPTS = [
    ("100001", "IN", "Blorbizide"),
    ("100002", "PIN", "Blorbizide hydrochloride"),
    ("100010", "SCDC", "Blorbizide 10 MG"),
    ("100011", "SCD", "Blorbizide 10 MG Oral Tablet"),
    ("100012", "SCD", "Blorbizide 10 MG Oral Tablet Extended Release"),
    ("100020", "BN", "Zorbex"),
    ("100021", "SBD", "Zorbex 10 MG Oral Tablet"),
    ("100030", "DF", "Oral Tablet"),
    ("100031", "DF", "Oral Tablet Extended Release"),
    # a second ingredient, and the combination product built from both
    ("200001", "IN", "Quixamet"),
    ("200010", "SCDC", "Quixamet 500 MG"),
    ("300001", "MIN", "Blorbizide / Quixamet"),
    ("300011", "SCD", "Blorbizide 10 MG / Quixamet 500 MG Oral Tablet"),
    ("300020", "BN", "Zorbamet"),
    ("300021", "SBD", "Zorbamet 10-500 MG Oral Tablet"),
]

#: Extra atoms: the same concept named the way a clinician might write it.
#: Real releases carry these from other sources (MSH, VANDF…), and they are
#: where a funnel's recall actually comes from.
SYNONYMS = [
    ("100001", "SY", "MSH", "Blorbizid"),
    ("100011", "SY", "MSH", "Blorbizide 10mg tablet"),
    ("100020", "SY", "MSH", "Zorbex brand of blorbizide"),
    ("300021", "SY", "MSH", "Zorbamet 10/500"),
]

# RXCUI1, RELA, RXCUI2  — read as: RXCUI2 --RELA--> RXCUI1, which is the
# release's own reading ("the relationship of the second concept to the
# first"). So a dose form row names the FORM first and the drug second:
# the edge runs drug --has_dose_form--> form.
RELATIONS = [
    ("100011", "ingredient_of", "100001"),
    ("100012", "ingredient_of", "100001"),
    ("100011", "constitutes", "100010"),
    ("100021", "has_tradename", "100011"),
    ("100021", "ingredient_of", "100001"),
    ("100020", "ingredient_of", "100001"),
    ("100030", "has_dose_form", "100011"),
    ("100031", "has_dose_form", "100012"),
    ("100002", "has_precise_ingredient", "100001"),
    # the combination
    ("300011", "ingredient_of", "100001"),
    ("300011", "ingredient_of", "200001"),
    ("300011", "constitutes", "100010"),
    ("300011", "constitutes", "200010"),
    ("300021", "has_tradename", "300011"),
    ("100030", "has_dose_form", "300011"),
]

# RXCUI, ATN, ATV
ATTRIBUTES = [
    ("100011", "RXN_AVAILABLE_STRENGTH", "10 MG"),
    ("100012", "RXN_AVAILABLE_STRENGTH", "10 MG"),
    ("300011", "RXN_AVAILABLE_STRENGTH", "10 MG / 500 MG"),
]


def _conso_row(rxcui: str, tty: str, string: str, sab: str = "RXNORM", suppress: str = "N") -> str:
    """One RXNCONSO line. Column order is the release's, and the trailing
    pipe is real — RRF ends every row with the separator."""
    columns = [""] * 18
    columns[0] = rxcui  # RXCUI
    columns[1] = "ENG"  # LAT
    columns[11] = sab  # SAB
    columns[12] = tty  # TTY
    columns[13] = rxcui  # CODE
    columns[14] = string  # STR
    columns[16] = suppress  # SUPPRESS — "O" means obsolete
    return "|".join(columns) + "|"


def _rel_row(rxcui1: str, rela: str, rxcui2: str) -> str:
    columns = [""] * 16
    columns[0] = rxcui1
    columns[2] = "CUI"
    columns[3] = "RO"
    columns[4] = rxcui2
    columns[6] = "CUI"
    columns[7] = rela
    columns[10] = "RXNORM"
    columns[14] = "N"
    return "|".join(columns) + "|"


def _sat_row(rxcui: str, atn: str, atv: str) -> str:
    columns = [""] * 13
    columns[0] = rxcui
    columns[4] = "CUI"
    columns[8] = atn
    columns[9] = "RXNORM"
    columns[10] = atv
    columns[11] = "N"
    return "|".join(columns) + "|"


def main() -> None:
    conso = [_conso_row(c, tty, name) for c, tty, name in CONCEPTS]
    conso += [_conso_row(c, tty, name, sab) for c, tty, sab, name in SYNONYMS]
    # a suppressed atom: the loader must leave it out, or a withdrawn name
    # stays searchable and a note mentioning it codes to a live concept
    conso.append(_conso_row("100011", "SY", "Blorbizide OLD NAME", suppress="O"))

    (HERE / "RXNCONSO.RRF").write_text("\n".join(conso) + "\n", encoding="utf-8")
    (HERE / "RXNREL.RRF").write_text(
        "\n".join(_rel_row(a, r, b) for a, r, b in RELATIONS) + "\n", encoding="utf-8"
    )
    (HERE / "RXNSAT.RRF").write_text(
        "\n".join(_sat_row(c, n, v) for c, n, v in ATTRIBUTES) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(conso)} atoms, {len(RELATIONS)} relations, {len(ATTRIBUTES)} attributes")


if __name__ == "__main__":
    main()
