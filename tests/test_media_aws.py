"""Phase-7 tests for the AWS media backends — the pure response-shaping and the
engines' loud-failure contract (design §9). No cloud, no boto3: the shaping is
pure (sample Transcribe/Textract responses) and the SDK-missing path is forced
by making the lazy boto3 import fail."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from hdh.modules.media.aws import (
    AwsStreamingTranscriber,
    PollySynthesizer,
    TextractOcr,
    TranscribeMedicalTranscriber,
    _BufferedTranscribeStream,
    build_aws_backends,
    extraction_from_textract,
    media_format_for,
    require_transcribe_bucket,
    s3_uri,
    stream_segments_from_transcribe,
    transcript_duration,
    transcript_from_transcribe,
    transcript_to_stream_segments,
    transcript_uri,
    transcription_request,
)
from hdh.modules.media.backends import MediaError, Segment, Transcript

# ── pure response-shaping ──────────────────────────────────────────────────────


def test_transcript_from_transcribe_diarizes_and_relabels():
    response = {
        "results": {
            "transcripts": [{"transcript": "How are you? My knee hurts."}],
            "speaker_labels": {
                "segments": [
                    {"speaker_label": "spk_0", "start_time": "0.0", "end_time": "1.0"},
                    {"speaker_label": "spk_1", "start_time": "1.0", "end_time": "3.0"},
                ]
            },
            "items": [
                {
                    "type": "pronunciation",
                    "start_time": "0.1",
                    "alternatives": [{"content": "How", "confidence": "0.99"}],
                },
                {
                    "type": "pronunciation",
                    "start_time": "1.5",
                    "alternatives": [{"content": "knee", "confidence": "0.4"}],
                },
            ],
        }
    }
    t = transcript_from_transcribe(response, duration_s=3.0)
    assert t.text == "How are you? My knee hurts."
    assert t.duration_s == 3.0 and t.model == "aws:transcribe-medical"
    # longest-speaking (spk_1, 2s) is relabelled Provider; the other Patient
    assert set(t.speakers) == {"Provider", "Patient"}
    provider = next(s for s in t.segments if s.speaker == "Provider")
    assert provider.needs_review is True  # its only word scored 0.4, below the floor


def test_extraction_from_textract_keeps_lines_in_order():
    response = {
        "Blocks": [
            {"BlockType": "PAGE", "Page": 1},
            {"BlockType": "LINE", "Text": "Patient: Jane Doe", "Confidence": 99.1, "Page": 1},
            {"BlockType": "LINE", "Text": "  ", "Confidence": 10.0, "Page": 1},  # blank → dropped
            {"BlockType": "WORD", "Text": "ignored", "Confidence": 99.0, "Page": 1},
            {"BlockType": "LINE", "Text": "smudged", "Confidence": 20.0, "Page": 2},
        ]
    }
    ext = extraction_from_textract(response)
    assert ext.text == "Patient: Jane Doe\nsmudged"
    assert ext.pages == 2 and ext.model == "aws:textract"
    assert len(ext.blocks) == 2  # the blank LINE and the WORD are excluded
    assert ext.blocks[0]["confidence"] == pytest.approx(0.991)
    assert ext.blocks[1]["needs_review"] is True  # 0.20 < floor


def test_stream_segments_from_transcribe_marks_partial_and_final():
    event = {
        "Transcript": {
            "Results": [
                {
                    "IsPartial": True,
                    "StartTime": 0.0,
                    "EndTime": 1.2,
                    "Alternatives": [
                        {
                            "Items": [
                                {
                                    "Type": "pronunciation",
                                    "Content": "good",
                                    "Speaker": "0",
                                    "Confidence": 0.9,
                                }
                            ]
                        }
                    ],
                },
                {
                    "IsPartial": False,
                    "StartTime": 0.0,
                    "EndTime": 1.5,
                    "Alternatives": [
                        {
                            "Items": [
                                {
                                    "Type": "pronunciation",
                                    "Content": "good",
                                    "Speaker": "0",
                                    "Confidence": 0.9,
                                },
                                {
                                    "Type": "pronunciation",
                                    "Content": "morning",
                                    "Speaker": "0",
                                    "Confidence": 0.95,
                                },
                            ]
                        }
                    ],
                },
            ]
        }
    }
    segs = stream_segments_from_transcribe(event)
    assert [s.final for s in segs] == [False, True]
    assert segs[1].text == "good morning"
    assert segs[1].speaker == "spk_0"


def test_stream_segments_ignores_empty_results():
    assert stream_segments_from_transcribe({"Transcript": {"Results": []}}) == []


# ── pure batch-job glue (the orchestration's building blocks) ───────────────────


def test_media_format_for_maps_the_browser_and_upload_types():
    assert media_format_for("audio/webm") == "webm"  # MediaRecorder
    assert media_format_for("audio/wav") == "wav"
    assert media_format_for("audio/mpeg") == "mp3"
    assert media_format_for("audio/flac") == "flac"
    assert media_format_for("audio/webm;codecs=opus") == "webm"  # parameters stripped


def test_media_format_for_refuses_an_unknown_type():
    with pytest.raises(MediaError, match="unsupported audio type"):
        media_format_for("audio/basic")


def test_transcription_request_asks_for_speaker_labels():
    req = transcription_request("job-1", "s3://bkt/clip.webm", "webm", max_speakers=2)
    assert req["MedicalTranscriptionJobName"] == "job-1"
    assert req["LanguageCode"] == "en-US"
    assert req["Media"] == {"MediaFileUri": "s3://bkt/clip.webm"}
    assert req["MediaFormat"] == "webm"
    assert req["Specialty"] == "PRIMARYCARE" and req["Type"] == "CONVERSATION"
    assert req["Settings"] == {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2}
    assert "OutputBucketName" not in req  # presigned-URL output, no output bucket


def test_s3_uri_builds_the_input_uri():
    assert s3_uri("my-bucket", "hdh-media/stt/job.webm") == "s3://my-bucket/hdh-media/stt/job.webm"


def test_transcript_uri_returns_the_file_uri_on_completion():
    job = {
        "MedicalTranscriptionJob": {
            "TranscriptionJobStatus": "COMPLETED",
            "Transcript": {"TranscriptFileUri": "https://x/y.json"},
        }
    }
    assert transcript_uri(job) == "https://x/y.json"


def test_transcript_uri_raises_on_a_failed_job_with_the_reason():
    job = {"MedicalTranscriptionJob": {"TranscriptionJobStatus": "FAILED", "FailureReason": "bad audio"}}
    with pytest.raises(MediaError, match="bad audio"):
        transcript_uri(job)


def test_transcript_uri_raises_when_completed_without_a_uri():
    job = {"MedicalTranscriptionJob": {"TranscriptionJobStatus": "COMPLETED", "Transcript": {}}}
    with pytest.raises(MediaError, match="without a transcript URI"):
        transcript_uri(job)


def test_transcript_duration_takes_the_latest_end_time():
    payload = {"results": {"items": [{"end_time": "1.2"}, {"end_time": "4.7"}, {"type": "punctuation"}]}}
    assert transcript_duration(payload) == 4.7
    assert transcript_duration({"results": {"items": []}}) == 0.0


def test_transcript_to_stream_segments_flattens_finals_and_drops_empty():
    transcript = Transcript(
        text="hi there",
        segments=[
            Segment(speaker="Provider", text="hi", t0=0.0, t1=1.0, confidence=0.9),
            Segment(speaker="Patient", text="  ", confidence=1.0),  # empty → dropped
            Segment(speaker="Patient", text="there", t0=1.0, t1=2.0, confidence=0.3, needs_review=True),
        ],
    )
    segs = transcript_to_stream_segments(transcript)
    assert [s.text for s in segs] == ["hi", "there"]
    assert all(s.final for s in segs)
    assert segs[1].needs_review is True and segs[1].speaker == "Patient"


# ── the live encounter: buffer the clips, transcribe on stop ────────────────────


def test_buffered_stream_buffers_on_push_and_transcribes_on_finish():
    seen: dict[str, Any] = {}

    def fake_transcribe(data: bytes, content_type: str) -> Transcript:
        seen["data"] = data
        seen["content_type"] = content_type
        return Transcript(
            text="good morning",
            segments=[Segment(speaker="Provider", text="good morning", t0=0.0, t1=1.0)],
        )

    stream = _BufferedTranscribeStream(fake_transcribe)
    assert stream.push(b"clip-a") == []  # nothing emitted mid-visit
    assert stream.push(b"clip-b") == []
    segs = stream.finish()
    assert seen["data"] == b"clip-aclip-b"  # concatenated fragments of one recording
    assert seen["content_type"] == "audio/webm"
    assert [s.text for s in segs] == ["good morning"] and segs[0].final is True


def test_buffered_stream_with_no_audio_transcribes_nothing():
    calls: list[bytes] = []

    def fake_transcribe(data: bytes, content_type: str) -> Transcript:  # pragma: no cover - not reached
        calls.append(data)
        return Transcript(text="")

    stream = _BufferedTranscribeStream(fake_transcribe)
    stream.push(b"")  # empty clip ignored
    assert stream.finish() == []
    assert calls == []  # no cloud call when there is nothing to transcribe


def test_aws_streaming_transcriber_opens_a_buffered_session():
    session = AwsStreamingTranscriber().open(sample_rate=16000, verifier=None)
    assert isinstance(session, _BufferedTranscribeStream)


def test_require_transcribe_bucket_reads_the_env(monkeypatch):
    monkeypatch.setenv("HDH_AWS_TRANSCRIBE_BUCKET", "baa-bucket")
    assert require_transcribe_bucket() == "baa-bucket"


def test_require_transcribe_bucket_fails_loudly_when_unset(monkeypatch):
    monkeypatch.delenv("HDH_AWS_TRANSCRIBE_BUCKET", raising=False)
    with pytest.raises(MediaError, match="HDH_AWS_TRANSCRIBE_BUCKET"):
        require_transcribe_bucket()


# ── the engines fail loudly without boto3 ──────────────────────────────────────


@pytest.fixture
def _no_boto3(monkeypatch):
    """Make `import boto3` fail, so the lazy-import loud-error path is exercised
    even on a box that happens to have the SDK."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3" or name.startswith("boto3."):
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_transcribe_fails_loudly_without_the_sdk(_no_boto3):
    with pytest.raises(MediaError, match="pip install 'hdh\\[aws\\]'"):
        TranscribeMedicalTranscriber().transcribe(b"audio", "audio/wav")


