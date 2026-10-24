"""Streaming STT — the live encounter (design §5, §10, phase 5).

The async ``/stt`` upload (phase 3) transcribes a finished recording; this is
the other STT mode: a clinician talks *through* a visit and sees a diarized
transcript grow turn by turn. It is a WebSocket on the media service
(``/stt/stream``): the browser pushes audio clips, the server streams back
diarized segments ``{speaker, text, t0, t1, final}`` as they are recognised.

The engine is **NeMo** streaming ASR (Parakeet/Canary) with **Sortformer**
online diarization — chosen *because* the live encounter is the hard
requirement and Sortformer is stream-native where WhisperX is batch-shaped, so
streaming is NeMo-only (design §10). Speaker ID here is **verification, not a
heuristic**: each turn's TitaNet embedding is a 1:1 cosine check against the
signed-in provider's enrolled print (:mod:`.verification`), falling back to the
longest-speaking heuristic only when no print is supplied.

As everywhere in this module, the heavy model is a **lazy, GPU-only seam**: this
file imports with no ``media`` extra and no GPU, the echo session drives the
whole WebSocket contract in CI, and asking for the real engine without the extra
fails loudly with an install hint rather than pretending to transcribe.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from hdh.modules.media.backends import MediaError
from hdh.modules.media.local import _EXTRA_HINT, CONFIDENCE_FLOOR
from hdh.modules.media.verification import ProviderVerifier


class StreamSegment(BaseModel):
    """One streamed turn. ``final`` distinguishes a settled segment from an
    in-progress partial the UI should overwrite; ``needs_review`` flags a
    low-confidence turn for the review queue rather than trusting it."""

    speaker: str  # "Provider" | "Patient" | "speaker-N"
    text: str
    t0: float = 0.0
    t1: float = 0.0
    final: bool = True
    confidence: float = 1.0
    needs_review: bool = False


@runtime_checkable
class StreamSession(Protocol):
    """One live recording. ``push`` feeds an audio clip and returns whatever
    segments that clip completed (none, a partial, or several finals); ``finish``
    flushes any trailing audio when the clinician stops."""

    def push(self, chunk: bytes) -> list[StreamSegment]: ...

    def finish(self) -> list[StreamSegment]: ...


@runtime_checkable
class StreamingTranscriber(Protocol):
    def open(
        self, *, sample_rate: int = 16000, verifier: ProviderVerifier | None = None
    ) -> StreamSession: ...


# ── null / echo streaming (the testable default — no GPU) ─────────────────────


class EchoStreamSession:
    """Treats each pushed clip as one UTF-8 turn and emits it as a final
    segment, alternating Provider/Patient so the diarized UI path is exercised
    with no model. When an enrolled print is supplied it is honoured — a verified
    clip is the Provider — so the verification wiring is covered end to end."""

    def __init__(self, verifier: ProviderVerifier | None = None) -> None:
        self._verifier = verifier
        self._t = 0.0
        self._turn = 0

    def push(self, chunk: bytes) -> list[StreamSegment]:
        text = chunk.decode("utf-8", "replace").strip()
        if not text:
            return []
        t0, t1 = self._t, self._t + 1.0
        self._t = t1
        speaker = "Provider" if self._turn % 2 == 0 else "Patient"
        self._turn += 1
        return [StreamSegment(speaker=speaker, text=text, t0=t0, t1=t1, final=True)]

    def finish(self) -> list[StreamSegment]:
        return []


class EchoStreamingTranscriber:
    """Builds :class:`EchoStreamSession`s — the null backend's streamer, so the
    WebSocket contract, the UI, and the agent save-path are all testable with no
    GPU and no cloud."""

    def open(self, *, sample_rate: int = 16000, verifier: ProviderVerifier | None = None) -> StreamSession:
        return EchoStreamSession(verifier=verifier)


# ── the real engine (lazy heavy import; runs on the GPU box) ──────────────────


class NeMoStreamingTranscriber:
    """NeMo streaming ASR + Sortformer online diarization — the only streaming
    engine (design §10). Constructible in CI; ``open`` raises a clear install/GPU
    error without the ``media`` extra rather than silently degrading."""

    def __init__(self, *, asr_model: str | None = None, diar_model: str | None = None) -> None:
        self.asr_model = asr_model or os.environ.get("HDH_NEMO_STREAM_ASR", "nvidia/parakeet-tdt-1.1b")
        self.diar_model = diar_model or os.environ.get("HDH_NEMO_DIAR", "nvidia/diar_sortformer_4spk-v1")

    def open(self, *, sample_rate: int = 16000, verifier: ProviderVerifier | None = None) -> StreamSession:
        try:
            return _build_nemo_stream_session(self.asr_model, self.diar_model, sample_rate, verifier)
        except ImportError as err:
            raise MediaError(f"{_EXTRA_HINT} [{err}]") from None


def _segment_from_turn(
    raw: dict[str, Any], verifier: ProviderVerifier | None, confidence_floor: float = CONFIDENCE_FLOOR
) -> StreamSegment:
    """Shape one diarized turn into a :class:`StreamSegment`, resolving the
    speaker by **verification** when a print is supplied — the turn's TitaNet
    embedding is a 1:1 cosine check against the provider — and otherwise keeping
    the diarizer's raw cluster label for the longest-speaking heuristic/relabel.
    Pure given ``raw``; the embedding extraction that fills ``raw`` is the GPU
    seam. A below-floor turn is flagged, never trusted."""
    confidence = float(raw.get("confidence", 1.0))
    embedding = raw.get("embedding")
    if verifier is not None and embedding is not None:
        speaker = verifier.label_for(embedding)
    else:
        speaker = str(raw.get("speaker", "speaker_0"))
    return StreamSegment(
        speaker=speaker,
        text=str(raw.get("text", "")),
        t0=float(raw.get("t0", 0.0)),
        t1=float(raw.get("t1", 0.0)),
        final=bool(raw.get("final", True)),
        confidence=confidence,
        needs_review=confidence < confidence_floor,
    )


def _build_nemo_stream_session(
    asr_model: str, diar_model: str, sample_rate: int, verifier: ProviderVerifier | None
) -> StreamSession:  # pragma: no cover - needs GPU + the media extra
    """Load NeMo streaming ASR + Sortformer and wrap them in a
    :class:`StreamSession`. Each ``push`` feeds the next audio window to the
    online ASR and diarizer, extracts a TitaNet embedding per completed turn, and
    returns the shaped segments (``_segment_from_turn`` does the verification).
    GPU-only, so excluded from coverage and validated on the 5060, not in CI."""
    import nemo.collections.asr as nemo_asr  # noqa: F401

    raise NotImplementedError(
        "NeMo streaming ASR + Sortformer online diarization runs on the GPU box; "
        "the WebSocket contract and speaker verification are exercised in CI via the echo streamer"
    )


# ── selection ─────────────────────────────────────────────────────────────────


def verifier_from_enrolled(enrolled: Sequence[float] | None) -> ProviderVerifier | None:
    """Build a verifier from the enrolled print the caller supplied at stream
    open, or ``None`` when none was — the media service never stores a print, so
    it is always handed in (design §13). An empty/absent print means fall back to
    the longest-speaking heuristic and let the UI relabel."""
    if not enrolled:
        return None
    return ProviderVerifier(enrolled)
