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
# Generate 10,000 patients (4 years of history) — or use a pre-built family_medicine.db
hdh generate --patients 10000 --years 4

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
```

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
│   ├── disease_engine.py   # 30+ ConditionProfiles: ICD-10, vitals deltas, labs, formularies
│   ├── generators.py       # Patient & visit-history generators (Faker-based)
│   └── exporters.py        # JSON, FHIR R4 Bundle, plain-text chart exporters
├── modules/         # Optional feature modules (each depends only on core)
│   ├── caregaps/           # Rule-based care-gap detection
│   ├── risk/               # ML risk stratification (features + model + tiers)
│   ├── agent/              # Agentic AI care assistant (Claude tool-use loop)
│   ├── narrative/          # SOAP-note narrative generation
│   ├── fhir_api/           # FHIR R4 REST API (FastAPI)
│   ├── icd10cm/            # ICD-10-CM knowledge graph: loader, retrieval funnel, agent tools
│   ├── ontology/           # SNOMED CT mapping (scaffold)
│   └── billing/            # CPT / RVU / claims simulation (scaffold)
└── cli.py           # `hdh` CLI — core commands + auto-discovered module subcommands
```

**Design rule:** `hdh.core` never imports from `hdh.modules`. Modules depend on
the core (and optional extras), never on each other's internals. Each module
exposes `register_cli(subparsers)` to add its subcommands.

## Disease Coverage (30+ conditions)

- **Pediatric (0–12):** otitis media, RSV, febrile illness, strep, conjunctivitis, eczema, well-child visits
- **Adolescent (13–17):** acne, sports physicals, sports injuries, mono, anxiety
- **Young adult (18–35):** annual physicals, influenza, URI, UTI, anxiety, low back pain, lacerations, contraception
- **Adult (36–65):** hypertension, type 2 diabetes, hyperlipidemia, GERD, osteoarthritis, obesity, hypothyroidism, COPD, depression
- **Senior (65+):** annual wellness, polypharmacy review, falls, COPD exacerbation, depression, hypothyroidism

Incidence is seasonally weighted (flu peaks in winter, UTIs and sports injuries in
summer) and comorbidities are seeded from age, family history, smoking, and BMI.

## The Database

The generator writes `family_medicine.db` (SQLite; 10k patients with full
charts). **v0.4.0 is a breaking schema change** — the chart expansion unified
the problem list and added family/medication/note entities; datasets from
earlier versions must be regenerated (or re-download the release asset).
It is **not** checked into git. Either build one (`hdh generate`) or download
the pre-built 10,000-patient database from the
[latest release](https://github.com/arsalanam/hdh/releases/latest)
(`family_medicine-10k.zip`, ~28 MB — unzip into the project folder). The
release copy is already SNOMED-tagged via `hdh ontology tag`.

## Documentation

- [Feature Guide](docs/FEATURE_GUIDE.md) — every feature with examples
- [Architecture](docs/ARCHITECTURE.md) — the core/modules design and its rules
- [User guides](docs/guides/README.md) — one practical guide per module:
  [core](docs/guides/core.md) · [care-gaps](docs/guides/caregaps.md) ·
  [risk](docs/guides/risk.md) · [agent](docs/guides/agent.md) ·
  [narrative](docs/guides/narrative.md) · [FHIR API](docs/guides/fhir-api.md) ·
  [ontology](docs/guides/ontology.md) · [billing](docs/guides/billing.md)

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
