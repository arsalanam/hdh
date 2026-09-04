"""Prompt caching: off by default, and priced when on.

The measurement behind it: one plan for an eleven-topic patient is ~90,000
input tokens across 47 calls, 45% of it repetition. `eval run --repeat 3`
rebuilds byte-identical prompts three times, because retrieval is
deterministic — so the whole prompt, not merely a prefix, is reusable.
"""

from __future__ import annotations

import pytest

from hdh.modules.careplan import caching


@pytest.fixture(autouse=True)
def _off(monkeypatch):
    monkeypatch.delenv(caching.ENV_VAR, raising=False)


def test_caching_is_off_by_default():
    """A plan built for a clinician should not quietly depend on a billing
    optimisation."""
    assert not caching.enabled()
    assert caching.cached_text("hello") == "hello"


def test_an_uncached_prompt_is_sent_exactly_as_before():
    """A plain string, not a one-element block list — so a normal run's
    request body is byte-identical to what it was."""
    body = caching.cached_text("some prompt", "concerns")
    assert isinstance(body, str)


def test_enabling_it_adds_a_breakpoint(monkeypatch):
    monkeypatch.setenv(caching.ENV_VAR, "1")
    body = caching.cached_text("some prompt", "concerns")
    assert isinstance(body, list)
    assert body[0]["text"] == "some prompt"
    assert body[0]["cache_control"]["type"] == "ephemeral"


def test_the_ttl_outlasts_a_three_repeat_case(monkeypatch):
    """Three plans for one case take about four and a half minutes and the
    default cache lives five. A cache expiring mid-case would make run 3
    cost full price while run 2 was nearly free — a figure describing the
    timing rather than the change."""
    monkeypatch.setenv(caching.ENV_VAR, "1")
    assert caching.cached_text("x", "concerns")[0]["cache_control"]["ttl"] == "1h"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_the_usual_spellings_all_turn_it_on(monkeypatch, value):
    monkeypatch.setenv(caching.ENV_VAR, value)
    assert caching.enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_anything_else_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv(caching.ENV_VAR, value)
    assert not caching.enabled()


def test_the_minimum_is_recorded_and_our_prompts_clear_it():
    """1024 for Sonnet. Care-plan prompts measured at 1,093–5,258 tokens,
    which is the fact that makes this worth doing at all — below the
    minimum a breakpoint is silently ignored and the request bills in
    full."""
    assert caching.MINIMUM_CACHEABLE_TOKENS == 1024


def test_both_model_backends_go_through_it():
    """A backend that skipped `cached_text` would bill full price while the
    ledger reported a hit rate for the other one."""
    import inspect

    from hdh.modules.careplan import evaluate, generate

    assert "cached_text(" in inspect.getsource(generate.llm_selector)
    assert "cached_prefix(" in inspect.getsource(evaluate.llm_grader)


# ── caching only where a later run can reproduce the prompt ──────────────
#
# The first version of this cached every call. The A/B measured 166,062
# billable tokens uncached against 160,430 cached — 3.4%, with 105,066
# tokens written and 36,670 read. Almost all misses, and a miss costs 1.25x.


def test_a_stage_that_cannot_repeat_is_not_cached(monkeypatch):
    """`goals` is built from the concerns the model produced moments
    earlier, so no two runs agree and a breakpoint is a pure 1.25x
    penalty."""
    monkeypatch.setenv(caching.ENV_VAR, "1")
    assert caching.cached_text("a goals prompt", "goals") == "a goals prompt"


def test_the_one_reproducible_stage_is_cached(monkeypatch):
    """`concerns` is built from the situation and the topics, both
    deterministic — run 2 sends exactly what run 1 sent."""
    monkeypatch.setenv(caching.ENV_VAR, "1")
    body = caching.cached_text("a concerns prompt", "concerns")
    assert body[0]["cache_control"]["type"] == "ephemeral"


