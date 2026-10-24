"""The media I/O service — a stateless HTTP transducer (design media-io-service.md).

Bytes in, text out (STT, OCR); text in, audio out (TTS). It holds no clinical
logic and no PHI at rest — every clinical consequence happens *after* its text
re-enters the agent or the comprehension path. The models sit behind injectable
backends (:mod:`.backends`); the default is a null/echo backend so this surface
is testable with no GPU. Every endpoint but ``/health`` requires a verified
**provider-realm** token — the same gate the agent API uses, shared from core.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from hdh.core.identity.web_auth import AuthError, keycloak_authenticator
from hdh.modules.media.backends import Backends, Extraction, MediaError, Transcript, make_backends
from hdh.modules.media.streaming import StreamSegment, verifier_from_enrolled
from hdh.modules.media.verification import ProviderVerifier

VERSION = "0.1.0"


class TtsRequest(BaseModel):
    """Text to voice, with an optional named voice."""

    text: str = Field(min_length=1, description="The text to synthesize.")
    voice: str | None = Field(default=None, description="A named voice; the default if omitted.")


def create_app(*, backends: Backends | None = None, authenticator: Any = None, backend: str | None = None):
    """Build the media service app.

    ``backends`` overrides the transducers (tests inject echo fakes); by default
    they are chosen by ``backend`` / ``HDH_MEDIA_BACKEND`` (null by default).
    ``authenticator`` verifies a request's provider-realm token; the default is
    the real Keycloak JWKS check. ``/health`` is open; everything else is gated.
    """
    bk = backends or make_backends(backend)
    authenticate = authenticator or keycloak_authenticator()

    app = FastAPI(
        title="HDH Media I/O Service",
        version=VERSION,
        summary="Stateless media transducer — diarized STT, OCR, TTS (design media-io-service.md).",
    )

    def require_identity(authorization: str | None = Header(default=None)):
        """Every transduction endpoint's gate: a valid provider-realm token or
        401 — where provider/patient realm separation is enforced."""
        try:
            return authenticate(authorization)
        except AuthError as err:
            raise HTTPException(status_code=401, detail=str(err)) from None

    @app.get("/health")
    def health() -> dict:
        """Liveness — open (no token): a proxy probes it before anyone signs in."""
        return {"status": "ok", "service": "hdh-media", "version": VERSION, "backend": bk.name}

    @app.get("/capabilities")
    def capabilities(_identity=Depends(require_identity)) -> dict:
        """What this instance can do and with which backend — so the UI can show
        or hide voice controls without guessing."""
        return {
            "backend": bk.name,
            "stt": True,
            "ocr": True,
            "tts": True,
            "streaming_stt": True,  # WS /stt/stream — the live encounter (phase 5)
        }

    @app.websocket("/stt/stream")
    async def stt_stream(websocket: WebSocket) -> None:
        """The live encounter: audio clips in, diarized segments out as they are
        recognised (design §5). Gated like every other endpoint — the provider's
        bearer token rides in the ``bearer`` WebSocket subprotocol, never the URL
        (which must carry no token or PHI). The stream itself is stateless: the
        finished transcript is posted to agent_api's ``/dictations`` by the UI."""
        token = _bearer_subprotocol(websocket)
        try:
            authenticate(f"Bearer {token}" if token else None)
        except AuthError:
            await websocket.close(code=1008)  # policy violation — no valid provider token
            return
        await websocket.accept(subprotocol="bearer" if token else None)
        await _run_stream(websocket, bk.streamer)

    @app.post("/stt", response_model=Transcript)
    async def stt(file: UploadFile = File(...), _identity=Depends(require_identity)) -> Transcript:
        """Audio → a diarized transcript. The transcript is returned, not stored;
        persistence (as a referenceable Conversation) is the agent API's job."""
        data = await file.read()
        try:
            return bk.transcriber.transcribe(data, file.content_type or "application/octet-stream")
        except MediaError as err:
            raise HTTPException(status_code=422, detail=str(err)) from None

    @app.post("/ocr", response_model=Extraction)
    async def ocr(file: UploadFile = File(...), _identity=Depends(require_identity)) -> Extraction:
        """Image/PDF → text, for the note path (never lab results, never orders)."""
        data = await file.read()
        try:
            return bk.ocr.extract(data, file.content_type or "application/octet-stream")
        except MediaError as err:
            raise HTTPException(status_code=422, detail=str(err)) from None

    @app.post("/tts")
    def tts(body: TtsRequest, _identity=Depends(require_identity)) -> StreamingResponse:
        """Text → audio. Voices the agent's concise ``spoken`` rendering; it
        originates no claim of its own."""
        try:
            audio = bk.synthesizer.synthesize(body.text, body.voice)
        except MediaError as err:
            raise HTTPException(status_code=422, detail=str(err)) from None
        return StreamingResponse(
            io.BytesIO(audio.data),
            media_type=audio.content_type,
            headers={"X-Audio-Duration": f"{audio.duration_s:.2f}"},
        )

    return app


