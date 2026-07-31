# Agent guide — the AI care assistant

A Claude-powered agent with tools over the dataset: it looks up charts,
searches cohorts, pulls care gaps and risk scores, and runs read-only SQL,
then answers with the evidence it gathered.

## Setup

```bash
uv sync --all-extras             # or: pip install -e ".[agent]"
```

**Configuring the API key** — pick one:

| Method | How | Scope |
|---|---|---|
| Project `.env` file (recommended) | `cp .env.example .env`, fill in `ANTHROPIC_API_KEY=sk-...` | Everything run through `just` (dotenv-load); for direct runs: `uv run --env-file .env hdh agent` |
| Machine-wide (Windows) | `setx ANTHROPIC_API_KEY "sk-..."` then open a new terminal | Every process, permanently |
| Current shell only | PowerShell: `$env:ANTHROPIC_API_KEY="sk-..."` · bash: `export ANTHROPIC_API_KEY=sk-...` | Until the terminal closes |
| OAuth profile | `ant auth login` — the SDK picks up the stored profile; no key variable needed | Per machine |
| Docker | `docker run -e ANTHROPIC_API_KEY hdh:latest agent "..."` (forwards from host) or `--env-file .env` | Per container |

`just check-env` reports whether the key is visible (without printing it).
The `.env` file is gitignored — never commit a real key.

Default model is `claude-opus-5`; override with `--model` or the
`HDH_AGENT_MODEL` environment variable (also settable in `.env`).

## Two engines

| | Pipeline (default, one-shot) | Simple loop (`--simple`, and the chat UI) |
|---|---|---|
| Architecture | LangGraph state machine with explicit stages | Plain SDK tool-runner loop |
| Guardrails | Topic guard + daily token quota before any work | none |
| Validation | Response validated against tool evidence **before** it is streamed; hallucinations trigger a retry | none |
| Retries | Executor re-runs with the validator's feedback, max `--max-tries` (3) | n/a |
| Cost | Higher (guard + intent + assembler + validator calls) | Lower |

## The pipeline (default for one-shot questions)

```bash
hdh agent "Who are the highest-risk patients with uncontrolled diabetes?"
# ┌─ pipeline · model claude-sonnet-4-6 · guard claude-haiku-4-5
#   ├─ gateway        quota today: 246,159 input / 85,389 output tokens left
#   ├─ guardrails     topic allowed ✓ (diabetes cohort query)
#   ├─ intent         risk · entities: E11.9, uncontrolled diabetes, HbA1c
#   ├─ tool-executor  attempt 1/3 · 5 tool call(s) recorded
#   ├─ assembler      drafted 137-word response
#   ├─ validator      INVALID — claims not supported by evidence → retrying executor
#   ├─ tool-executor  attempt 2/3 · addressing: claims not supported ...
#   ├─ validator      VALID ✓ — response is grounded in tool evidence
#   └─ streaming validated response
```

Stages (see `src/hdh/modules/agent/pipeline/`):

1. **Gateway** — the composition root: wires the client, tools, quota store,
   and graph; single entry point (`Gateway.ask`).
2. **Guardrails** — daily input/output token quota (persisted in
   `~/.hdh/quota.json`; limits via `HDH_QUOTA_INPUT_TOKENS` /
   `HDH_QUOTA_OUTPUT_TOKENS`), then a topic guard on a small model
   (`claude-haiku-4-5`, override with `HDH_GUARD_MODEL`) that keeps the agent
   on its preconfigured clinical topics.
3. **Intent analysis** — classifies the ask and sketches a tool plan
   (schema-enforced JSON).
4. **Tool executor** — the heart: has the conversation context, the DB
   schema, and every tool; on a retry it receives the validator's feedback
   about exactly what failed.
5. **Response assembler** — drafts the answer strictly from tool evidence.
6. **Response validator** — schema-enforced verdict checking every MRN,
   value, and count against the evidence. Invalid → back to the executor
   (max 3 attempts); after the cap the draft is delivered clearly flagged
   as unvalidated. Only a validated response is streamed.

Every dependency is injected (`PipelineDeps`), so the full graph — including
the retry loop — runs offline in `tests/test_pipeline.py` with fake LLMs.

## Traceability (`hdh trace`)

Every pipeline execution is recorded in a trace database (`runs` → `turns` →
`steps`, see `pipeline/tracing.py`):

- **run id** — a new one every time a gateway session starts (`hdh agent`
  one-shot = a 1-turn run; `hdh agent --pipeline` = a multi-turn run).
- **turn id** — one per question, with status
  (validated/unvalidated/rejected/error), attempts, answer, and total tokens.
