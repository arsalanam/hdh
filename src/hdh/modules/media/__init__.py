"""The media I/O service (design docs/design/media-io-service.md).

A standalone, **stateless** HTTP transducer: diarized speech-to-text, OCR, and
text-to-speech, so the hdh agent can be spoken to, handed a recording or a
scanned page, and talk back. It holds no clinical logic and no PHI at rest —
every clinical consequence happens *after* its text re-enters the agent or the
comprehension path, which already carry grounding and the order lifecycle.

Phase 1 (this module) is the skeleton: the backend protocols, a null/echo
backend (no GPU), and ``/health`` ``/capabilities`` ``/stt`` ``/tts`` ``/ocr``.
Real models (NeMo / Sortformer / TitaNet / Kokoro), streaming, and the AWS
backends land in later phases behind the same contract.
"""

from hdh.modules.media.server import create_app

__all__ = ["create_app"]
