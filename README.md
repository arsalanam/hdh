# hdh — Health Data Hub

[![ci](https://github.com/arsalanam/hdh/actions/workflows/ci.yml/badge.svg)](https://github.com/arsalanam/hdh/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**A working mini-EHR you drive by talking to it — and the synthetic practice
it runs on.**

![hdh — an agent-first EHR: a note goes in, coded chart entries come out, and what cannot be coded is sent to a human](docs/assets/hdh-card-wide.png)

Chart a dictated note. Place the orders it implies. Receive results back from
a lab. Correct what's wrong, with an audit trail. Ask who's overdue. All of it
against 10,000 synthetic patients, so you can break things freely: no PHI,
ever.

`hdh` is two things that need each other. Underneath is a medically realistic
**synthetic family-medicine practice** — households with hereditary risk,
seasonal disease incidence, comorbidity webs, four years of visits, labs and
prescriptions. On top is an **agent-fronted EHR**: the clinical surface is a
conversation, and every write goes through the same reconciliation, coding and
audit machinery the CLI uses.

It is a **proof of concept**, not a product, and not a validated clinical
tool. What it is good for is showing what an EHR looks like when the primary
interface is a model that reads notes and calls tools, and where the honest
answer to an ambiguous mention is a review queue rather than a guess.

📖 **[Clinician's Guide](docs/guides/practitioner-guide.md)** (start here if
you're not a developer) · **[Feature Guide](docs/FEATURE_GUIDE.md)** ·
**[Architecture](docs/ARCHITECTURE.md)** ·
**[Module guides](docs/guides/README.md)**

---

## The clinical loop

This is the part worth looking at first. Every step below is a real command
against a real chart, and each one is also reachable by asking the agent in
plain language.

### 1 · A note becomes a chart

Paste what a clinician dictated. Prose, SOAP, or something in between:

```bash
hdh comprehend --file note.txt --mrn MRN67606524 --apply --dry-run
```

```
  confirmed  condition   'type 2 diabetes' ≡ chart E11.9 — referenced, not duplicated
  updated    condition   'type 2 diabetes': controlled True → False
  new        medication  Metformin ER 2 x 500mg
  new        request     medication: Metformin ER
  new        request     lab: HbA1c
  new        request     Follow-up visit in 90 days
  review     condition   'blorbitis' — no billing mapping; NOT written
```

Seven passes run between the text and those verdicts: deterministic
segmentation → LLM extraction (which **finds and types, never codes or
asserts**) → validation against a closed schema with verbatim-span grounding →
SNOMED / LOINC / RxNorm normalization → rules-first assertion (so *"denies
chest pain"* is skipped) → contextual disambiguation → a reconciled write.

Two rules do most of the work:

- **The LLM classifies; deterministic code decides.** The model never picks a
  code. It says *"this span is a problem, and this span is its dose"*, and the
  terminology modules resolve the rest.
- **Refuse, don't guess.** An entity that can't be resolved confidently goes
  to a review queue and is never charted silently. `hdh comprehend --review`
  is where a human settles it.

Drop `--dry-run` to write it. Same-day notes reconcile into the existing
encounter rather than duplicating it, and the text is stored as the visit's
note, so the chart can always answer *"where did this come from?"*

### 2 · Orders leave the building

A note that says *"repeat HbA1c in 3 months"* creates a **draft** order, not
an active one. Drafts are what a comprehended note is allowed to produce;
releasing is a human act:

```bash
hdh orders list --mrn MRN67606524           # what's outstanding
hdh orders add --mrn MRN67606524 --kind lab --display "Basic metabolic panel"
hdh orders release --mrn MRN67606524 --outbox ./outbox
```

`--outbox` writes a FHIR order bundle for a partner to collect.

### 3 · Results come back

A note can *ask for* a test. It can never *report* one — there's no specimen,
no method, no reference range and no performing lab behind a sentence. Results
arrive from outside, matched against the order that asked for them:

```bash
hdh interchange run --partner mock-lab --outbox ./outbox --inbox ./inbox
hdh interchange import --inbox ./inbox
hdh interchange review                      # results that were refused
```

A result that matches no order isn't filed — it's queued for a human. That
single rule is what keeps a chart's lab table meaning one thing.

### 4 · The chart gets corrected

Charts get things wrong: a mis-transcribed vital, a duplicate encounter, a
diagnosis that turned out to be something else.

```bash
hdh chart history --mrn MRN67606524
hdh chart amend --entity Condition --id 42 --set status=resolved \
                --reason "resolved at follow-up" --dry-run
hdh chart void --visit 2064 --reason "duplicate encounter"
```

Clinical rows are **voided, never deleted** — the row stops appearing in
charts, exports and cohort queries while the audit event keeps its referent.
Every change needs a reason. The agent holds the same three tools and **no
delete tool at all**.

### 5 · The panel gets watched

```bash
hdh care-gaps --limit 20                    # overdue preventive care, missed follow-ups
hdh risk train && hdh risk score --top 20   # ML risk stratification
```

The uncontrolled-chronic gap fires on `chronic AND NOT controlled` — the two
columns a note writes when it says *"well treated hypertension"* or *"h/o type
2 diabetes"*. So charting a note changes what the panel view says the next
morning, which is the point of these being one system rather than two.

---

## The same loop, by talking

```
you> Chart this for MRN67606524, dated today: "68yo returns for chronic
     disease review. BP 128/78. Well treated hypertension, uncontrolled
     type 2 diabetes. Continue lisinopril 10 mg daily. Repeat HbA1c in
     3 months. Refer to ophthalmology."

agent> Charted to visit #2073.
       ✅ Hypertension (I10) — referenced, not duplicated; now flagged controlled
       ✅ Type 2 diabetes — refined to SNOMED 443694000 "Uncontrolled type 2
          diabetes mellitus"; problem list updated
       ✅ Lisinopril 10 mg daily — prescription recorded
       📋 3 draft orders: lab HbA1c (due 2026-11-22), referral to
          ophthalmology, follow-up visit in 90 days
       ✅ Vitals BP 128/78 recorded

you> Anything overdue on this patient?
```

The agent runs through a **LangGraph pipeline** — gateway → topic guard and
token quota → intent classification → tool executor → response assembler →
response validator. A response is only streamed after the validator confirms
every specific claim is grounded in tool evidence; ungrounded claims send the
executor back with feedback (up to 3 tries).

Its tools are the same code paths the CLI uses. That's deliberate: **an agent
tool may not contain a decision a non-agent caller would also need**, so the
agent and the terminal can never disagree about what a chart says.

```bash
hdh agent                                   # interactive chat
hdh agent "Which patients need outreach?"   # one-shot
hdh agent --compact-after 8                 # demo context compaction
```

---

## Run it on PostgreSQL

**Use PostgreSQL.** SQLite works and the test suite runs on it, but the
clinical surface is meaningfully weaker there, and the difference is
mechanical rather than a matter of taste.

Terminology lookup — the step that turns *"SOB"* into a code — is a funnel
with several rungs:

| | PostgreSQL | SQLite |
|---|---|---|
| exact term | ✅ | ✅ |
| abbreviation alias (`SOB - Shortness of breath`) | ✅ | ❌ |
| full-text search (`ts_rank`, stemming, word order) | ✅ | ❌ |
| trigram similarity (typo recovery) | ✅ | ❌ |
| | | substring `LIKE` only |

On SQLite, *"SOB"* never reaches **Dyspnea**, a misspelt drug name never
recovers, and *"diabetes mellitus type 2"* misses *"Type 2 diabetes mellitus"*
because substring matching can't reorder words. SNOMED CT, LOINC and RxNorm
are also large enough that you want a real server holding them.

```bash
just deps                                   # PostgreSQL 16 on port 5433
export HDH_DB_URL="postgresql+psycopg://hdh:hdh@localhost:5433/hdh"
just db-upgrade                             # apply migrations
hdh generate --patients 100 --years 2       # or migrate an existing SQLite file
```

Every command accepts `--db` if you'd rather be explicit:

```bash
hdh --db postgresql+psycopg://hdh:hdh@localhost:5433/hdh care-gaps
```

---

## Install

With [uv](https://docs.astral.sh/uv/) (recommended — creates `.venv` from the
committed `uv.lock`, so everyone gets identical dependency versions):

```bash
uv sync --all-extras          # everything + dev tools
uv run hdh stats              # run commands through the managed venv
```

Or with pip:

```bash
pip install -e .              # core: generation, exports, CLI
pip install -e ".[risk]"      # + ML risk stratification (scikit-learn)
pip install -e ".[agent]"     # + the agent, comprehension, chart maintenance
pip install -e ".[api]"       # + FHIR REST API (FastAPI)
pip install -e ".[all]"       # everything
```

The agent needs an Anthropic API key: copy `.env.example` to `.env` and fill
it in (`just` loads it automatically; `just check-env` verifies).

---

## The terminologies

The clinical surface is only as good as its vocabularies, and each one has a
job the others can't do:

| Vocabulary | Answers | Loaded with |
|---|---|---|
| **SNOMED CT** | what the clinician asserted | `hdh snomed load --download` |
| **ICD-10-CM** | what it bills as | `hdh icd load --download` |
| **LOINC** | what test was ordered | `hdh loinc load --source <dir>` |
| **RxNorm** | what drug was prescribed | `hdh rxnorm load --source <dir>` |

The line between them is a design rule, not a preference: **a note asserts;
only a partner reports.** A note names a medication (RxNorm), orders a lab
(LOINC), and asserts clinical facts (SNOMED). A lab *value* mentioned in prose
is evidence about a condition — it never becomes a lab result.

```bash
hdh snomed search "heart attack"            # synonym-aware concept search
hdh snomed subsumes 64572001 73211009       # is diabetes a kind of disease? True
hdh icd codify "broke her left ankle, first visit"
hdh rxnorm code "Metformin ER 500mg"        # deepest level the evidence supports
hdh loinc search "hba1c"
```

All four are licensed or large; **loaders ship, data never does.** ICD-10-CM
and SNOMED CT download themselves (SNOMED needs a free
[UMLS key](https://uts.nlm.nih.gov/uts/signup-login) in `.env`). LOINC and
RxNorm releases are fetched by hand from their own sites and pointed at with
`--source`. Release builds are gated by `just release-check`, so a licensed
catalog can't leave the building.

With catalogs loaded, the agent answers population questions by **graph
semantics instead of string-matching**:

```bash
hdh agent "Which patients have a disorder under cerebrovascular disease?"
#   → snomed_descendants sweeps the subtree, maps to ICD-10, finds the cohort
hdh agent "Is atrial fibrillation a kind of heart disease?"
#   → snomed_subsumes answers from the closure table — no guessing
```

---

## Where the patients come from

The substrate: `hdh` generates outpatient records driven by age, sex and
seasonal disease probability — as **households**, so hereditary risk is
derived from relatives' actual generated conditions rather than sampled
independently.

```bash
hdh generate --patients 100 --years 2       # a small practice to explore
hdh stats
hdh show --mrn MRN12345678
hdh export --format all --limit 500 --output-dir exports/
hdh add-spike --condition influenza --month 1 --n 300   # inject a flu outbreak
hdh advance --months 6                      # move the clock; chronic patients accrue visits
hdh serve --port 8000                       # FHIR R4 REST API
```

10,000 patients · 165,000+ visits · 777,000+ lab results. Full charts make
big runs slow — download the pre-built 10k database from the
[latest release](https://github.com/arsalanam/hdh/releases/latest) instead of
generating it. `--seed N` makes any run byte-for-byte reproducible.


---

## Project Structure

```
src/hdh/
├── core/            # Stable synthetic-data engine (no module may be imported here)
│   ├── models.py           # SQLAlchemy ORM: Patient, Visit, Vital, Diagnosis, Rx, Lab
│   ├── conditions.py       # Condition contracts + catalog: frozen profiles, packs, comorbidity webs
│   ├── disease_engine.py   # family-medicine-core pack (32 conditions)
│   ├── cardiometabolic.py  # cardiometabolic pack (7): CKD staged, CAD, HF, AFib, stroke hx
│   ├── chartedit/          # The one sanctioned chart-mutation path + audit trail
│   ├── generators.py       # Patient & visit-history generators (Faker-based)
│   └── exporters.py        # JSON, FHIR R4 Bundle, plain-text chart exporters
├── modules/         # Optional feature modules (each depends only on core)
│   ├── caregaps/           # Rule-based care-gap detection
│   ├── risk/               # ML risk stratification (features + model + tiers)
│   ├── agent/              # Agentic AI care assistant (Claude tool-use loop)
│   ├── narrative/          # SOAP-note narrative generation
│   ├── fhir_api/           # FHIR R4 REST API (FastAPI)
│   ├── icd10cm/            # ICD-10-CM knowledge graph: loader, retrieval funnel, agent tools
│   ├── snomed/             # SNOMED CT US Edition: RF2 loader, closure DAG, normalize() funnel, agent tools
│   ├── ontology/           # SNOMED tagging demo (schema-registry extension)
│   ├── comprehension/      # Doctor-note comprehension: free text → coded chart update
│   └── billing/            # CPT / RVU / claims simulation (scaffold)
└── cli.py           # `hdh` CLI — core commands + auto-discovered module subcommands
```

**Design rule:** `hdh.core` never imports from `hdh.modules`. Modules depend on
the core (and optional extras), never on each other's internals. Each module
exposes `register_cli(subparsers)` to add its subcommands.

## Disease Coverage (39 conditions in two packs)

**family-medicine-core** (32):
- **Pediatric (0–12):** otitis media, RSV, febrile illness, strep, conjunctivitis, eczema, well-child visits
- **Adolescent (13–17):** acne, sports physicals, sports injuries, mono, anxiety
- **Young adult (18–35):** annual physicals, influenza, URI, UTI, anxiety, low back pain, lacerations, contraception
- **Adult (36–65):** hypertension, type 2 diabetes, hyperlipidemia, GERD, osteoarthritis, obesity, hypothyroidism, COPD, depression
- **Senior (65+):** annual wellness, polypharmacy review, falls, COPD exacerbation, depression, hypothyroidism

**cardiometabolic** (7): CKD (staged 3a→5), coronary artery disease, chronic
heart failure, atrial fibrillation (anticoagulated), stroke/TIA history,
asthma, iron-deficiency anemia — every profile authored with ICD-10 **and**
SNOMED CT codes.

Incidence is seasonally weighted (flu peaks in winter, UTIs and sports
injuries in summer). Chronic disease arrives in two phases: baseline
seeding from age, family history, smoking, and BMI — then **annual onset
rolls through comorbidity webs** (hypertension ×3 and diabetes ×3 drive
CKD; CAD ×4 drives heart failure; AFib ×4 drives stroke), so onset dates
read in clinical order. Staged conditions re-evaluate severity on a
configurable cadence (`--progression-cadence yearly|quarterly`), and
`--seed` makes any run byte-for-byte reproducible. Condition packs are
pluggable (`ConditionSource` protocol — see
[docs/design/clinical-breadth.md](docs/design/clinical-breadth.md)).

## The Database

PostgreSQL is the recommended backend (see above — the terminology funnel
loses three of its four rungs on SQLite). The generator's default is
`family_medicine.db`, a SQLite file, which is convenient for a first look and
for the test suite; `hdh migrate` copies one into PostgreSQL when you outgrow
it.

**v0.4.0 is a breaking schema change** — the chart expansion unified the
problem list and added family/medication/note entities; datasets from earlier
versions must be regenerated (or re-download the release asset). The database
is **not** checked into git. Either build one (`hdh generate`) or download
the pre-built 10,000-patient database from the
[latest release](https://github.com/arsalanam/hdh/releases/latest)
(`family_medicine-10k.zip`, ~28 MB — unzip into the project folder). The
release copy is already SNOMED-tagged via `hdh ontology tag`. (Tagged
concept IDs are fine to ship; the licensed SNOMED CT *catalog* never is —
release builds are gated by `just release-check`, see CONTRIBUTING.)

## Documentation

- [Feature Guide](docs/FEATURE_GUIDE.md) — every feature with examples
- [Architecture](docs/ARCHITECTURE.md) — the core/modules design and its rules
- [User guides](docs/guides/README.md) — one practical guide per module:
  [core](docs/guides/core.md) · [care-gaps](docs/guides/caregaps.md) ·
  [risk](docs/guides/risk.md) · [agent](docs/guides/agent.md) ·
  [narrative](docs/guides/narrative.md) · [FHIR API](docs/guides/fhir-api.md) ·
  [snomed](docs/guides/snomed.md) ·
  [ontology](docs/guides/ontology.md) · [billing](docs/guides/billing.md)
- Chart maintenance — [chart-maintenance.md](docs/design/chart-maintenance.md)
  (symptom billing coverage + amend/void with an audit trail)
- Design docs — [notes-comprehension-service.md](docs/design/notes-comprehension-service.md) ·
  [comprehension-extraction-schema.md](docs/design/comprehension-extraction-schema.md) ·
  [service-requests-and-interchange.md](docs/design/service-requests-and-interchange.md) ·
  [rxnorm-and-terminology-boundaries.md](docs/design/rxnorm-and-terminology-boundaries.md) ·
  [snomed-module.md](docs/design/snomed-module.md) ·
  [icd10cm-ontology-module.md](docs/design/icd10cm-ontology-module.md) ·
  [clinical-breadth.md](docs/design/clinical-breadth.md) ·
  [fhir-emitters.md](docs/design/fhir-emitters.md) ·
  [care-plan-module.md](docs/design/care-plan-module.md)
- [Note comprehension introduction](docs/articles/note-comprehension-agent-ui.md) —
  what it is, using it via the agent, and the roadmap toward an
  agent-driven EHR

## Build pipeline

The project uses [`just`](https://github.com/casey/just) as its command runner
and [uv](https://docs.astral.sh/uv/) for dependency management — recipes run
through `uv run`, so they work identically on every platform with no venv
path juggling, and the Docker image installs exactly what `uv.lock` pins.
`just build` produces the image **only after every quality gate passes**:

```bash
just              # list all recipes
just test         # unit tests
just coverage     # tests + coverage report (HTML in htmlcov/)
just lint         # ruff linting
just format-check # ruff formatting (just format to apply)
just typecheck    # mypy
just quality      # design-quality gate (see below)
just security     # security scan (trivy/pip-audit if installed; OWASP slot)
just qa           # all of the above, in order
just build        # qa → docker build -t hdh:latest
```

### The design-quality gate

Beyond style (ruff) and types (mypy), `scripts/quality_gate.py` enforces the
design principles this project is meant to teach — each finding names the
principle it violates:

| Check | Principle |
|---|---|
| `contracts` | Clear contracts: public classes and non-trivial functions state what they promise (docstrings) |
| `no-god-class` | Clear responsibilities: size limits on classes, functions, and parameter lists |
| `pluggability` | Pluggable code: every CLI module complies with the `register_cli` interface |
| `dependency-injection` | Collaborators (DB sessions, API clients) are injected; only composition roots construct them |
| `immutability` | No mutable default arguments; constants prefer tuples over lists |
| `injection-safety` | No `eval`/`exec`, no `shell=True`, no string-built SQL reaching `text()`/`execute()` |
| `data-abstraction` | Public APIs return typed structures (dataclasses), not bare dicts |

Errors fail the build; warnings are advisory. A justified exception is waived
inline with `# quality: allow(<check>)` plus a comment — visible in code
review, never hidden in config. The checker itself demonstrates the
principles: checks are pluggable implementations of a `QualityCheck`
protocol, findings are frozen dataclasses, and each check receives its parsed
module by injection.

Container usage:

```bash
just docker-generate 1000   # generate a dataset into ./data
just docker-serve           # FHIR API on :8000 against ./data
docker run --rm -v "$PWD/data:/data" hdh:latest stats
```

The `security` recipe (`scripts/security_scan.py`) audits the locked
dependency set for known CVEs with pip-audit on every run, adds a trivy scan
when trivy is installed, and is the extension point for OWASP Dependency-Check
or a ZAP baseline scan.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run `just qa` before submitting.

## License & attribution

Copyright © 2026 **Ajmal Mahmood**. Released under the [MIT License](LICENSE).

You are free to use, modify, and redistribute this project — including
commercially and in closed-source derivatives. The one condition MIT imposes
is that the original copyright notice and permission notice stay included in
all copies or substantial portions of the software: keep the LICENSE file
(or its text) with your copies and derivatives, and the original attribution
is preserved.
