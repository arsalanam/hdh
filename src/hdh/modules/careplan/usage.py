"""What a care-plan run cost, in tokens.

The harness has always measured how good a plan is and never what it cost to
produce. That is half a measurement: a prompt change that raises the cohort
mean by 0.1 while tripling the token count is a trade, not an improvement,
and until now the trade was invisible — the baseline recorded scores and
noise but nothing about cost.

The API told us all along. ``llm_selector`` and ``llm_grader`` read
``response.content`` and dropped ``response.usage`` on the floor, so the
numbers existed on every one of the ~33 calls a single plan makes and were
never looked at.

**Why an ambient ledger rather than a return value.** A selector is a
``Callable[[SelectionTask], dict]`` that the graph, the revise loop, the
tuning loop and the eval harness all call without knowing what backs it —
the whole point of the injection seam. Threading usage back out would mean
changing that signature everywhere, including for the fake selectors that
have no usage at all. So the ledger is opened by whoever wants the numbers
and the backends record into it if one is open.

Nothing here fails a run. A backend that reports usage in a shape we do not
recognise gets counted as a call with zero tokens, because a cost figure is
not worth crashing a plan over.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

#: What each kind of input token costs, relative to an ordinary one.
#:
#: Anthropic's published multipliers for a 1-hour cache. They live here
#: rather than in a comment because `billable_input` is the number any
#: before/after comparison rests on, and a saving computed from raw counts
#: is not a saving.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

#: The open ledger, if any. A ContextVar rather than a module global so
#: concurrent runs cannot pour their tokens into each other's totals.
_ledger: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("careplan_usage_ledger", default=None)


@dataclass
class Ledger:
    """Tokens and calls, accumulated over some stretch of work."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: Tokens written into the cache, and read back out of it.
    #:
    #: Separate from ``input_tokens`` because the API reports them
    #: separately and they are priced differently — a write costs 1.25x an
    #: ordinary input token and a read costs 0.1x. Folding them together
    #: would make a large saving look like a small one and an expensive
    #: first call look free.
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    #: Per-stage totals, so "where did the tokens go" is answerable. The
    #: interventions node fans out furthest, and a reader should be able to
    #: see that rather than infer it.
    by_stage: dict[str, Ledger] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """Every input token, however it was billed, plus output."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens + self.output_tokens

    @property
    def billable_input(self) -> float:
        """Input tokens weighted by what each kind costs.

        The number to compare across runs. Raw counts hide the point of
        caching entirely: a cached run reads *more* tokens than an uncached
        one bills, because the read is nearly free.
        """
        return (
            self.input_tokens
            + self.cache_write_tokens * CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * CACHE_READ_MULTIPLIER
        )

    @property
    def cache_hit_rate(self) -> float:
        """Share of input tokens that came from the cache."""
        offered = self.input_tokens + self.cache_write_tokens + self.cache_read_tokens
        return self.cache_read_tokens / offered if offered else 0.0

    def add(
        self,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        cache_write: int = 0,
        cache_read: int = 0,
    ) -> None:
        """Record one call against the whole ledger and its stage.

        Both are updated in one pass so a stage total can never drift from
        the top-level one — they are the same numbers seen at two
        granularities, and a caller adding to only one of them is the bug
        this shape prevents.
        """
        for entry in (self, self.by_stage.setdefault(stage, Ledger()) if stage else self):
            entry.calls += 1
            entry.input_tokens += input_tokens
            entry.output_tokens += output_tokens
            entry.cache_write_tokens += cache_write
            entry.cache_read_tokens += cache_read
            if entry is self and not stage:
                break

    def as_dict(self) -> dict:
        """The ledger as JSON, for the baseline to carry.

        Stages are sorted so two baselines differ only where the numbers
        differ — an unstable key order would show as a diff on every run.
        """
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "billable_input": round(self.billable_input, 1),
            "by_stage": {
                stage: {
                    "calls": entry.calls,
                    "input_tokens": entry.input_tokens,
                    "output_tokens": entry.output_tokens,
                    "cache_write_tokens": entry.cache_write_tokens,
                    "cache_read_tokens": entry.cache_read_tokens,
                }
                for stage, entry in sorted(self.by_stage.items())
            },
        }


@contextmanager
def collecting() -> Iterator[Ledger]:
    """Count what happens inside this block.

    Nests: an inner ledger takes the recording, and the outer one sees only
    what happened outside it. That is deliberate — the tuning loop opens one
    per side, and neither side's tokens should land in the other's.
    """
    ledger = Ledger()
    token = _ledger.set(ledger)
    try:
        yield ledger
    finally:
        _ledger.reset(token)


def record(response, stage: str = "") -> None:
    """Note one API response against the open ledger, if there is one.

    Never raises. This is called on the success path of every model call,
    and an accounting error must not become a failed plan.
    """
    ledger = _ledger.get()
    if ledger is None:
        return
    usage = getattr(response, "usage", None)
    ledger.add(
        stage,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
        # Absent on a response from a model or SDK that does not cache, and
        # zero on a call that simply missed — both mean "no cache traffic",
        # which is what 0 says.
        int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    )


def summarise(ledger: Ledger, label: str = "") -> list[str]:
    """The ledger as lines, with the stages that dominate visible."""
    if not ledger.calls:
        return [f"{label}no model calls were made" if label else "no model calls were made"]

    head = f"{label}{ledger.calls} calls, "
    head += f"{ledger.input_tokens:,} in / {ledger.output_tokens:,} out"
    head += f" ({ledger.total_tokens:,} total)"
    lines = [head]
    if ledger.cache_read_tokens or ledger.cache_write_tokens:
        # Both the raw traffic and what it actually costs. The raw numbers
        # alone read as MORE tokens, not fewer, because a cache read is
        # still a token that was offered to the model — the saving is only
        # visible once each kind is priced.
        uncached = ledger.input_tokens + ledger.cache_write_tokens + ledger.cache_read_tokens
        saved = uncached - ledger.billable_input
        lines.append(
            f"    cache: {ledger.cache_write_tokens:,} written, "
            f"{ledger.cache_read_tokens:,} read "
            f"({ledger.cache_hit_rate:.0%} of input)"
        )
        lines.append(
            f"    billed as {ledger.billable_input:,.0f} input-equivalent "
            f"vs {uncached:,} uncached — {saved:,.0f} saved "
            f"({saved / uncached:.0%})"
            if uncached
            else "    nothing billed"
        )
    for stage, entry in sorted(ledger.by_stage.items(), key=lambda kv: -kv[1].total_tokens):
        lines.append(
            f"    {stage:<16}{entry.calls:>4} calls{entry.input_tokens:>10,} in{entry.output_tokens:>9,} out"
        )
    return lines
