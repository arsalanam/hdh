"""Prompt caching, for the one place it is nearly free money.

**The measurement that motivated it.** One care plan for an eleven-topic
patient costs ~90,000 input tokens across 47 calls, and 45% of that is
repetition: the patient situation sent 41 times (21,115 tokens) and the
plan text re-sent to five extra grading dimensions (19,850).

**The opportunity the eval harness creates.** `eval run --repeat 3` builds
the same plan three times to measure the noise floor, and repeats for one
case run consecutively. Retrieval is deterministic, so run 2 and run 3
rebuild *byte-identical* prompts — not merely a shared prefix, the whole
thing. Cached, that is one write at 1.25x and two reads at 0.1x instead of
three full charges.

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


def cached_text(prompt: str) -> list[dict[str, Any]]:
    """A user message body with a cache breakpoint at the end of it.

    The breakpoint goes *after* the whole prompt rather than after a shared
    prefix, deliberately: within one plan every call diverges immediately
    into its own topic and menu, so there is no useful common prefix to cut
    at. What repeats is the entire prompt, on the next run of the same case.

    Returns a plain string body when caching is off, so a non-measurement
    run sends exactly what it sent before.
    """
    if not enabled():
        return prompt  # type: ignore[return-value]
    return [
        {
            "type": "text",
            "text": prompt,
            "cache_control": {"type": "ephemeral", "ttl": TTL},
        }
    ]
