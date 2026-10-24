"""Phase-6 tests for local TTS — the pure WAV encoding and the Kokoro engine's
loud-failure contract (design §6, §10). No GPU, no model: the encoding is
stdlib and the engine's missing-extra path is forced with monkeypatch."""

from __future__ import annotations

import io
import wave

import pytest

from hdh.modules.media.backends import MediaError, SilentSynthesizer
from hdh.modules.media.tts import (
    KOKORO_SAMPLE_RATE,
    KokoroSynthesizer,
    float_pcm_to_wav,
    wav_duration_s,
)

# ── the pure WAV encoding ──────────────────────────────────────────────────────


def test_float_pcm_to_wav_is_a_real_playable_wav():
    samples = [0.0, 0.5, -0.5, 1.0, -1.0] * 100
    data = float_pcm_to_wav(samples, 24000)
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2  # 16-bit
        assert wav.getframerate() == 24000
        assert wav.getnframes() == len(samples)


def test_float_pcm_clamps_out_of_range_samples():
    # Values beyond [-1, 1] must not wrap into noise — they clamp to the rails.
    data = float_pcm_to_wav([2.0, -2.0], 24000)
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = wav.readframes(2)
    hi = int.from_bytes(frames[0:2], "little", signed=True)
    lo = int.from_bytes(frames[2:4], "little", signed=True)
    assert hi == 32767 and lo == -32767


def test_wav_duration_tracks_sample_count():
    assert wav_duration_s([0.0] * 24000, 24000) == pytest.approx(1.0)
    assert wav_duration_s([], 24000) == 0.0


# ── the Kokoro engine ──────────────────────────────────────────────────────────


def test_kokoro_rejects_empty_text():
    with pytest.raises(MediaError, match="nothing to synthesize"):
        KokoroSynthesizer().synthesize("   ")


def test_kokoro_turns_a_missing_extra_into_a_loud_error(monkeypatch):
    # Constructible with no GPU; synthesizing without the Kokoro model surfaces
    # a MediaError with an install hint — never silent, never a different voice.
    from hdh.modules.media import tts

    def _missing(*_args, **_kwargs):
        raise ImportError("No module named 'kokoro'")

    monkeypatch.setattr(tts, "_build_kokoro_pipeline", _missing)
    with pytest.raises(MediaError, match="pip install 'hdh\\[media\\]'"):
        KokoroSynthesizer().synthesize("Charted to visit twelve.")


def test_kokoro_synthesizes_via_an_injected_pipeline(monkeypatch):
    # With the model stubbed, the engine shapes samples into a playable WAV and
    # reports a duration — the glue around the model, exercised without it.
    from hdh.modules.media import tts

    def _fake_pipeline(_lang):
        def run(_text, _voice):
            return [0.1] * KOKORO_SAMPLE_RATE  # one second of tone

        return run

    monkeypatch.setattr(tts, "_build_kokoro_pipeline", _fake_pipeline)
    audio = KokoroSynthesizer().synthesize("Three patients have high blood pressure.")
    assert audio.content_type == "audio/wav"
    assert audio.duration_s == pytest.approx(1.0)
    with wave.open(io.BytesIO(audio.data), "rb") as wav:
        assert wav.getframerate() == KOKORO_SAMPLE_RATE


# ── backend selection ──────────────────────────────────────────────────────────


def test_local_backend_uses_kokoro_for_tts():
    from hdh.modules.media.local import build_local_backends

    backends = build_local_backends()
    assert isinstance(backends.synthesizer, KokoroSynthesizer)  # real TTS now (phase 6)


def test_null_backend_still_uses_the_silent_synthesizer():
    from hdh.modules.media.backends import make_backends

    assert isinstance(make_backends("null").synthesizer, SilentSynthesizer)  # CI-safe default
