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
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from hdh.core.identity.web_auth import AuthError, keycloak_authenticator
from hdh.modules.media.backends import Backends, Extraction, MediaError, Transcript, make_backends

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
            "streaming_stt": False,  # WS /stt/stream lands in phase 5
        }

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
