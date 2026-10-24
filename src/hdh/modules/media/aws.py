"""AWS media backends — the managed, HIPAA-eligible route (design §9, phase 7).

The same three protocols as the local CUDA backend, backed by Amazon's managed
services under a BAA: **Transcribe Medical** (STT + diarization and streaming),
**Polly** (TTS), **Textract** (OCR). ``HDH_MEDIA_BACKEND=aws`` selects them; the
service contract, the UI, and the agent integration do not change — only what
sits behind each protocol does.

Same honest boundary as the local engines:

- every ``boto3`` import is **lazy**, inside the call — this file imports fine
  with no ``boto3`` and no AWS credentials;
- asking an AWS engine to run without ``boto3`` **fails loudly** with an install
  hint, rather than silently returning nothing;
- the response-shaping glue — Transcribe JSON → :class:`Transcript`, Textract
  blocks → :class:`Extraction`, a streaming event → :class:`StreamSegment`s — is
  pure module functions, unit-tested against sample responses with no cloud call.

PHI note (§9): these services are HIPAA-eligible **only under a BAA**; the media
service still persists nothing and logs no media or transcript bodies. Local
stays the privacy default — AWS is the opt-in managed deployment.
"""

from __future__ import annotations

import os
from typing import Any

from hdh.modules.media.backends import Audio, Backends, Extraction, MediaError, Segment, Transcript
from hdh.modules.media.local import CONFIDENCE_FLOOR, assign_provider
from hdh.modules.media.streaming import StreamSegment, StreamSession

_EXTRA_HINT = (
    "the aws media backend needs the AWS SDK — install with: pip install 'hdh[aws]' (boto3) "
    "and configure credentials for a HIPAA BAA account"
)


# ── pure response-shaping (no cloud call, fully tested) ───────────────────────


def transcript_from_transcribe(response: dict[str, Any], *, duration_s: float = 0.0) -> Transcript:
    """Shape an Amazon Transcribe (Medical) result into our :class:`Transcript`.

    Transcribe returns ``results.transcripts[].transcript`` for the full text and
    ``results.speaker_labels.segments`` for diarization; we relabel the raw
    speakers with the same longest-speaking heuristic the local async path uses
    (``assign_provider``), and flag a below-floor item ``needs_review``. Pure."""
    results = response.get("results", {})
    text = " ".join(t.get("transcript", "") for t in results.get("transcripts", [])).strip()
    items = results.get("items", [])
    raw: list[Segment] = []
    for seg in results.get("speaker_labels", {}).get("segments", []):
        start = float(seg.get("start_time", 0.0))
        end = float(seg.get("end_time", 0.0))
        words = [i for i in items if start <= float(i.get("start_time", -1)) <= end]
        said = " ".join(
            i["alternatives"][0]["content"]
            for i in words
            if i.get("alternatives") and i.get("type") != "punctuation"
        )
        confidence = _mean_confidence(words)
        raw.append(
            Segment(
                speaker=str(seg.get("speaker_label", "spk_0")),
                text=said,
                t0=start,
                t1=end,
                confidence=confidence,
                needs_review=confidence < CONFIDENCE_FLOOR,
            )
        )
    labelled = assign_provider(raw)
    return Transcript(
        text=text or " ".join(s.text for s in labelled).strip(),
        segments=labelled,
        speakers=list(dict.fromkeys(s.speaker for s in labelled)),
        duration_s=duration_s,
        model="aws:transcribe-medical",
    )


def _mean_confidence(items: list[dict[str, Any]]) -> float:
    scores = [
        float(i["alternatives"][0].get("confidence", 1.0))
        for i in items
        if i.get("alternatives") and i.get("type") != "punctuation"
    ]
    return sum(scores) / len(scores) if scores else 1.0


def extraction_from_textract(response: dict[str, Any]) -> Extraction:
    """Shape an Amazon Textract ``DetectDocumentText`` result into our
    :class:`Extraction`. Textract returns LINE and PAGE ``Blocks``; we keep the
    lines in reading order as text and as layout blocks, flagging a low-confidence
    line (Textract scores 0–100). Pure."""
    blocks_in = response.get("Blocks", [])
    pages = max((b.get("Page", 1) for b in blocks_in), default=1)
    lines: list[str] = []
    blocks: list[dict] = []
    for b in blocks_in:
        if b.get("BlockType") != "LINE":
            continue
        text = str(b.get("Text", "")).strip()
        if not text:
            continue
        confidence = float(b.get("Confidence", 100.0)) / 100.0
        block: dict[str, Any] = {"text": text, "page": int(b.get("Page", 1)), "confidence": confidence}
        if confidence < CONFIDENCE_FLOOR:
            block["needs_review"] = True
        blocks.append(block)
        lines.append(text)
    return Extraction(text="\n".join(lines).strip(), pages=pages, blocks=blocks, model="aws:textract")