- **steps** — one row per component execution (guardrails, intent,
  tool-executor, assembler, validator) with its structured **input/output
  JSON blobs**, input/output **tokens**, duration, attempt number, and
  status. Retries appear as repeated executor→assembler→validator cycles.

**Daily quota is computed from these tables** — usage accounting and
observability share one source of truth (`TraceStore.daily_usage`).

```bash
hdh trace runs                 # recent runs: turns, tokens, model
hdh trace show e912fc78        # one run: every turn and step with timings/tokens
hdh trace show e912fc78 --json # full structured payloads (the stored blobs)
hdh trace usage --days 7       # daily token totals
```

The store defaults to SQLite at `~/.hdh/traces.db` — right-sized for a local
single user. Because it is plain SQLAlchemy, `HDH_TRACE_DB` accepts any
database URL (`postgresql://...` for concurrent multi-user setups) with no
code changes; that is the intended migration path if SQLite ever becomes the
bottleneck.

## Simple one-shot

```bash
hdh agent --simple "Which patients need outreach?"
#   🔧 get_care_gaps(limit=25)
```

Use `--quiet` to hide the trace in either engine.

## Interactive chat

```bash
hdh agent
```

Opens a chat UI where the conversation is remembered across questions —
follow-ups like *"and which of those are seniors?"* work naturally. Answers
render as markdown; tool calls trace live; previous questions are recallable
with the arrow keys (history persists across sessions in `~/.hdh/`).

| Command | Effect |
|---|---|
| `/history` | Replay the whole conversation (you/agent panels, tool trace, summaries) |
| `/context` | Context size: message count, **API-measured token count**, compaction log |
| `/compact` | Summarize older turns into a briefing right now |
| `/save [file]` | Export the transcript as Markdown (default `chat_transcript.md`) |
| `/clear` | Start a fresh conversation |
| `/exit` | Quit (Ctrl-D works too) |

## The agent's tools

| Tool | What it returns |
|---|---|
| `get_patient_chart(mrn)` | The full plain-text chart |
| `search_patients(name, min_age, max_age, icd10_prefix, limit)` | Matching patients with their chronic conditions |
| `get_care_gaps(mrn, limit)` | Care gaps from the caregaps module, ranked by severity |
| `get_risk_scores(mrn, top)` | Risk-model output (needs `hdh risk train` first) |
| `query_database(sql)` | A single read-only SELECT (max 200 rows) |
| `dataset_stats()` | Patient/visit/dx/rx/lab counts |

## Context management (the 100-message problem)

Long conversations accumulate context — especially here, where every tool
round-trip (a full patient chart can be thousands of tokens) lives in the
history. The chat session bounds this automatically:

1. When the history exceeds **`--compact-after`** messages (default **100**),
   the older portion is rendered to a transcript and summarized by the model
   into a `<conversation_summary>` briefing that preserves MRNs, clinical
   findings, care gaps, risk scores, decisions, and open follow-ups.
2. That summary replaces the old turns as a single message; the most recent
   **`--keep-recent`** messages (default **20**) stay verbatim.
3. The cut point is always a plain user turn, so a tool result is never
   orphaned from its tool call (which the API would reject).

A yellow panel announces each compaction with before/after counts and the
summary text; `/context` shows the running log.

**Demonstrate it quickly** (no need for 100 real messages):

```bash
hdh agent --compact-after 8      # compaction triggers after ~4 exchanges
```

Ask a few questions, watch the compaction panel appear, then `/history` — the
old turns are now one summary block — and `/context` to see the token count
drop. The compaction pipeline is also exercised offline in
`tests/test_agent_chat.py`, which collapses a fabricated 120-message
conversation to 21.

## Python API

```python
from hdh.modules.agent.chat import ChatSession

chat = ChatSession(db_session=session, max_messages=100, keep_recent=20)
answer, compaction = chat.ask("Who needs outreach this week?")
answer, _ = chat.ask("Draft a plan for the first patient.")   # remembers context
chat.token_count()      # API-measured input tokens of the current context
chat.to_markdown()      # exportable transcript
```

`ChatSession(summarizer=callable)` injects a custom summarizer — used by the
offline tests, and useful if you want a cheaper model to do the summarizing.

## Safety and cost notes

- The agent runs with server-side refusal fallbacks enabled: if the primary
  model's safety system declines a request, it is retried on Anthropic's
  recommended fallback model automatically.
- `query_database` accepts a single SELECT only; the data is synthetic, but
  the guard keeps the agent from mutating your generated dataset.
- Every question is a metered API call; tool-heavy questions cost more. Use
  `/context` to keep an eye on context size — compaction also reduces spend.
