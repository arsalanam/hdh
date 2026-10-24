"""AWS media backends — the managed, HIPAA-eligible route (design §9, phase 7).

The same three protocols as the local CUDA backend, backed by Amazon's managed
services under a BAA: **Transcribe Medical** (STT + diarization), **Polly**
(TTS), **Textract** (OCR). ``HDH_MEDIA_BACKEND=aws`` selects them; the service
contract, the UI, and the agent integration do not change — only what sits
behind each protocol does.

Both STT paths run a **batch** Transcribe Medical job (upload to S3, start,
poll, fetch): the async ``/stt`` upload transcribes its clip, and the live
encounter buffers its clips and transcribes the whole visit when the clinician
stops. (Streaming Transcribe needs raw PCM; the browser sends webm, so the
encounter is transcribe-on-stop, not turn-by-turn — a raw-PCM capture path for
true streaming is a design follow-up.) Set ``HDH_AWS_TRANSCRIBE_BUCKET`` to the
S3 bucket Transcribe reads input from; the uploaded clip is deleted after.

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
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from hdh.modules.media.backends import Audio, Backends, Extraction, MediaError, Segment, Transcript
from hdh.modules.media.local import CONFIDENCE_FLOOR, assign_provider
from hdh.modules.media.streaming import StreamSegment, StreamSession

_EXTRA_HINT = (
    "the aws media backend needs the AWS SDK — install with: pip install 'hdh[aws]' (boto3) "
    "and configure credentials for a HIPAA BAA account"
)

# Transcribe Medical batch config (env-overridable). Both the async ``/stt``
# upload and the live encounter run a batch Transcribe Medical job — streaming
# Transcribe needs raw PCM and the browser sends webm, so the encounter buffers
# its clips and transcribes the whole visit on stop (see the engines below).
_KEY_PREFIX = "hdh-media/stt/"
_BUCKET_ENV = "HDH_AWS_TRANSCRIBE_BUCKET"
_DEFAULT_SPECIALTY = "PRIMARYCARE"
_DEFAULT_TYPE = "CONVERSATION"  # keeps speaker labels; a single speaker collapses to Provider
_DEFAULT_MAX_SPEAKERS = 2
_DEFAULT_POLL_S = 3.0
_DEFAULT_TIMEOUT_S = 600.0

# Request content types → Transcribe ``MediaFormat`` (batch accepts these). The
# browser's MediaRecorder sends webm/opus; the async upload is usually wav.
_MEDIA_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "mp4",
    "audio/x-m4a": "mp4",
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/amr": "amr",
}


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


# ── pure batch-job glue (no cloud call, fully tested) ─────────────────────────


def media_format_for(content_type: str) -> str:
    """Map a request content type to a Transcribe ``MediaFormat``
    (wav|mp3|mp4|webm|ogg|flac|amr). Transcribe needs the container named; an
    unrecognised type is refused loudly rather than guessed. Pure."""
    base = (content_type or "").split(";")[0].strip().lower()
    fmt = _MEDIA_FORMATS.get(base)
    if fmt is None:
        allowed = ", ".join(sorted(set(_MEDIA_FORMATS.values())))
        raise MediaError(
            f"unsupported audio type for Transcribe: {content_type!r} (expected one of {allowed})"
        )
    return fmt


def s3_uri(bucket: str, key: str) -> str:
    """The ``s3://`` URI Transcribe reads its input from. Pure."""
    return f"s3://{bucket}/{key}"


def transcription_request(
    job_name: str,
    media_uri: str,
    media_format: str,
    *,
    specialty: str = _DEFAULT_SPECIALTY,
    job_type: str = _DEFAULT_TYPE,
    max_speakers: int = _DEFAULT_MAX_SPEAKERS,
) -> dict[str, Any]:
    """Build the ``start_medical_transcription_job`` params. Transcribe Medical
    is English-only; ``ShowSpeakerLabels`` gives the diarization the Provider /
    Patient relabel keys off. No output bucket is set, so the transcript comes
    back as a presigned HTTPS URL. Pure — the shape is tested with no cloud call."""
    return {
        "MedicalTranscriptionJobName": job_name,
        "LanguageCode": "en-US",
        "MediaFormat": media_format,
        "Media": {"MediaFileUri": media_uri},
        "Specialty": specialty,
        "Type": job_type,
        "Settings": {"ShowSpeakerLabels": True, "MaxSpeakerLabels": max_speakers},
    }


