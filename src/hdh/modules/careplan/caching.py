"""Prompt caching, for the one place it is nearly free money.

**The measurement that motivated it.** One care plan for an eleven-topic
patient costs ~90,000 input tokens across 47 calls, and 45% of that is
repetition: the patient situation sent 41 times (21,115 tokens) and the
plan text re-sent to five extra grading dimensions (19,850).

**What actually repeats, measured rather than assumed.** `eval run
--repeat 3` builds the same plan three times, and a case's repeats run
consecutively — so the first version of this cached every call, on the
reasoning that deterministic retrieval would rebuild byte-identical prompts.

The A/B said otherwise: 166,062 billable input tokens uncached against
160,430 cached, a 3.4% saving, with 105,066 tokens *written* to the cache
and only 36,670 read back. Almost all of it was misses, and a miss is not
free — a write costs 1.25x.

The reason is that only the **first** stage is reproducible. `concerns` is
built from the situation and the topics, both deterministic, so run 2 and
run 3 send exactly what run 1 sent. `goals` is built from the concerns the
model just produced, and `interventions` from the goals — each is
conditioned on the previous sampling, so no two runs agree and a breakpoint
there is a pure 1.25x penalty on 30 of the 41 generation calls.

So caching is applied **per stage**, and only to stages whose prompts a
later run can actually reproduce.

**And it does not disturb what the repeats are for.** Caching changes how a
prompt is billed, never how it is sampled: the model still produces a
different plan each time. The variance the repeats exist to measure is
untouched, which is the property that makes this safe to switch on for a
measurement run.

**Why the TTL is an hour.** Three plans for one case take about four and a
half minutes, and the default cache lives five. A cache that expires
mid-case would make run 3 cost full price while run 2 was nearly free, and
the resulting figure would describe the timing rather than the change.

Off by default. Caching is a billing optimisation, and a plan built for a
clinician should not quietly depend on one — but a measurement run over
twenty-four plans should not pay four times for the same tokens either.
"""

from __future__ import annotations

import os
from typing import Any

#: Turns caching on. Off unless asked for, so a normal run is unchanged.
ENV_VAR = "HDH_CAREPLAN_CACHE"

#: An hour, not the five-minute default — see the module docstring.
TTL = "1h"

#: Below this many tokens Anthropic will not cache a prefix at all, and the
#: request is billed as if no breakpoint were set.
#:
#: 1024 for Sonnet and Opus; 2048 for Haiku. Care-plan prompts measured at
#: 1,093–5,258 tokens, so every one of them clears the Sonnet bar — which
#: is the fact that makes this worth doing rather than a rounding error.
MINIMUM_CACHEABLE_TOKENS = 1024


def enabled() -> bool:
    """Is caching switched on for this run?"""
    return (os.environ.get(ENV_VAR) or "").strip().lower() in {"1", "true", "yes", "on"}


#: Stages whose prompt a later run of the same case reproduces exactly.
#:
#: `concerns` only, and that is a measurement rather than a preference: it
#: is built from the situation and the topics, both deterministic. `goals`
#: is built from the concerns the model produced moments earlier and
#: `interventions` from the goals, so each is conditioned on sampling and no
#: two runs agree. Caching those cost 1.25x on every one of 30 calls and
#: returned nothing — the difference between a 3.4% saving and a real one.
#:
#: `grading` is absent here but cached by :func:`cached_prefix` instead,
#: because it repeats along a different axis: not the next run of the case,
#: but the next of the six dimensions grading the same plan.
REPEATABLE_STAGES: frozenset[str] = frozenset({"concerns"})

#: Stages whose prompt begins with a block their sibling calls repeat.
#:
#: Grading sends the situation and the plan six times, once per dimension —
#: 3,970 of each call's 5,258 tokens. Until `default@2` the per-dimension
#: title and anchors sat in front of them, so the repeated part was not a
#: *prefix* and no breakpoint could reach it. Reordering the prompt is what
#: turned this from unreachable into the largest saving in the module.
PREFIX_STAGES: frozenset[str] = frozenset({"grading"})


def cached_text(prompt: str, stage: str = "") -> list[dict[str, Any]] | str:
    """A user message body, with a cache breakpoint only where one can pay.

    The breakpoint goes *after* the whole prompt rather than after a shared
    prefix: within one plan every call diverges immediately into its own
    topic and menu, so there is no useful common prefix to cut at. What
    repeats is the entire prompt, on the next run of the same case — and
    only for the stages in :data:`REPEATABLE_STAGES`.

    Returns a plain string when caching is off or the stage cannot repeat,
    so those calls send exactly what they sent before.
    """
    if not enabled() or stage not in REPEATABLE_STAGES:
        return prompt
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": {"type": "ephemeral", "ttl": TTL},
        }
    ]


def cached_prefix(prefix: str, suffix: str, stage: str = "") -> list[dict[str, Any]] | str:
    """Two blocks with the breakpoint between them, so `prefix` is reused.

    The one rule that matters is that `prefix` must be **byte-identical**
    across the calls meant to share it; a single differing character makes
    every call a miss that also pays the 1.25x write. That is why the split
    lives in the prompt set as two texts rather than being sliced out of one
    formatted string here.

    Returns the plain concatenation when caching is off or the stage has no
    shared prefix, so those calls send exactly what they always sent.
    """
    if not enabled() or stage not in PREFIX_STAGES:
        return prefix + suffix
    return [
        {
            "type": "text",
            "text": prefix,
            "cache_control": {"type": "ephemeral", "ttl": TTL},
        },
        {"type": "text", "text": suffix},
    ]
