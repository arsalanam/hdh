# Media I/O service — design (draft)

Status: draft, for review. Realises #87 (speech capture) and generalises it:
one standalone, **stateless** service that turns **media into text** (diarized
speech-to-text, OCR) and **text into speech** (TTS), so the hdh agent can be
*spoken to during an encounter*, *handed a recording or a scanned page*, and
*talk back*. The transcripts it produces are stored by the domain (not the
service) and can be referenced later — "update the chart from the last
conversation." Defers to [agentic-ui-module.md](agentic-ui-module.md) for the UI
and to #88/#178 for the note/order boundary it inherits (restated precisely in
§2).

## 1. Capabilities and modes

Three capabilities. STT has two **modes**, because a live encounter and an
uploaded recording are different shapes of the same transduction:

- **STT · encounter mode (live, streaming, diarized)** — a physician says
  "record the encounter." The browser streams audio in short clips; the service
  returns incremental **diarized** segments (who said what — provider vs
  patient). Two-speaker conversation capture.
- **STT · async mode (upload, batch, diarized)** — a physician uploads a
  recorded conversation *or* a dictated doctor's note; the service transcribes
  the whole file asynchronously (diarized for a conversation; single-speaker for
  a dictation) and returns when done.
- **OCR** — image/PDF → text (printed; handwriting via a vision model).
- **TTS** — text → audio. An enable/disable mode on the UI; when on, the agent's
  concise spoken rendering (§6) is voiced with **Kokoro**.

Noise handling is deliberately **light**.  ordinary exam-room audio needs only format-convert →
16 kHz mono, peak-normalise, and a domain-vocabulary correction pass (clinical
terms, drug names). **Diarization, not denoising, is the hard requirement.**

## 2. The boundary (non-negotiable — restated precisely)

The service is a **transducer**: bytes ↔ text, holding no clinical logic and no
patient data. Everything consequential happens *after* its text re-enters an
existing, validated path. What that path may do depends on **what the media
is** — and this is the correction to earlier drafts:

| Media | Is | May produce |
|---|---|---|
| **Live encounter / dictated post-visit note** | the provider instructing the chart *now* | chart history **and** order changes — medications, lab **orders**, procedure orders: **add / update / cancel** — through the agent's existing in-chart order path (→ Pharmacy / fulfilment), as **drafts** under the normal review/reconciliation lifecycle |
| **Scanned old report / historical document** | the provider telling the chart what already happened | chart **history only** |

Hard limits that hold for **all** media:

- Order changes are **drafted, never auto-committed** — same `ai_generated`,
  reconciled, provider-approved lifecycle the agent already uses. Dictation does
  not silently prescribe.
- It **never** creates `LabResult` *values*. A lab **order** is fine (a
  `ServiceRequest`); a lab **result** ("K⁺ 5.1") is partner-reported, arrives
  only through `interchange` matched to an order, and no transcript can mint one.
- The media service itself **writes nothing** and **talks to no clinical table**.
  Only the agent writes, only through its existing tools.

## 3. Conversations — store once, reference later, act on command

This is the spine of the "record now, chart later" workflow, and it is **domain
state, so it lives in the hdh DB and is owned by `agent_api` / comprehension —
never in the stateless media service.**

- **Every transcript is auto-saved** as a `Conversation`: a unique id, the
  diarized segments + full text, the signed-in provider, an optional patient /
  encounter binding (from the UI context when recording was started), a
  timestamp, and a generated **summary**. "Auto-send" = it is persisted and
  acknowledged without the provider re-typing or confirming the text.
- **"This / last conversation" is a per-provider pointer** to the most recent
  saved conversation (scoped to patient when one is bound). It is what lets the
  provider speak in two steps:

  1. *"Record the encounter"* → (talk) → stop → saved as conversation `C-…`,
     summary generated, set as the provider's current conversation.
  2. *"OK, update the chart now."* → the agent resolves **the last conversation**
     (+ any dictated notes), uses its **summary + transcript as the evidence**,
     and proposes chart-history updates and order changes (add/update/cancel
     meds, labs, procedures) — as drafts, for approval.

- The agent reaches this through one tool (e.g. `use_conversation` /
  `last_conversation`) that returns the resolved transcript + summary + patient
  binding; the charting/ordering then runs through the **existing** comprehension
  and order tools. No new write path — a new *source* for the ones there.
