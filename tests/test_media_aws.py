"""Phase-7 tests for the AWS media backends — the pure response-shaping and the
engines' loud-failure contract (design §9). No cloud, no boto3: the shaping is
pure (sample Transcribe/Textract responses) and the SDK-missing path is forced
by making the lazy boto3 import fail."""

from __future__ import annotations

import builtins

import pytest

from hdh.modules.media.aws import (
    PollySynthesizer,
    TextractOcr,
    TranscribeMedicalTranscriber,
    build_aws_backends,
    extraction_from_textract,
    stream_segments_from_transcribe,
    transcript_from_transcribe,
)
from hdh.modules.media.backends import MediaError

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
