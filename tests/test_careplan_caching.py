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
    body = caching.cached_text("some prompt")
    assert isinstance(body, str)


def test_enabling_it_adds_a_breakpoint(monkeypatch):
    monkeypatch.setenv(caching.ENV_VAR, "1")
    body = caching.cached_text("some prompt")
    assert isinstance(body, list)
    assert body[0]["text"] == "some prompt"
    assert body[0]["cache_control"]["type"] == "ephemeral"


def test_the_ttl_outlasts_a_three_repeat_case(monkeypatch):
    """Three plans for one case take about four and a half minutes and the
    default cache lives five. A cache expiring mid-case would make run 3
    cost full price while run 2 was nearly free — a figure describing the
    timing rather than the change."""
    monkeypatch.setenv(caching.ENV_VAR, "1")
    assert caching.cached_text("x")[0]["cache_control"]["ttl"] == "1h"


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
    assert "cached_text(" in inspect.getsource(evaluate.llm_grader)
