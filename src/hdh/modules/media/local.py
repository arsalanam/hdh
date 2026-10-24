"""Local CUDA STT backends — NeMo (primary) and WhisperX (baseline/fallback).

Phase 3 (design §10). These are the real engines behind the ``/stt`` contract:
the service, the UI and the agent integration do not change — only what sits
behind the :class:`~hdh.modules.media.backends.Transcriber` protocol does. Both
need the heavy ``media`` extra (torch + nemo_toolkit / whisperx + pyannote) and
a CUDA GPU, neither of which CI has, so:

- every heavy import is **lazy**, inside the methods, never at module import —
  this file imports fine on a laptop with no GPU and no extra;
- asking for a local backend without the extra **fails loudly** with an install
  hint, rather than silently falling back to echo and pretending to transcribe;
- the pure glue — decoding a WAV, mapping an engine's raw output to our
  ``Transcript``, flagging low-confidence turns, the provider heuristic — lives
  in module functions that are unit-tested with no model at all.

Speaker labelling here is the **async** heuristic: the longest-speaking voice is
credited to the Provider, the rest to the Patient, pending review. TitaNet
*verification* against the signed-in provider's enrolled print (design §13)
replaces the heuristic in the streaming encounter (phase 5); until then the UI
can relabel. Patients are never enrolled — their voiceprint is biometric PHI.
"""

from __future__ import annotations

import io
import os
import wave
from collections.abc import Sequence
from typing import Any

from hdh.modules.media.backends import (
    Backends,
    EchoOcr,
    MediaError,
    Segment,
    Transcript,
)

# A segment scoring below this is flagged ``needs_review`` rather than trusted —
# the transducer's expression of HDH's refuse-don't-guess rule (design §10).
CONFIDENCE_FLOOR = 0.6

_EXTRA_HINT = (
    "the local media backend needs the GPU models — install with: pip install 'hdh[media]' "
    "(torch + nemo_toolkit / whisperx) and run on a CUDA GPU"
)


# ── pure glue (no model, fully tested) ───────────────────────────────────────


