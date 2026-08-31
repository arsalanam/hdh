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

#: The open ledger, if any. A ContextVar rather than a module global so
#: concurrent runs cannot pour their tokens into each other's totals.
_ledger: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("careplan_usage_ledger", default=None)


@dataclass
class Ledger:
    """Tokens and calls, accumulated over some stretch of work."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: Per-stage totals, so "where did the tokens go" is answerable. The
    #: interventions node fans out furthest, and a reader should be able to
    #: see that rather than infer it.
    by_stage: dict[str, Ledger] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, stage: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if stage:
            entry = self.by_stage.setdefault(stage, Ledger())
            entry.calls += 1
            entry.input_tokens += input_tokens
            entry.output_tokens += output_tokens

    def as_dict(self) -> dict:
        """The ledger as JSON, for the baseline to carry.

        Stages are sorted so two baselines differ only where the numbers
        differ — an unstable key order would show as a diff on every run.
        """
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "by_stage": {
                stage: {
                    "calls": entry.calls,
                    "input_tokens": entry.input_tokens,
                    "output_tokens": entry.output_tokens,
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
    )


def summarise(ledger: Ledger, label: str = "") -> list[str]:
    """The ledger as lines, with the stages that dominate visible."""
    if not ledger.calls:
        return [f"{label}no model calls were made" if label else "no model calls were made"]

    head = f"{label}{ledger.calls} calls, "
    head += f"{ledger.input_tokens:,} in / {ledger.output_tokens:,} out"
    head += f" ({ledger.total_tokens:,} total)"
    lines = [head]
    for stage, entry in sorted(ledger.by_stage.items(), key=lambda kv: -kv[1].total_tokens):
        lines.append(
            f"    {stage:<16}{entry.calls:>4} calls{entry.input_tokens:>10,} in{entry.output_tokens:>9,} out"
        )
    return lines
