"""Chart amendment with an append-only audit trail (issue #40).

Core, not a module (design chart-maintenance.md §7 Q6): changing a chart
row is core behavior, so the contracts and the audit table live here and
every surface — CLI, agent tools, comprehension's review resolution — is
a thin client of :func:`apply_edits`.

The rules that make an agent-maintained chart credible:

- **one sanctioned path** — nothing mutates a chart row except through
  this API, so the trail can never be incomplete;
- **void, never delete** — clinical rows are marked entered-in-error and
  stop being visible, so the audit event keeps its referent (real
  deletion is :func:`purge_visit`, an admin operation);
- **a reason** is mandatory for clinical entities;
- **refusals are outcomes**, not exceptions — the CLI and the agent
  report the same words.
"""

from hdh.core.chartedit.api import apply_edits, history, purge_visit, record_creation
from hdh.core.chartedit.contracts import Actor, ChartEdit, EditAction, EditOutcome
from hdh.core.chartedit.entities import REGISTRY, spec_for

__all__ = [
    "REGISTRY",
    "Actor",
    "ChartEdit",
    "EditAction",
    "EditOutcome",
    "apply_edits",
    "history",
    "purge_visit",
    "record_creation",
    "spec_for",
]
