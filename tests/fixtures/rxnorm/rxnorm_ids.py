"""RXCUIs in the fabricated RxNorm fixture — invented, not real RxNorm.

Named `rxnorm_ids` rather than `fixture_ids`: the SNOMED fixture already
claims that name and every fixture directory joins sys.path, so a second
`fixture_ids` silently hands one suite another's constants.
"""

BLORBIZIDE_IN = "100001"  # ingredient
BLORBIZIDE_PIN = "100002"  # precise ingredient
BLORBIZIDE_10_SCDC = "100010"  # ingredient + strength
BLORBIZIDE_10_TAB = "100011"  # SCD — the prescribable clinical drug
BLORBIZIDE_10_ER = "100012"  # SCD, extended release
ZORBEX_BN = "100020"  # brand name
ZORBEX_10_TAB = "100021"  # SBD — the branded clinical drug
ORAL_TABLET = "100030"  # dose form
ORAL_TABLET_ER = "100031"

QUIXAMET_IN = "200001"
QUIXAMET_500_SCDC = "200010"

COMBO_MIN = "300001"  # multiple ingredients
COMBO_SCD = "300011"  # the combination product
ZORBAMET_BN = "300020"
ZORBAMET_SBD = "300021"
