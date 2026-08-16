# hdh — Health Data Hub

[![ci](https://github.com/arsalanam/hdh/actions/workflows/ci.yml/badge.svg)](https://github.com/arsalanam/hdh/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**Medically realistic synthetic family-medicine EHR data — plus modular AI care-program tooling on top.**

10,000 patients · 165,000+ visits · 777,000+ lab results · no PHI, ever.

`hdh` generates outpatient (OPD) records driven by age, sex, and seasonal disease
probabilities — as **households**: family structure, hereditary risk derived from
relatives' actual generated conditions, structured allergies, medication lists,
immunizations, procedures, and a stored SOAP note for every visit. Optional
feature modules layer on top: care-gap detection, ML risk stratification, an
agentic AI care assistant with an ICD-10-CM knowledge graph, narratives, and a
FHIR R4 REST API.

📖 **[Feature Guide](docs/FEATURE_GUIDE.md)** · **[Architecture](docs/ARCHITECTURE.md)** · **[User Guides (per module)](docs/guides/README.md)** · **[Practitioner Guide (start here if you're not a developer)](docs/guides/practitioner-guide.md)**

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
pip install -e ".[agent]"     # + agentic AI assistant (Anthropic SDK)
pip install -e ".[api]"       # + FHIR REST API (FastAPI)
pip install -e ".[all]"       # everything (dev tools need uv or pip install pytest ruff mypy)
```

## Quick Start

```bash
# Generate a quick 100-patient panel (full charts make big runs slow —
# download the pre-built 10k database from Releases instead of generating it)
hdh generate --patients 100 --years 2

# Inspect
hdh stats
hdh show --mrn MRN12345678
hdh list-conditions

# Export to JSON / FHIR R4 / plain text
hdh export --format all --limit 500 --output-dir exports/

# Simulate: inject a January flu spike, advance the clock 6 months
hdh add-spike --condition influenza --month 1 --n 300
hdh advance --months 6
```

Feature modules add their own subcommands:

```bash
hdh care-gaps --limit 20                  # overdue preventive care, missed follow-ups
hdh risk train && hdh risk score --top 20 # ML risk stratification
hdh agent                                 # interactive AI chat over the dataset
hdh agent "Which patients need outreach?" # ...or one-shot
hdh narrative --mrn MRN12345678           # SOAP-note narratives
hdh serve --port 8000                     # FHIR R4 REST API
hdh icd load --download && hdh icd codify "broke her left ankle, first visit"
                                          # ICD-10-CM knowledge graph + coding
hdh snomed load --download                # SNOMED CT US Edition (needs a free
                                          # UMLS key — see .env.example; data is
                                          # licensed and never ships with hdh)
hdh snomed search "heart attack"          # synonym-aware concept search
hdh snomed subsumes 64572001 73211009     # is diabetes a kind of disease? True
hdh comprehend --file note.txt            # doctor-note comprehension: free text →
                                          # coded, span-grounded structured record
```

### Ask the agent ontology-grounded questions

With the ICD-10-CM and SNOMED CT catalogs loaded, the agent holds coding
tools from both ontology modules alongside its chart/SQL tools — so
population questions can be answered by **graph semantics instead of
string-matching**, and every cited code is grounded in tool evidence:

```bash
hdh agent "Which patients have a disorder under cerebrovascular disease?"
#   → snomed_descendants sweeps the SNOMED subtree (transitive closure),
#     maps to ICD-10 prefixes, then search_patients finds the cohort

hdh agent "Is atrial fibrillation a kind of heart disease?"
#   → snomed_subsumes answers authoritatively — one closure hit, no guessing

hdh agent "Normalize the mention 'stomach flu' and code it for billing"
#   → snomed_normalize (synonym funnel: FTS → trigram → semantic-tag fit),
#     then icd_codify for the billing view

hdh agent "What are the defining attributes of a mechanical thrombectomy?"
#   → snomed_lookup returns method / procedure-site attribute edges
```

The toolsets are discovered through each module's published API and are
offered only when the catalog is actually loaded — the agent runs fine
without them.

### Chart free-text notes through the agent

With the comprehension module active (SNOMED loaded + `[agent]` extra), a
provider can maintain the chart **by talking to the agent** — paste a
note, get a reconciled chart update back:

```
you> Can you add the following note to the patient chart for patient
     MRN67606524, provided yesterday by Dr. Priya Sharma:
     Patient seen in clinic for evaluation of elevated blood pressure...
     Start Lisinopril 10mg once daily for hypertension. ...

agent> ✅ Essential hypertension (I10) — already on the problem list,
          referenced, not duplicated
       ✅ Lisinopril 10mg once daily — new medication added
       ✅ Vitals BP 152/94, HR 88, Weight 82.5kg — recorded
       ⚠️ "headaches" (SNOMED 25064002) — no billing mapping;
          NOT written, queued for human review
```

Under the hood that is one `apply_note` tool call running the full
pipeline: deterministic segmentation → LLM extraction (finds and types,
**never** codes or asserts) → validation against a closed schema with
verbatim-span grounding → SNOMED/LOINC/drug-catalog normalization →
rules-first assertion (negations like "denies chest pain" are skipped) →
a reconciled chart write with a verdict per entity: `new`, `confirmed`,
`review`, or `skipped`. Review items are **never written silently** — the
agent reports them for human resolution (`hdh comprehend --review`).
Addenda for the same date reconcile into the existing visit instead of
duplicating it, and the pasted text is stored as the visit's note for
full provenance. The same pipeline is scriptable without the agent:

```bash
hdh comprehend --file note.txt                          # print the coded record
hdh comprehend --mrn MRN... --visit-date 2026-08-14 --store   # persist it
hdh comprehend --mrn MRN... --visit-date 2026-08-14 --apply --dry-run
hdh comprehend --review                                 # the human review queue
hdh comprehend --file note.txt --fhir bundle.json       # FHIR document export
```

Design docs: [notes-comprehension-service.md](docs/design/notes-comprehension-service.md)
(the service) and [comprehension-extraction-schema.md](docs/design/comprehension-extraction-schema.md)
(the extraction contract, milestones, eval baseline, and testing plan).
For the bigger picture — why the agent tier is the new EHR UI — read the
[note-comprehension introduction](docs/articles/note-comprehension-agent-ui.md).

### The agent pipeline

One-shot questions run through a production-style **LangGraph pipeline**:
gateway → guardrails (topic guard + daily token quota) → intent analysis →
tool executor → response assembler → response validator. A response is only
streamed after the validator confirms every claim is grounded in tool
evidence; hallucinations send the executor back with feedback (max 3 tries).
`--simple` uses the plain tool-runner loop instead. See
[docs/guides/agent.md](docs/guides/agent.md).

### The agent chat UI

`hdh agent` (no arguments) opens an interactive chat with the care-program
agent. It needs an Anthropic API key: copy `.env.example` to `.env` and fill
it in (`just` loads it automatically; `just check-env` verifies), or set
`ANTHROPIC_API_KEY` machine-wide — see
[docs/guides/agent.md](docs/guides/agent.md) for all options. The
conversation is remembered across questions, answers render as markdown, tool
calls are traced live, and previous questions are recallable with the arrow
keys. Slash commands: `/history` (full chat so far), `/context` (messages +
real token count), `/compact`, `/save`, `/clear`, `/exit`.

**Context management:** once the conversation exceeds 100 messages (tool
round-trips included), the agent automatically summarizes the older turns
into a compact `<conversation_summary>` briefing — preserving MRNs, findings,
and decisions — and keeps the 20 most recent messages verbatim, so context
stays bounded in long sessions. Demo it without waiting 100 messages:

```bash
hdh agent --compact-after 8    # compaction kicks in after ~4 exchanges
```

The compaction logic itself is exercised offline in
`tests/test_agent_chat.py`, which collapses a 120-message conversation to 21.

## Project Structure

```
src/hdh/
├── core/            # Stable synthetic-data engine (no module may be imported here)
│   ├── models.py           # SQLAlchemy ORM: Patient, Visit, Vital, Diagnosis, Rx, Lab
│   ├── conditions.py       # Condition contracts + catalog: frozen profiles, packs, comorbidity webs
│   ├── disease_engine.py   # family-medicine-core pack (32 conditions)
│   ├── cardiometabolic.py  # cardiometabolic pack (7): CKD staged, CAD, HF, AFib, stroke hx
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

The generator writes `family_medicine.db` (SQLite; 10k patients with full
charts). **v0.4.0 is a breaking schema change** — the chart expansion unified
the problem list and added family/medication/note entities; datasets from
earlier versions must be regenerated (or re-download the release asset).
It is **not** checked into git. Either build one (`hdh generate`) or download
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
- Design docs — [notes-comprehension-service.md](docs/design/notes-comprehension-service.md) ·
  [comprehension-extraction-schema.md](docs/design/comprehension-extraction-schema.md) ·
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
