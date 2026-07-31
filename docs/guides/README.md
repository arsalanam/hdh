# hdh user guides

**New to hdh, or not a developer?** Start with the
**[Practitioner Guide](practitioner-guide.md)** — a complete Windows/PowerShell
walkthrough from installation to using every feature, written for clinicians
and care teams.

For developers: one guide per module. Start with [core](core.md) — everything
else builds on it.

| Guide | What it covers | Requires |
|---|---|---|
| [core.md](core.md) | Generating, inspecting, exporting, and simulating the dataset | base install |
| [caregaps.md](caregaps.md) | Detecting overdue care and missed follow-ups | base install |
| [risk.md](risk.md) | Training and using the risk-stratification model | `hdh[risk]` |
| [agent.md](agent.md) | The AI chat assistant, its tools, and context compaction | `hdh[agent]` + API key |
| [narrative.md](narrative.md) | SOAP-note generation | base (LLM polish: `hdh[agent]`) |
| [fhir-api.md](fhir-api.md) | Serving the dataset as a FHIR R4 API | `hdh[api]` |
| [ontology.md](ontology.md) | ICD-10 → SNOMED mapping (scaffold) | base install |
| [billing.md](billing.md) | CPT / RVU claim estimation (scaffold) | base install |

Project-level docs: [Feature Guide](../FEATURE_GUIDE.md) · [Architecture](../ARCHITECTURE.md) · [Contributing](../../CONTRIBUTING.md)