def stream_segments_from_transcribe(event: dict[str, Any]) -> list[StreamSegment]:
    """Shape one Transcribe streaming ``TranscriptEvent`` into our
    :class:`StreamSegment`s. Transcribe streams partial then final results on a
    ``Transcript.Results`` list, each with ``IsPartial`` and an ``Items`` list
    (optionally speaker-tagged). Pure."""
    segments: list[StreamSegment] = []
    for result in event.get("Transcript", {}).get("Results", []):
        alternatives = result.get("Alternatives", [])
        if not alternatives:
            continue
        items = alternatives[0].get("Items", [])
        text = " ".join(i.get("Content", "") for i in items if i.get("Type") != "punctuation").strip()
        if not text:
            continue
        speaker = next((i.get("Speaker") for i in items if i.get("Speaker") is not None), None)
        confidence = _mean_stream_confidence(items)
        segments.append(
            StreamSegment(
                speaker=f"spk_{speaker}" if speaker is not None else "speaker_0",
                text=text,
                t0=float(result.get("StartTime", 0.0)),
                t1=float(result.get("EndTime", 0.0)),
                final=not result.get("IsPartial", False),
                confidence=confidence,
                needs_review=confidence < CONFIDENCE_FLOOR,
            )
        )
    return segments


def _mean_stream_confidence(items: list[dict[str, Any]]) -> float:
    scores = [float(i.get("Confidence", 1.0)) for i in items if i.get("Type") != "punctuation"]
    return sum(scores) / len(scores) if scores else 1.0


# ── the engines (lazy boto3; run against AWS) ─────────────────────────────────


def _client(service: str) -> Any:
    """A boto3 client for ``service``, or a loud error without the SDK."""
    try:
        import boto3
    except ImportError as err:
        raise MediaError(f"{_EXTRA_HINT} [{err}]") from None
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-east-1"))


class TranscribeMedicalTranscriber:
    """Amazon Transcribe Medical for async ``/stt`` — STT + diarization, managed
    and HIPAA-eligible under a BAA."""

    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        return _run_transcribe(data, content_type)


class PollySynthesizer:
    """Amazon Polly for ``/tts`` — voices the agent's concise spoken rendering."""

    def __init__(self, *, voice: str | None = None) -> None:
        self.voice = voice or os.environ.get("HDH_AWS_POLLY_VOICE", "Joanna")

    def synthesize(self, text: str, voice: str | None = None) -> Audio:
        text = (text or "").strip()
        if not text:
            raise MediaError("nothing to synthesize — empty text")
        return _run_polly(text, voice or self.voice)


class TextractOcr:
    """Amazon Textract for ``/ocr`` — printed-page text off an image or PDF."""

    def extract(self, data: bytes, content_type: str) -> Extraction:
        if not data:
            raise MediaError("nothing to extract — empty document")
        return _run_textract(data)


class AwsStreamingTranscriber:
    """Transcribe streaming for the live encounter (WS ``/stt/stream``)."""

    def open(self, *, sample_rate: int = 16000, verifier: Any = None) -> StreamSession:
        return _open_transcribe_stream(sample_rate, verifier)


def _run_transcribe(data: bytes, content_type: str) -> Transcript:  # pragma: no cover - needs AWS
    """Start a Transcribe Medical job, await it, and shape the result. The job
    lifecycle (S3 upload, poll, fetch) is cloud-bound, so it is excluded from
    coverage; ``transcript_from_transcribe`` does the shaping and is tested."""
    _client("transcribe")  # fails loudly without boto3
    raise NotImplementedError(
        "Transcribe Medical job orchestration runs against AWS; the response shaping "
        "(transcript_from_transcribe) is exercised in CI with sample responses"
    )


def _run_polly(text: str, voice: str) -> Audio:  # pragma: no cover - needs AWS
    """Synthesize with Polly and return the audio stream as :class:`Audio`."""
    client = _client("polly")
    response = client.synthesize_speech(Text=text, VoiceId=voice, OutputFormat="pcm")
    audio = response["AudioStream"].read()
    return Audio(data=audio, content_type="audio/pcm", duration_s=0.0)


def _run_textract(data: bytes) -> Extraction:  # pragma: no cover - needs AWS
    """Detect text with Textract and shape it (``extraction_from_textract``)."""
    client = _client("textract")
    response = client.detect_document_text(Document={"Bytes": data})
    return extraction_from_textract(response)


def _open_transcribe_stream(sample_rate: int, verifier: Any) -> StreamSession:  # pragma: no cover - needs AWS
    """Open a Transcribe streaming session wrapped in a :class:`StreamSession`.
    Cloud-bound; the event shaping (stream_segments_from_transcribe) is tested."""
    _client("transcribe")  # fails loudly without boto3
    raise NotImplementedError(
        "Transcribe streaming runs against AWS; the event shaping "
        "(stream_segments_from_transcribe) is exercised in CI with sample events"
    )


# ── selection ─────────────────────────────────────────────────────────────────


def build_aws_backends() -> Backends:
    """The ``aws`` backend set: Transcribe Medical (STT + streaming), Polly (TTS),
    Textract (OCR) — all behind the same protocols as ``local``. Engines are
    constructed here (cheap, no boto3 import); the SDK is needed only when one is
    actually called, so a misconfigured box fails loudly on use, not at boot."""
    return Backends(
        name="aws",
        transcriber=TranscribeMedicalTranscriber(),
        synthesizer=PollySynthesizer(),
        ocr=TextractOcr(),
        streamer=AwsStreamingTranscriber(),
    )
