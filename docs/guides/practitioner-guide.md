# hdh for Clinicians — Running a Mini-EHR on Your Own Machine (Windows)

A step-by-step guide for clinicians, care managers, quality and
population-health staff, and educators. You need basic PowerShell comfort
(opening a terminal, typing commands) — no programming experience.

**What you get:** a small but complete electronic health record running on
your laptop, stocked with a fully synthetic family-medicine practice. You can
dictate a note and watch it become coded chart entries, place lab and
medication orders, receive results back from a simulated lab, correct
mistakes with an audit trail, and ask an AI assistant who on your panel is
overdue — and you can do most of it by typing English rather than commands.

> ⚠️ **Read this first**
> - Every patient in hdh is **synthetic**. No real person's data is involved,
>   which is exactly why you can experiment freely.
> - **Never type real patient information into the AI assistant.** Questions
>   you ask it are sent to a cloud AI service (Anthropic). Synthetic MRNs and
>   names from this dataset are fine; real PHI is not.
> - Nothing here is medical advice or a validated clinical tool, and hdh is a
>   **proof of concept**, not a product. It is an educational sandbox for
>   learning how these systems work — and for seeing where they should refuse
>   to guess.

---

## Contents

**Setting up**

- [Part 1 — Install the prerequisites](#part-1--install-the-prerequisites)
- [Part 2 — Get hdh](#part-2--get-hdh)
- [Part 3 — Start the database](#part-3--start-the-database)
- [Part 4 — Create your practice](#part-4--create-your-practice)
- [Part 5 — Load the clinical vocabularies](#part-5--load-the-clinical-vocabularies)

**The clinical day**

- [Part 6 — Open a chart](#part-6--open-a-chart)
- [Part 7 — Chart a note](#part-7--chart-a-note)
- [Part 8 — Orders: labs, drugs, referrals](#part-8--orders-labs-drugs-referrals)
- [Part 9 — Results come back](#part-9--results-come-back)
- [Part 10 — The review queue](#part-10--the-review-queue)
- [Part 11 — Correcting the chart](#part-11--correcting-the-chart)

**Doing it all by talking**

- [Part 12 — The AI assistant](#part-12--the-ai-assistant)

**Looking at the whole panel**

- [Part 13 — Care gaps](#part-13--care-gaps)
- [Part 14 — Risk stratification](#part-14--risk-stratification)
- [Part 15 — Narratives, exports and FHIR](#part-15--narratives-exports-and-fhir)
- [Part 16 — Simulate scenarios](#part-16--simulate-scenarios)
- [Part 17 — See what the AI did](#part-17--see-what-the-ai-did)
- [Troubleshooting](#troubleshooting)

---

## Part 1 — Install the prerequisites

Open **PowerShell** (Start menu → type "PowerShell" → Enter) and install
three tools. `winget` comes with Windows 10/11.

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

- **Git** downloads the project (and keeps it updatable).
- **uv** manages Python for you — you do *not* need to install Python
  yourself; uv fetches the right version automatically.

Then install **Docker Desktop** (free) from
<https://www.docker.com/products/docker-desktop/>. On Windows, accept its
suggestion to use WSL 2. Docker runs the database — see
[Part 3](#part-3--start-the-database) for why that matters.

**Close PowerShell and open a new window** so the tools are on your PATH.
Verify:

```powershell
git --version
uv --version
docker --version
```

All three should print version numbers. If not, see
[Troubleshooting](#troubleshooting).

## Part 2 — Get hdh

```powershell
cd $HOME\Documents
git clone https://github.com/arsalanam/hdh.git
cd hdh
uv sync --all-extras
```

`uv sync` creates a private environment inside the project folder and
installs everything (a few minutes the first time). Check it:

```powershell
uv run hdh --help
```

You should see the command list: `generate, stats, show, chart, orders,
comprehend, interchange, care-gaps, risk, agent`, and more.

> **The `uv run hdh ...` pattern:** every hdh command in this guide is
> prefixed with `uv run`. That runs it inside the project's own environment —
> nothing is installed system-wide, and nothing else on your machine is
> affected. Always run these commands from the `hdh` folder.

## Part 3 — Start the database

hdh can keep everything in a single file, and for a first look that is fine.
**For the clinical features, use PostgreSQL.** This is not a preference about
tidiness — the difference shows up directly in what the system can understand.

When a note says **"SOB"**, hdh has to turn that into a code. It tries a
ladder of increasingly forgiving strategies:

| How it looks | PostgreSQL | Single file |
|---|---|---|
| the exact term | ✅ | ✅ |
| an abbreviation the terminology spells out (`SOB - Shortness of breath`) | ✅ | ❌ |
| full-text search — word order, plurals, stemming | ✅ | ❌ |
| fuzzy match, for typos | ✅ | ❌ |

Only the first rung survives without PostgreSQL. In practice that means
**"SOB" never reaches *Dyspnea***, a misspelt drug name is simply lost, and
*"diabetes mellitus type 2"* fails to match *"Type 2 diabetes mellitus"*
because it can only look for one string inside another and cannot reorder
words. The clinical vocabularies are also large enough that a real database
server handles them far better.

Start it (the first run downloads PostgreSQL, roughly 30 seconds):

```powershell
just deps
```

Open the `.env` file in the project folder and remove the `#` in front of the
`HDH_DB_URL=` line. Then apply the database structure:

```powershell
just db-upgrade
```

`just deps-down` stops the database later without losing anything.

> **Already have data in a file?** `uv run hdh migrate` copies an existing
> `family_medicine.db` into PostgreSQL. Your file is not modified.

## Part 4 — Create your practice

```powershell
uv run hdh generate --patients 100 --years 2
```

100 patients is plenty to explore. The full 10,000-patient practice takes a
long time to build — download it ready-made from the project's
[Releases page](https://github.com/arsalanam/hdh/releases/latest) instead.

What you get is not a spreadsheet of random rows. Patients arrive in
**households**, so a patient's family history is derived from conditions
their relatives actually have. Disease incidence is seasonal — influenza
peaks in winter, sports injuries in summer. Chronic disease accumulates
through comorbidity webs, so hypertension and diabetes drive chronic kidney
disease, and onset dates read in clinical order rather than at random.

```powershell
uv run hdh stats
```

## Part 5 — Load the clinical vocabularies

hdh understands a note by mapping what it says onto standard clinical
terminologies. Each one answers a different question:

| Vocabulary | Answers | Needed for |
|---|---|---|
| **SNOMED CT** | what the clinician asserted | charting notes — **the important one** |
| **ICD-10-CM** | what it bills as | putting a diagnosis on the problem list |
| **LOINC** | what test was ordered | coded lab orders |
| **RxNorm** | what drug was prescribed | coded prescriptions |

**Without SNOMED CT loaded, note charting will not do anything useful** —
every clinical mention will fail to resolve and land in the review queue. Load
it and ICD-10-CM at minimum:

```powershell
uv run hdh icd load --download
uv run hdh snomed load --download
```

SNOMED CT is licensed and free for most countries, but you need an account.
Register for a free [UMLS key](https://uts.nlm.nih.gov/uts/signup-login), then
put it in `.env` as `UMLS_API_KEY=...`. The download is about 1.6 GB and is
cached, so reloading never re-downloads.

LOINC and RxNorm are optional and are fetched by hand from their own sites,
then pointed at:

```powershell
uv run hdh loinc load --source C:\path\to\loinc-release
uv run hdh rxnorm load --source C:\path\to\rxnorm-release
```

> **The data never ships with hdh.** Only the loaders do. That is a licensing
> requirement, and release builds are automatically checked to make sure no
> licensed catalog escapes.

---

## Part 6 — Open a chart

```powershell
uv run hdh show --mrn MRN12345678
```

Pick an MRN from `uv run hdh list-conditions` or search:

```powershell
uv run hdh agent "find me a patient with type 2 diabetes and hypertension"
```

The chart prints as plain text: demographics, the problem list, medications,
allergies, immunizations, and every visit with its vitals, diagnoses,
prescriptions, labs and note.

## Part 7 — Chart a note

This is the centre of the system. Put a note in a text file — prose, SOAP, or
anything in between:

```powershell
notepad note.txt
```

```
68yo returns for chronic disease review. Reports good adherence, no chest
pain or shortness of breath. BP 128/78. Well treated hypertension.
Uncontrolled type 2 diabetes mellitus. Continue lisinopril 10 mg daily.
Repeat HbA1c in 3 months. Refer to ophthalmology.
```

**Always look before you write.** `--dry-run` computes everything and changes
nothing:

```powershell
uv run hdh comprehend --file note.txt --mrn MRN12345678 --apply --dry-run
```

```
  confirmed  condition   'hypertension' ≡ chart I10 — referenced, not duplicated
  updated    condition   'hypertension': controlled False → True
  new        condition   'type 2 diabetes mellitus' → E11.9 / snomed 443694000
  new        medication  Lisinopril 10 mg
  new        vitals      bp_diastolic, bp_systolic
  new        request     medication: lisinopril
  new        request     lab: HbA1c
  new        request     referral: ophthalmology
  new        request     Follow-up visit in 90 days
```

Read that list as a set of decisions the system is asking you to approve:

- **`confirmed`** — the patient already has this problem. It is referenced,
  not duplicated. Charting the same note twice will not give them
  hypertension twice.
- **`updated`** — something changed about a problem already on the list. Here
  the note said *"well treated"*, so hypertension is now flagged controlled,
  which is what care-gap rules read.
- **`new`** — a chart row will be created. Notice type 2 diabetes was coded to
  SNOMED **443694000, *Uncontrolled* type 2 diabetes mellitus** — the note
  said "uncontrolled", and the terminology has a concept for exactly that.
- **`review`** — something could not be resolved confidently. It is **not**
  written. See [Part 10](#part-10--the-review-queue).
- **`skipped`** — deliberately not charted. Negated findings land here: *"no
  chest pain"* means the patient does not have chest pain, so nothing goes on
  the problem list.

Happy with it? Run it again without `--dry-run`:

```powershell
uv run hdh comprehend --file note.txt --mrn MRN12345678 --apply
```

### What it will not do

Three refusals are worth knowing, because they are deliberate and you will
meet them:

**It never invents a lab result.** *"Came in with an HbA1c over 7"* does not
create a lab result, no matter how clearly it is written. There is no
specimen, no method, no reference range and no performing lab behind a
sentence — filing it would put two kinds of row in the lab table that look
identical and are not, one produced by an instrument and one by a
recollection. A value mentioned in prose is evidence about the patient's
*condition*. Results arrive the way [Part 9](#part-9--results-come-back)
describes.

**It never guesses a drug strength.** *"Start metformin"* codes to the
ingredient, not to a 500 mg tablet. A drug is exactly where a confident guess
does the most harm.

**It never charts what it cannot code.** An unresolvable problem goes to the
review queue rather than onto the chart with an approximate code.

### Where entries came from

Every row a note creates is recorded as having arrived that way, and the
note's text is stored on the visit:

```powershell
uv run hdh chart history --mrn MRN12345678
```

## Part 8 — Orders: labs, drugs, referrals

Notice that the note in Part 7 produced **requests** as well as chart rows.
*"Repeat HbA1c in 3 months"* became a lab order with a due date; *"refer to
ophthalmology"* became a referral.

Those arrive as **drafts**. A comprehended note can propose an order; it
cannot send one. Releasing is a human act.

```powershell
uv run hdh orders list --mrn MRN12345678
```

Add one directly:

```powershell
uv run hdh orders add --mrn MRN12345678 --kind lab --display "Basic metabolic panel"
uv run hdh orders add --mrn MRN12345678 --kind medication --display "Amlodipine" `
                      --sig "Amlodipine 5 mg once daily" --route PO
```

`--kind` is one of `medication`, `lab`, `referral`, `procedure`,
`follow_up`. Then release them, writing an order bundle for a lab to collect:

```powershell
uv run hdh orders release --mrn MRN12345678 --outbox .\outbox
```

## Part 9 — Results come back

hdh ships a simulated lab partner so you can see the full round trip. It
reads the outbox, produces results appropriate to the patient's conditions,
and writes them to an inbox:

```powershell
uv run hdh interchange run --partner mock-lab --outbox .\outbox --inbox .\inbox
uv run hdh interchange import --inbox .\inbox
```

Now look at the chart again — the results are on the visit, attached to the
order that asked for them.

**A result that matches no order is not filed.** It goes to a separate queue
instead:

```powershell
uv run hdh interchange review
```

That single rule is what keeps the lab table meaning one thing. A result
nobody ordered is either a mis-routed message or a patient mix-up, and both
deserve a human rather than a silent insert.

## Part 10 — The review queue

Everything the system refused to chart is waiting here:

```powershell
uv run hdh comprehend --review
```

Each entry shows the note, the mention, and why it stopped. Usually it is one
of: the term did not resolve confidently, or it resolved but has no ICD-10
billing mapping.

You resolve it by supplying what was missing:

```powershell
uv run hdh comprehend --review --resolve 12 --decision accept --icd10 R51.9
uv run hdh comprehend --review --resolve 13 --decision reject
```

An accepted item is charted and recorded as having been approved by a human.

> This queue is the system working, not failing. The alternative to a review
> queue is a chart full of confident approximations, which is worse and much
> harder to notice.

## Part 11 — Correcting the chart

A vital typed wrong, a duplicated encounter, a diagnosis that turned out to be
something else:

```powershell
uv run hdh chart history --mrn MRN12345678

uv run hdh chart amend --entity Condition --id 42 --set status=resolved `
                       --reason "resolved at follow-up" --dry-run

uv run hdh chart void --visit 2064 --reason "duplicate encounter"
```

Two rules:

- **A reason is required.** There is no way to change a chart row without
  saying why.
- **Clinical rows are voided, never deleted.** A voided row stops appearing in
  the chart, in exports and in cohort queries — but the audit trail keeps
  pointing at it, so the record of the correction survives the correction.

---

## Part 12 — The AI assistant

Everything above can be done by asking.

### One-time setup: an API key

The assistant uses Anthropic's Claude. Get a key at
<https://console.anthropic.com> (paid, but questions cost fractions of a
cent), then put it in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Check it without printing the key:

```powershell
just check-env
```

### Charting by talking

```powershell
uv run hdh agent
```

```
you> Chart this note for MRN12345678, dated today: 68yo returns for chronic
     disease review. BP 128/78. Well treated hypertension, uncontrolled type
     2 diabetes. Continue lisinopril 10 mg daily. Repeat HbA1c in 3 months.

agent> Charted to visit #2073.
       ✅ Hypertension (I10) — referenced, not duplicated; now flagged controlled
       ✅ Type 2 diabetes — coded to SNOMED 443694000 "Uncontrolled type 2
          diabetes mellitus"
       ✅ Lisinopril 10 mg daily — prescription recorded
       📋 2 draft orders: lab HbA1c (due 2026-11-22), follow-up in 90 days

you> What's overdue for this patient?
you> Amend that BP — it was 138/78, I mistyped it
you> Which of my diabetics haven't had an HbA1c in a year?
```

The assistant holds the same tools the commands above use, so a correction
made by talking and one made at the terminal land in the same audit trail
with the same shape. It has amend and void tools and **no delete tool at
all**.

### Questions about the panel

```powershell
uv run hdh agent "Which patients need outreach?"
uv run hdh agent "How many patients have uncontrolled diabetes and no follow-up?"
uv run hdh agent "Is atrial fibrillation a kind of heart disease?"
```

With the vocabularies loaded, that last kind of question is answered from the
terminology's own hierarchy rather than by matching words — so *"disorders
under cerebrovascular disease"* finds the whole subtree, including conditions
whose names share nothing.

### What keeps it honest

Answers go through a validator before you see them. Every specific claim —
MRNs, counts, values, dates — must be traceable to something a tool actually
returned; unsupported claims send the assistant back to look again. That is
also why it sometimes says it does not know: it would rather stop than
produce a number that reads well.

### Conversation handling

The chat remembers context across questions. Arrow keys recall previous
questions. Slash commands: `/history`, `/context`, `/compact`, `/save`,
`/clear`, `/exit`. Long conversations are summarized automatically so they
stay affordable.

---

## Part 13 — Care gaps

```powershell
uv run hdh care-gaps --limit 20
uv run hdh care-gaps --mrn MRN12345678
```

Care gaps look for overdue preventive visits, chronic conditions that are
uncontrolled with no follow-up booked, missed follow-ups, and senior
polypharmacy — ranked by severity.

This connects back to Part 7. The uncontrolled-chronic gap looks for problems
that are **both** on the chronic problem list **and** flagged not-controlled —
and both of those columns are written by charting a note. Saying *"well
treated hypertension"* clears the flag; saying *"h/o type 2 diabetes"* puts
the problem on the chronic list in the first place.

So the note you dictate in the morning changes who appears on this list in the
afternoon. That is the point of them being one system rather than two.

## Part 14 — Risk stratification

```powershell
uv run hdh risk train
uv run hdh risk score --top 20
uv run hdh risk score --mrn MRN12345678
```

A machine-learning model estimates each patient's probability of an urgent
visit or a critical lab within 180 days, from patterns in the chart. Training
takes a minute or two and only needs doing once per dataset.

Treat the output as a worked example of how such models are built and
evaluated — it is trained on synthetic data and means nothing clinically.

## Part 15 — Narratives, exports and FHIR

```powershell
uv run hdh narrative --mrn MRN12345678         # SOAP narratives from the chart
uv run hdh export --format all --limit 500 --output-dir exports\
uv run hdh serve --port 8000                   # FHIR R4 REST API
```

With the API running, open <http://localhost:8000/docs> for an interactive
browser. FHIR is the standard other health systems speak, so this is how hdh
data would reach one.

## Part 16 — Simulate scenarios

```powershell
uv run hdh add-spike --condition influenza --month 1 --n 300
uv run hdh advance --months 6
```

The first injects a January influenza outbreak. The second moves the clock
forward, so chronic patients accrue follow-up visits and labs. Re-run care
gaps afterwards and watch what changes.

## Part 17 — See what the AI did

```powershell
uv run hdh trace runs
uv run hdh trace show <id>
uv run hdh trace usage
```

Every assistant run is recorded: which tools it called, what they returned,
how many tokens it used and what that cost. `trace usage` reports daily token spend. `trace show` is the honest answer
to *"why did it say that?"* — you can read the evidence it actually had.

---

## Troubleshooting

**`git`, `uv` or `docker` is not recognized** — close PowerShell and open a
new window after installing. The PATH only updates for new terminals.

**`just` is not recognized** — install it with `winget install --id Casey.Just -e`,
or use the underlying `uv run ...` commands directly.

**Docker won't start** — Docker Desktop must be running (whale icon in the
system tray) before `just deps`. On Windows it needs WSL 2, which its
installer offers to set up.

**`just deps` fails on a port** — something else is using PostgreSQL's port.
hdh uses 5433 specifically to avoid the usual 5432 conflict; if 5433 is also
taken, change it in `docker-compose.yml` and in `.env`.

**Charting a note produces only review items** — SNOMED CT is not loaded. See
[Part 5](#part-5--load-the-clinical-vocabularies).

**"SOB" or a misspelling doesn't resolve** — you are on the single-file
database. See [Part 3](#part-3--start-the-database).

**The assistant says the API key is missing** — check `.env` is in the `hdh`
folder itself, the line reads `ANTHROPIC_API_KEY=sk-ant-...` with no quotes
and no spaces around `=`, then run `just check-env`.

**A command says no patients exist** — you are pointed at an empty database.
If you switched to PostgreSQL after generating, run `uv run hdh migrate` to
copy your data across.

**Something else** — open an issue at
<https://github.com/arsalanam/hdh/issues> with the command you ran and what
it printed.
