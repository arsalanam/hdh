"""The agent's HTTP front door (design docs/design/agentic-ui-module.md).

A thin, standalone FastAPI surface over ``Gateway.ask()`` — the *same* seam
the CLI drives. It adds no rules of its own: every request runs the full
pipeline (topic gate, quota, intent, executor, assembler, response validator)
because it calls the one agent, not a reimplementation. A front door may
present differently; it may not decide differently (design §7, issue #88).

Phase 1 (this module) is the backend skeleton: ``POST /ask`` and ``/health``.
Streaming, conversation history, login and upload land in later phases.
"""

from hdh.modules.agent_api.server import create_app

__all__ = ["create_app"]
