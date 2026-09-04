"""What a care-plan run costs, in tokens.

The harness measured how good a plan was and never what it cost to produce.
The API reported usage on every one of the ~33 calls a single plan makes,
and both backends read `response.content` and dropped `response.usage`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hdh.modules.careplan import usage


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Response:
    usage: object


def _reply(inp: int, out: int) -> _Response:
    return _Response(usage=_Usage(inp, out))


# ── the ledger ───────────────────────────────────────────────────────────


def test_nothing_is_recorded_without_an_open_ledger():
    """Recording must be free when nobody asked for the numbers — the graph
    and the revise loop call these backends constantly."""
    usage.record(_reply(10, 5), "concerns")  # must not raise


def test_an_open_ledger_counts_calls_and_tokens():
    with usage.collecting() as ledger:
        usage.record(_reply(100, 20), "concerns")
        usage.record(_reply(200, 30), "goals")
    assert ledger.calls == 2
    assert ledger.input_tokens == 300
    assert ledger.output_tokens == 50
    assert ledger.total_tokens == 350


def test_tokens_are_attributed_to_the_stage_that_spent_them():
    """ "Where did the tokens go" has to be answerable. The interventions
    node fans out furthest and a reader should see that, not infer it."""
    with usage.collecting() as ledger:
        usage.record(_reply(100, 10), "concerns")
        usage.record(_reply(400, 40), "interventions")
        usage.record(_reply(300, 30), "interventions")
    assert ledger.by_stage["interventions"].calls == 2
    assert ledger.by_stage["interventions"].input_tokens == 700
    assert ledger.by_stage["concerns"].calls == 1


def test_ledgers_nest_without_leaking():
    """The tuning loop opens one per side. Neither side's tokens may land in
    the other's."""
    with usage.collecting() as outer:
        usage.record(_reply(10, 1), "concerns")
        with usage.collecting() as inner:
            usage.record(_reply(500, 50), "goals")
        usage.record(_reply(20, 2), "concerns")
    assert inner.calls == 1 and inner.input_tokens == 500
    assert outer.calls == 2 and outer.input_tokens == 30


def test_a_response_with_no_usage_counts_as_a_call_not_a_crash():
    """A cost figure is not worth failing a plan over."""
    with usage.collecting() as ledger:
        usage.record(object(), "concerns")
        usage.record(_Response(usage=None), "goals")
    assert ledger.calls == 2
    assert ledger.total_tokens == 0


def test_the_ledger_survives_an_exception_inside_the_block():
    with pytest.raises(RuntimeError), usage.collecting():
        usage.record(_reply(1, 1), "concerns")
        raise RuntimeError("boom")
    usage.record(_reply(9, 9), "concerns")  # ledger closed; must not raise


# ── how it reads ─────────────────────────────────────────────────────────


def test_the_summary_leads_with_the_totals():
    with usage.collecting() as ledger:
        usage.record(_reply(12345, 678), "concerns")
    text = "\n".join(usage.summarise(ledger))
    assert "12,345 in" in text and "678 out" in text
    assert "13,023 total" in text


def test_the_summary_orders_stages_by_what_they_cost():
    with usage.collecting() as ledger:
        usage.record(_reply(10, 1), "concerns")
        usage.record(_reply(900, 90), "interventions")
    lines = usage.summarise(ledger)
    assert "interventions" in lines[1]
    assert "concerns" in lines[2]


def test_a_run_with_no_calls_says_so_rather_than_showing_zeroes():
    """Zeroes read as "it cost nothing"; no calls means it did not run."""
    assert "no model calls" in "\n".join(usage.summarise(usage.Ledger()))


def test_the_ledger_serialises_for_the_baseline():
    with usage.collecting() as ledger:
        usage.record(_reply(100, 10), "grading")
    payload = ledger.as_dict()
    assert payload["calls"] == 1
    assert payload["by_stage"]["grading"]["input_tokens"] == 100


# ── the backends are wired to it ─────────────────────────────────────────


