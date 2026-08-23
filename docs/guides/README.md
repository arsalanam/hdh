# hdh user guides

**New to hdh, or not a developer?** Start with the
**[Clinician's Guide](practitioner-guide.md)** — a complete
Windows/PowerShell walkthrough that takes you from an empty machine to
charting notes, placing orders, receiving results and correcting the record.

For developers: one guide per module. Start with [core](core.md) — everything
else builds on it.

## The clinical surface

These are the modules that make hdh an EHR rather than a dataset. They work
best on PostgreSQL — the terminology funnel loses three of its four rungs on
SQLite (see the [Clinician's Guide, Part 3](practitioner-guide.md#part-3--start-the-database)).

| Guide | What it covers | Requires |
|---|---|---|
| [agent.md](agent.md) | The AI assistant: its tools, pipeline, and context compaction | `hdh[agent]` + API key |
| [snomed.md](snomed.md) | SNOMED CT: loading, the normalize funnel, subsumption | base install + UMLS key |
| [icd10cm.md](icd10cm.md) | ICD-10-CM knowledge graph and billing-code selection | base install |

Note comprehension, service requests, interchange and chart maintenance are
documented in their design notes rather than separate guides — they are
described end to end in the [Clinician's Guide](practitioner-guide.md) and
specified in:

- [notes-comprehension-service.md](../design/notes-comprehension-service.md) — the pipeline
- [comprehension-extraction-schema.md](../design/comprehension-extraction-schema.md) — the extraction contract, and §10.0, *"a note asserts; only a partner reports"*
- [service-requests-and-interchange.md](../design/service-requests-and-interchange.md) — orders and results
- [rxnorm-and-terminology-boundaries.md](../design/rxnorm-and-terminology-boundaries.md) — which vocabulary owns which slot
- [chart-maintenance.md](../design/chart-maintenance.md) — amend, void, and the audit trail

## The panel view

| Guide | What it covers | Requires |
|---|---|---|
| [caregaps.md](caregaps.md) | Detecting overdue care and missed follow-ups | base install |
| [risk.md](risk.md) | Training and using the risk-stratification model | `hdh[risk]` |

## The substrate

| Guide | What it covers | Requires |
|---|---|---|
| [core.md](core.md) | Generating, inspecting, exporting, and simulating the practice | base install |
| [narrative.md](narrative.md) | SOAP-note generation | base (LLM polish: `hdh[agent]`) |
| [fhir-api.md](fhir-api.md) | Serving the dataset as a FHIR R4 API | `hdh[api]` |
| [ontology.md](ontology.md) | ICD-10 → SNOMED mapping | base install |
| [billing.md](billing.md) | CPT / RVU claim estimation (scaffold) | base install |

Project-level docs: [Feature Guide](../FEATURE_GUIDE.md) · [Architecture](../ARCHITECTURE.md) · [Contributing](../../CONTRIBUTING.md)