- If no patient is bound (a note dictated with no chart open), the agent must
  resolve the patient before charting — ask, or match from the transcript — and
  never guess.

## 4. The seam — reuse, don't reimplement

- **OCR replaces the vision pre-pass already in `agent_api/notes.py`** —
  `transcribe()` already turns an uploaded image/PDF into text before
  comprehension; OCR is a swappable backend for that step.
- **STT fills the hole `transcribe()` already names** — today it raises
  `NoteError("audio dictation is the speech front door (#87)…")`. This service
  *is* #87.
- **TTS reads `Gateway.ask()`'s output** — it voices the agent's concise
  rendering (§6), inventing nothing.
- **Charting/ordering from a transcript reuses the comprehension + orders
  modules** — the transcript is just new input text to paths that already carry
  grounding, reconciliation, and the order lifecycle.

## 5. Architecture — stateless transducer, portable backends

```
                    ┌──────────────────────────────────────────┐
  UI / agent_api    │        media service (HTTP + WS)          │  STATELESS
  ──────────────────▶  WS /stt/stream   POST /stt   /tts  /ocr   │  (no PHI at rest)
   bearer token     │        │              │       │     │      │
                    │   Transcriber(+diarizer) Synth  OcrEngine  │  ← protocols
                    │        │              │       │     │      │
                    │     local(CUDA) / aws(Transcribe/Polly/Textract)
                    └──────────────────────────────────────────┘
        transcripts persisted by agent_api ─▶  hdh DB: Conversation
                                               (id, segments, summary,
                                                provider, patient?, ts)
```

- Every model sits behind a protocol — `Transcriber` (with a diarizer),
  `Synthesizer`, `OcrEngine` — and `HDH_MEDIA_BACKEND=local|aws` (with
  per-capability overrides) picks the implementation at startup. A **null/echo
  backend** is the CI default, so the contract, the UI, and the agent
  integration are all testable with no GPU and no cloud — the same injectable
  pattern `agent_api` already uses for `ask`/`ingest`/`authenticator`.
- **Separate service, not part of `agent_api`:** models are VRAM-heavy, slow to
  load, scale on their own (one GPU box), and the AWS target is managed
  endpoints with their own lifecycle. A clean HTTP/WS boundary lets the media
  tier run on the GPU host locally and move to ECS/managed-AI on AWS without
  touching the agent.
- **Streaming (encounter mode) is a WebSocket**: the browser sends audio clips,
  the server streams back diarized partial/final segments. **Async mode** is a
  multipart upload returning a job id, with completion over SSE or poll.
- **The media service persists nothing.** `agent_api` receives the finished
  transcript and writes the `Conversation` (PHI) into the hdh DB.

## 6. The concise spoken answer

When TTS is enabled, the agent returns **two renderings of the same grounded
answer**: `answer` (full, for the screen) and `spoken` (≤ ~40 words, plain prose,
no markdown/citations, numbers spelled for the ear). **The agent produces
`spoken`; the media service only voices it** — what to say aloud is a content
decision, so it is grounded and validated, available to the CLI
(`hdh agent --spoken`), and never invented by the TTS layer. It is generated in
the same assembler pass as `answer`, constrained to assert nothing absent from
`answer` (containment check), and requested only when TTS is on. The UI shows a
visible "🔊 spoken summary" line of exactly what was voiced. Engine: **Kokoro**.

## 7. API contract (v1)

Identity-gated by the **provider-realm** bearer token (§9); the media endpoints
persist nothing. The conversation endpoints live on `agent_api` (they touch the
hdh DB), the transduction endpoints on the media service.

**Media service**

| Method | Path | In | Out |
|---|---|---|---|
| `GET` | `/health` · `/capabilities` | — | status; loaded backends/models/voices |
| `WS` | `/stt/stream` | audio clips | diarized segments `{speaker, text, t0, t1, final}` |
| `POST` | `/stt` | audio file (multipart) | job id → `{segments[], text, speakers, duration_s}` |
| `POST` | `/tts` | `{text, voice?}` | `audio/*` stream (Kokoro) |
| `POST` | `/ocr` | image/PDF | `{text, blocks?, pages}` |

**agent_api (domain)**

