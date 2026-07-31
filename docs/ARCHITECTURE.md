# hdh — Architecture

How the project is put together, and the rules that keep it modular.

> Historical note: the original pre-restructure design notes (flat file layout
> and the JSON-driven schema-registry proposal) are preserved in
> [design/original-design-notes.md](design/original-design-notes.md).

## 1. The big picture

```
                        ┌──────────────────────────────────────────────┐
                        │                  hdh CLI                     │
                        │  core commands  +  auto-discovered module    │
                        │                    subcommands               │
                        └───────┬──────────────────────┬───────────────┘
                                │                      │
              ┌─────────────────▼─────┐   ┌────────────▼─────────────────────┐
              │       hdh.core        │   │           hdh.modules            │
              │  (synthetic data      │◄──┤  caregaps   risk      agent      │
              │   engine — stable)    │   │  narrative  fhir_api  ontology   │
              │                       │   │  billing                         │
              │  models.py            │   └──────────────────────────────────┘
              │  disease_engine.py    │        each module depends ONLY on
              │  generators.py        │        core + its own pip extras
              │  exporters.py         │
              └──────────┬────────────┘
                         │ SQLAlchemy ORM
              ┌──────────▼────────────┐
              │   family_medicine.db  │  SQLite (generated, gitignored)
              └───────────────────────┘
```

Two layers, one direction of dependency:

- **`hdh.core`** — the synthetic data engine. Self-contained; it must never
  import from `hdh.modules`. Everything else is built on top of it.
- **`hdh.modules`** — optional features. Each module may import from core and
  from its own optional third-party dependencies, but never from another
  module's internals. (Modules may call another module's *public* API behind a
  guarded import — e.g. the agent's tools call care-gap detection — and must
  degrade gracefully when it is absent.)

This is the project's one load-bearing rule. It keeps the generator usable on
its own (`pip install hdh` brings in only SQLAlchemy and Faker) and lets each
feature evolve — or be deleted — without touching the data engine.

## 2. Repository layout

```
hdh/
├── pyproject.toml            # packaging; extras: risk, agent, api, docs, dev, all
├── README.md · LICENSE · CONTRIBUTING.md
├── docs/
│   ├── ARCHITECTURE.md       # this file
│   ├── FEATURE_GUIDE.md      # feature overview
│   ├── guides/               # per-module user guides
│   └── design/               # historical design documents
├── scripts/                  # one-off tooling (feature-guide docx builder)
├── src/hdh/
│   ├── __init__.py
│   ├── cli.py                # `hdh` entry point
│   ├── core/
│   │   ├── models.py         # ORM: Patient, Visit, Vital, Diagnosis, Rx, Lab
│   │   ├── disease_engine.py # ConditionProfiles: ICD-10, vitals deltas, labs, formularies
│   │   ├── generators.py     # patient + visit-history generation
│   │   └── exporters.py      # JSON / FHIR R4 / plain-text
│   └── modules/
│       ├── __init__.py       # CLI_MODULES registry
│       ├── caregaps/         # rule-based care-gap detection
│       ├── risk/             # ML risk stratification
│       ├── agent/            # Claude tool-use agent + chat UI + compaction
│       ├── narrative/        # SOAP-note rendering
│       ├── fhir_api/         # FastAPI FHIR R4 facade
│       ├── ontology/         # ICD-10 → SNOMED scaffold
│       └── billing/          # CPT/RVU scaffold
└── tests/                    # pytest; in-memory generated dataset fixture
```

## 3. The core engine

### Data model (`core/models.py`)

```
Patient ──< Visit ──< Diagnosis
        │        ──< Prescription
        │        ──< LabResult
        │        ──1 Vital
        └──< ChronicCondition
```

SQLAlchemy declarative ORM over SQLite. `get_engine(db_path)` creates tables
on first use; there is no migration framework yet (regenerating the dataset is
cheap, so schema changes are handled by regeneration).

### Disease engine (`core/disease_engine.py`)

The realism lives here. Each of the 30+ conditions is a `ConditionProfile`
dataclass declaring: ICD-10 code, chief complaint, visit type, vital-sign
deltas from baseline (mean, sd), lab panels (`LabSpec` with LOINC codes and
condition shifts), formulary options (`RxSpec`), a follow-up interval, and
month-by-month seasonal weights. `pick_condition(age, month, existing)`
samples a condition for a visit; `comorbidity_seeds()` seeds chronic disease
from age, family history, smoking, and BMI.

### Generators (`core/generators.py`)

`build_dataset(session, n_patients, years_of_history)` drives everything:
demographics via Faker (seeded — runs are reproducible), a visit count drawn
from age and chronic burden, then per-visit vitals/diagnosis/prescriptions/labs
from the condition profile. Chronic conditions accumulate as visits reveal them.

