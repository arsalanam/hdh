"""Provider verification — the streaming encounter's speaker ID (design §13).

In the live encounter the speaking provider is *known* (they are signed in), so
speaker identification is a **1:1 verification**, not open-set identification: a
diarized turn's voice embedding is compared by cosine similarity against *that
provider's* enrolled print. One embedder (TitaNet) produces both the embeddings
Sortformer clusters on and this verification vector, so the match is a direct
cosine in one vector space — no second speaker model.

This module is the **pure** half: cosine, the 1:1 decision, and passive
re-enrollment. It holds no model and no GPU dependency, so the whole policy is
unit-tested without the ``media`` extra. TitaNet embedding extraction is the
GPU seam, in :mod:`.streaming`.

Two rules from §13 are structural here, not optional:

- **Providers only are ever enrolled.** A voiceprint is biometric PHI; the
  patient is simply "the other speaker", never embedded for identity. Nothing
  in this module takes a patient embedding.
- **The media service persists nothing.** The enrolled print is *supplied by
  the caller* at stream open and the re-enrolled print is *handed back* for the
  caller (agent_api, which owns PHI) to store — consented, revocable, tied to
  the Keycloak subject. This module keeps a print only for the life of one
  stream.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Cosine at or above this is the same speaker. TitaNet same-speaker pairs sit
# well above it and different-speaker pairs well below, so 0.6 separates them
# with margin; tune per deployment once measured on the GPU box.
MATCH_THRESHOLD = 0.6

# Passive re-enrollment weight: each confirmed provider turn nudges the stored
# print a little toward the live voice (room acoustics, a cold), without letting
# one noisy turn redefine it. A slow EMA — the print stays recognisably theirs.
RE_ENROLL_WEIGHT = 0.1


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors, in ``[-1, 1]``.

    Pure Python so the null backend stays dependency-free. A zero-magnitude
    vector has no direction, so its similarity to anything is ``0.0`` rather
    than a division error."""
    if len(a) != len(b):
        raise ValueError(f"embedding length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of one 1:1 check: did this voice match the enrolled provider,
    and how close was it. ``score`` is reported so a low-but-passing match can
    still flag ``needs_review`` upstream rather than being trusted silently."""

    matched: bool
    score: float


class ProviderVerifier:
    """A 1:1 verifier bound to one provider's enrolled print for one stream.

    It is handed the enrolled embedding at stream open, verifies each turn's
    voice against it, and — on a confirmed match — passively re-enrolls so the
    print tracks the live voice. The current print is readable back out so the
    caller can persist the updated one; this object never writes anything."""

    def __init__(
        self,
        enrolled: Sequence[float],
        *,
        threshold: float = MATCH_THRESHOLD,
        learn: bool = True,
    ) -> None:
        if not enrolled:
            raise ValueError("an enrolled print cannot be empty — enroll the provider first")
        self._enrolled = [float(x) for x in enrolled]
        self.threshold = threshold
        self.learn = learn

    @classmethod
    def from_samples(
        cls, samples: Sequence[Sequence[float]], *, threshold: float = MATCH_THRESHOLD
    ) -> ProviderVerifier:
        """Enroll a provider from one or more voice embeddings — the print is
        their mean. Enrollment is a provider-only, consented act; this is where
        that print is first formed, before any stream."""
        if not samples:
            raise ValueError("enrollment needs at least one voice sample")
        width = len(samples[0])
        if any(len(s) != width for s in samples):
            raise ValueError("all enrollment samples must be the same length")
        mean = [sum(s[i] for s in samples) / len(samples) for i in range(width)]
        return cls(mean, threshold=threshold)

    @property
    def enrolled(self) -> list[float]:
        """The current print — the passively re-enrolled one after a stream, for
        the caller to store."""
        return list(self._enrolled)

    def verify(self, embedding: Sequence[float]) -> VerifyResult:
        """Is this turn's voice the enrolled provider? A confirmed match nudges
        the stored print toward it (passive re-enrollment); a miss leaves the
        print untouched, so the patient's voice never contaminates it."""
        score = cosine(self._enrolled, embedding)
        matched = score >= self.threshold
        if matched and self.learn:
            self._re_enroll(embedding)
        return VerifyResult(matched=matched, score=score)

    def label_for(self, embedding: Sequence[float]) -> str:
        """The diarized label for a turn with this embedding: ``"Provider"`` when
        it verifies, else ``"Patient"`` — the streaming encounter's speaker ID."""
        return "Provider" if self.verify(embedding).matched else "Patient"

    def _re_enroll(self, embedding: Sequence[float], weight: float = RE_ENROLL_WEIGHT) -> None:
        self._enrolled = [
            (1.0 - weight) * cur + weight * float(obs)
            for cur, obs in zip(self._enrolled, embedding, strict=True)
        ]
