"""Local OCR — printed pages off an image or PDF (design §10, phase 7).

This is the real engine behind the ``/ocr`` contract and the
:class:`~hdh.modules.media.backends.OcrEngine` protocol: **docTR** (detection +
recognition) for printed pages, on CUDA. It is the swappable replacement for the
cloud vision pre-pass in ``agent_api/notes.py`` (design §4) — same text-out
contract, kept on-box.

Same honest boundary as STT/TTS (phases 3/5/6):

- the heavy import is **lazy**, inside the method — this file imports fine with
  no ``media`` extra and no GPU;
- asking for the local OCR engine without the extra **fails loudly** with an
  install hint, never a silent empty extraction;
- the pure glue — assembling recognised regions into an :class:`Extraction`
  with page count and layout blocks — is a module function unit-tested with no
  model at all.

**Scope:** printed text only. Handwriting stays the cloud vision path for now
(design open-q #4 — a local VLM is a later on-box-privacy upgrade); this engine
never invents characters it did not read, and a low-confidence region is carried
through on its block so the note path can flag it for review rather than trust it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from hdh.modules.media.backends import Extraction, MediaError

# A recognised region scoring below this is still returned (OCR, unlike STT, has
# no "needs_review" field on the page text) but flagged on its block so the note
# path can route it to review rather than trust it.
CONFIDENCE_FLOOR = 0.5

_EXTRA_HINT = (
    "the local OCR backend needs the docTR model — install with: pip install 'hdh[media]' "
    "(python-doctr + torch) and run on a CUDA GPU"
)


# ── pure glue (no model, fully tested) ───────────────────────────────────────


def to_extraction(pages: Sequence[Sequence[dict[str, Any]]], *, model: str) -> Extraction:
    """Assemble recognised regions into an :class:`Extraction`.

    ``pages`` is one list of region dicts per page — ``{text, confidence?,
    bbox?}`` — the shape the docTR glue produces. Pure: the engine reads the
    model, this shapes it, and the shaping is what the tests cover. A region's
    confidence is carried onto its block and a below-floor region is flagged so
    the note path can review rather than trust it; the page text is the regions
    joined in reading order."""
    blocks: list[dict] = []
    lines: list[str] = []
    for page_no, regions in enumerate(pages, start=1):
        page_lines: list[str] = []
        for region in regions:
            text = str(region.get("text", "")).strip()
            if not text:
                continue
            confidence = float(region.get("confidence", 1.0))
            block: dict[str, Any] = {"text": text, "page": page_no, "confidence": confidence}
            if confidence < CONFIDENCE_FLOOR:
                block["needs_review"] = True
            if region.get("bbox") is not None:
                block["bbox"] = region["bbox"]
            blocks.append(block)
            page_lines.append(text)
        lines.append("\n".join(page_lines))
    return Extraction(
        text="\n\n".join(lines).strip(),
        pages=max(1, len(pages)),
        blocks=blocks,
        model=model,
    )


# ── the engine (lazy heavy import; runs on the GPU box) ───────────────────────


class DocTROcr:
    """docTR OCR for printed pages. Constructible in CI; ``extract`` raises a
    clear install/GPU error without the ``media`` extra rather than returning an
    empty page."""

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model or os.environ.get("HDH_OCR_MODEL", "doctr:db_resnet50+crnn_vgg16_bn")
        self._pipeline: Any = None

    def _load(self) -> Any:
        if self._pipeline is None:
            try:
                self._pipeline = _build_doctr_pipeline()
            except ImportError as err:
                raise MediaError(f"{_EXTRA_HINT} [{err}]") from None
        return self._pipeline

    def extract(self, data: bytes, content_type: str) -> Extraction:
        if not data:
            raise MediaError("nothing to extract — empty document")
        pipeline = self._load()
        pages = pipeline(data, content_type)
        return to_extraction(pages, model=self.model)


def _build_doctr_pipeline() -> Any:  # pragma: no cover - needs the media extra + model
    """Load docTR and return a ``(bytes, content_type) -> list[list[dict]]``
    callable (one region list per page). Imported lazily; GPU/model-bound, so
    excluded from coverage and validated on the 5060, not in CI."""
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor

    predictor = ocr_predictor(pretrained=True)

    def run(data: bytes, content_type: str) -> list[list[dict[str, Any]]]:
        if content_type == "application/pdf":
            document = DocumentFile.from_pdf(data)
        else:
            document = DocumentFile.from_images(data)
        result = predictor(document)
        pages: list[list[dict[str, Any]]] = []
        for page in result.pages:
            regions: list[dict[str, Any]] = []
            for block in page.blocks:
                for line in block.lines:
                    text = " ".join(word.value for word in line.words)
                    confidence = (
                        sum(word.confidence for word in line.words) / len(line.words) if line.words else 1.0
                    )
                    regions.append({"text": text, "confidence": confidence, "bbox": line.geometry})
            pages.append(regions)
        return pages

    return run