### Exporters (`core/exporters.py`)

Three per-patient renderings from the same ORM objects: a denormalized JSON
bundle, a FHIR R4 `Bundle` (Patient, Encounter, Observation, Condition,
MedicationRequest), and an LLM-ready plain-text chart. Modules reuse these
rather than re-serializing (the FHIR API serves `patient_to_fhir_bundle`
verbatim; the agent's chart tool serves `patient_to_text`).

## 4. The CLI and module discovery

`hdh.cli` defines the core subcommands (`generate`, `stats`, `export`, `show`,
`advance`, `add-spike`, `list-conditions`) and then walks the `CLI_MODULES`
registry in `hdh/modules/__init__.py`:

```python
CLI_MODULES = {
    "hdh.modules.caregaps.cli": None,      # value = pip extra providing its deps
    "hdh.modules.risk.cli": "risk",
    ...
}
```

Each module's `cli.py` exposes `register_cli(subparsers)` which adds argparse
subparsers and sets `parser.set_defaults(func=handler)`; the handler receives
`(session, args)` with an open SQLAlchemy session. Two conventions make this
robust:

1. **Module `cli.py` files import only the standard library at module level.**
   Heavy imports (sklearn, anthropic, fastapi) happen inside the handler, so
   `hdh --help` always works on a core-only install and a missing extra
   produces a friendly "pip install hdh[x]" message instead of a stack trace.
2. **A module that fails to import is skipped silently** — the core CLI never
   breaks because an optional feature is broken.

## 5. Module architectures in brief

| Module | Shape | Key design decision |
|---|---|---|
| `caregaps` | Pure functions over aggregate SQL | Reference date defaults to the dataset's latest visit, not the wall clock — gap detection stays meaningful for any generation date. |
| `risk` | features.py (extraction) + model.py (train/score) | Temporal split: features from 12 months before a cutoff, label (urgent visit or critical lab) from the 180 days after. The trained model, feature names, tier thresholds, and AUC ship as one joblib artifact. |
| `agent` | tools.py + chat.py + ui.py + pipeline/ | Two engines. Simple: SDK tool runner with persistent history and auto-compaction (§6). Pipeline (default one-shot): a LangGraph state machine — gateway (composition root) → guardrails (topic guard + daily token quota) → intent → tool executor → assembler → validator, with validator-feedback retries capped at 3 and responses streamed only after validation. All node dependencies injected (`PipelineDeps`), so the graph tests offline. |
| `narrative` | Template renderer + optional LLM polish | Deterministic SOAP notes work offline; `--llm` is additive, never required. |
| `fhir_api` | FastAPI app factory | Thin read-only facade over the core FHIR exporter — no separate serialization logic to drift. |
| `ontology`, `billing` | Library scaffolds | Starter mappings (ICD-10→SNOMED, E/M CPT + RVU) with READMEs describing the extension path. |

## 6. Agent context management

The chat session keeps the *entire* conversation — user turns, assistant
turns, tool calls, tool results — as API-format messages, which gives the
agent real memory but grows without bound. `ChatSession` bounds it:

```
messages > max_messages (default 100)
        │
        ▼
find_clean_cut()      # earliest cut ≥ len-keep_recent that starts a plain
        │             # user turn — never orphans a tool_result from its
        ▼             # assistant tool_use (the API rejects that)
render_transcript()   # old turns → plain text, tool results truncated
        │
        ▼
LLM summarization     # preserves MRNs, findings, decisions, open follow-ups
        │
        ▼
[summary-as-user-message] + last keep_recent messages (default 20), verbatim
```

The summarizer is injectable (`ChatSession(summarizer=...)`), which is how the
compaction pipeline is tested offline without an API key.

## 7. Testing strategy

`tests/conftest.py` generates a small dataset into in-memory SQLite once per
test session; core and module tests run against it. Optional-dependency tests
use `pytest.importorskip`, so the suite passes on any subset of extras.
Anything that would need the Anthropic API is tested up to the API boundary
(tool-schema generation, compaction with an injected summarizer).

## 8. Adding a module (checklist)

1. `src/hdh/modules/<name>/` with `__init__.py` (docstring = module contract).
2. Optional `cli.py` with `register_cli(subparsers)`; stdlib-only at module level.
3. Register in `CLI_MODULES`; declare new deps as a pyproject extra.
4. Tests in `tests/`; a guide in `docs/guides/<name>.md`.
5. Never import from `hdh.modules.<other>.<internals>` — if two modules need
   the same logic, it belongs in core.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full workflow.
