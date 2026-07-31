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

## One-shot questions

```bash
hdh agent "Which patients with uncontrolled hypertension also have a high risk score?"
#   🔧 get_care_gaps(limit=25)
#   🔧 get_risk_scores(top=20)
# Three patients overlap: MRN51248682 (Jonathan Ward, 41), ...
```

Use `--quiet` to hide the tool trace.

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
