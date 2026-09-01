"""Traceability for the agent pipeline: runs → turns → steps.

Every gateway session opens a *run* (new run id); every question in that
session is a *turn*; and every component execution — guardrails, intent,
tool executor, assembler, validator — is recorded as a *step* with its
structured input/output JSON, token usage, duration, and status. Daily token
quota is computed from these tables, so usage accounting and observability
share one source of truth.

The trace database is separate from the clinical database (its own
DeclarativeBase and engine). It defaults to SQLite at ``~/.hdh/traces.db`` —
fine for a single local user — and because everything goes through
SQLAlchemy, pointing ``HDH_TRACE_DB`` at e.g. ``postgresql://...`` switches
the backend without code changes.

These models use SQLAlchemy 2.0 ``Mapped[]`` typing — the modern style the
core models will eventually migrate to.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from time import perf_counter

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .state import PipelineDeps

MAX_BLOB_CHARS = 60_000  # cap any single stored JSON payload


class TraceBase(DeclarativeBase):
    """Declarative base for trace tables (separate from the clinical schema)."""


class Run(TraceBase):
    """One gateway session: a new run id every time a session starts."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    source: Mapped[str] = mapped_column(String(40), default="pipeline")
    model: Mapped[str] = mapped_column(String(80), default="")
    guard_model: Mapped[str] = mapped_column(String(80), default="")
    max_attempts: Mapped[int] = mapped_column(default=3)

    turns: Mapped[list[Turn]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Turn(TraceBase):
    """One question/answer cycle within a run (multi-turn sessions have many)."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(default=0)
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    # validated | unvalidated | rejected | error | running
    attempts: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    final_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped[Run] = relationship(back_populates="turns")
    steps: Mapped[list[Step]] = relationship(back_populates="turn", cascade="all, delete-orphan")


class Step(TraceBase):
    """One component execution inside a turn, with structured input/output."""

    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    turn_id: Mapped[int] = mapped_column(ForeignKey("turns.id"), index=True)
    seq: Mapped[int] = mapped_column(default=0)
    stage: Mapped[str] = mapped_column(String(30))
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | rejected | invalid
    input_payload: Mapped[dict | None] = mapped_column("input", JSON, nullable=True)
    output_payload: Mapped[dict | None] = mapped_column("output", JSON, nullable=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    turn: Mapped[Turn] = relationship(back_populates="steps")


@dataclass(frozen=True)
class StepRecord:
    """One component execution, ready to persist (immutable)."""

    turn_id: int
    seq: int
    stage: str
    attempt: int
    status: str
    input_payload: dict | None
    output_payload: dict | None
    input_tokens: int
    output_tokens: int
    duration_ms: int


def _clip(payload: dict | None) -> dict | None:
    """Bound a JSON payload's size before storage."""
    if payload is None:
        return None
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_BLOB_CHARS:
        return payload
    return {"_truncated": True, "_chars": len(text), "preview": text[:MAX_BLOB_CHARS]}


class TraceStore:
    """Persistence facade for runs, turns, steps, and usage accounting."""

    def __init__(self, url: str):
        """Open (and create, if needed) the trace database at ``url``."""
        self.engine = create_engine(url)
        TraceBase.metadata.create_all(self.engine)

    # ── Writing ──────────────────────────────────────────────────────────────

    def start_run(self, source: str, model: str, guard_model: str, max_attempts: int) -> str:
        """Register a new session; returns its run id."""
        run_id = str(uuid.uuid4())
        with Session(self.engine) as s:
            s.add(
                Run(id=run_id, source=source, model=model, guard_model=guard_model, max_attempts=max_attempts)
            )
            s.commit()
        return run_id

    def start_turn(self, run_id: str, turn_index: int, question: str) -> int:
        """Open a turn for a question; returns its turn id."""
        with Session(self.engine) as s:
            turn = Turn(run_id=run_id, turn_index=turn_index, question=question)
            s.add(turn)
            s.commit()
            return turn.id

    def record_step(self, record: StepRecord) -> None:
        """Persist one component execution."""
        with Session(self.engine) as s:
            s.add(
                Step(
                    turn_id=record.turn_id,
                    seq=record.seq,
                    stage=record.stage,
                    attempt=record.attempt,
                    status=record.status,
                    input_payload=_clip(record.input_payload),
                    output_payload=_clip(record.output_payload),
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    duration_ms=record.duration_ms,
                )
            )
            s.commit()

    def end_turn(
        self,
        turn_id: int,
        status: str,
        answer: str,
        attempts: int,
        usage: dict,
        final_state: dict | None = None,
    ) -> None:
        """Close a turn with its outcome and total token usage."""
        with Session(self.engine) as s:
            turn = s.get(Turn, turn_id)
            if turn is None:
                return
            turn.status = status
            turn.answer = answer[:MAX_BLOB_CHARS]
            turn.attempts = attempts
            turn.input_tokens = usage.get("input_tokens", 0)
            turn.output_tokens = usage.get("output_tokens", 0)
            turn.ended_at = datetime.now()
            turn.final_state = _clip(final_state)
            s.commit()

    # ── Usage accounting (quota derives from the trace, one source of truth) ─

    def daily_usage(self, day: date | None = None) -> tuple[int, int]:
        """(input, output) tokens recorded across all steps on ``day``."""
        day = day or date.today()
        start, end = datetime.combine(day, time.min), datetime.combine(day, time.max)
        with Session(self.engine) as s:
            row = s.execute(
                select(
                    func.coalesce(func.sum(Step.input_tokens), 0),
                    func.coalesce(func.sum(Step.output_tokens), 0),
                ).where(Step.created_at.between(start, end))
            ).one()
            return int(row[0]), int(row[1])

    def check_quota(self, daily_input_tokens: int, daily_output_tokens: int) -> str | None:
        """Rejection reason if today's recorded usage exceeds either limit."""
        used_in, used_out = self.daily_usage()
        if used_in >= daily_input_tokens:
            return f"daily input-token quota exhausted ({used_in:,}/{daily_input_tokens:,})"
        if used_out >= daily_output_tokens:
            return f"daily output-token quota exhausted ({used_out:,}/{daily_output_tokens:,})"
        return None

    # ── Reading (the `hdh trace` viewer) ─────────────────────────────────────

    def recent_runs(self, limit: int = 15) -> list[dict]:
        """Newest runs with turn counts and token totals."""
        with Session(self.engine) as s:
            runs = s.execute(select(Run).order_by(Run.started_at.desc()).limit(limit)).scalars()
            out = []
            for run in runs:
                out.append(
                    {
                        "run_id": run.id,
                        "started_at": run.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "source": run.source,
                        "model": run.model,
                        "turns": len(run.turns),
                        "input_tokens": sum(t.input_tokens for t in run.turns),
                        "output_tokens": sum(t.output_tokens for t in run.turns),
                    }
                )
            return out

    def run_detail(self, run_prefix: str) -> dict | None:
        """Full run → turns → steps tree for a run id (prefix match)."""
        with Session(self.engine) as s:
            run = (
                s.execute(select(Run).where(Run.id.like(f"{run_prefix}%")).order_by(Run.started_at.desc()))
                .scalars()
                .first()
            )
            if run is None:
                return None
            return {
                "run_id": run.id,
                "started_at": run.started_at.isoformat(),
                "source": run.source,
                "model": run.model,
                "guard_model": run.guard_model,
                "turns": [
                    {
                        "turn_id": t.id,
                        "turn_index": t.turn_index,
                        "question": t.question,
                        "status": t.status,
                        "attempts": t.attempts,
                        "input_tokens": t.input_tokens,
                        "output_tokens": t.output_tokens,
                        "answer": t.answer,
                        "steps": [
                            {
                                "seq": st.seq,
                                "stage": st.stage,
                                "attempt": st.attempt,
                                "status": st.status,
                                "input": st.input_payload,
                                "output": st.output_payload,
                                "input_tokens": st.input_tokens,
                                "output_tokens": st.output_tokens,
                                "duration_ms": st.duration_ms,
                            }
                            for st in sorted(t.steps, key=lambda x: x.seq)
                        ],
                    }
                    for t in sorted(run.turns, key=lambda x: x.turn_index)
                ],
            }

    def usage_by_day(self, days: int = 7) -> list[dict]:
        """Recent daily token totals (for `hdh trace usage`)."""
        with Session(self.engine) as s:
            rows = s.execute(
                select(
                    func.date(Step.created_at),
                    func.sum(Step.input_tokens),
                    func.sum(Step.output_tokens),
                    func.count(Step.id),
                )
                .group_by(func.date(Step.created_at))
                .order_by(func.date(Step.created_at).desc())
                .limit(days)
            ).all()
            return [
                {
                    "day": str(r[0]),
                    "input_tokens": int(r[1] or 0),
                    "output_tokens": int(r[2] or 0),
                    "steps": int(r[3]),
                }
                for r in rows
            ]


@dataclass
class TurnContext:
    """Mutable per-turn cursor shared by the instrumented dependencies."""

    turn_id: int | None = None
    seq: int = field(default=0)
    attempt: int = field(default=0)

    def next_seq(self) -> int:
        """Advance and return the step sequence number."""
        self.seq += 1
        return self.seq


def instrument_deps(deps: PipelineDeps, store: TraceStore, ctx: TurnContext) -> PipelineDeps:
    """Wrap every pipeline dependency so each execution is recorded as a Step.

    Pure decoration: node code and the dependency contracts are unchanged —
    this is the same injection seam the tests use for fakes.
    """

    def record(stage, status, input_payload, output_payload, usage, t0):
        store.record_step(
            StepRecord(
                turn_id=ctx.turn_id or 0,
                seq=ctx.next_seq(),
                stage=stage,
                attempt=max(ctx.attempt, 1),
                status=status,
                input_payload=input_payload,
                output_payload=output_payload,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                duration_ms=int((perf_counter() - t0) * 1000),
            )
        )

    def check_topic(question, history=None):
        t0 = perf_counter()
        allowed, label, usage = deps.check_topic(question, history)
        record(
            "guardrails",
            "ok" if allowed else "rejected",
            {"question": question},
            {"allowed": allowed, "label": label},
            usage,
            t0,
        )
        return allowed, label, usage

    def quota_check():
        t0 = perf_counter()
        reason = deps.quota_check()
        if reason:  # only rejections are worth a step record; passes are free
            record("guardrails-quota", "rejected", None, {"reason": reason}, {}, t0)
        return reason

    def analyze_intent(question, history):
        t0 = perf_counter()
        result, usage = deps.analyze_intent(question, history)
        record("intent", "ok", {"question": question}, result, usage, t0)
        return result, usage

    def run_tools(question, intent, feedback, history):
        ctx.attempt += 1
        t0 = perf_counter()
        findings, evidence, usage = deps.run_tools(question, intent, feedback, history)
        record(
            "tool-executor",
            "ok",
            {"question": question, "intent": intent, "feedback": feedback},
            {"findings": findings, "evidence": evidence},
            usage,
            t0,
        )
        return findings, evidence, usage

    def assemble(question, findings, evidence):
        t0 = perf_counter()
        draft, usage = deps.assemble(question, findings, evidence)
        record(
            "assembler",
            "ok",
            {"question": question, "evidence_items": len(evidence)},
            {"draft": draft},
            usage,
            t0,
        )
        return draft, usage

    def validate(question, draft, evidence):
        t0 = perf_counter()
        valid, reason, usage = deps.validate(question, draft, evidence)
        record(
            "validator",
            "ok" if valid else "invalid",
            {"draft_chars": len(draft), "evidence_items": len(evidence)},
            {"valid": valid, "reason": reason},
            usage,
            t0,
        )
        return valid, reason, usage

    return PipelineDeps(
        config=deps.config,
        check_topic=check_topic,
        analyze_intent=analyze_intent,
        run_tools=run_tools,
        assemble=assemble,
        validate=validate,
        quota_check=quota_check,
        trace=deps.trace,
    )
