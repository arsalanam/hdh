# Testing the encounter workflow (browser)

End-to-end manual test plan for the web-UI encounter workflow: **select a
patient → record/dictate → review & edit → optional note → ask the agent to
update the chart**, with every input reachable by **typing/clicking or by
speech-to-text**.

Two test cycles, each run first against the **local** media backend, then
(where supported) against **AWS**:

- **Cycle A — a dictated clinical note → chart it.** A single-speaker visit note.
- **Cycle B — a mock encounter → note → chart it.** A Provider/Patient
  conversation, then an optional note.

Related: [Agent UI guide](agent-ui.md) (the browser front door), the
[media I/O design](../design/media-io-service.md), and the
[encounter-UI plan](../design/media-io-service.md) phases.

> **Read this first — what "transcription" you will see depends on the backend.**
> The media service has three backends. The **`null`** backend (the default, no
> GPU) is an **echo**: it turns each audio clip's *bytes* into a stub string like
> `[stt stub: 4096 bytes audio/webm]`, **not your words**. That is intentional —
> it proves the whole UI path (streaming, diarization labels, edit, note, save,
> chart-update) with no GPU and no cloud. For **real speech→text** you need
> either the **`local`** backend (NeMo, a CUDA GPU, the `hdh[media]` extra) or
> the **`aws`** backend (Transcribe Medical, a BAA account + an S3 bucket). The
> AWS backend transcribes with a **batch** job, so its transcript lands when you
> **stop** recording, not turn-by-turn (see
> [Cycle C](#cycle-c--aws-the-managed-route)).

---

## Part 1 — Initial setup (one time)

**Requires:** Python env (`uv`), Node (for the SPA), Docker (for Keycloak +
PostgreSQL), an Anthropic API key, and — for real local STT — a CUDA GPU with
the `hdh[media]` extra.

### 1. Configure `.env`

```bash
cp .env.example .env
```

Fill in at least:

```ini
ANTHROPIC_API_KEY=sk-ant-...                                   # the agent (charting) needs this
HDH_DB_URL=postgresql+psycopg://hdh:hdh@localhost:5433/hdh     # the `just deps` container
```

> `uv run hdh ...` does **not** read `.env`. Run every `hdh` command below
> through `just` (which loads `.env`) — e.g. `just hdh generate ...` — or
> `uv run --env-file .env hdh ...`.

### 2. Start the dependency containers (Keycloak + PostgreSQL)

```bash
just deps          # PostgreSQL on :5433, Keycloak on :8080 with the `hdh` realm
```

If you ever see **"Client not found"** or **"Invalid redirect URI"** at login,
the realm is stale: `just deps-nuke && just deps`, then re-run step 4.

### 3. Seed a synthetic dataset and get an MRN

```bash
just hdh generate --patients 50 --years 2     # writes to HDH_DB_URL (or the SQLite file)
```

You need a patient MRN to select in the UI. The easiest way is to ask the agent
once it is running (step 6): type **"list five patient MRNs"**. Or from the
shell:

```bash
just hdh show --mrn <any-guess>     # errors print nothing useful; prefer the agent
```

### 4. Link the demo providers to profiles (idempotent)

```bash
just seed-identities      # so a signed-in write carries a real provider_id
```

### 5. Install the SPA deps

```bash
npm --prefix web install
```

### 6. Run the three processes

For testing, use **dev mode** — Vite hot-reloads the SPA and proxies the agent
API **and** the media service for you:

| Terminal | Command | Serves |
|---|---|---|
| 1 | `just hdh serve-agent` | agent API on **:8100** (`/ask`, `/patients/{mrn}`, `/dictations`, …) |
| 2 | `just hdh serve-media --backend null` | media service on **:8200** (`/stt`, `/stt/stream`, `/tts`) |
| 3 | `npm --prefix web run dev` | the SPA on **:5173** (proxies `/…`→:8100 and `/stt`,`/tts`→:8200) |

Open **http://localhost:5173** and sign in as **`dr.chen` / `dr.chen`**
(password = username for every demo user; `dr.chen` can read charts and
prescribe/order). `:5173` is a registered redirect URI, so login works in dev.

> **Production mode** (`npm --prefix web run build` + `just serve-agent`, served
> at `:8100`) does **not** proxy the media routes. There you must point the SPA
> at the media host at build time: `VITE_MEDIA_URL=http://127.0.0.1:8200`. For
> testing, stick with dev mode above.

---

## Part 2 — Choosing a media backend

Switch backends by restarting **terminal 2** (`serve-media`) with a different
`--backend`. Nothing else changes.

| Backend | Command | STT you get | Needs |
|---|---|---|---|
| `null` (default) | `just hdh serve-media --backend null` | **echo stub** (not your words) — plumbing only | nothing |
| `local` | `just hdh serve-media --backend local` | **real** NeMo STT + Sortformer diarization (live, turn-by-turn) | `hdh[media]` extra + a CUDA GPU |
| `aws` | `just hdh serve-media --backend aws` | **real** Transcribe Medical STT (batch — transcript on **stop**) + Polly TTS + Textract OCR | `hdh[aws]` extra + AWS creds (BAA) + an S3 bucket |

For `local`:

```bash
uv pip install -e '.[media]'          # torch + nemo + whisperx + kokoro + docTR (heavy, GPU)
# optional: HDH_MEDIA_STT=whisperx to use the WhisperX engine instead of NeMo
```

For `aws`, add to `.env`:

```ini
# AWS managed route (HIPAA-eligible only under a BAA)
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
HDH_AWS_TRANSCRIBE_BUCKET=my-baa-bucket   # S3 bucket Transcribe reads STT input from
```

```bash
uv pip install -e '.[aws]'            # boto3 + amazon-transcribe
```

The STT bucket is **required** for `aws` — Transcribe reads its audio from S3.
The credentials must be able to write and delete objects in it (the media
service uploads the clip, then deletes it — it persists nothing), and the
Transcribe service must be able to read it. Starting `--backend aws` without
`HDH_AWS_TRANSCRIBE_BUCKET` builds fine but any STT call fails loudly with a
hint; Polly (TTS) and Textract (OCR) need no bucket.

---

## Part 3 — The test cycles (local backend)

Run these first with `--backend null` to confirm the UI wires up (you will see
stub text), then with `--backend local` for real transcription. **Grant the
browser microphone permission** the first time you record or hold the mic.

### Cycle A — a dictated clinical note → chart it

1. **Select the patient.** In the top bar, enter the MRN and click **Select** —
   the active-patient chip shows the name/age/sex. (404 ⇒ the MRN is wrong.)
2. **Record the note.** In the Encounter panel click **🎙️ Record encounter**,
   speak a short visit note aloud (e.g. *"Follow-up for hypertension. Blood
   pressure one-thirty over eighty. Continue lisinopril. Order a basic
   metabolic panel."*), then click **⏹ Stop**.
   - **Expect:** turns appear in the scrollable window as you speak (real words
     on `local`; a stub on `null`).
3. **Review & edit.** Fix any turn's text, click a **Provider/Patient** label to
   relabel it, or **✕** to delete a turn.
4. **(Optional) add a provider note.** Type into the *Provider note* field, or
   **hold 🎤** beside it to dictate. This is additive to the transcript.
5. **Save.** Click **Save encounter** — a chat line confirms it was saved for the
   patient.
6. **Update the chart.** Click **Update the chart from this encounter** (this is
   the agent ask, carrying the patient). Or type/🎤-dictate your own ask in the
   composer, e.g. *"Update this patient's chart from the latest encounter."*
   - **Expect:** the agent streams its stages, then a grounded answer saying it
     charted the history and **drafted** any orders (meds/labs) for review.

### Cycle B — a mock encounter → note → chart it

Same as Cycle A, but in step 2 speak a **two-speaker conversation** (provider
questions + patient answers). In step 3, use the **Provider/Patient** relabel to
correct diarization (on `null` the labels just alternate). Then note → Save →
Update the chart.

### Testing the two modalities

- **UI (click/typing):** buttons for Record/Stop/Save/Update-chart; type the
  note and the composer ask.
- **Push-to-talk (voice):** **hold 🎤** on the composer to dictate your ask, and
  **hold 🎤** by the Provider-note field to dictate the note. Release to
  transcribe; the text is appended to whatever is already there.

### Verifying the chart was updated

The agent's answer is the first signal (it names the charted visit and the
drafted orders). Confirm in the data:

```bash
just hdh show --mrn <MRN>                      # the patient chart — look for the new visit note
just hdh orders list --mrn <MRN> --status draft  # the orders the agent drafted (DRAFT, origin=AGENT)
```

Orders are **drafts** — releasing them to pharmacy/lab stays a human step
(`just hdh orders release ...`); the agent never sets them active.

---

## Cycle C — AWS: the managed route

With `--backend aws`, all three transductions run against Amazon's managed,
HIPAA-eligible services (under a BAA): **Transcribe Medical** (STT),
**Polly** (TTS), **Textract** (OCR).

**One difference from `local` to expect: STT is a *batch* job.** Transcribe
streaming needs raw PCM and the browser sends webm, so rather than transcode
mid-visit, the AWS backend uploads the audio to S3 and runs a batch Transcribe
Medical job. In practice:

- **Push-to-talk** (`/stt`): hold 🎤, release, then **wait** — the batch job
  runs (upload → poll → fetch), typically tens of seconds, before the dictated
  text appears. It is slower than `local`, but it is real transcription.
- **Recording an encounter** (`/stt/stream`): the clips are buffered as you
  speak and transcribed **when you click ⏹ Stop** — so the diarized turns appear
  in the review window all at once on stop, not live. Edit / relabel / note /
  Save / Update-chart are then identical to Cycles A and B.
- **Polly (TTS)** and **Textract (OCR)** are direct calls, no bucket or polling —
  the voice-output toggle and note-image upload work the same as `local`.

So run **Cycle A and Cycle B exactly as above** on `--backend aws`, allowing for
the stop-to-transcribe delay. Before you start, confirm the backend is up:

```bash
# a bearer token is required; grab one from the browser devtools (Authorization
# header on any API call) or your Keycloak client, then:
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8200/capabilities
# → {"backend": "aws", "stt": true, "streaming_stt": true, ...}
```

If an STT call errors with a bucket hint, set `HDH_AWS_TRANSCRIBE_BUCKET` (see
setup above) and restart `serve-media`.

> **True low-latency AWS streaming** (turn-by-turn, like `local`) would need the
> browser to capture raw PCM instead of webm and feed the `amazon-transcribe`
> streaming SDK (`stream_segments_from_transcribe` already shapes its events).
> That is a tracked follow-up; the batch path above is the working AWS STT today.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Transcript shows `[stt stub: … bytes …]` | You are on `--backend null`. Switch to `--backend local` for real words. |
| "Client not found" / "Invalid redirect URI" at login | Stale Keycloak realm: `just deps-nuke && just deps`, then `just seed-identities`. |
| Record/🎤 does nothing | Browser mic permission denied, or no mic. Grant it; typing still works regardless. |
| `/patients/{mrn}` 404 when selecting | Wrong MRN, or the DB was not seeded — re-run `just hdh generate`. |
| Agent answers but charts nothing | The question didn't read as charting, or no patient was selected. Select a patient and use the **Update the chart** button. |
| Agent errors about tokens/key | `ANTHROPIC_API_KEY` not visible — run via `just` (loads `.env`): `just check-env`. |
| Media routes 404 in dev | Restart `npm run dev` after a `vite.config.ts` change; confirm `serve-media` is up on :8200. |
| `--backend local` errors "pip install 'hdh[media]'" | The GPU extra isn't installed, or no CUDA GPU. `uv pip install -e '.[media]'` on the GPU box. |
| `--backend aws` STT errors about a bucket | Set `HDH_AWS_TRANSCRIBE_BUCKET` to an S3 bucket the creds can write and Transcribe can read, then restart `serve-media`. |
| `--backend aws` encounter transcript only appears on Stop | Expected — AWS STT is a batch job, so the encounter transcribes on stop, not live (see Cycle C). |
