"""The standalone FastAPI app over ``Gateway.ask()`` (Phase 1).

``create_app`` takes an injectable ``ask`` callable — the composition-root
pattern the pipeline already uses (``PipelineDeps``). The default wraps a real
:class:`~hdh.modules.agent.pipeline.Gateway`; tests pass a fake, so the HTTP
surface is exercised without an Anthropic key or a live model.

The response is exactly what a front door needs, and no more — the answer, the
classified intent, the grounding verdict, token usage, and the trace id the
whole run can be inspected by (``hdh trace show <id>``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

AskFn = Callable[[str], dict]


class AskRequest(BaseModel):
    """One question for the agent."""

    question: str = Field(min_length=1, description="A clinical question for the agent.")


class AskResponse(BaseModel):
    """What a front door needs: the answer and the seam around it."""

    answer: str
    status: str  # validated | unvalidated | rejected
    intent: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


def _status_of(state: dict) -> str:
    """The turn's outcome, in the trace store's own vocabulary."""
    if state.get("rejected"):
        return "rejected"
    if state.get("failed"):
        return "unvalidated"
    return "validated"


def _normalize(state: dict, run_id: str) -> dict:
    """The pipeline state, reduced to the seam a front door consumes."""
    from hdh.modules.agent.pipeline import Gateway

    return {
        "answer": Gateway.answer_of(state),
        "status": _status_of(state),
        "intent": state.get("intent"),
        "verdict": state.get("verdict"),
        "usage": state.get("usage") or {},
        "trace_id": run_id[:8],
    }


def _gateway_ask(*, db_path: str, model: str | None) -> AskFn:
    """The default ``ask``: one traced run per question through the real agent.

    A fresh session and gateway per request keeps the app stateless; multi-turn
    conversations (one run, many turns) arrive with the history phase. The DB
    is resolved the same way the CLI resolves it — ``HDH_DB_URL`` first, then
    the SQLite file — so the API and the terminal see one chart.
    """

    def ask(question: str) -> dict:
        from hdh.core.models import get_engine, get_session
        from hdh.modules.agent.pipeline import Gateway

        # This app IS the composition root for its HTTP surface; the session is
        # request-scoped and cannot be injected from outside (mirrors
        # fhir_api.create_app).
        session = get_session(get_engine(db_path))  # quality: allow(dependency-injection)
        try:
            gateway = Gateway(session, model=model, source="ui", identity=None)
            state = gateway.ask(question)
            return _normalize(state, gateway.run_id)
        finally:
            session.close()

    return ask


def create_app(ask: AskFn | None = None, *, db_path: str = "family_medicine.db", model: str | None = None):
    """Build the agent HTTP app.

    ``ask`` overrides the agent backend (tests inject a fake); by default it is
    a real gateway bound to ``db_path`` / ``HDH_DB_URL``.
    """
    from fastapi import FastAPI, HTTPException

    run_ask: AskFn = ask or _gateway_ask(db_path=db_path, model=model)

    app = FastAPI(
        title="HDH Agent API",
        version="0.1.0",
        summary="The hdh agent, over HTTP — one agent, three front doors (#88).",
    )

    @app.get("/health")
    def health() -> dict:
        """Liveness — cheap, and it names the service so a proxy can label it."""
        return {"status": "ok", "service": "hdh-agent-api", "version": app.version}

    @app.post("/ask", response_model=AskResponse)
    def ask_endpoint(body: AskRequest) -> dict:
        """Ask the agent one question; get a grounded, validated answer.

        Runs the full pipeline — the same one the CLI runs — so the answer is
        topic-gated, grounded in tool evidence, and checked by the response
        validator before it is returned.
        """
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="question must not be empty")
        return run_ask(question)

    return app
