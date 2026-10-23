"""Phase-3 tests for the local STT backends (media/local.py).

The models need a CUDA GPU and the ``media`` extra, which CI has neither of, so
these cover the parts that do NOT: the WAV decode, the raw→Transcript mapping,
the provider-labelling heuristic, engine selection, and — importantly — that
asking a local engine to transcribe WITHOUT the extra fails loudly with an
install hint instead of silently echoing. The model inference itself is proven
on the GPU box by ``hdh media-benchmark``."""

from __future__ import annotations

import io
import wave

import pytest

from hdh.modules.media.backends import MediaError, Segment, make_backends
from hdh.modules.media.local import (
    NeMoTranscriber,
    WhisperXTranscriber,
    assign_provider,
    build_local_backends,
    decode_wav_pcm16,
    to_transcript,
)


def _wav(samples: list[int], rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        import struct

        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buf.getvalue()


# ── decode ─────────────────────────────────────────────────────────────────────


def test_decode_wav_round_trips_samples_rate_channels():
    data = _wav([0, 1000, -1000, 32767, -32768], rate=16000)
    samples, rate, channels = decode_wav_pcm16(data)
    assert samples == [0, 1000, -1000, 32767, -32768]
    assert rate == 16000
    assert channels == 1


def test_decode_rejects_non_wav():
    with pytest.raises(MediaError, match="not a readable WAV"):
        decode_wav_pcm16(b"this is not audio")


def test_decode_rejects_non_16_bit():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)  # 8-bit
        w.setframerate(8000)
        w.writeframes(b"\x00\x01\x02")
    with pytest.raises(MediaError, match="16-bit"):
        decode_wav_pcm16(buf.getvalue())


# ── provider heuristic ───────────────────────────────────────────────────────


def test_assign_provider_credits_the_longest_speaker():
    segs = [
        Segment(speaker="spk0", text="hi", t0=0, t1=1),
        Segment(speaker="spk1", text="doctor talks a lot", t0=1, t1=5),
        Segment(speaker="spk0", text="ok", t0=5, t1=6),
    ]
    out = assign_provider(segs)
    # spk1 spoke 4s vs spk0's 2s → spk1 is the Provider.
    assert out[1].speaker == "Provider"
    assert out[0].speaker == "Patient" and out[2].speaker == "Patient"


def test_assign_provider_single_speaker_is_all_provider():
    segs = [Segment(speaker="spk0", text="solo dictation", t0=0, t1=3)]
    assert assign_provider(segs)[0].speaker == "Provider"


def test_assign_provider_empty():
    assert assign_provider([]) == []


# ── raw → Transcript mapping ─────────────────────────────────────────────────


def test_to_transcript_flags_low_confidence_segments():
    raw = [
        {"speaker": "spk0", "text": "clear", "t0": 0, "t1": 3, "confidence": 0.95},
        {"speaker": "spk1", "text": "mumbled", "t0": 3, "t1": 4, "confidence": 0.4},
    ]
    t = to_transcript("clear mumbled", raw, model="nemo:test", duration_s=4.0)
    # spk0 spoke longer → Provider; its high confidence is trusted
    provider_seg = next(s for s in t.segments if s.speaker == "Provider")
    patient_seg = next(s for s in t.segments if s.speaker != "Provider")
    assert provider_seg.needs_review is False
    assert patient_seg.needs_review is True  # 0.4 < floor 0.6
    assert t.model == "nemo:test"
    assert t.duration_s == 4.0
    assert t.speakers == ["Provider", "Patient"]


def test_to_transcript_confidence_floor_is_configurable():
    raw = [{"speaker": "spk0", "text": "x", "t0": 0, "t1": 1, "confidence": 0.7}]
    assert to_transcript("x", raw, model="m", confidence_floor=0.6).segments[0].needs_review is False
    assert to_transcript("x", raw, model="m", confidence_floor=0.8).segments[0].needs_review is True


# ── engine selection + graceful failure without the extra ────────────────────


def test_build_local_backends_defaults_to_nemo():
    backends = build_local_backends()
    assert backends.name == "local:nemo"
    assert isinstance(backends.transcriber, NeMoTranscriber)


def test_build_local_backends_selects_whisperx(monkeypatch):
    monkeypatch.setenv("HDH_MEDIA_STT", "whisperx")
    backends = build_local_backends()
    assert backends.name == "local:whisperx"
    assert isinstance(backends.transcriber, WhisperXTranscriber)


def test_build_local_backends_rejects_unknown_engine(monkeypatch):
    monkeypatch.setenv("HDH_MEDIA_STT", "deepspeech")
    with pytest.raises(ValueError, match="unknown local STT engine"):
        build_local_backends()


def test_make_backends_local_builds_without_importing_models():
    # Selecting the local backend must not require the GPU extra — only
    # transcribing does. So make_backends("local") succeeds in CI.
    backends = make_backends("local")
    assert backends.name.startswith("local:")


def test_local_transcribe_without_extra_fails_with_install_hint():
    # nemo_toolkit is not installed in CI, so the lazy load raises ImportError,
    # which the backend turns into an actionable MediaError — never a silent echo.
    try:
        import nemo  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        nemo_present = False
    else:
        nemo_present = True
    if nemo_present:
        pytest.skip("nemo is installed; the no-extra path cannot be exercised here")
    with pytest.raises(MediaError, match="pip install"):
        NeMoTranscriber().transcribe(_wav([0, 1, 2]), "audio/wav")


def test_aws_backend_still_unbuilt():
    with pytest.raises(NotImplementedError, match="phase 7"):
        make_backends("aws")
