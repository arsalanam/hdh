# hdh for Health Practitioners — Complete Walkthrough (Windows)

A step-by-step guide for clinicians, care managers, quality/population-health
staff, and educators who want to run hdh on their own Windows machine. You
need basic PowerShell comfort (opening a terminal, typing commands) — no
programming experience.

**What you get:** a fully synthetic family-medicine practice — 10,000
patients with four years of visits, diagnoses, prescriptions, and labs — plus
tools you can point at it: care-gap detection, an ML risk model, an AI
assistant that answers questions about the panel, auto-generated SOAP notes,
and a FHIR interface.

> ⚠️ **Read this first**
> - Every patient in hdh is **synthetic**. No real person's data is involved,
>   which is exactly why you can experiment freely.
> - **Never type real patient information into the AI assistant.** Questions
>   you ask it are sent to a cloud AI service (Anthropic). Synthetic MRNs and
>   names from this dataset are fine; real PHI is not.
> - Nothing here is medical advice or a validated clinical tool. hdh is an
>   educational sandbox for learning how these systems work.

---

## Contents

- [Part 1 — Install the prerequisites](#part-1--install-the-prerequisites)
- [Part 2 — Get hdh and set it up](#part-2--get-hdh-and-set-it-up)
- [Part 3 — Generate your synthetic practice](#part-3--generate-your-synthetic-practice)
- [Part 4 — Explore patients and charts](#part-4--explore-patients-and-charts)
- [Part 5 — Find care gaps](#part-5--find-care-gaps)
- [Part 6 — Risk stratification](#part-6--risk-stratification)
- [Part 7 — The AI assistant](#part-7--the-ai-assistant)
- [Part 8 — SOAP-note narratives](#part-8--soap-note-narratives)
- [Part 9 — Export data and the FHIR interface](#part-9--export-data-and-the-fhir-interface)
- [Part 10 — Simulate scenarios](#part-10--simulate-scenarios)
- [Part 11 — See what the AI did (traces & spending)](#part-11--see-what-the-ai-did-traces--spending)
- [Troubleshooting](#troubleshooting)

---

## Part 1 — Install the prerequisites

Open **PowerShell** (Start menu → type "PowerShell" → Enter) and install two
small tools. `winget` comes with Windows 10/11.

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

- **Git** downloads the project (and keeps it updatable).
- **uv** manages Python for you — you do *not* need to install Python
  yourself; uv fetches the right version automatically.

**Close PowerShell and open a new window** so the tools are on your PATH.
Verify:

```powershell
git --version
uv --version
```

Both should print version numbers. If not, see [Troubleshooting](#troubleshooting).

## Part 2 — Get hdh and set it up

```powershell
cd $HOME\Documents
git clone https://github.com/arsalanam/hdh.git
cd hdh
uv sync --all-extras
```

`uv sync` creates a private environment inside the project folder and
installs everything (a few minutes the first time). Then check the install:

```powershell
uv run hdh --help
```

You should see the command list: `generate, stats, export, show,
care-gaps, risk, agent, narrative, serve, trace`, and more.

> **The `uv run hdh ...` pattern:** every hdh command in this guide is
> prefixed with `uv run`. That runs it inside the project's own environment —
> nothing is installed system-wide, and nothing else on your machine is
> affected. Always run these commands from the `hdh` folder.

## Part 3 — Generate your synthetic practice

```powershell
uv run hdh generate --patients 10000 --years 4
```

(Prefer not to wait? Download `family_medicine-10k.zip` from
[the latest release](https://github.com/arsalanam/hdh/releases/latest) and
unzip it into the `hdh` folder instead — same 10,000 patients, ready to use.)

This builds `family_medicine.db` (~95 MB) — your practice. It takes a few
minutes; you'll see progress every 500 patients. For a quicker first play,
`--patients 2000` finishes in well under a minute (the AI and risk features
work fine on the smaller panel too).

What the generator actually does, clinically: each patient gets demographics,
insurance, allergies, and family history; visit frequency follows age and
chronic burden (infants and seniors ~5 visits/year); conditions follow
age/sex/seasonal epidemiology (RSV in winter infants, UTIs peaking in summer,
flu in January); chronic disease is seeded from age, family history, smoking,
and BMI so comorbidities cluster the way you'd expect; and every visit
carries vitals, an ICD-10 diagnosis, formulary-plausible prescriptions, and
LOINC-coded labs whose values shift with the condition.

Check the result:

```powershell
uv run hdh stats
```

Expect ~165,000 visits, hypertension/T2DM/hyperlipidemia leading the
diagnosis table, and a age pyramid weighted toward seniors and young
children — like a real family practice.

## Part 4 — Explore patients and charts

```powershell
uv run hdh list-conditions          # the 30+ conditions the engine models
uv run hdh stats                    # panel overview
```

To read one patient's chart, you need an MRN. Grab a few from the care-gap
list (next section) or risk list, then:

```powershell
uv run hdh show --mrn MRN12345678
```

You get a full chart: demographics, family history, active problem list with
control status, and every visit with vitals, assessment, prescriptions, labs
(flagged when out of range), and the follow-up plan.

## Part 5 — Find care gaps

This is the population-health workhorse — your outreach list:

```powershell
uv run hdh care-gaps --limit 25
```

Four rules run against the panel, ranked most severe first:

| Gap type | Meaning | Severity |
|---|---|---|
| `uncontrolled_chronic` | An uncontrolled condition (e.g. HTN flagged uncontrolled) with **no visit in 90+ days** | high |
| `missed_follow_up` | Provider asked for a follow-up in N days; N×1.5 has passed with no return | medium |
| `polypharmacy_review` | 65+, on 5+ medications, not seen in 6 months | medium |
| `overdue_preventive` | No annual physical / well-child visit within the age-appropriate interval | low |

Useful variations:

```powershell
uv run hdh care-gaps --mrn MRN12345678       # one patient's gaps
uv run hdh care-gaps --limit 100 --json > gaps.json   # export for a spreadsheet
```

Dates are judged against the dataset's own timeline (its most recent visit),
so the results stay meaningful no matter when you generated the data.

**AI chart review (optional).** Once your API key is set up (Part 7), a
second finder reviews charts the way a quality reviewer would — catching
things the fixed rules can't, like a diabetic overdue for an HbA1c or two
overlapping statins on the med list:

```powershell
uv run hdh care-gaps --finder ai --mrn MRN12345678    # one patient (~a few cents)
uv run hdh care-gaps --finder ai --sample 5           # your 5 most complex patients
```

Rules are free, instant, and reproducible; the AI finder is slower, costs a
little, and varies between runs — but reasons clinically. Comparing the two
on the same patient is one of the most instructive exercises in this toolkit.

## Part 6 — Risk stratification

Train a machine-learning model on *your* generated panel, then rank patients
by predicted risk of deterioration (an urgent visit or critical lab within
180 days):

```powershell
uv run hdh risk train
```

Expect output like: `Patients: 10,000 | Positive rate: 6.4% | Held-out ROC
AUC: 0.714`. In plain terms: the model was tested on patients it never saw
during training, and an AUC of ~0.71 means it ranks a truly-deteriorating
patient above a stable one about 71% of the time — a realistic figure for
utilization models on this kind of data.

```powershell
uv run hdh risk score --top 20               # your highest-risk patients
uv run hdh risk score --mrn MRN12345678      # one patient
```

The output shows each patient's probability, tier (high/moderate/low), and
the drivers: age, chronic-condition count, uncontrolled conditions, recent
urgent visits, critical labs. Sanity-check it clinically — the high tier
should be dominated by multimorbid seniors with uncontrolled disease, and it
is. Note what the model does *not* weight heavily (e.g. a single very high BP
in an otherwise low-utilizing patient) — a good discussion point about the
difference between utilization risk and clinical severity.

## Part 7 — The AI assistant

The assistant answers questions about your panel in plain language, by
querying the database itself — every claim it makes is checked against the
data before you see it.

### 7a. One-time setup: an Anthropic API key

The AI runs on Anthropic's Claude models, which requires an account and API
key (usage is pay-per-use; typical questions cost a few cents):

1. Create an account at **platform.claude.com** and add a small credit balance.
2. Create an API key (starts with `sk-ant-`).
3. In PowerShell, store it permanently:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-your-key-here"
```

4. **Close and reopen PowerShell** (setx only affects new windows), return to
   the hdh folder, and verify:

```powershell
cd $HOME\Documents\hdh
uv run python scripts/check_env.py
```

It should say the key is set (showing only the last 4 characters).

### 7b. Ask questions

```powershell
uv run hdh agent "Which patients have uncontrolled hypertension AND a high risk score? Who should we call first?"
```

Watch the stage trace as it works:

```
┌─ pipeline · model claude-sonnet-4-6 · guard claude-haiku-4-5
  ├─ gateway        run 3f9c21ab · quota today: 500,000 input / 100,000 output tokens left
  ├─ guardrails     topic allowed ✓ (clinical cohort query)
  ├─ intent         cohort_search · entities: uncontrolled HTN, risk score
  ├─ tool-executor  attempt 1/3 · 2 tool call(s) recorded
  ├─ assembler      drafted 180-word response
  ├─ validator      VALID ✓ — response is grounded in tool evidence
  └─ streaming validated response
```

What those stages mean for you:

- **guardrails** — off-topic questions (sports, recipes...) are refused, and
  a built-in daily token budget caps spending.
- **tool executor** — the AI queries the database (charts, care gaps, risk
  scores, SQL) rather than guessing.
- **validator** — before you see anything, a second check confirms every
  MRN, value, and count in the answer actually appears in the query results.
  If not, the AI is sent back to gather better evidence (up to 3 tries). If
  it still can't verify, the answer arrives clearly labeled *"treat with
  caution."* This is the hallucination defense — and why answers take
  a little longer than a chatbot.

Good questions to try:

```powershell
uv run hdh agent "How many seniors are overdue for their annual wellness visit? Name the two most overdue."
uv run hdh agent "Which diabetic patients had an HbA1c above 9 in their most recent test?"
uv run hdh agent "Summarize the care gaps for MRN12345678 and suggest an outreach plan."
```

### 7c. Conversations

For follow-up questions in context, use the chat:

```powershell
uv run hdh agent                # rich chat UI: history, /commands, arrow-key recall
uv run hdh agent --pipeline     # chat through the validated pipeline (fully traced)
```

In the chat UI, type `/help` for commands — `/history` replays the
conversation, `/context` shows how much context you're using, `/save`
exports a transcript. Long conversations automatically summarize their older
turns so they never grow unbounded.

## Part 8 — SOAP-note narratives

Render any patient's visits as SOAP notes — useful for documentation
teaching, scribe training data, or just a more familiar read of the chart:

```powershell
uv run hdh narrative --mrn MRN12345678 --last 3
```

Add `--llm` to have the AI rewrite the templated notes as natural clinical
prose (requires the API key; values, codes, and dates are preserved):

```powershell
uv run hdh narrative --mrn MRN12345678 --last 3 --llm
```

## Part 9 — Export data and the FHIR interface

Export charts for use in other tools:

```powershell
uv run hdh export --format text --limit 100 --output-dir exports   # readable chart files
uv run hdh export --format json --limit 100 --output-dir exports   # structured data
uv run hdh export --format fhir --limit 100 --output-dir exports   # FHIR R4 bundles
```

Or serve the panel as a live FHIR R4 API — the standard hospital-integration
interface:

```powershell
uv run hdh serve --port 8000
```

Then open **http://127.0.0.1:8000/docs** in your browser for an interactive
console, or try these URLs directly:

- `http://127.0.0.1:8000/Patient/MRN12345678` — one Patient resource
- `http://127.0.0.1:8000/Patient?name=smith` — search
- `http://127.0.0.1:8000/Patient/MRN12345678/$everything` — the full bundle:
  Encounters, Observations (vitals + labs with LOINC codes), Conditions
  (ICD-10), MedicationRequests

Press `Ctrl+C` in PowerShell to stop the server.

## Part 10 — Simulate scenarios

Two commands make the dataset dynamic — useful for teaching surveillance and
longitudinal care:

```powershell
# Inject a January influenza outbreak (300 extra cases)
uv run hdh add-spike --condition influenza --month 1 --n 300

# Advance the clock 6 months: chronic patients accrue follow-up visits
uv run hdh advance --months 6
```

Re-run `uv run hdh stats` or the care-gap report afterward and watch the
numbers move. To start over from scratch, delete `family_medicine.db` and
generate again — it's synthetic, nothing is ever lost.

## Part 11 — See what the AI did (traces & spending)

Every AI question is recorded — which stages ran, what each one did, how
long it took, and exactly how many tokens (money) it used:

```powershell
uv run hdh trace runs                 # your sessions, with token totals
uv run hdh trace show 3f9c21ab        # one session, step by step (use the id from `runs`)
uv run hdh trace usage --days 7       # daily spending in tokens
```

The step view is worth studying once: you'll see the guardrail check cost a
few hundred tokens, the tool executor cost the most, and the validator's
verdict — the anatomy of a trustworthy AI answer. The daily quota
(500k input / 100k output tokens by default) is enforced from these same
records; when it's exhausted, the assistant politely declines until tomorrow.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `git` or `uv` "not recognized" after install | Close and reopen PowerShell (PATH updates only apply to new windows). If still failing, log out/in of Windows. |
| `winget` not found | Install "App Installer" from the Microsoft Store, or download Git/uv installers from their websites. |
| `uv sync` is slow the first time | Normal — it downloads Python and ~70 packages once. Subsequent runs take seconds. |
| `No valid Anthropic API key` | Run `setx ANTHROPIC_API_KEY "sk-ant-..."`, then **open a new PowerShell window**. Verify with `uv run python scripts/check_env.py`. |
| AI answers seem slow | That's the validation pipeline working (guard → tools → assemble → validate). Use `uv run hdh agent --simple "..."` for a faster, unvalidated answer. |
| `daily ... quota exhausted` | The built-in spending cap. It resets at midnight, or raise it: `setx HDH_QUOTA_INPUT_TOKENS 1000000` (new window afterward). |
| `hdh risk train` refuses (too few positives) | Your panel is too small — generate at least a few thousand patients. |
| Weird characters (`â”€`) in output | Use Windows Terminal or modern PowerShell; the CLI already forces UTF-8, but very old consoles may still struggle. |
| Want a clean slate | Delete `family_medicine.db` (and `artifacts\risk_model.joblib`), then generate + train again. |

**Updating hdh later:**

```powershell
cd $HOME\Documents\hdh
git pull
uv sync --all-extras
```

---

*hdh is an educational project (MIT license, © 2026 Ajmal Mahmood). All data
is synthetic; the AI assistant is a demonstration of validated agentic
architecture, not a clinical decision-support device.*
