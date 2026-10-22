"""Phase-1 contract tests for the media I/O service — the null/echo backend and
the provider-realm gate, no GPU and no Keycloak needed."""

from __future__ import annotations

import io
import wave

import pytest
from fastapi.testclient import TestClient

from hdh.core.identity import Identity
from hdh.core.identity.web_auth import AuthError
from hdh.modules.media import create_app
from hdh.modules.media.backends import (
    EchoOcr,
    EchoTranscriber,
    SilentSynthesizer,
    make_backends,
)

_OK = Identity(subject="sub-test", username="dr.test", roles=frozenset({"clinician"}))


def _ALLOW(_header):  # a permissive authenticator (def, not lambda — E731)
    return _OK


def _DENY(_header):
    raise AuthError("no token")


def _client(authenticator=_ALLOW) -> TestClient:
    return TestClient(create_app(authenticator=authenticator))


# ── the open probe ────────────────────────────────────────────────────────────


def test_health_is_open():
    resp = TestClient(create_app(authenticator=_DENY)).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "hdh-media"
    assert body["backend"] == "null"


# ── the gate ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/capabilities"])
def test_gated_endpoints_refuse_without_token(path):
    resp = TestClient(create_app(authenticator=_DENY)).get(path)
    assert resp.status_code == 401


def test_stt_refused_without_token():
    resp = TestClient(create_app(authenticator=_DENY)).post(
        "/stt", files={"file": ("n.txt", b"hi", "text/plain")}
    )
    assert resp.status_code == 401


def test_capabilities_reports_backend_and_no_streaming_yet():
    body = _client().get("/capabilities").json()
    assert body["backend"] == "null"
    assert body["stt"] and body["ocr"] and body["tts"]
    assert body["streaming_stt"] is False  # WS lands in phase 5


# ── transduction via the echo backend ────────────────────────────────────────


def test_stt_echoes_text_as_a_diarized_segment():
    resp = _client().post("/stt", files={"file": ("visit.txt", b"patient reports a cough", "text/plain")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "patient reports a cough"
    assert body["speakers"] == ["Provider"]
    assert body["segments"][0]["speaker"] == "Provider"
    assert body["segments"][0]["needs_review"] is False
    assert body["model"] == "null-echo"


def test_ocr_echoes_text():
    resp = _client().post("/ocr", files={"file": ("scan.txt", b"old report: BP 130/80", "text/plain")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "old report: BP 130/80"
    assert body["pages"] == 1


def test_tts_returns_a_valid_wav():
    resp = _client().post("/tts", json={"text": "Charted to visit 12."})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert float(resp.headers["x-audio-duration"]) > 0
    # the bytes are a real, parseable WAV
    with wave.open(io.BytesIO(resp.content), "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getnframes() > 0


def test_tts_rejects_empty_text():
    assert _client().post("/tts", json={"text": ""}).status_code == 422


# ── backend selection ─────────────────────────────────────────────────────────


def test_null_backend_is_the_default():
    bk = make_backends()
    assert bk.name == "null"
    assert isinstance(bk.transcriber, EchoTranscriber)
    assert isinstance(bk.synthesizer, SilentSynthesizer)
    assert isinstance(bk.ocr, EchoOcr)


def test_aws_backend_fails_loudly_until_built():
    # `local` is real as of phase 3 (NeMo/WhisperX, tests/test_media_local.py);
    # `aws` (Transcribe Medical / Polly / Textract) still lands in phase 7.
    with pytest.raises(NotImplementedError):
        make_backends("aws")


def test_unknown_backend_is_a_value_error():
    with pytest.raises(ValueError):
        make_backends("wishful")


# ── the echo backend, directly ────────────────────────────────────────────────


def test_echo_transcriber_stubs_non_text_bytes():
    t = EchoTranscriber().transcribe(b"\xff\xfe\x00\x01", "audio/wav")
    assert "stt stub" in t.text
    assert t.segments[0].speaker == "Provider"


def test_silent_synthesizer_scales_with_text_length():
    short = SilentSynthesizer().synthesize("hi")
    longer = SilentSynthesizer().synthesize("a much longer sentence to voice aloud")
    assert longer.duration_s > short.duration_s
    assert short.content_type == "audio/wav"
