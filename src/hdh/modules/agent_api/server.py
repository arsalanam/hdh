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

import json
from collections.abc import Callable, Iterator
from typing import Any

from pydantic import BaseModel, Field

AskFn = Callable[[str], dict]
#: A streaming backend: yields ("stage"|"answer"|"error", payload) events.
StreamFn = Callable[[str], Iterator[tuple[str, dict]]]

#: Internal pipeline stage → the phrase a person reads while they wait. Anything
#: not here (e.g. "gateway" bookkeeping) passes through title-cased.
_STAGE_LABELS = {
    "guardrails": "Checking the question…",
    "intent": "Thinking…",
    "tool-executor": "Fetching data…",
    "assembler": "Assembling the answer…",
    "validator": "Checking the answer…",
}


def _label(stage: str) -> str:
    """The human-facing label for a pipeline stage."""
    return _STAGE_LABELS.get(stage, stage.replace("-", " ").replace("_", " ").capitalize())


def _sse(event: str, data: dict) -> str:
    """One Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


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


class ConversationSummary(BaseModel):
    """One past conversation, for the history sidebar."""

    conversation_id: str
    started_at: str
    title: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0


class TranscriptTurn(BaseModel):
    """One question/answer in a conversation transcript."""

    turn_index: int
    question: str
    answer: str | None = None
    status: str
    input_tokens: int = 0
    output_tokens: int = 0


class Conversation(BaseModel):
    """A conversation's full transcript."""

    conversation_id: str
    started_at: str
    model: str | None = None
    turns: list[TranscriptTurn] = Field(default_factory=list)


#: The trace-store ``source`` the API tags its runs with — what distinguishes a
#: UI conversation from a CLI or eval run, and what the history endpoints show.
UI_SOURCE = "ui"


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


def _gateway_stream(*, db_path: str, model: str | None) -> StreamFn:
    """The default streaming backend: run the gateway on a worker thread and
    relay its stage callbacks live, then the validated answer.

    The pipeline is synchronous (``graph.invoke``), so the work runs on a
    thread whose ``trace`` callback pushes stage events onto a queue; this
    generator drains the queue as they arrive and yields the final answer once
    the thread finishes. Stages stream live; the *answer* is only emitted after
    the response validator has passed — the grounding guarantee, unchanged.
    """

    def stream(question: str) -> Iterator[tuple[str, dict]]:
        import queue
        import threading

        events: queue.Queue = queue.Queue()
        done = object()
        outcome: dict[str, dict] = {}

        def trace(stage: str, message: str) -> None:
            events.put(("stage", {"stage": stage, "label": _label(stage), "detail": message}))

        def work() -> None:
            from hdh.core.models import get_engine, get_session
            from hdh.modules.agent.pipeline import Gateway

            # request-scoped session, on this worker thread; see _gateway_ask.
            session = get_session(get_engine(db_path))  # quality: allow(dependency-injection)
            try:
                gateway = Gateway(session, model=model, source="ui", identity=None, trace=trace)
                outcome["answer"] = _normalize(gateway.ask(question), gateway.run_id)
            except Exception as error:  # noqa: BLE001 - surfaced to the client as an error event
                outcome["error"] = {"detail": f"{type(error).__name__}: {error}"}
            finally:
                session.close()
                events.put(done)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while True:
            item = events.get()
            if item is done:
                break
            yield item
        worker.join()
        yield ("error", outcome["error"]) if "error" in outcome else ("answer", outcome["answer"])

    return stream


def _default_store():
    """The trace store the CLI writes to — the conversation-history backbone."""
    from hdh.modules.agent.pipeline.gateway import default_trace_url
    from hdh.modules.agent.pipeline.tracing import TraceStore

    return TraceStore(default_trace_url())


def _conversation_summaries(store, limit: int) -> list[dict]:
    """The UI's past conversations, newest first. Over-fetches then filters to
    ``source == 'ui'`` so CLI and eval runs never show in the UI's history."""
    rows = [r for r in store.recent_runs(limit=max(limit * 4, limit)) if r.get("source") == UI_SOURCE]
    return [
        {
            "conversation_id": r["run_id"],
            "started_at": r["started_at"],
            "title": (r.get("title") or "").strip() or "(no question)",
            "turns": r["turns"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
        }
        for r in rows[:limit]
    ]


def _transcript(store, conversation_id: str) -> dict | None:
    """One conversation's transcript, or None if it is not a UI conversation."""
    detail = store.run_detail(conversation_id)
    if detail is None or detail.get("source") != UI_SOURCE:
        return None
    return {
        "conversation_id": detail["run_id"],
        "started_at": detail["started_at"],
        "model": detail.get("model"),
        "turns": [
            {
                "turn_index": t["turn_index"],
                "question": t["question"],
                "answer": t["answer"],
                "status": t["status"],
                "input_tokens": t["input_tokens"],
                "output_tokens": t["output_tokens"],
            }
            for t in detail["turns"]
        ],
    }


def create_app(
    ask: AskFn | None = None,
    *,
    stream: StreamFn | None = None,
    store: Any = None,
    db_path: str = "family_medicine.db",
    model: str | None = None,
):
    """Build the agent HTTP app.

    ``ask`` / ``stream`` override the agent backend (tests inject fakes); by
    default both are real gateways bound to ``db_path`` / ``HDH_DB_URL``.
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse

    run_ask: AskFn = ask or _gateway_ask(db_path=db_path, model=model)
    run_stream: StreamFn = stream or _gateway_stream(db_path=db_path, model=model)
    history = store or _default_store()

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

    @app.post("/ask/stream")
    def ask_stream(body: AskRequest) -> Any:
        """Ask the agent, streamed. Server-Sent Events: ``stage`` frames as the
        agent works (thinking, fetching data, assembling, checking), then one
        ``answer`` frame with the validated result — or an ``error`` frame.

        The answer is emitted only after the response validator passes; the
        stages are the live progress, not ungrounded draft text.
        """
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="question must not be empty")

        def frames() -> Iterator[str]:
            for event, data in run_stream(question):
                yield _sse(event, data)

        return StreamingResponse(frames(), media_type="text/event-stream")

    @app.get("/conversations", response_model=list[ConversationSummary])
    def conversations(limit: int = 20) -> Any:
        """The caller's past conversations, newest first — the history sidebar.

        Scoped to conversations held through this UI (trace-store source
        ``ui``); CLI and eval runs are not shown. Per-*user* scoping arrives
        with login (a conversation is a run, and a run will carry its owner).
        """
        return _conversation_summaries(history, max(1, min(limit, 100)))

    @app.get("/conversations/{conversation_id}", response_model=Conversation)
    def conversation(conversation_id: str) -> Any:
        """One conversation's transcript: each turn's question, answer and status."""
        transcript = _transcript(history, conversation_id)
        if transcript is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        return transcript

    return app
