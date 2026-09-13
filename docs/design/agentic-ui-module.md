# Agentic UI module — design

Status: **phases 1–6 shipped** (backend, streaming, history, React SPA, login,
note upload — PRs up to #189). Phase 7 (the typed chart spec) is the only phase
left, and audio capture arrives through phase 6's door via #87 (a local
speech/OCR service). Supersedes the *stack* and *auth* sections of issue #88 for
the concrete module, and defers to #88 for the product vision (one agent, three
front doors; the typed chart spec; type/talk/upload).

To run it, see the [Agent UI guide](../guides/agent-ui.md).

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

## 7. Identity & login — SHIPPED (phase 5, #188), realm-separated

The early phases shipped **without a login screen**, role from configuration —
honest for a skeleton. Phase 5 (#188) replaced that with **real OIDC against
the existing Keycloak**: the SPA (`web/src/auth.ts`, oidc-client-ts, auth-code
+ PKCE against the `hdh-web` client) obtains a provider-realm access token and
sends it as a Bearer on every call; the API (`agent_api/auth.py`) verifies the
signature against the realm's JWKS and checks the issuer, so a token from any
other realm is refused. Every data endpoint is gated (`require_identity`);
`/health` and the SPA itself are open. The one hard constraint from the owner
is enforced in code:

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

## 8. File upload — SHIPPED as simple multipart (phase 6, #189); Tus later

Phase 6 (#189) shipped the **simple multipart** form of this: `POST
/notes/upload` (`agent_api/notes.py`), identity-gated, for one purpose —
**saving the provider from typing**. What a clinician uploads is a **note** — a
handwritten encounter note or a scanned old report — and it is treated as one,
always. A **PDF or image is not text yet**, so `transcribe()` runs a
vision pre-pass (marking anything illegible rather than guessing) *before* the
pipeline sees it; the text then meets the same gate as a typed note through
`comprehend_text` → `comprehend_note` → `apply_to_chart`, so an ambiguous
transcribed value reaches the **review queue**, never a confident guess.

Still deferred: **Tus resumable upload** (6b, for large/flaky uploads) and
**audio** — dictation is rejected today (`NoteError`) and is the #87 speech
front door, which will bring its own transcription via a local speech service
(the same milestone as OCR/vision, run on the owner's GPU).

What an upload is **not**, and the boundary that matters:

- **Not lab results.** Even an old report with numbers on it becomes
  **narrative text in the chart's history** — it does not become
  `LabResult` rows. Structured, LOINC-coded results are a different channel
  entirely: they arrive through `interchange`, matched to an order. A report a
  provider scans in is the provider *telling the chart something*, not a
  result feed asserting values.
- **Not orders.** A treatment or order a provider dictates is applied to the
  chart and goes to Pharmacy for fulfilment through the order path — it is not
  conjured by an upload.

So the upload flow never routes to `interchange` and never writes results or
orders: it is one more **front door onto the note-comprehension path**, never
a new set of rules. The distinction is not "route by file contents" but "a
file a provider uploads is always a note."

## 9. The typed chart spec — later (#88)

#88's "a chart is a structured answer": the agent returns a typed chart
specification (title, type, series, axes, and the rows behind it) the UI
renders and a reader can inspect. Constraints carried from #88: it is **not an
agent-only decision** (`hdh agent --chart` emits the same JSON, renderable in
the terminal), it is **grounded** (numbers from `query_database`, rows carried
so the validator can check them), and it **says what it counted**. Scoped after
the chat/history/streaming core works.

## 10. Phased plan

| Phase | Status | Delivers | Proves |
|---|---|---|---|
| **1 · Backend skeleton** | ✅ shipped | standalone `agent_api`; `POST /ask` (non-streaming) returning `Gateway.ask()`; `/health`; role-from-config tool shaping; tests | one agent over HTTP, same decisions as the CLI |
| **2 · Streaming** | ✅ shipped | `/ask/stream` as SSE with friendly stage labels via `instrument_deps` | the UI feels alive; grounding guarantee intact |
| **3 · History** | ✅ shipped (#187) | thread → run tree (`/threads`), `/conversations` + `/conversations/{id}` | scroll back through prior conversations, grouped by thread |
| **4 · The React SPA** | ✅ shipped | Vite React app — ask box, streamed stages, answer + verdict view, foldable history sidebar — built and served by FastAPI | the front door a clinician clicks |
| **5 · Login** | ✅ shipped (#188) | Keycloak OIDC (provider realm only, JWKS-verified); identity threaded; realm separation enforced | who is asking, without mixing realms |
| **6 · Upload** | ✅ simple multipart (#189); Tus + audio deferred | `POST /notes/upload`; OCR/vision pre-pass with verdicts; every upload is a note → comprehension → chart history (never results or orders) | type *or* upload — one gate, one path |
| **7 · Charts** | ⬜ open | typed chart spec, `--chart` parity, UI renderer | a dashboard is a grounded, inspectable answer |

Each phase is independently mergeable; 1–4 are the usable core, and 1–6 are now
shipped. Phase 7, Tus resumable upload, and audio capture (#87) remain.

## 11. Out of scope / non-negotiables

- No second agent and no HTTP-only decision — the seam is `Gateway.ask()`.
- No ungrounded streaming — stages stream live; answer text streams only once
  validated.
- No realm mixing — provider and patient identities are separate realms,
  forever.
- An uploaded file is always a **note** — transcribed to text and charted as
  history. It never becomes a `LabResult` (those are LOINC-coded, via
  `interchange`) or an order (created in-chart → Pharmacy). Tus saves typing,
  not a new write path.
- Auth, multi-tenancy and consent beyond the above remain the
  HITRUST/HIPAA workstream #88 points at, not this module.
