# Agentic UI module — design (draft)

Status: draft · Supersedes the *stack* and *auth* sections of issue #88 for
the concrete module, and defers to #88 for the product vision (one agent,
three front doors; the typed chart spec; type/talk/upload).

## 1. What this is

A browser UI for talking to the hdh agent: ask a question, watch it work,
read a grounded answer, and scroll back through the conversation. It is the
**React front door** of #88 — the third way into the *same* agent the CLI and
(future) MCP server reach.

The one rule it inherits, unchanged (design §7, restated by #88): **a front
door may present differently; it may not decide differently.** Every request
goes through the full pipeline — topic gate, quota, intent, executor,
assembler, response validator — by calling the seam the CLI already calls.

## 2. The seam — reuse, don't reimplement

`Gateway.ask(question) -> {answer, intent, verdict, usage, trace_id}` is the
whole contract. The CLI builds a `Gateway` and calls `ask`; the API builds the
*same* `Gateway` and calls the *same* `ask`. There is no second agent, no
HTTP-only logic, no forked toolset.

"Piggyback on the CLI" was the instinct, and the spirit is right — but the CLI
is a stdio process gated on a local login, so the literal path is wrong. The
honest reading is **reuse the core seam**, not the CLI process. The API imports
`Gateway`, `build_tools(identity=, include=)`, `core.identity`, and the
`TraceStore` — the CLI's own dependencies — and wraps them in HTTP.

## 3. Architecture

```
 browser ──HTTP/SSE──►  FastAPI: hdh.modules.agent_api  (standalone)
   React SPA              │  /ask  (SSE)      → Gateway.ask(), instrumented
   (Vite build,           │  /conversations   → TraceStore
    served as static)     │  /conversations/{id}
                          │  /me              → identity (later)
                          │  (later) /files   → Tus → OCR/vision → comprehension
                          └─ serves the built SPA as static files (same runtime)
```

- **Standalone app** (`hdh/modules/agent_api`), beside the FHIR facade, not
  bolted onto it — a stateful agent surface and a read-only FHIR facade are
  different concerns with different lifecycles. `hdh serve` can mount either
  or both.
- **One runtime.** The React app is built to static assets and served by the
  same FastAPI process (`StaticFiles`). No Node server in production; the UI
  and API share an origin, which also sidesteps CORS.
- **Plain React + Vite, not Next.js.** There is no SSR/routing/server-actions
  need here that would pay for Next.js; a single-page React app built by Vite
  into static files is smaller to own and matches "served by the same runtime".

## 4. Endpoints (v1)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ask` | Ask a question. **SSE** stream: friendly stage events, then the validated answer + verdict + usage + trace_id. Body: `{question, conversation_id?}`. |
| `GET` | `/conversations` | The caller's past conversations (runs), newest first. |
| `GET` | `/conversations/{id}` | One conversation's transcript (turns + per-turn verdict/usage). |
| `GET` | `/health` | Liveness + model/config echo. |
| `GET` | `/` (+ assets) | The built React SPA. |

A **conversation is a `TraceStore` run**; a **turn** is one question/answer.
The store already persists runs → turns → steps with token usage, so history
is a read away — no new store.

## 5. Streaming

The pipeline **validates before it emits** (the CLI streams the *validated*
answer, never a live draft). The UI keeps that guarantee and turns it into a
feature: it streams **what the agent is doing**, not ungrounded tokens.

`POST /ask` is an SSE stream built on the existing `instrument_deps` hooks,
which already fire once per stage. Each stage maps to a human label:

```
event: stage   data: {"label": "Thinking…",            "stage": "intent"}
event: stage   data: {"label": "Fetching data…",       "stage": "executor", "tool": "query_database"}
event: stage   data: {"label": "Assembling answer…",   "stage": "assembler"}
event: stage   data: {"label": "Checking it…",         "stage": "validator"}
event: answer  data: {"text": "...", "verdict": {...}, "usage": {...}, "trace_id": "ab12cd34"}
```

**Nice-to-have, not v1:** token-by-token of the *final* answer. It is additive
— once the verdict is in, the already-validated text can be chunked into
`event: token` frames before the closing `answer` event. Deferred.

## 6. Conversation history

`TraceStore.recent_runs` / `run_detail` back the sidebar and transcript. Two
small additions:

- **Scope runs to the caller.** `start_run` gains an optional owner (the
  signed-in subject, or a config role while auth is deferred) so
  `/conversations` lists *your* conversations, not everyone's.
- **A resumable `conversation_id`.** `/ask` accepts one to continue a run
  (multi-turn), or starts a new run when absent.

## 7. Identity & login — deferred, and realm-separated

v1 ships **without a login screen**, role from configuration (reusing the
`include` parameter `build_tools` already takes to shape the toolset). This is
#88's stance and it keeps v1 honest: a half-built login is worse than none.

When login lands (after the UI is finalized), it is **real OIDC against the
existing Keycloak**, with one hard constraint from the owner:

> **Provider and patient realms must never mix.**

This UI is a **provider/clinician** surface, so it binds to the **provider
realm only** — the API verifies access tokens against that realm's issuer and
JWKS and refuses tokens from any other. A future patient portal is a separate
front door bound to a **separate patient realm**; the two never share a realm,
a token audience, or a session. The identity seam already threads an
`Identity` into `Gateway`/`build_tools`, so adding auth is: verify the token →
build the `Identity` → pass it in. No pipeline change.

(One backend note for that phase: `core.identity` decodes token claims
*without* signature verification, which is safe for the CLI's same-trust
resource-owner flow but **not** across the browser trust boundary. The API
must verify signatures via the provider realm's JWKS — a new, small
verification step, not a change to the identity model.)

## 8. File upload — later (Tus), and the boundary that guards it

Tus resumable upload to a `/files` endpoint, then the honest part #88 names: a
**PDF or image is not text yet.** An OCR/vision pre-pass must produce text (or
structured values) *before* the pipeline sees it, and it meets the same gate —
an ambiguous scanned value reaches the **review queue**, never a confident
guess.

And the boundary that must be settled before an upload button exists (#88,
§10.0): an imported **lab report carrying real results is not a note asserting
them**. If it has results it arrives through `interchange` and matches an
order; only narrative documents go through comprehension. The upload flow
routes by that distinction rather than treating every file as a note.

## 9. The typed chart spec — later (#88)

#88's "a chart is a structured answer": the agent returns a typed chart
specification (title, type, series, axes, and the rows behind it) the UI
renders and a reader can inspect. Constraints carried from #88: it is **not an
agent-only decision** (`hdh agent --chart` emits the same JSON, renderable in
the terminal), it is **grounded** (numbers from `query_database`, rows carried
so the validator can check them), and it **says what it counted**. Scoped after
the chat/history/streaming core works.

## 10. Phased plan

| Phase | Delivers | Proves |
|---|---|---|
| **1 · Backend skeleton** | standalone `agent_api`; `POST /ask` (non-streaming) returning `Gateway.ask()`; `/health`; role-from-config tool shaping; tests | one agent over HTTP, same decisions as the CLI |
| **2 · Streaming** | `/ask` as SSE with friendly stage labels via `instrument_deps` | the UI feels alive; grounding guarantee intact |
| **3 · History** | per-caller run scoping; `/conversations` + `/conversations/{id}`; resumable `conversation_id` | scroll back through prior conversations |
| **4 · The React SPA** | Vite React app — ask box, streamed stages, answer + verdict view, history sidebar — built and served by FastAPI | the front door a clinician clicks |
| **5 · Login** | Keycloak OIDC (provider realm only, JWKS-verified); identity threaded; realm separation enforced | who is asking, without mixing realms |
| **6 · Upload** | Tus `/files`; OCR/vision pre-pass with verdicts; interchange-vs-note routing | type, talk, *or* upload — one gate |
| **7 · Charts** | typed chart spec, `--chart` parity, UI renderer | a dashboard is a grounded, inspectable answer |

Each phase is independently mergeable; 1–4 are the usable core.

## 11. Out of scope / non-negotiables

- No second agent and no HTTP-only decision — the seam is `Gateway.ask()`.
- No ungrounded streaming — stages stream live; answer text streams only once
  validated.
- No realm mixing — provider and patient identities are separate realms,
  forever.
- Auth, multi-tenancy and consent beyond the above remain the
  HITRUST/HIPAA workstream #88 points at, not this module.