def transcript_uri(job: dict[str, Any]) -> str:
    """Pull the finished transcript's URI from a ``get_medical_transcription_job``
    response — a presigned HTTPS URL when no output bucket was set. A FAILED or
    still-running job is an error, never a silent empty transcript. Pure."""
    info = job.get("MedicalTranscriptionJob", {})
    status = info.get("TranscriptionJobStatus")
    if status != "COMPLETED":
        reason = info.get("FailureReason") or status or "unknown"
        raise MediaError(f"Transcribe job did not complete ({reason})")
    uri = info.get("Transcript", {}).get("TranscriptFileUri")
    if not uri:
        raise MediaError("Transcribe job completed without a transcript URI")
    return str(uri)


def transcript_duration(payload: dict[str, Any]) -> float:
    """The audio duration as the latest item end-time — Transcribe gives no
    top-level duration, so we take it from the results. Pure."""
    items = payload.get("results", {}).get("items", [])
    ends = [float(i["end_time"]) for i in items if i.get("end_time")]
    return max(ends, default=0.0)


def transcript_to_stream_segments(transcript: Transcript) -> list[StreamSegment]:
    """Flatten a finished batch :class:`Transcript` into final
    :class:`StreamSegment`s for the encounter's done-flush. The AWS encounter
    transcribes on stop, so every turn is ``final`` (never a partial). Pure."""
    return [
        StreamSegment(
            speaker=s.speaker,
            text=s.text,
            t0=s.t0,
            t1=s.t1,
            final=True,
            confidence=s.confidence,
            needs_review=s.needs_review,
        )
        for s in transcript.segments
        if s.text.strip()
    ]


def require_transcribe_bucket() -> str:
    """The S3 bucket Transcribe reads input from — required for the aws backend's
    STT (batch Transcribe needs an ``s3://`` input), with a loud hint when unset."""
    bucket = os.environ.get(_BUCKET_ENV)
    if not bucket:
        raise MediaError(
            f"the aws STT backend needs an S3 bucket for Transcribe input — set {_BUCKET_ENV} "
            "to a bucket in your BAA account that the credentials can write and Transcribe can read"
        )
    return bucket


# ── the live-encounter stream (buffer the clips, transcribe on stop) ──────────


class _BufferedTranscribeStream:
    """The AWS live encounter as a :class:`StreamSession`. Streaming Transcribe
    needs raw PCM and the browser sends webm clips, so rather than transcode
    mid-visit we buffer the clips and run one batch Transcribe Medical job when
    the clinician stops — the diarized transcript lands on stop, not turn by
    turn. ``push`` buffers (nothing to emit yet); ``finish`` concatenates the
    clips (they are fragments of one MediaRecorder stream, so concatenation is a
    valid file) and transcribes the whole visit. The ``transcribe`` callable is
    injected so the buffering/flatten contract is tested with no cloud call.

    Speaker verification (TitaNet) does not apply here — Transcribe gives no
    voice embeddings — so turns keep the diarizer's Provider / Patient labels and
    the UI relabels as needed."""

    def __init__(
        self, transcribe: Callable[[bytes, str], Transcript], *, content_type: str = "audio/webm"
    ) -> None:
        self._transcribe = transcribe
        self._content_type = content_type
        self._chunks: list[bytes] = []

    def push(self, chunk: bytes) -> list[StreamSegment]:
        if chunk:
            self._chunks.append(chunk)
        return []

    def finish(self) -> list[StreamSegment]:
        data = b"".join(self._chunks)
        self._chunks = []
        if not data:
            return []
        return transcript_to_stream_segments(self._transcribe(data, self._content_type))


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
    """Transcribe for the live encounter (WS ``/stt/stream``) — buffers the
    clips and transcribes the visit with a batch Transcribe Medical job on stop
    (see :class:`_BufferedTranscribeStream`)."""

    def open(self, *, sample_rate: int = 16000, verifier: Any = None) -> StreamSession:
        return _open_transcribe_stream(sample_rate, verifier)