def test_polly_fails_loudly_without_the_sdk(_no_boto3):
    with pytest.raises(MediaError, match="pip install 'hdh\\[aws\\]'"):
        PollySynthesizer().synthesize("Charted to visit twelve.")


def test_textract_fails_loudly_without_the_sdk(_no_boto3):
    with pytest.raises(MediaError, match="pip install 'hdh\\[aws\\]'"):
        TextractOcr().extract(b"\x89PNG...", "image/png")


def test_polly_and_textract_reject_empty_input():
    with pytest.raises(MediaError, match="nothing to synthesize"):
        PollySynthesizer().synthesize("  ")
    with pytest.raises(MediaError, match="nothing to extract"):
        TextractOcr().extract(b"", "image/png")


# ── backend selection ──────────────────────────────────────────────────────────


def test_build_aws_backends_wires_all_four_protocols():
    bk = build_aws_backends()
    assert bk.name == "aws"
    assert isinstance(bk.transcriber, TranscribeMedicalTranscriber)
    assert isinstance(bk.synthesizer, PollySynthesizer)
    assert isinstance(bk.ocr, TextractOcr)
    assert bk.streamer is not None


def test_make_backends_aws_builds_without_boto3(_no_boto3):
    # Selecting the backend is boto3-free; only using an engine needs the SDK.
    from hdh.modules.media.backends import make_backends

    assert make_backends("aws").name == "aws"
