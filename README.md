# hdh — Health Data Hub

**Medically realistic synthetic family-medicine EHR data — plus modular AI care-program tooling on top.**

10,000 patients · 165,000+ visits · 777,000+ lab results · no PHI, ever.

`hdh` generates outpatient (OPD) records driven by age, sex, and seasonal disease
probabilities, and layers optional feature modules on top of that core: care-gap
detection, ML risk stratification, an agentic AI care assistant, SOAP-note
narratives, and a FHIR R4 REST API.

📖 **[Full Feature Guide](docs/FEATURE_GUIDE.md)** · **[Architecture](docs/ARCHITECTURE.md)**

## Install

```bash
pip install -e .              # core: generation, exports, CLI
pip install -e ".[risk]"      # + ML risk stratification (scikit-learn)
pip install -e ".[agent]"     # + agentic AI assistant (Anthropic SDK)
pip install -e ".[api]"       # + FHIR REST API (FastAPI)
pip install -e ".[all]"       # everything
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
```

### The agent chat UI

`hdh agent` (no arguments) opens an interactive chat with the care-program
agent (requires `pip install hdh[agent]` and an `ANTHROPIC_API_KEY`). The
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

The generator writes `family_medicine.db` (SQLite, ~87 MB for 10k patients).
It is **not** checked into git — run `hdh generate` to build one, or attach a
pre-built copy from your release artifacts.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Run tests with `pytest`.

## License

[MIT](LICENSE)