> **Naming (resolved phase 2):** this document first wrote `Conversation` /
> `/conversations`, but both are already taken — agent_api's `/conversations`
> and `Conversation` model are the agent's **Q&A chat-thread history**, and
> `Transcript` is the media service's transduction DTO. The recorded-encounter
> store therefore ships as **`Dictation` / `/dictations`** (entity owned by the
> comprehension module; the agent tool is `use_last_dictation`). "The last
> conversation" in prose = the provider's most recent non-voided dictation.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/dictations` | persist a finished transcript → `Dictation{id,…}` (newest = current), generate summary |
| `GET` | `/dictations/{id}` · `/dictations/current` | fetch a dictation / the provider's last one (scoped to the bound patient when `?mrn=` is given) |
| `POST` | `/ask` (`want_spoken`) | returns `answer` + `spoken`; "update the chart now" resolves the last dictation as evidence |

`/notes/upload` also learns to accept `audio/*` → `/stt` → comprehension,
closing the `NoteError` hole for a one-shot dictated note.

## 8. UI integration (into the SPA)

- **Record encounter** (streaming): a record control opens the WebSocket, shows
  a live diarized transcript (Provider / Patient turns); on stop the transcript
  **auto-saves** as the current conversation — no edit-then-confirm step.
- **Upload** (async): drop a recording or a dictation; a progress indicator;
  on completion it auto-saves the same way.
- **"Update the chart now"** is then an ordinary agent turn — the agent pulls
  the current conversation and proposes history + order drafts for approval.
- **Voice output** is a single enable/disable toggle; when on, answers are
  voiced (Kokoro) with the visible spoken-summary line.
- **Scan** (later) routes an image/PDF through `/ocr` into the note path.
- The media service is **started from the operator CLI** (`hdh serve-media` or a
  compose service); the UI only invokes it and hides voice controls gracefully
  when it is absent — the text path is unaffected.

## 9. Identity, PHI, AWS

- Audio, transcripts, and scanned pages **are PHI**. Every call carries a
  verified provider-realm Keycloak token; the media service logs no media and no
  transcript bodies and keeps nothing at rest. The `Conversation` store (in the
  hdh DB) is the only place a transcript persists, under the same chart
  protections.