# ── streaming helpers (the WS /stt/stream protocol) ───────────────────────────


def _bearer_subprotocol(websocket: WebSocket) -> str | None:
    """Pull the bearer token offered as the ``bearer`` WebSocket subprotocol
    (``["bearer", "<token>"]``) — the header-safe place for it, since a browser
    cannot set ``Authorization`` on a WebSocket and the URL must stay clean."""
    protocols = websocket.scope.get("subprotocols") or []
    if len(protocols) >= 2 and protocols[0] == "bearer":
        return protocols[1]
    return None


@dataclass
class _StreamAccumulator:
    """What one live encounter adds up to, built as segments stream so the
    ``done`` frame carries the finished transcript for the UI to save."""

    session: Any = None
    verifier: ProviderVerifier | None = None
    text_parts: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    needs_review: bool = False

    def record(self, seg: StreamSegment) -> None:
        if seg.final and seg.text:
            self.text_parts.append(seg.text)
        if seg.speaker not in self.speakers:
            self.speakers.append(seg.speaker)
        self.duration_s = max(self.duration_s, seg.t1)
        self.needs_review = self.needs_review or seg.needs_review


def _start_session(control: dict, acc: _StreamAccumulator, streamer: Any) -> None:
    """Open a session from the client's ``start`` frame. The enrolled print (if
    any) is supplied here and handed back updated on ``done`` — the service
    stores no voiceprint (design §13)."""
    acc.verifier = verifier_from_enrolled(control.get("enrolled"))
    acc.session = streamer.open(sample_rate=int(control.get("sample_rate", 16000)), verifier=acc.verifier)


def _done_payload(acc: _StreamAccumulator) -> dict:
    """The closing frame: the finished transcript plus the re-enrolled provider
    print for the caller (which owns PHI) to persist, or ``null`` when no print
    was supplied."""
    return {
        "type": "done",
        "text": " ".join(acc.text_parts).strip(),
        "speakers": acc.speakers,
        "duration_s": acc.duration_s,
        "needs_review": acc.needs_review,
        "enrolled": acc.verifier.enrolled if acc.verifier else None,
    }


async def _finish_stream(websocket: WebSocket, acc: _StreamAccumulator) -> None:
    """Flush any trailing audio, emit remaining segments, then the ``done`` frame."""
    if acc.session is not None:
        for seg in acc.session.finish():
            acc.record(seg)
            await websocket.send_json({"type": "segment", **seg.model_dump()})
    await websocket.send_json(_done_payload(acc))
    await websocket.close()


async def _run_stream(websocket: WebSocket, streamer: Any) -> None:
    """Drive one live encounter: ``start`` opens a session, binary frames are
    pushed and their segments streamed back, ``stop`` finishes. A model failure
    is reported as an ``error`` frame rather than a silent drop."""
    acc = _StreamAccumulator()
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                control = json.loads(text)
                if control.get("type") == "start":
                    _start_session(control, acc, streamer)
                elif control.get("type") == "stop":
                    await _finish_stream(websocket, acc)
                    return
                continue
            chunk = message.get("bytes")
            if chunk is not None and acc.session is not None:
                for seg in acc.session.push(chunk):
                    acc.record(seg)
                    await websocket.send_json({"type": "segment", **seg.model_dump()})
    except WebSocketDisconnect:
        return
    except MediaError as err:
        await websocket.send_json({"type": "error", "detail": str(err)})
        await websocket.close(code=1011)
