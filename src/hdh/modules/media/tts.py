"""Local text-to-speech — Kokoro (design §6, §10, phase 6).

The media service voices the agent's concise ``spoken`` rendering; it originates
no words of its own (what to say aloud is the agent's grounded decision, never
the TTS layer's invention — design §6). This is the real engine behind the
``/tts`` contract and the :class:`~hdh.modules.media.backends.Synthesizer`
protocol: **Kokoro-82M** on CUDA/ONNX.

Same honest boundary as STT (phase 3/5):

- the heavy import is **lazy**, inside the method — this file imports fine with
  no ``media`` extra and no GPU;
- asking for Kokoro without the extra **fails loudly** with an install hint,
  never a silent fallback to a different voice or to silence;
- the pure glue — turning float PCM samples into a playable WAV — is a module
  function unit-tested with no model at all.

The null backend keeps :class:`~hdh.modules.media.backends.SilentSynthesizer`
(a real, playable silent WAV) so the UI playback path and the ``/tts`` contract
are testable in CI; Kokoro is selected only for the ``local`` backend.
"""

from __future__ import annotations

import io
import os
import wave
from collections.abc import Sequence
from typing import Any

from hdh.modules.media.backends import Audio, MediaError

# Kokoro-82M synthesizes at 24 kHz; the default voice is a standard American
# English preset. Both overridable by env for a deployment that enrolled others.
KOKORO_SAMPLE_RATE = 24000
DEFAULT_VOICE = "af_heart"

_EXTRA_HINT = (
    "the local TTS backend needs the Kokoro model — install with: pip install 'hdh[media]' "
    "(kokoro + torch) and run on a CUDA GPU"
)


# ── pure glue (no model, fully tested) ───────────────────────────────────────


def float_pcm_to_wav(samples: Sequence[float], sample_rate: int = KOKORO_SAMPLE_RATE) -> bytes:
    """Encode mono float samples in ``[-1, 1]`` as 16-bit PCM WAV bytes.

    Stdlib only, so the encode path is testable with no audio library and the
    bytes are a real, playable WAV. Out-of-range samples are clamped rather than
    allowed to wrap into noise."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        frames = bytearray()
        for sample in samples:
            clamped = -1.0 if sample < -1.0 else 1.0 if sample > 1.0 else float(sample)
            value = int(clamped * 32767)
            frames += int.to_bytes(value & 0xFFFF, 2, "little")
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def wav_duration_s(samples: Sequence[float], sample_rate: int = KOKORO_SAMPLE_RATE) -> float:
    """The duration of a mono clip, for the ``X-Audio-Duration`` header."""
    return (len(samples) / sample_rate) if sample_rate else 0.0


# ── the engine (lazy heavy import; runs on the GPU box) ───────────────────────


class KokoroSynthesizer:
    """Kokoro-82M TTS. Constructible in CI; ``synthesize`` raises a clear
    install/GPU error without the ``media`` extra rather than voicing silence."""

    def __init__(self, *, voice: str | None = None, lang: str | None = None) -> None:
        self.voice = voice or os.environ.get("HDH_TTS_VOICE", DEFAULT_VOICE)
        self.lang = lang or os.environ.get("HDH_TTS_LANG", "a")  # 'a' = American English
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                self._pipeline = _build_kokoro_pipeline(self.lang)
            except ImportError as err:
                raise MediaError(f"{_EXTRA_HINT} [{err}]") from None
        return self._pipeline

    def synthesize(self, text: str, voice: str | None = None) -> Audio:
        text = (text or "").strip()
        if not text:
            raise MediaError("nothing to synthesize — empty text")
        pipeline = self._load()
        samples = pipeline(text, voice or self.voice)
        return Audio(
            data=float_pcm_to_wav(samples, KOKORO_SAMPLE_RATE),
            content_type="audio/wav",
            duration_s=wav_duration_s(samples, KOKORO_SAMPLE_RATE),
        )


def _build_kokoro_pipeline(lang: str) -> Any:  # pragma: no cover - needs the media extra + model
    """Load Kokoro and return a ``(text, voice) -> list[float]`` callable. Imported
    lazily; GPU/model-bound, so excluded from coverage and validated on the 5060,
    not in CI."""
    from kokoro import KPipeline

    pipeline = KPipeline(lang_code=lang)

    def run(text: str, voice: str) -> list[float]:
        chunks: list[float] = []
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice):
            chunks.extend(float(x) for x in audio)
        return chunks

    return run
