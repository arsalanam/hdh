"""Media transduction backends — protocols, results, and a null/echo default.

The media service is a **stateless transducer** (design docs/design/media-io-service.md):
bytes ↔ text, no clinical logic, no PHI at rest. Every model sits behind one of
three protocols so the deployment target — local CUDA now, AWS later — is a
config choice, not a rewrite. ``HDH_MEDIA_BACKEND`` picks the implementation at
startup; the **null/echo** backend is the default so the HTTP contract, the UI,
and the agent integration are all testable with no GPU and no cloud.

Phase 1 ships the contract and the null backend only. ``local`` (NeMo /
Sortformer / TitaNet / Kokoro) and ``aws`` (Transcribe Medical / Polly /
Textract) arrive in later phases behind these same protocols.
"""

from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class MediaError(Exception):
    """A transduction could not be produced (bad media, model failure)."""


# ── result contracts ─────────────────────────────────────────────────────────


class Segment(BaseModel):
    """One diarized span: who said what, when, and how sure we are."""

    speaker: str  # "Provider" | "Patient" | "speaker-N"
    text: str
    t0: float = 0.0
    t1: float = 0.0
    confidence: float = 1.0
    needs_review: bool = False  # low-confidence → the review queue, never a guess


class Transcript(BaseModel):
    """A speech-to-text result: full text plus diarized segments."""

    text: str
    segments: list[Segment] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
    model: str = ""


class Extraction(BaseModel):
    """An OCR result: text off an image or PDF, with optional layout blocks."""

    text: str
    pages: int = 1
    #: Optional per-region layout — {text, page, confidence?, bbox?}. Printed-page
    #: engines (docTR, Textract) populate it; the echo placeholder leaves it empty.
    blocks: list[dict] = Field(default_factory=list)
    model: str = ""


@dataclass(frozen=True)
class Audio:
    """Synthesized speech — not a JSON body, so a dataclass, not a model."""

    data: bytes
    content_type: str = "audio/wav"
    duration_s: float = 0.0


# ── protocols ────────────────────────────────────────────────────────────────


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, data: bytes, content_type: str) -> Transcript: ...


@runtime_checkable
class Synthesizer(Protocol):
    def synthesize(self, text: str, voice: str | None = None) -> Audio: ...


@runtime_checkable
class OcrEngine(Protocol):
    def extract(self, data: bytes, content_type: str) -> Extraction: ...


# ── null / echo backend (the testable default — no GPU, no cloud) ─────────────


def _as_text(data: bytes, content_type: str, kind: str) -> str:
    """Echo the input as text when it decodes cleanly, else a stub that names
    the shape — enough to exercise the contract without a real model."""
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = ""
    return text or f"[{kind} stub: {len(data)} bytes {content_type or 'unknown'}]"


class EchoTranscriber:
    """Returns a single-speaker transcript echoing the input text."""

    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        text = _as_text(data, content_type, "stt")
        seg = Segment(speaker="Provider", text=text, confidence=1.0)
        return Transcript(text=text, segments=[seg], speakers=["Provider"], model="null-echo")


class SilentSynthesizer:
    """Returns a valid silent WAV sized to the text — real, playable audio so
    the UI's playback path is exercised without a TTS model."""

    def synthesize(self, text: str, voice: str | None = None) -> Audio:
        duration = min(10.0, max(0.3, len(text) * 0.05))
        rate = 16000
        frames = int(duration * rate)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(rate)
            wav.writeframes(b"\x00\x00" * frames)
        return Audio(data=buf.getvalue(), content_type="audio/wav", duration_s=duration)


class EchoOcr:
    """Returns an extraction echoing the input text."""

    def extract(self, data: bytes, content_type: str) -> Extraction:
        return Extraction(text=_as_text(data, content_type, "ocr"), pages=1, model="null-echo")


# ── selection ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Backends:
    """The transducers a service instance runs, plus the chosen name. ``streamer``
    drives the live encounter (WS ``/stt/stream``, phase 5) alongside the async
    ``transcriber``; it is a :class:`~hdh.modules.media.streaming.StreamingTranscriber`
    (typed loosely here to keep this module free of the streaming import)."""

    name: str
    transcriber: Transcriber
    synthesizer: Synthesizer
    ocr: OcrEngine
    streamer: object


def make_backends(name: str | None = None) -> Backends:
    """Build the backend set named by ``name`` (or ``HDH_MEDIA_BACKEND``,
    default ``null``). ``local`` and ``aws`` are not built yet — asking for one
    fails loudly at startup rather than pretending to transduce."""
    chosen = (name or os.environ.get("HDH_MEDIA_BACKEND") or "null").lower()
    if chosen in ("null", "echo"):
        # Imported here (not at module top) so backends.py stays free of the
        # streaming import — streaming.py depends on this module.
        from hdh.modules.media.streaming import EchoStreamingTranscriber

        return Backends(
            name="null",
            transcriber=EchoTranscriber(),
            synthesizer=SilentSynthesizer(),
            ocr=EchoOcr(),
            streamer=EchoStreamingTranscriber(),
        )
    if chosen == "local":
        # Real CUDA STT (NeMo / WhisperX) behind the same protocols. Imported
        # lazily so the heavy `media` extra is needed only when a local backend
        # is actually requested — null stays dependency-free (design §10, phase 3).
        from hdh.modules.media.local import build_local_backends

        return build_local_backends()
    if chosen == "aws":
        # Managed, HIPAA-eligible route (Transcribe Medical / Polly / Textract).
        # Imported lazily so boto3 is needed only when the aws backend is chosen.
        from hdh.modules.media.aws import build_aws_backends

        return build_aws_backends()
    raise ValueError(f"unknown media backend: {chosen!r} (expected null | local | aws)")
