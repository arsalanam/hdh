# hdh — Feature Guide

**Health Data Hub: a medically realistic synthetic family-medicine EHR, plus a
modular AI care-program toolkit built on top of it.**

10,000 patients · 165,000+ visits · 777,000+ lab results · no PHI, ever.

| | |
|---|---|
| Stack | Python · SQLAlchemy · SQLite · Faker · scikit-learn · Anthropic SDK · FastAPI |
| Install | `pip install -e ".[all]"` (core alone: `pip install -e .`) |
| Docs | [Architecture](ARCHITECTURE.md) · [Per-module guides](guides/) |

> The original (v0.1) feature guide for the flat, generator-only project is
> preserved as `Feature_Guide.docx`; this document covers the current modular
> project.

---

## Contents

1. [Overview](#1-overview)
2. [Feature matrix](#2-feature-matrix)
3. [Core: synthetic data generation](#3-core-synthetic-data-generation)
4. [Care-gap detection](#4-care-gap-detection)
5. [Risk stratification](#5-risk-stratification)
6. [Agentic AI care assistant](#6-agentic-ai-care-assistant)
7. [SOAP-note narratives](#7-soap-note-narratives)
8. [FHIR R4 REST API](#8-fhir-r4-rest-api)
9. [Ontology & billing scaffolds](#9-ontology--billing-scaffolds)
10. [Roadmap](#10-roadmap)

---

## 1. Overview

hdh generates outpatient (OPD) records driven by age, sex, and seasonal
disease probabilities — a safe, no-PHI sandbox for building and testing
agentic AI care programs, clinical decision tools, FHIR pipelines, and
analytics. On top of the generator sit optional feature modules that turn the
dataset into a working care-program lab: rule-based care-gap detection, an ML
deterioration-risk model, a Claude-powered chat agent with database tools,
clinical narratives, and a FHIR façade.

**Who it's for:** AI/ML engineers building agentic care programs; healthcare
integration developers testing FHIR R4 pipelines; data scientists prototyping
risk-stratification or care-gap models; anyone needing realistic clinical data
without privacy or compliance overhead.

## 2. Feature matrix

| Feature | Command | Extra | Status |
|---|---|---|---|
| Synthetic data generation (30+ conditions) | `hdh generate` | — | ✅ core |
| Dataset statistics & patient charts | `hdh stats`, `hdh show` | — | ✅ core |
| JSON / FHIR R4 / plain-text export | `hdh export` | — | ✅ core |
| Simulation: disease spikes, time advance | `hdh add-spike`, `hdh advance` | — | ✅ core |
| Care-gap detection | `hdh care-gaps` | — | ✅ |
| ML risk stratification | `hdh risk train/score` | `[risk]` | ✅ |
| Agentic AI chat (history + context compaction) | `hdh agent` | `[agent]` | ✅ |
| SOAP-note narratives (+ optional LLM polish) | `hdh narrative` | — (`[agent]` for `--llm`) | ✅ |
| FHIR R4 REST API | `hdh serve` | `[api]` | ✅ |
| ICD-10 → SNOMED mapping | library | — | 🧩 scaffold |
| CPT / RVU claim estimation | library | — | 🧩 scaffold |

## 3. Core: synthetic data generation

The disease engine models each condition as a profile: ICD-10 code and chief
complaint, vital-sign deltas from an age/sex baseline, LOINC-coded lab panels
with condition-shifted values, a condition-appropriate drug formulary,
guideline follow-up intervals, and seasonal weights (flu peaks in winter,
UTIs and sports injuries in summer). Chronic disease is seeded from age,
family history, smoking, and BMI, so comorbidities cluster realistically.

```bash
hdh generate --patients 10000 --years 4   # ~87 MB SQLite, reproducible (seeded)
hdh stats                                 # counts, top diagnoses, age pyramid
hdh show --mrn MRN12345678                # one patient's full chart
hdh export --format all --limit 500 --output-dir exports/
hdh add-spike --condition influenza --month 1 --n 300
hdh advance --months 6                    # follow-up visits for chronic patients
```

Coverage spans well-child visits and RSV in infants through polypharmacy
reviews and falls in seniors — the shipped 10k dataset averages 16.6 visits
per patient with hypertension, T2DM, and hyperlipidemia as the top chronic
diagnoses. → [guides/core.md](guides/core.md)

## 4. Care-gap detection

Four rules, evaluated against the dataset's own timeline (the reference date
defaults to the latest visit, so results stay meaningful whenever the data was
generated):

1. **Overdue preventive** — no preventive visit within the age-based interval.
2. **Uncontrolled chronic** — an uncontrolled condition with no visit in 90+ days (high severity).
3. **Missed follow-up** — a requested follow-up window elapsed (×1.5 grace) with no return visit.
4. **Polypharmacy review** — seniors on 5+ medications with no recent visit.

```bash
hdh care-gaps --limit 25            # ranked most-severe first
hdh care-gaps --mrn MRN123... --json
```

→ [guides/caregaps.md](guides/caregaps.md)

## 5. Risk stratification

A predictive model of near-term deterioration: from 17 features of each
patient's prior 12 months (demographics, chronic burden, visit mix, distinct
drugs, abnormal labs, vitals aggregates), a gradient-boosting classifier
predicts an **urgent visit or critical lab within 180 days**. Training holds
out the last 180 days of data as the label window; on the shipped 10k dataset
it reaches ~0.71 held-out ROC AUC. Patients are tiered high / moderate / low
from training-set probability quantiles.

```bash
pip install -e ".[risk]"
hdh risk train                       # prints positive rate, AUC, tier cutoffs
hdh risk score --top 20              # riskiest patients with driving factors
hdh risk score --mrn MRN123... --json
```

→ [guides/risk.md](guides/risk.md)

## 6. Agentic AI care assistant

A Claude-powered agent (Anthropic SDK tool runner, `claude-opus-5` by
default) with six database tools: patient charts, cohort search, care gaps,
risk scores, read-only SQL, and dataset stats. Answers are grounded in actual
tool results, with the tool trace shown live.

```bash
pip install -e ".[agent]"     # + set ANTHROPIC_API_KEY
hdh agent "Which uncontrolled-HTN patients also score high risk?"
hdh agent                     # interactive chat
```

**Interactive chat UI:** persistent conversation with markdown rendering,
arrow-key input history, and slash commands — `/history`, `/context`
(API-measured token count), `/compact`, `/save`, `/clear`.

**Context management:** beyond 100 messages (configurable via
`--compact-after`), older turns are automatically summarized into a
`<conversation_summary>` briefing that preserves MRNs, findings, and
decisions; the 20 most recent messages stay verbatim. Demo it early with
`hdh agent --compact-after 8`. → [guides/agent.md](guides/agent.md)

## 7. SOAP-note narratives

Every visit renders as a Subjective / Objective / Assessment / Plan note from
a deterministic template (works offline); `--llm` optionally has Claude
rewrite the notes as natural clinical prose with all values preserved.

```bash
hdh narrative --mrn MRN12345678 --last 3
hdh narrative --mrn MRN12345678 --llm
```

→ [guides/narrative.md](guides/narrative.md)

## 8. FHIR R4 REST API

A read-only FHIR façade over the core exporter — the same bundles `hdh export
--format fhir` writes, served over HTTP with interactive docs at `/docs`:

```bash
pip install -e ".[api]"
hdh serve --port 8000
# GET /Patient/{mrn}              Patient resource
# GET /Patient?name=smith         searchset Bundle
# GET /Patient/{mrn}/$everything  full clinical Bundle
# GET /metadata                   CapabilityStatement
```

→ [guides/fhir-api.md](guides/fhir-api.md)

## 9. Ontology & billing scaffolds

Library-level starting points with documented extension paths:

- **Ontology** — `snomed_for_icd10()` over a starter ICD-10→SNOMED map for the
  dataset's highest-volume diagnoses. → [guides/ontology.md](guides/ontology.md)
- **Billing** — E/M CPT assignment from visit type and age, work RVUs, and
  `estimate_claim()` charge estimates. → [guides/billing.md](guides/billing.md)

## 10. Roadmap

- **Care-plan generation subagent** — designed, not yet built: a checkpointed
  LangGraph subagent producing HL7/FHIR-shaped care plans (concerns → goals →
  interventions → outcomes) via RAG over curated knowledge bases, with
  rubric-driven auto-evaluation and human-in-the-loop approval. Full design:
  [design/care-plan-module.md](design/care-plan-module.md)
  ([PDF](design/care-plan-module.pdf)).
- **ICD-10-CM clinical ontology module** — open RFC: the full ~74k-code
  catalog as a knowledge graph (hierarchy, laterality, severity, and
  coding-rule edges) via the schema registry, loaded from official CMS
  files, with description→code retrieval for the agent and care-plan
  codification — and hdh's move to PostgreSQL + Redis via `just deps`,
  retiring SQLite. Full design:
  [design/icd10cm-ontology-module.md](design/icd10cm-ontology-module.md)
  ([PDF](design/icd10cm-ontology-module.pdf)).
- Claims lifecycle simulation (submit → adjudicate → pay/deny) and an `hdh billing` command.
- Care-gap → agent outreach loop: let the agent draft outreach plans for detected gaps.
- Survival-style risk modeling (time-to-event) alongside the classifier.
- CI (GitHub Actions), packaged releases, and a downloadable pre-built database.
