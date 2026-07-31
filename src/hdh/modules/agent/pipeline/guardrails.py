"""Input guardrails: daily token quota (topic checking lives in the guard LLM).

The quota store persists per-day input/output token counts to a small JSON
file and refuses further requests once either daily limit is reached — a
simple, inspectable cost-control tier in front of the agent.
"""

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class QuotaStore:
    """Per-day token accounting with hard daily limits."""

    path: Path
    daily_input_tokens: int
    daily_output_tokens: int

    def _load(self) -> dict:
        """Read today's counters; a new day resets them."""
        today = date.today().isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("date") != today:
            data = {"date": today, "input_tokens": 0, "output_tokens": 0}
        return data

    def check(self) -> str | None:
        """Return a rejection reason if either daily limit is exhausted."""
        data = self._load()
        if data["input_tokens"] >= self.daily_input_tokens:
            return (
                f"daily input-token quota exhausted "
                f"({data['input_tokens']:,}/{self.daily_input_tokens:,}) — resets tomorrow"
            )
        if data["output_tokens"] >= self.daily_output_tokens:
            return (
                f"daily output-token quota exhausted "
                f"({data['output_tokens']:,}/{self.daily_output_tokens:,}) — resets tomorrow"
            )
        return None

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Add a request's usage to today's counters."""
        data = self._load()
        data["input_tokens"] += input_tokens
        data["output_tokens"] += output_tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def remaining(self) -> tuple[int, int]:
        """(input, output) tokens left today."""
        data = self._load()
        return (
            max(0, self.daily_input_tokens - data["input_tokens"]),
            max(0, self.daily_output_tokens - data["output_tokens"]),
        )