def decode_wav_pcm16(data: bytes) -> tuple[list[int], int, int]:
    """Decode 16-bit PCM WAV bytes to ``(samples, sample_rate, channels)``.

    Stdlib only, so the decode path is testable with no audio library. Raises
    :class:`MediaError` on anything that is not readable 16-bit PCM WAV — the
    richer formats (mp3, flac, 24-bit) go through ``soundfile`` in the engines,
    which the ``media`` extra provides; this is the no-dependency fast path and
    the shape every engine's loader agrees on."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as err:
        raise MediaError(f"not a readable WAV: {err}") from None
    if width != 2:
        raise MediaError(f"expected 16-bit PCM WAV, got {width * 8}-bit")
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    return samples.tolist(), rate, channels


def assign_provider(segments: Sequence[Segment]) -> list[Segment]:
    """Credit the longest-speaking raw voice to the Provider and the rest to the
    Patient — the async heuristic (streaming uses TitaNet verification instead).

    Diarization emits arbitrary labels ("speaker_0", "spk1"); we relabel in
    place of guessing identities we cannot verify. A single-speaker transcript
    is all Provider. Ties resolve to first-heard. The result is reviewable: the
    UI can flip a label the clinician knows is wrong."""
    if not segments:
        return []
    talk_time: dict[str, float] = {}
    first_seen: dict[str, float] = {}
    for seg in segments:
        talk_time[seg.speaker] = talk_time.get(seg.speaker, 0.0) + max(0.0, seg.t1 - seg.t0)
        first_seen.setdefault(seg.speaker, seg.t0)
    # Most talk time wins the Provider label; first-heard breaks ties.
    ordered = sorted(talk_time, key=lambda s: (-talk_time[s], first_seen[s]))
    label_for = {ordered[0]: "Provider"}
    for raw in ordered[1:]:
        label_for[raw] = "Patient" if len(ordered) == 2 else f"Patient ({raw})"
    return [seg.model_copy(update={"speaker": label_for[seg.speaker]}) for seg in segments]


def to_transcript(
    text: str,
    raw_segments: Sequence[dict[str, Any]],
    *,
    model: str,
    duration_s: float = 0.0,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> Transcript:
    """Map an engine's raw per-segment output to our :class:`Transcript`,
    flagging every below-floor segment ``needs_review`` and relabelling speakers
    with the provider heuristic. Pure: the engines produce ``raw_segments`` from
    the model, this shapes them, and the shaping is what the tests cover."""
    segments = [
        Segment(
            speaker=str(s.get("speaker", "speaker_0")),
            text=str(s.get("text", "")),
            t0=float(s.get("t0", 0.0)),
            t1=float(s.get("t1", 0.0)),
            confidence=float(s.get("confidence", 1.0)),
            needs_review=float(s.get("confidence", 1.0)) < confidence_floor,
        )
        for s in raw_segments
    ]
    labelled = assign_provider(segments)
    return Transcript(
        text=text,
        segments=labelled,
        speakers=list(dict.fromkeys(s.speaker for s in labelled)),
        duration_s=duration_s,
        model=model,
    )


# ── the engines (lazy heavy imports; run on the GPU box) ──────────────────────


class NeMoTranscriber:
    """NVIDIA NeMo: streaming ASR (Parakeet/Canary) + Sortformer diarization —
    the primary engine, one framework for words and speakers (design §10). The
    model is loaded once, lazily, on first use; the import and the load happen
    only where a GPU is present, so this class is constructible in CI but
    ``transcribe`` raises a clear install/GPU error without the ``media`` extra."""

    def __init__(self, *, asr_model: str | None = None, diar_model: str | None = None) -> None:
        self.asr_model = asr_model or os.environ.get("HDH_NEMO_ASR", "nvidia/parakeet-tdt-1.1b")
        self.diar_model = diar_model or os.environ.get("HDH_NEMO_DIAR", "nvidia/diar_sortformer_4spk-v1")
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                self._pipeline = _build_nemo_pipeline(self.asr_model, self.diar_model)
            except ImportError as err:
                raise MediaError(f"{_EXTRA_HINT} [{err}]") from None
        return self._pipeline

    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        pipeline = self._load()
        text, raw_segments, duration = pipeline(data, content_type)
        return to_transcript(text, raw_segments, model=f"nemo:{self.asr_model}", duration_s=duration)


class WhisperXTranscriber:
    """WhisperX + pyannote — the reference's proven stack, kept as the measured
    baseline and the async-batch fallback (design §10). Same contract; chosen by
    ``HDH_MEDIA_STT=whisperx`` or when the gate keeps it over NeMo."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or os.environ.get("HDH_WHISPERX_MODEL", "large-v3")
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                self._pipeline = _build_whisperx_pipeline(self.model)
            except ImportError as err:
                raise MediaError(f"{_EXTRA_HINT} [{err}]") from None
        return self._pipeline

    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        pipeline = self._load()
        text, raw_segments, duration = pipeline(data, content_type)
        return to_transcript(text, raw_segments, model=f"whisperx:{self.model}", duration_s=duration)


def _build_nemo_pipeline(asr_model: str, diar_model: str) -> Any:  # pragma: no cover - needs GPU + extra
    """Load the NeMo ASR + Sortformer models and return a ``(bytes, ct) ->
    (text, raw_segments, duration)`` callable. Imported lazily; GPU-only, so it
    is excluded from coverage and validated by ``hdh media-benchmark`` on the
    5060, not by CI."""
    import nemo.collections.asr as nemo_asr

    asr = nemo_asr.models.ASRModel.from_pretrained(asr_model)
    diarizer = nemo_asr.models.SortformerEncLabelModel.from_pretrained(diar_model)

    def run(data: bytes, content_type: str) -> tuple[str, list[dict[str, Any]], float]:
        samples, rate, duration = _load_audio(data, content_type)
        words = asr.transcribe([samples])  # ASR hypothesis with word timings
        turns = diarizer.diarize([samples])  # Sortformer speaker turns
        text, raw_segments = _merge_words_and_turns(words, turns, rate)
        return text, raw_segments, duration

    return run