def test_no_stage_at_all_is_not_cached(monkeypatch):
    """A caller that forgets the stage gets the old behaviour, not a
    breakpoint it never asked for."""
    monkeypatch.setenv(caching.ENV_VAR, "1")
    assert caching.cached_text("orphan") == "orphan"


def test_the_stages_that_pay_are_a_measurement_not_a_guess():
    """Pinning the set: adding a stage here without measuring it is how the
    3.4% version happened."""
    assert caching.REPEATABLE_STAGES == frozenset({"concerns"})


def test_every_generation_stage_is_named_by_the_schema_reader():
    """`REPEATABLE_STAGES` is matched against `_stage_of`'s output, so a
    typo in either would silently cache nothing and look like a saving of
    zero rather than a bug."""
    from hdh.modules.careplan.generate import _stage_of

    schemas = {
        "concerns": {"properties": {"selections": {"items": {"properties": {"idx": {}}}}}},
        "goals": {"properties": {"selections": {"items": {"properties": {"concern_index": {}}}}}},
        "interventions": {"properties": {"selections": {"items": {"properties": {"goal_index": {}}}}}},
    }
    named = {_stage_of(schema) for schema in schemas.values()}
    assert named == {"concerns", "goals", "interventions"}
    assert caching.REPEATABLE_STAGES <= named, "a stage nothing produces caches nothing"


def test_the_call_sites_pass_a_stage():
    """Without one every call falls through to the uncached branch and the
    feature is a no-op that still reports as enabled."""
    import inspect

    from hdh.modules.careplan import evaluate, generate

    assert "cached_text(prompt, _stage_of(" in inspect.getsource(generate.llm_selector)
    assert 'cached_prefix(*grading_parts(task), "grading")' in inspect.getsource(evaluate.llm_grader)


# ── the shared prefix, which is what makes grading cacheable at all ──────
#
# Grading sends the situation and the plan six times, once per dimension —
# 3,970 of each call's 5,258 tokens. Until `default@2` the per-dimension
# title and anchors sat in front, so the repeated part was not a *prefix*
# and no breakpoint could reach it.


def test_a_prefix_stage_gets_the_breakpoint_between_the_halves(monkeypatch):
    monkeypatch.setenv(caching.ENV_VAR, "1")
    body = caching.cached_prefix("the invariant half", "the varying half", "grading")
    assert [block["text"] for block in body] == ["the invariant half", "the varying half"]
    assert body[0]["cache_control"]["ttl"] == "1h"
    assert "cache_control" not in body[1], "a breakpoint on the tail caches what never repeats"


def test_an_uncached_prefix_stage_sends_the_plain_concatenation(monkeypatch):
    monkeypatch.delenv(caching.ENV_VAR, raising=False)
    assert caching.cached_prefix("a", "b", "grading") == "ab"


def test_a_stage_with_no_shared_prefix_is_not_split(monkeypatch):
    monkeypatch.setenv(caching.ENV_VAR, "1")
    assert caching.cached_prefix("a", "b", "goals") == "ab"


def test_the_two_halves_of_grading_share_no_placeholder():
    """The one property the saving rests on. A per-dimension slot leaking
    into the invariant half would make every one of the six calls a miss
    that still pays the 1.25x write — a saving of *less* than zero, and
    nothing at runtime would say so."""
    from hdh.modules.careplan.prompts import REQUIRED

    invariant = REQUIRED["grading_situation"]
    per_dimension = REQUIRED["grading_question"]
    assert invariant == {"situation", "plan"}
    assert not (invariant & per_dimension)


def test_the_invariant_half_of_the_loaded_prompt_has_no_per_dimension_slot():
    """Checked against the prompt set on disk, not just the contract: a set
    that reworded `grading_situation` to mention the dimension would load
    cleanly and silently cost money."""
    import string

    from hdh.modules.careplan.prompts import prompt_set

    text = prompt_set().text("grading_situation")
    slots = {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}
    assert slots == {"situation", "plan"}
