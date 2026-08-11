"""Milestone D tests: the description→code funnel and the pattern compiler.

Everything below the LLM is deterministic, so the funnel runs offline with
a stub extractor over the fixture slice — including the design's worked
example ("inner side of left ankle, first visit, skin intact" → S82.52XA)
and the missing-axis behavior the agent turns into follow-up questions.
"""

from pathlib import Path

import pytest

from hdh.core.models import get_engine, get_session
from hdh.core.schema_registry import bootstrap_schema
from hdh.modules.icd10cm.loader import run_load
from hdh.modules.icd10cm.patterns import PatternError, run_pattern, validate_pattern
from hdh.modules.icd10cm.service import AxisExtraction, codify, stub_extractor

FIXTURES = Path(__file__).parent / "fixtures" / "icd10cm"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """The fixture slice, loaded once for all retrieval tests."""
    bootstrap_schema()
    db = tmp_path_factory.mktemp("retrieval") / "catalog.db"
    engine = get_engine(str(db))
    session = get_session(engine)
    run_load(session, FIXTURES, 2026)
    yield session
    session.close()
    engine.dispose()


def test_codify_the_design_worked_example(catalog):
    """'Broke the inner side of her left ankle, first visit, skin intact'
    → S82.52XA, with every requested axis matched (design §7.2)."""
    extractor = stub_extractor(
        "fracture medial malleolus",
        {"aspect": "medial", "laterality": "left", "encounter": "initial", "exposure": "closed"},
    )
    extraction, candidates = codify(catalog, "broke inner left ankle, first visit", extractor)
    assert extraction.axes["laterality"] == "left"
    top = candidates[0]
    assert top.code in ("S82.52XA", "S82.55XA")  # displacement was never stated
    assert set(top.matched) == {"aspect", "laterality", "encounter", "exposure"}
    assert not top.conflicts
    # both displacement branches surface near the top — the funnel's honest
    # answer when the description doesn't say (RFC Q9)
    top_codes = {c.code for c in candidates[:3]}
    assert {"S82.52XA", "S82.55XA"} <= top_codes


def test_codify_ulna_right_initial(catalog):
    """Laterality + encounter narrow the S52.00 family to one code."""
    extractor = stub_extractor(
        "fracture upper end ulna",
        {"laterality": "right", "encounter": "initial", "exposure": "closed"},
    )
    _extraction, candidates = codify(catalog, "broke top of right forearm bone, first visit", extractor)
    assert candidates[0].code == "S52.001A"
    assert candidates[0].exact


def test_codify_conflict_detection(catalog):
    """Requesting the LEFT side marks right-side codes as conflicting."""
    extractor = stub_extractor("fracture upper end ulna", {"laterality": "left"})
    _extraction, candidates = codify(catalog, "left forearm fracture", extractor, limit=10)
    by_code = {c.code: c for c in candidates}
    assert "laterality" in by_code["S52.002A"].matched if "S52.002A" in by_code else True
    rights = [c for c in candidates if c.code.startswith("S52.001")]
    assert all("laterality" in c.conflicts for c in rights)


def test_codify_validates_axes():
    """Unknown axes and values are dropped, never scored."""
    extraction = AxisExtraction("terms", {"laterality": "left", "bogus": "x", "aspect": "sideways"})
    clean = extraction.validated()
    assert clean.axes == {"laterality": "left"}


def test_pattern_left_billable_fracture_codes(catalog):
    """The design §7.3 pattern shape end-to-end."""
    hits = run_pattern(
        catalog,
        {
            "anchor": {"code": "S82.5"},
            "axes": {"laterality": "left"},
            "traverse": [{"edge": "parent_of", "dir": "down", "depth": "*"}],
            "constraints": {"billable": True},
        },
    )
    codes = {h.code for h in hits}
    assert codes == {
        "S82.52XA",
        "S82.52XB",
        "S82.52XD",
        "S82.52XS",
        "S82.55XA",
        "S82.55XB",
        "S82.55XD",
        "S82.55XS",
    }


def test_pattern_typed_edge_traversal(catalog):
    """Anchor on a code, hop its contralateral edge."""
    hits = run_pattern(
        catalog,
        {"anchor": {"code": "S52.001A"}, "traverse": [{"edge": "contralateral"}]},
    )
    assert [h.code for h in hits] == ["S52.002A"]


def test_pattern_rejections():
    """The closed schema rejects with actionable feedback (retry loop)."""
    with pytest.raises(PatternError, match="unknown pattern keys"):
        validate_pattern({"anchor": {"terms": "x"}, "sql": "DROP TABLE"})
    with pytest.raises(PatternError, match="unknown edge type"):
        validate_pattern({"anchor": {"terms": "x"}, "traverse": [{"edge": "buddy_of"}]})
    with pytest.raises(PatternError, match="unknown axis"):
        validate_pattern({"anchor": {"terms": "x"}, "axes": {"vibe": "left"}})
    with pytest.raises(PatternError, match="unknown value"):
        validate_pattern({"anchor": {"terms": "x"}, "axes": {"laterality": "starboard"}})
    with pytest.raises(PatternError, match="depth 1 only"):
        validate_pattern({"anchor": {"terms": "x"}, "traverse": [{"edge": "contralateral", "depth": "*"}]})


def test_pattern_axes_filter_uses_stored_json(catalog):
    """Axis filtering reads properties.axes (portable JSON path)."""
    hits = run_pattern(
        catalog,
        {
            "anchor": {"code": "S52"},
            "traverse": [{"edge": "parent_of", "dir": "down", "depth": "*"}],
            "axes": {"laterality": "unspecified"},
            "constraints": {"billable": True},
        },
        limit=50,
    )
    assert hits and all(h.code.startswith("S52.009") for h in hits)
