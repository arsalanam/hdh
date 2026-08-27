"""Lexical helpers shared across the module, and nothing else.

These lived in :mod:`reconcile`, which imports :mod:`generate`, which made
them unreachable from anything upstream of generation — triage needed
``mentions`` and importing it drew a cycle through four modules. A stopword
list is not a node's business; it is a utility, and it belongs somewhere
with no dependencies of its own.

One rule governs everything here: **these functions compare words, and they
never claim more than that.** The naming discipline in :mod:`facts` rests
on it.
"""

from __future__ import annotations

import re

#: Words carrying no distinguishing information in a clinical instruction.
#:
#: Shared so that reconciliation and the fact checks cannot disagree about
#: which words carry meaning — two lists would drift, and the drift would
#: show up as a duplicate that failed to merge or a problem wrongly
#: reported as unmentioned.
STOPWORDS = frozenset(
    "the a an and or of to for in on with that this is are be as by given if it its "
    "at any so whether still been was were has have had not no can could should".split()
)

#: Shorter than this and a word is too common to indicate that two pieces of
#: text are about the same thing. Four is the shortest clinically
#: distinctive length in practice — "COPD", "gout" and "type" all qualify.
SIGNIFICANT_LENGTH = 4


def significant_words(phrase: str, minimum: int = SIGNIFICANT_LENGTH) -> set[str]:
    """The words in ``phrase`` worth matching on."""
    return {
        word
        for word in re.findall(r"[a-z]+", phrase.lower())
        if word not in STOPWORDS and len(word) >= minimum
    }


def mentions(phrase: str, haystack: str) -> bool:
    """Does ``haystack`` use any distinctive word from ``phrase``?

    Lexical only, and deliberately shallow. It answers *"do these talk about
    the same thing at all"* — not *"is it handled correctly"*, which no word
    comparison can answer.

    A phrase with no distinctive words returns True. Reporting a miss about
    something with nothing to look for would be a finding manufactured by
    the check rather than found by it.
    """
    words = significant_words(phrase)
    if not words:
        return True
    return any(word in haystack for word in words)