def _run_transcribe(data: bytes, content_type: str) -> Transcript:  # pragma: no cover - needs AWS
    """Run one batch Transcribe Medical job and shape the result: upload the
    audio to S3, start the job, poll it to completion, fetch the transcript JSON,
    and hand it to the tested ``transcript_from_transcribe``. The input object is
    deleted afterwards (the service persists nothing). Cloud-bound, so excluded
    from coverage; the request/response glue it calls is pure and unit-tested."""
    import json as json_
    import urllib.request

    media_format = media_format_for(content_type)
    s3 = _client("s3")  # fails loudly without boto3
    transcribe = _client("transcribe")
    bucket = require_transcribe_bucket()
    job_name = f"hdh-medical-{uuid.uuid4().hex}"
    key = f"{_KEY_PREFIX}{job_name}.{media_format}"
    s3.put_object(Bucket=bucket, Key=key, Body=data)
    try:
        transcribe.start_medical_transcription_job(
            **transcription_request(
                job_name,
                s3_uri(bucket, key),
                media_format,
                specialty=os.environ.get("HDH_AWS_TRANSCRIBE_SPECIALTY", _DEFAULT_SPECIALTY),
                job_type=os.environ.get("HDH_AWS_TRANSCRIBE_TYPE", _DEFAULT_TYPE),
                max_speakers=int(os.environ.get("HDH_AWS_MAX_SPEAKERS", _DEFAULT_MAX_SPEAKERS)),
            )
        )
        job = _await_transcribe_job(transcribe, job_name)
        with urllib.request.urlopen(transcript_uri(job)) as response:  # presigned HTTPS
            payload = json_.loads(response.read().decode("utf-8"))
    finally:
        with suppress(Exception):
            s3.delete_object(Bucket=bucket, Key=key)
    return transcript_from_transcribe(payload, duration_s=transcript_duration(payload))


def _await_transcribe_job(transcribe: Any, job_name: str) -> dict[str, Any]:  # pragma: no cover - needs AWS
    """Poll a medical transcription job until it is COMPLETED or FAILED, or the
    timeout elapses. Returns the terminal job response for ``transcript_uri`` to
    read (which raises on anything but COMPLETED)."""
    interval = float(os.environ.get("HDH_AWS_TRANSCRIBE_POLL_S", _DEFAULT_POLL_S))
    deadline = time.monotonic() + float(os.environ.get("HDH_AWS_TRANSCRIBE_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    while True:
        job = transcribe.get_medical_transcription_job(MedicalTranscriptionJobName=job_name)
        status = job.get("MedicalTranscriptionJob", {}).get("TranscriptionJobStatus")
        if status in ("COMPLETED", "FAILED"):
            return job
        if time.monotonic() > deadline:
            raise MediaError(f"Transcribe job {job_name} timed out before completing")
        time.sleep(interval)


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


def _open_transcribe_stream(sample_rate: int, verifier: Any) -> StreamSession:
    """Open the live encounter's session. ``sample_rate`` and ``verifier`` are
    part of the streaming contract but unused here: the AWS encounter buffers the
    clips and transcribes the visit with a batch Transcribe Medical job on stop
    (:class:`_BufferedTranscribeStream`), and Transcribe exposes no voice
    embeddings for TitaNet verification. The real batch job (``_run_transcribe``)
    is injected, so the buffering/flatten contract is tested with no cloud call.

    A true low-latency streaming session (``stream_segments_from_transcribe``
    shapes its events) would need raw-PCM capture in the browser; see the design
    follow-ups."""
    del sample_rate, verifier
    return _BufferedTranscribeStream(_run_transcribe)


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