- **Local backend keeps PHI on-box** — the privacy default.
- **AWS, two routes, same protocols:** (a) managed — Amazon Transcribe
  **Medical** (STT + diarization), Polly (TTS), Textract (OCR), all HIPAA-eligible
  under a BAA; or (b) self-hosted — the *same* NeMo / Kokoro models productionised
  the way the reference service productionises its stack: FastAPI + a job worker
  on Kubernetes, GPU workers autoscaled by KEDA on queue depth, Karpenter
  on-demand GPU nodes (scale-to-zero when idle), durable queue with a DLQ, OTEL
  tracing, and a full audit trail. The **local** deployment is the simple
  single-process, single-GPU form of that; the production shape is a scale-out of
  the identical protocols. HDH keeps **Keycloak** auth (not the reference's API
  keys) and **HIPAA** posture (the reference's CJIS controls map across).

## 10. Pipeline & models (RTX 5060, 16 GB, CUDA)

**Base pipeline: NVIDIA NeMo (Option B)** — one framework for ASR + diarization
+ speaker embeddings, with a first-class streaming path. This is chosen *because*
HDH's hard requirement is the **live, streaming** encounter (§1), which is where
a single stream-native framework beats stitching Whisper + pyannote together
(the reference's WhisperX stack is batch-shaped and bolts diarization on after).

```
 audio ─▶ preprocess (light) ─▶ NeMo streaming ASR  +  Sortformer diarization ─▶ speaker ID ─▶ confidence ─▶ segments
          16kHz mono,           (Parakeet / Canary)    (stream-native, online)   TitaNet       per-segment   {speaker,
          normalize, vocab fix                                                    verification  needs_review  text,t0,t1,conf}
```

- **ASR + diarization** — **NeMo** streaming ASR (**Parakeet** / **Canary**) with
  **Sortformer** diarization, which is designed for *online* speaker-attributed
  transcription rather than diarize-after-the-fact. One toolkit, one CUDA setup,
  jointly supported, fits two speakers in 16 GB with room to spare.
- **Speaker ID = one embedder for both jobs.** **TitaNet** (NeMo's speaker model)
  provides the embeddings Sortformer clusters on *and* the provider-verification
  embedding — same model, same vector space, so matching the provider cluster to
  the enrolled print is a direct cosine (no second SV model). Enrollment is
  **verification, not open-set ID**: the signed-in provider is known, so it is a
  1:1 check against *their* enrolled embedding (§13), with a heuristic fallback
  and manual relabel in the UI.
- **Confidence → `needs_review`.** Low-confidence segments are flagged, not
  silently trusted — the transducer's expression of HDH's refuse-don't-guess
  rule. A flagged segment that feeds charting lands in the **review queue**.
- **Dropped** (body-cam-only): denoising, DeepFilterNet, spectral/narrowband and
  BEATs background classification. Clinic audio does not need them.
- **TTS** — **Kokoro-82M** (ONNX/CUDA, ~0.5 GB), per your choice.
- **OCR** — docTR / PaddleOCR for printed pages; handwriting via a vision model
  (keep the cloud vision pass initially; local VLM — Qwen2.5-VL 7B int4,
  ~5–6 GB — as a later on-box-privacy upgrade).

**Why NeMo over the reference's WhisperX stack, given history:** an earlier
attempt (~2026-04) found NeMo diarization weak — but that was on **noisy
body-cam** audio, the exact failure mode we are dropping, and used batch
submission. Clinic audio is clean and the requirement is now **streaming**, where
Sortformer is both newer and the right tool; WhisperX has no native streaming
story. So the earlier result does not transfer. **It must still be re-validated**
on real clinic-style audio before we commit — see phase 3's benchmark gate.

**WhisperX + pyannote stays as the comparison baseline** (and the async-batch
fallback): it is the reference's proven path, so phase 3 measures NeMo **against**
it on WER/DER before NeMo is locked in. If NeMo diarization still disappoints on
clean clinic audio, we fall back to WhisperX for async and keep NeMo only for the
streaming mode.

**Throughput expectation:** the reference (WhisperX on a 48 GB L40S) hits
~20–30 s per hour of audio. The RTX 5060 (16 GB) is smaller, but a clinic needs
only one or two concurrent recordings — ample. Measure WER/DER with a benchmark
harness before trusting any engine or model swap.

## 11. Phased plan

| Phase | Delivers | Proves |
|---|---|---|
| **1 · Skeleton** | `hdh.modules.media`, `hdh serve-media`, `/health`+`/capabilities`, backend protocols, null/echo backend, tests | contract + injection, no GPU |
| **2 · Dictations** ✅ shipped | `Dictation` entity + store (owned by comprehension), `/dictations` + `/dictations/current` + `/dictations/{id}`, per-provider "last" pointer, injectable summary, agent `use_last_dictation` tool | record→save→reference, two-step "chart it now" |
| **3 · STT async + diarization** ✅ shipped | `/stt` on **NeMo** (primary) + **WhisperX** (baseline/fallback) behind the same `Transcriber` protocol; the **WER/DER benchmark gate** (`hdh media-benchmark`) that locks NeMo in only if it is within margin of WhisperX on both metrics; `media` optional extra (torch/nemo/whisperx); upload → saved transcript is the real `/stt` + phase-2 `/dictations`, orchestrated by the UI (service stays stateless) | NeMo diarization actually works on clinic audio; upload a visit recording, get a diarized transcript |
| **4 · "Update the chart now"** ✅ shipped | agent charts history (`apply_note`) **and** drafts med/lab/procedure orders from the last dictation via new `draft_order` / `update_order` / `cancel_order` / `list_orders` tools (wired into the `charting` intent); every change is an unreleased `ServiceRequest(DRAFT, origin=AGENT)` — releasing to Pharmacy stays the human `hdh orders release` act, and an already-ACTIVE order is refused | the headline workflow, end to end, as drafts |
| **5 · STT encounter (streaming)** | WS `/stt/stream` on NeMo streaming + Sortformer (online), live diarized capture, UI record control | talk through a visit live |
| **6 · TTS + concise answer** | assembler `spoken`; `/ask want_spoken`; `/tts` (Kokoro); UI toggle | it talks back, concisely and grounded |
| **7 · OCR + AWS backends** | `/ocr`; `aws` implementations behind the protocols | scanned pages; cloud deployment |

Phases 2–4 are the clinical heart (record → reference → chart with orders); 5–6
complete voice in/out.

## 12. Non-negotiables

- The service **transduces only** — no clinical logic, no PHI at rest, writes
  nothing.
- Order changes from a transcript are **drafts under review**, never
  auto-committed, and flow through the agent's existing order path.
- **Never a lab result value** from any transcript — lab orders yes, results
  only via `interchange`.
- The **spoken answer is the agent's**, grounded and validated — never the TTS
  layer's invention.
- **PHI stays on-box locally**; AWS requires a BAA and HIPAA-eligible services.

## 13. Decisions

**Settled (2026-10-19):**
- Diarization **required** (provider + patient); heavy denoising **not** needed.
- **Two STT modes**: live streaming encounter **and** async upload — both.
- Transcripts **auto-saved** (not edit-first) with a unique id and a
  "this/last conversation" reference; drive "update the chart now."
- A transcript **may draft order changes** (meds/labs/procedures,
  add/update/cancel) via the agent — correcting the earlier "history only" line.
- TTS is **enable/disable**; engine **Kokoro**.
- Clinic pipeline **drops** the reference's body-cam preprocessing (denoise,
  DeepFilterNet, radio/BEATs background detection).

**Settled (2026-10-20) — base engine:**
- **Base pipeline is NVIDIA NeMo (Option B)**: streaming ASR (Parakeet/Canary) +
  **Sortformer** diarization + **TitaNet** embeddings — one framework, streaming-
  native, chosen because the live encounter is the hard requirement.
- **One embedder (TitaNet) serves both** diarization clustering **and** provider
  verification — same vector space, no second SV model.
- **Speaker ID = verification, not open-set identification**: the signed-in
  provider is known, so it is a 1:1 cosine check against *their* enrolled
  embedding, with continuous/passive re-enrollment from confirmed turns;
  heuristic fallback + manual relabel.
- **Providers only are enrolled, never patients** — the voiceprint is biometric
  PHI (consent, secure storage tied to the Keycloak subject, revocable); patients
  are simply "the other speaker."
- **History note:** NeMo diarization underperformed ~2026-04, but on **noisy
  body-cam / batch** — the dropped failure mode. **Must be re-validated** on clean
  clinic audio in phase 3 (WER/DER gate) before it is locked; **WhisperX +
  pyannote is kept as the measured baseline and the async-batch fallback.**

**Still open:**
1. **Channel vs embedding** for provider capture — a dedicated provider mic
   (headset/lapel = known channel, no voiceprint at all) where hardware allows,
   vs. embedding verification for single-mic deployments. Offer both.
2. **Conversation scope for "last"** — per provider, or per provider+patient
   (recommended: per provider, narrowed to the bound patient when one is open).
3. **Summary generation timing** — at save (async, ready when asked) vs. lazily
   when the agent first acts on it.
4. **Handwriting OCR** — cloud vision now vs. local VLM for full on-box privacy.

**Settled (2026-10-22) — phase 3:**
- **Base engine not yet locked — it is gated, not assumed.** NeMo and WhisperX
  both ship behind the `Transcriber` protocol; `hdh media-benchmark <manifest>`
  measures them on labeled clinic audio and `decide_gate` locks NeMo in for async
  **only if** its mean WER *and* DER are within a margin (default 0.05) of
  WhisperX. Otherwise WhisperX stays the async engine and NeMo is reserved for
  streaming (phase 5), where it is the only option. The metric and gate logic are
  unit-tested; the measurement runs on the GPU box, not in CI.
- **Speaker ID (async) = heuristic, not verification.** The longest-speaking
  voice is credited to the Provider, the rest to the Patient, reviewable/relabel-
  able in the UI. TitaNet 1:1 verification against the signed-in provider's
  enrolled print (§13) replaces it in the streaming encounter (phase 5).
- **Local job/async machinery (was open #5): in-process for now.** `/stt` is
  synchronous; a clinic runs one or two concurrent recordings, well within a
  single GPU process. The queue+worker/KEDA scale-out shape (§9) is the AWS
  self-hosted deployment, not the local one — adopt it only when load demands.
- TTS (Kokoro) and local OCR stay placeholders in the `local` backend until
  phases 6 and 7; only STT is real.
