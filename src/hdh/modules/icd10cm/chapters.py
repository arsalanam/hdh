"""The 22 ICD-10-CM chapters: code ranges and titles.

Chapter membership is defined by category-code ranges (stable across fiscal
years). The block level between chapter and category comes from the tabular
XML and arrives with the full-catalog load (milestone C); until then the
hierarchy is chapter → category → subcategory → code.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Chapter:
    """One ICD-10-CM chapter: number, category range, and title."""

    number: int
    first: str  # first category code in range, e.g. "S00"
    last: str  # last category code in range, e.g. "T88"
    title: str

    @property
    def concept_id(self) -> str:
        return f"icd10cm:ch{self.number}"

    @property
    def range_code(self) -> str:
        return f"{self.first}-{self.last}"

    @property
    def path_segment(self) -> str:
        return f"ch{self.number}"


CHAPTERS: tuple[Chapter, ...] = (
    Chapter(1, "A00", "B99", "Certain infectious and parasitic diseases"),
    Chapter(2, "C00", "D49", "Neoplasms"),
    Chapter(
        3,
        "D50",
        "D89",
        "Diseases of the blood and blood-forming organs and certain disorders involving the immune mechanism",
    ),
    Chapter(4, "E00", "E89", "Endocrine, nutritional and metabolic diseases"),
    Chapter(5, "F01", "F99", "Mental, behavioral and neurodevelopmental disorders"),
    Chapter(6, "G00", "G99", "Diseases of the nervous system"),
    Chapter(7, "H00", "H59", "Diseases of the eye and adnexa"),
    Chapter(8, "H60", "H95", "Diseases of the ear and mastoid process"),
    Chapter(9, "I00", "I99", "Diseases of the circulatory system"),
    Chapter(10, "J00", "J99", "Diseases of the respiratory system"),
    Chapter(11, "K00", "K95", "Diseases of the digestive system"),
    Chapter(12, "L00", "L99", "Diseases of the skin and subcutaneous tissue"),
    Chapter(13, "M00", "M99", "Diseases of the musculoskeletal system and connective tissue"),
    Chapter(14, "N00", "N99", "Diseases of the genitourinary system"),
    Chapter(15, "O00", "O9A", "Pregnancy, childbirth and the puerperium"),
    Chapter(16, "P00", "P96", "Certain conditions originating in the perinatal period"),
    Chapter(17, "Q00", "Q99", "Congenital malformations, deformations and chromosomal abnormalities"),
    Chapter(
        18,
        "R00",
        "R99",
        "Symptoms, signs and abnormal clinical and laboratory findings, not elsewhere classified",
    ),
    Chapter(19, "S00", "T88", "Injury, poisoning and certain other consequences of external causes"),
    Chapter(20, "V00", "Y99", "External causes of morbidity"),
    Chapter(21, "Z00", "Z99", "Factors influencing health status and contact with health services"),
    Chapter(22, "U00", "U85", "Codes for special purposes"),
)


def chapter_for(category: str) -> Chapter | None:
    """The chapter whose range contains a 3-character category code."""
    for chapter in CHAPTERS:
        if chapter.first <= category <= chapter.last:
            return chapter
    return None
