"""S5b: the fast loop, and its refusal to be the arbiter.

Most of what matters here is what the module declines to say. A tuning run
reports what moved; it never reports that the move was an improvement,
because a single case cannot clear the cohort's noise floor and the person
reading it just made the change and wants it to have worked.
"""

from __future__ import annotations

import json

import pytest

from hdh.modules.careplan import prompts
from hdh.modules.careplan.tune import (
    CANNOT_DECIDE,
    Side,
    TuneResult,
    cohort_noise,
    summarise,
)


@pytest.fixture(autouse=True)
def _fresh():
    prompts.reset()
    yield
    prompts.reset()


def _result(**overrides) -> TuneResult:
    defaults = dict(
        mrn="MRN0001",
        before=Side(
            prompt_set="default@1",
            scores={"traceability": 2, "goal_quality": 3, "safety": 5},
            concerns=8,
            goals=11,
            interventions=21,
            uncited=0,
        ),
        after=Side(
            prompt_set="default@2",
            scores={"traceability": 3, "goal_quality": 4, "safety": 5},
            concerns=8,
            goals=11,
            interventions=17,
            uncited=0,
        ),
        noise=0.207,
    )
    defaults.update(overrides)
    return TuneResult(**defaults)


# ── what it refuses to say ───────────────────────────────────────────────


def test_it_never_calls_a_single_case_an_improvement():
    text = "\n".join(summarise(_result()))
    for word in ("improved", "better", "improvement", "regression"):
        assert word not in text.lower(), f"a tuning run claimed {word!r} from one case"


def test_the_refusal_is_the_last_thing_on_screen():
    """It has to be the sentence still visible when someone decides what to
    do next — not a disclaimer at the top that scrolls away."""
    lines = [line for line in summarise(_result()) if line.strip()]
    assert "eval run" in lines[-1]
    assert CANNOT_DECIDE in "\n".join(lines[-3:])


def test_it_names_the_measured_noise_floor():
    text = "\n".join(summarise(_result()))
    assert "0.207" in text
    assert "this run measures none" in text


def test_it_names_the_arbiter_and_the_version_bump():
    text = "\n".join(summarise(_result()))
    assert "bump the prompt set version" in text
    assert "--repeat 3" in text


def test_the_noise_floor_is_read_from_the_baseline_not_written_down():
    """A number written into the source is one that was true once."""
    assert cohort_noise() > 0


def test_an_unknown_cohort_reports_no_floor_rather_than_a_wrong_one():
    assert cohort_noise("does-not-exist") == 0.0


# ── what it does say ─────────────────────────────────────────────────────


def test_it_shows_both_sides_of_every_dimension():
    text = "\n".join(summarise(_result()))
    assert "traceability" in text and "goal_quality" in text


def test_it_reports_the_governing_dimension_moving():
    """The lowest dimension decides the verdict, so the interesting question
    is not whether the mean rose but whether the governing dimension did."""
    text = "\n".join(summarise(_result()))
    assert "governing: traceability -> traceability" in text


def test_the_governing_dimension_names_ties():
    side = Side(prompt_set="x", scores={"a": 2, "b": 2, "c": 5})
    assert side.governing == "a, b"


def test_a_dimension_that_did_not_move_says_so_rather_than_plus_zero():
    result = _result(
        after=Side(prompt_set="default@2", scores={"traceability": 2, "goal_quality": 3, "safety": 5})
    )
    rows = [line for line in summarise(result) if line.strip().startswith(("traceability", "safety"))]
    assert rows and all("same" in row for row in rows)
    # The mean line may legitimately print +0.000; the per-dimension rows
    # must not, because "+0" reads as a movement that did not happen.
    assert not any("+0" in row for row in rows)


def test_deltas_cover_only_dimensions_both_sides_scored():
    """An ungraded dimension on one side is not a change of zero."""
    result = _result(after=Side(prompt_set="default@2", scores={"traceability": 4}))
    assert result.deltas() == {"traceability": 2}


def test_uncited_counts_are_shown_because_that_is_what_traceability_grades():
    result = _result(
        before=Side(prompt_set="default@1", uncited=3),
        after=Side(prompt_set="default@2", uncited=0),
    )
    text = "\n".join(summarise(result))
    assert "elements citing nothing" in text


def test_an_ungraded_comparison_still_reports_shape():
    """Without an API key there are no scores, but counts still tell you
    whether the wording changed how much the plan proposes."""
    result = _result(
        before=Side(prompt_set="default@1", concerns=8, interventions=21),
        after=Side(prompt_set="default@2", concerns=8, interventions=12),
        noise=0.0,
    )
    text = "\n".join(summarise(result))
    assert "interventions" in text
    assert CANNOT_DECIDE in text


# ── the prompt set applies to the whole run, and is restored ─────────────


def _write_set(tmp_path, name: str, version: int) -> None:
    payload = {
        "prompt_set_id": name,
        "version": version,
        "prompts": {
            key: "text " + " ".join("{" + p + "}" for p in req) for key, req in prompts.REQUIRED.items()
        },
    }
    (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_using_applies_to_everything_inside_it(tmp_path, monkeypatch):
    """Generation and grading both. The grading instruction is part of the
    set, so an edit to how plans are GRADED would otherwise be invisible."""
    _write_set(tmp_path, "trial", 7)
    monkeypatch.setattr(prompts, "HERE", tmp_path)
    prompts.reset()
    with prompts.using("trial"):
        assert prompts.prompt_set().stamp == "trial@7"


def test_using_restores_even_when_the_block_raises(tmp_path, monkeypatch):
    """A failed tuning run must not leave the process quietly generating
    under the wrong wording."""
    _write_set(tmp_path, "trial", 7)
    _write_set(tmp_path, "default", 1)
    monkeypatch.setattr(prompts, "HERE", tmp_path)
    prompts.reset()
    before = prompts.prompt_set().stamp
    with pytest.raises(RuntimeError), prompts.using("trial"):
        raise RuntimeError("boom")
    assert prompts.prompt_set().stamp == before


def test_using_nests(tmp_path, monkeypatch):
    _write_set(tmp_path, "one", 1)
    _write_set(tmp_path, "two", 2)
    monkeypatch.setattr(prompts, "HERE", tmp_path)
    prompts.reset()
    with prompts.using("one"):
        with prompts.using("two"):
            assert prompts.prompt_set().stamp == "two@2"
        assert prompts.prompt_set().stamp == "one@1"


def test_an_explicit_name_still_wins_inside_a_block(tmp_path, monkeypatch):
    """The override is for callers that did not ask; one that named a set
    meant it."""
    _write_set(tmp_path, "one", 1)
    _write_set(tmp_path, "two", 2)
    monkeypatch.setattr(prompts, "HERE", tmp_path)
    prompts.reset()
    with prompts.using("one"):
        assert prompts.prompt_set("two").stamp == "two@2"
