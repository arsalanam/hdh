"""Phase-7 tests for local OCR — the pure region-to-Extraction shaping and the
docTR engine's loud-failure contract (design §10). No GPU, no model: the shaping
is pure and the engine's missing-extra path is forced with monkeypatch."""

from __future__ import annotations

import pytest

from hdh.modules.media.backends import Extraction, MediaError
from hdh.modules.media.ocr import CONFIDENCE_FLOOR, DocTROcr, to_extraction

# ── the pure shaping ───────────────────────────────────────────────────────────


def test_to_extraction_joins_pages_and_keeps_blocks():
    pages = [
        [{"text": "Chief complaint: cough", "confidence": 0.98}, {"text": "BP 130/80", "confidence": 0.95}],
        [{"text": "Plan: rest and fluids", "confidence": 0.9}],
    ]
    ext = to_extraction(pages, model="doctr:test")
    assert isinstance(ext, Extraction)
    assert ext.pages == 2
    assert "Chief complaint: cough" in ext.text and "Plan: rest and fluids" in ext.text
    assert ext.text.count("\n\n") == 1  # one blank line between the two pages
    assert len(ext.blocks) == 3
    assert ext.blocks[0] == {"text": "Chief complaint: cough", "page": 1, "confidence": 0.98}
    assert ext.blocks[2]["page"] == 2
    assert ext.model == "doctr:test"


def test_to_extraction_flags_low_confidence_regions():
    pages = [[{"text": "illegible scrawl", "confidence": 0.2}, {"text": "clear line", "confidence": 0.99}]]
    ext = to_extraction(pages, model="doctr:test")
    assert ext.blocks[0]["needs_review"] is True  # below the floor → flagged
    assert "needs_review" not in ext.blocks[1]  # clear → not flagged
    assert CONFIDENCE_FLOOR == 0.5


def test_to_extraction_skips_empty_regions_and_carries_bbox():
    pages = [[{"text": "  ", "confidence": 1.0}, {"text": "kept", "confidence": 1.0, "bbox": [0, 0, 1, 1]}]]
    ext = to_extraction(pages, model="m")
    assert len(ext.blocks) == 1  # the blank region dropped
    assert ext.blocks[0]["bbox"] == [0, 0, 1, 1]


def test_to_extraction_of_nothing_is_one_empty_page():
    ext = to_extraction([], model="m")
    assert ext.text == "" and ext.pages == 1 and ext.blocks == []


# ── the docTR engine ───────────────────────────────────────────────────────────


def test_doctr_rejects_empty_document():
    with pytest.raises(MediaError, match="nothing to extract"):
        DocTROcr().extract(b"", "image/png")


def test_doctr_turns_a_missing_extra_into_a_loud_error(monkeypatch):
    # Constructible with no GPU; extracting without docTR surfaces a MediaError
    # with an install hint — never a silent empty page.
    from hdh.modules.media import ocr

    def _missing(*_args, **_kwargs):
        raise ImportError("No module named 'doctr'")

    monkeypatch.setattr(ocr, "_build_doctr_pipeline", _missing)
    with pytest.raises(MediaError, match="pip install 'hdh\\[media\\]'"):
        DocTROcr().extract(b"\x89PNG...", "image/png")


def test_doctr_extracts_via_an_injected_pipeline(monkeypatch):
    # With the model stubbed, the engine shapes recognised regions into an
    # Extraction — the glue around the model, exercised without it.
    from hdh.modules.media import ocr

    def _fake_pipeline():
        def run(_data, _ct):
            return [[{"text": "old report", "confidence": 0.97}]]

        return run

    monkeypatch.setattr(ocr, "_build_doctr_pipeline", _fake_pipeline)
    ext = DocTROcr().extract(b"\x89PNG...", "image/png")
    assert ext.text == "old report"
    assert ext.blocks[0]["confidence"] == 0.97


# ── backend selection ──────────────────────────────────────────────────────────


def test_local_backend_uses_doctr_for_ocr():
    from hdh.modules.media.local import build_local_backends

    assert isinstance(build_local_backends().ocr, DocTROcr)  # real OCR now (phase 7)