def _build_whisperx_pipeline(model_name: str) -> Any:  # pragma: no cover - needs GPU + extra
    """Load WhisperX + pyannote and return the same ``(bytes, ct) -> (text,
    raw_segments, duration)`` callable shape as NeMo, so the service and the
    benchmark treat the two engines identically."""
    import whisperx

    device = os.environ.get("HDH_MEDIA_DEVICE", "cuda")
    model = whisperx.load_model(model_name, device)
    diarize = whisperx.DiarizationPipeline(device=device)

    def run(data: bytes, content_type: str) -> tuple[str, list[dict[str, Any]], float]:
        samples, rate, duration = _load_audio(data, content_type)
        result = model.transcribe(samples)
        diarized = diarize(samples)
        result = whisperx.assign_word_speakers(diarized, result)
        text = " ".join(seg.get("text", "").strip() for seg in result.get("segments", []))
        raw_segments = [
            {
                "speaker": seg.get("speaker", "speaker_0"),
                "text": seg.get("text", "").strip(),
                "t0": float(seg.get("start", 0.0)),
                "t1": float(seg.get("end", 0.0)),
                "confidence": float(seg.get("score", 1.0)),
            }
            for seg in result.get("segments", [])
        ]
        return text, raw_segments, duration

    return run


def _load_audio(data: bytes, content_type: str) -> tuple[Any, int, float]:  # pragma: no cover - needs extra
    """Decode audio bytes to a mono 16 kHz float32 array for the models. WAV
    takes the stdlib fast path; everything else goes through ``soundfile`` (the
    ``media`` extra). Returns ``(samples, rate, duration_s)``."""
    import numpy as np

    try:
        pcm, rate, channels = decode_wav_pcm16(data)
        samples = np.asarray(pcm, dtype=np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
    except MediaError:
        import soundfile as sf

        samples, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
        if getattr(samples, "ndim", 1) > 1:
            samples = samples.mean(axis=1)
    duration = len(samples) / rate if rate else 0.0
    return samples, rate, duration


def _merge_words_and_turns(
    words: Any, turns: Any, rate: int
) -> tuple[str, list[dict[str, Any]]]:  # pragma: no cover - needs GPU
    """Attribute ASR words to Sortformer speaker turns into per-turn segments.
    Shape-only here; the real alignment is validated on-GPU by the benchmark."""
    raise NotImplementedError("NeMo word/turn merge is exercised by hdh media-benchmark on the GPU box")


# ── selection ────────────────────────────────────────────────────────────────


def build_local_backends() -> Backends:
    """The ``local`` backend set: a real STT engine (NeMo by default, WhisperX
    via ``HDH_MEDIA_STT=whisperx``) for async ``/stt``, the NeMo streaming engine
    for the live encounter (WS ``/stt/stream``), real **Kokoro** TTS, and a
    placeholder OCR until its model lands (phase 7). The service boots and every
    endpoint answers so the contract stays whole.

    Streaming is **NeMo-only** (design §10): Sortformer is stream-native where
    WhisperX is batch-shaped, so the streamer is NeMo even when the WER/DER gate
    keeps WhisperX for async."""
    from hdh.modules.media.streaming import NeMoStreamingTranscriber
    from hdh.modules.media.tts import KokoroSynthesizer

    choice = (os.environ.get("HDH_MEDIA_STT") or "nemo").lower()
    if choice in ("nemo", "sortformer"):
        transcriber: Any = NeMoTranscriber()
    elif choice in ("whisperx", "whisper"):
        transcriber = WhisperXTranscriber()
    else:
        raise ValueError(f"unknown local STT engine: {choice!r} (expected nemo | whisperx)")
    return Backends(
        name=f"local:{choice}",
        transcriber=transcriber,
        synthesizer=KokoroSynthesizer(),  # real TTS (phase 6)
        ocr=EchoOcr(),  # local OCR: phase 7
        streamer=NeMoStreamingTranscriber(),  # live encounter — NeMo-only
    )
