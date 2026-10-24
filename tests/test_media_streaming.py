"""Phase-5 tests for the streaming encounter — the WS ``/stt/stream`` contract,
the echo streamer, and provider verification (design §5, §10, §13).

No GPU, no Keycloak: the echo streaming backend drives the whole WebSocket
protocol and the verification policy is pure, so CI proves everything except the
NeMo/Sortformer model itself (which runs on the 5060)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hdh.core.identity import Identity
from hdh.core.identity.web_auth import AuthError
from hdh.modules.media import create_app
from hdh.modules.media.streaming import (
    EchoStreamingTranscriber,
    NeMoStreamingTranscriber,
    StreamSegment,
    _segment_from_turn,
    verifier_from_enrolled,
)
from hdh.modules.media.verification import MATCH_THRESHOLD, ProviderVerifier, cosine

_OK = Identity(subject="sub", username="dr.who", roles=frozenset({"clinician"}))


def _ALLOW(_header):  # permissive authenticator (def, not lambda — E731)
    return _OK


def _DENY(_header):
    raise AuthError("no token")


def _client(authenticator=_ALLOW) -> TestClient:
    return TestClient(create_app(authenticator=authenticator))


# ── the WebSocket gate ────────────────────────────────────────────────────────


def test_stream_refuses_without_a_valid_token():
    client = _client(authenticator=_DENY)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/stt/stream", subprotocols=["bearer", "bad"]):
            pass


def test_capabilities_now_advertises_streaming():
    body = _client().get("/capabilities").json()
    assert body["streaming_stt"] is True  # flipped on in phase 5


# ── the live diarized stream (echo backend) ───────────────────────────────────


def test_stream_returns_diarized_segments_then_a_done_frame():
    with _client().websocket_connect("/stt/stream", subprotocols=["bearer", "tok"]) as ws:
        ws.send_json({"type": "start", "sample_rate": 16000})
        ws.send_bytes(b"patient reports a cough")
        first = ws.receive_json()
        ws.send_bytes(b"how long has it lasted")
        second = ws.receive_json()
        ws.send_json({"type": "stop"})
        done = ws.receive_json()

    assert first["type"] == "segment"
    assert first["speaker"] == "Provider" and first["final"] is True
    assert second["speaker"] == "Patient"  # alternates — a two-voice encounter
    assert done["type"] == "done"
    assert done["text"] == "patient reports a cough how long has it lasted"
    assert done["speakers"] == ["Provider", "Patient"]
    assert done["duration_s"] == 2.0
    assert done["needs_review"] is False


def test_stream_ignores_audio_before_start():
    # A binary frame with no session open is a no-op, not a crash; the first
    # segment only arrives once `start` has opened a session.
    with _client().websocket_connect("/stt/stream", subprotocols=["bearer", "tok"]) as ws:
        ws.send_bytes(b"too early")  # dropped — no session yet
        ws.send_json({"type": "start"})
        ws.send_bytes(b"now charted")
        seg = ws.receive_json()
        ws.send_json({"type": "stop"})
        done = ws.receive_json()
    assert seg["text"] == "now charted"
    assert done["text"] == "now charted"  # the early frame never counted


def test_stream_carries_the_enrolled_print_in_and_the_reenrolled_one_out():
    # With an enrolled print supplied, the done frame returns the (re-enrolled)
    # print for the caller to persist — the service stores nothing itself.
    enrolled = [1.0, 0.0, 0.0]
    with _client().websocket_connect("/stt/stream", subprotocols=["bearer", "tok"]) as ws:
        ws.send_json({"type": "start", "enrolled": enrolled})
        ws.send_bytes(b"good morning")
        ws.receive_json()
        ws.send_json({"type": "stop"})
        done = ws.receive_json()
    assert done["enrolled"] is not None
    assert len(done["enrolled"]) == len(enrolled)


# ── the echo streamer, directly ────────────────────────────────────────────────


def test_echo_session_alternates_speakers_and_advances_time():
    session = EchoStreamingTranscriber().open()
    a = session.push(b"first turn")
    b = session.push(b"second turn")
    assert [s.speaker for s in a] == ["Provider"]
    assert [s.speaker for s in b] == ["Patient"]
    assert a[0].t1 == b[0].t0  # contiguous timeline
    assert session.push(b"   ") == []  # blank clip → nothing
    assert session.finish() == []


# ── provider verification (pure, design §13) ──────────────────────────────────


def test_cosine_is_one_for_parallel_and_zero_for_orthogonal():
    assert cosine([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector: no direction, not an error


def test_cosine_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="length mismatch"):
        cosine([1.0, 2.0], [1.0])


def test_verifier_matches_the_enrolled_voice_and_rejects_another():
    verifier = ProviderVerifier([1.0, 0.0, 0.0])
    assert verifier.label_for([0.9, 0.1, 0.0]) == "Provider"  # close → the provider
    assert verifier.label_for([0.0, 1.0, 0.0]) == "Patient"  # orthogonal → the other speaker


def test_verify_reports_the_score_and_the_threshold_decides():
    verifier = ProviderVerifier([1.0, 0.0], threshold=0.99)
    result = verifier.verify([1.0, 0.1])  # ~0.995 cosine
    assert result.score > 0.99 and result.matched
    strict = ProviderVerifier([1.0, 0.0], threshold=0.999)
    assert not strict.verify([1.0, 0.1]).matched  # same voice, stricter bar → refused


def test_passive_reenrollment_nudges_the_print_only_on_a_match():
    verifier = ProviderVerifier([1.0, 0.0, 0.0], learn=True)
    before = verifier.enrolled
    verifier.verify([0.8, 0.6, 0.0])  # a match → the print drifts toward it
    assert verifier.enrolled != before
    frozen = ProviderVerifier([1.0, 0.0, 0.0], learn=False)
    frozen.verify([0.8, 0.6, 0.0])
    assert frozen.enrolled == [1.0, 0.0, 0.0]  # learning off → unchanged


def test_a_miss_never_contaminates_the_print():
    verifier = ProviderVerifier([1.0, 0.0, 0.0], learn=True)
    before = verifier.enrolled
    verifier.verify([0.0, 1.0, 0.0])  # the patient's voice — a miss
    assert verifier.enrolled == before  # the provider's print is untouched


def test_enroll_from_samples_averages_the_prints():
    verifier = ProviderVerifier.from_samples([[2.0, 0.0], [0.0, 2.0]])
    assert verifier.enrolled == [1.0, 1.0]


def test_enroll_rejects_empty_or_ragged_samples():
    with pytest.raises(ValueError, match="at least one"):
        ProviderVerifier.from_samples([])
    with pytest.raises(ValueError, match="same length"):
        ProviderVerifier.from_samples([[1.0, 0.0], [1.0]])


def test_an_empty_print_is_refused():
    with pytest.raises(ValueError, match="cannot be empty"):
        ProviderVerifier([])


# ── verifier wiring ────────────────────────────────────────────────────────────


def test_verifier_from_enrolled_is_none_without_a_print():
    assert verifier_from_enrolled(None) is None
    assert verifier_from_enrolled([]) is None  # absent/empty → heuristic fallback
    assert isinstance(verifier_from_enrolled([1.0, 0.0]), ProviderVerifier)


def test_echo_session_honours_the_supplied_verifier():
    # A verifier makes the session resolve speakers by verification; here the
    # match threshold is unreachable, so every clip is the Patient.
    verifier = ProviderVerifier([1.0, 0.0], threshold=2.0)
    session = EchoStreamingTranscriber().open(verifier=verifier)
    assert session._verifier is verifier  # the verifier reached the session


# ── the NeMo streaming engine fails loudly without the extra ──────────────────


def test_nemo_streaming_needs_the_media_extra():
    # Constructible with no GPU; opening a session raises a clear install hint
    # (never a silent echo) when nemo_toolkit is absent, as CI is.
    from hdh.modules.media.backends import MediaError

    with pytest.raises(MediaError, match="pip install 'hdh\\[media\\]'"):
        NeMoStreamingTranscriber().open()


def test_match_threshold_is_a_sane_default():
    assert 0.0 < MATCH_THRESHOLD < 1.0


def test_stream_segment_flags_low_confidence():
    seg = StreamSegment(speaker="Provider", text="mumbled", confidence=0.3, needs_review=True)
    assert seg.needs_review


# ── shaping a diarized turn (the glue the NeMo seam feeds) ────────────────────


def test_segment_from_turn_verifies_the_speaker_when_a_print_is_given():
    verifier = ProviderVerifier([1.0, 0.0, 0.0])
    seg = _segment_from_turn(
        {"text": "good morning", "t0": 0.0, "t1": 1.5, "embedding": [0.95, 0.05, 0.0]}, verifier
    )
    assert seg.speaker == "Provider"  # the embedding verified against the print
    other = _segment_from_turn({"text": "my knee hurts", "embedding": [0.0, 1.0, 0.0]}, verifier)
    assert other.speaker == "Patient"  # the other voice


def test_segment_from_turn_keeps_the_raw_label_without_a_verifier():
    seg = _segment_from_turn({"speaker": "speaker_1", "text": "hello"}, None)
    assert seg.speaker == "speaker_1"  # heuristic/relabel handles identity later


def test_segment_from_turn_flags_a_low_confidence_turn():
    seg = _segment_from_turn({"text": "…", "confidence": 0.2}, None)
    assert seg.needs_review and not _segment_from_turn({"text": "clear", "confidence": 0.99}, None).needs_review