def test_the_selector_records_against_the_stage_that_asked():
    """The stage is read from the schema, because only a goal carries
    concern_index and only an intervention goal_index — so the shape of the
    answer names the node, without changing the Selector signature."""
    from hdh.modules.careplan.generate import _stage_of

    def shaped(**properties):
        return {"properties": {"selections": {"items": {"properties": properties}}}}

    assert _stage_of(shaped(goal_index={}, statement={})) == "interventions"
    assert _stage_of(shaped(concern_index={}, statement={})) == "goals"
    assert _stage_of(shaped(concern_type={}, statement={})) == "concerns"
    # A shape it does not recognise is still a call worth counting.
    assert _stage_of({}) == "selection"


def test_the_baseline_carries_what_the_run_cost():
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@1")
    report.usage = {"calls": 33, "input_tokens": 90000, "output_tokens": 4000, "by_stage": {}}
    assert report.as_dict()["usage"]["calls"] == 33


def test_a_baseline_without_usage_still_serialises():
    """Every baseline written before this existed has no usage, and must
    not become unreadable for it."""
    from hdh.modules.careplan import evalset

    assert evalset.Report(cohort="default").as_dict()["usage"] == {}


# ── cache traffic, and what it actually costs ────────────────────────────


def _reply_cached(inp: int, out: int, write: int = 0, read: int = 0) -> _Response:
    @dataclass
    class _U:
        input_tokens: int
        output_tokens: int
        cache_creation_input_tokens: int
        cache_read_input_tokens: int

    return _Response(usage=_U(inp, out, write, read))


def test_cache_writes_and_reads_are_counted_separately():
    """Folding them into `input_tokens` would make a large saving look like
    a small one, because the API prices them differently."""
    with usage.collecting() as ledger:
        usage.record(_reply_cached(0, 10, write=1500), "concerns")
        usage.record(_reply_cached(0, 10, read=1500), "concerns")
    assert ledger.cache_write_tokens == 1500
    assert ledger.cache_read_tokens == 1500
    assert ledger.input_tokens == 0


def test_billable_input_prices_each_kind():
    """The number a before/after comparison rests on. Raw counts hide the
    point of caching entirely."""
    with usage.collecting() as ledger:
        usage.record(_reply_cached(100, 5), "concerns")
        usage.record(_reply_cached(0, 5, write=1000), "concerns")
        usage.record(_reply_cached(0, 5, read=1000), "concerns")
    # 100 + 1000*1.25 + 1000*0.1
    assert ledger.billable_input == pytest.approx(1450.0)


def test_a_cached_run_bills_less_than_it_reads():
    """The property worth the whole exercise: more tokens offered to the
    model, fewer tokens paid for."""
    with usage.collecting() as ledger:
        usage.record(_reply_cached(0, 5, write=1000), "grading")
        for _ in range(5):
            usage.record(_reply_cached(0, 5, read=1000), "grading")
    offered = ledger.input_tokens + ledger.cache_write_tokens + ledger.cache_read_tokens
    assert offered == 6000
    assert ledger.billable_input < offered / 3


def test_the_hit_rate_is_a_share_of_input_not_of_calls():
    with usage.collecting() as ledger:
        usage.record(_reply_cached(1000, 5), "concerns")
        usage.record(_reply_cached(0, 5, read=3000), "concerns")
    assert ledger.cache_hit_rate == pytest.approx(0.75)


def test_a_response_without_cache_fields_still_records():
    """A model or SDK that does not cache reports nothing, and that means
    no cache traffic — which is what 0 says."""
    with usage.collecting() as ledger:
        usage.record(_reply(100, 10), "concerns")
    assert ledger.cache_write_tokens == 0
    assert ledger.cache_read_tokens == 0
    assert ledger.billable_input == 100


def test_the_summary_prices_the_saving_rather_than_counting_tokens():
    """Raw traffic reads as MORE tokens, not fewer — a cache read is still a
    token offered to the model. The saving only appears once priced."""
    with usage.collecting() as ledger:
        usage.record(_reply_cached(0, 5, write=1000), "grading")
        for _ in range(5):
            usage.record(_reply_cached(0, 5, read=1000), "grading")
    text = "\n".join(usage.summarise(ledger))
    assert "cache:" in text
    assert "saved" in text
    assert "uncached" in text


def test_the_baseline_carries_the_cache_figures():
    with usage.collecting() as ledger:
        usage.record(_reply_cached(0, 5, read=1000), "grading")
    payload = ledger.as_dict()
    assert payload["cache_read_tokens"] == 1000
    assert "billable_input" in payload
    assert payload["by_stage"]["grading"]["cache_read_tokens"] == 1000
