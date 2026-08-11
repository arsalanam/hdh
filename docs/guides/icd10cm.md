# ICD-10-CM ontology guide

The full ICD-10-CM catalog (~74,700 billable codes) as a knowledge graph:
hierarchy, laterality, severity, episode, and coding-rule edges — loaded
from the official CMS release files. Design: [../design/icd10cm-ontology-module.md](../design/icd10cm-ontology-module.md).

## Load the catalog

```bash
hdh icd load --download            # fetches the public CMS zips (cached in ~/.hdh)
hdh icd load --source <dir>        # or point at already-downloaded files
hdh icd status                     # load ledger + counts
hdh icd link                       # link Diagnosis rows to graph concepts
```

~9s on SQLite, ~36s on PostgreSQL (which also builds FTS + trigram indexes).

## Explore the graph

```bash
hdh icd lookup S82.52XA            # ancestors, axes, contralateral, variants, episodes
hdh icd search fracture malleolus  # FTS on PostgreSQL, LIKE elsewhere
hdh icd lateral S52.001A           # → S52.002A (other side)
hdh icd bench                      # measured latencies per access pattern
```

## Description → code (the retrieval funnel)

```bash
# LLM axis extraction (needs ANTHROPIC_API_KEY):
hdh icd codify "broke the inner side of her left ankle, first visit"
# offline — you supply terms + stated axes:
hdh icd codify "..." --terms "fracture medial malleolus" --axes "laterality=left,encounter=initial"
```

Candidates return with `matches` / `CONFLICTS` / `unstated` per axis, and
the funnel tells you what to ask when the description didn't say
("💬 ask about: displacement"). The LLM only classifies; retrieval is
deterministic SQL.

## Graph patterns

```bash
hdh icd pattern '{"anchor":{"code":"S52.0"},"traverse":[{"edge":"parent_of","depth":"*"}],"axes":{"laterality":"left"},"constraints":{"billable":true}}'
```

Patterns are validated against a closed schema (bad keys/edges/axes are
rejected with the reason), then compiled to parameterized SQL — an LLM can
propose them but never writes SQL.

## In the agent

`hdh agent` gains a `coding` intent with tools `icd_codify`, `icd_lookup`,
`icd_search`, `icd_pattern` (only these load for coding questions):

```bash
hdh agent "What code fits a nondisplaced fracture of the lateral malleolus
           of the right fibula, initial visit, and what should be excluded with it?"
```

The agent does the axis extraction as tool arguments, the validator grounds
every cited code in tool results, and unstated axes become follow-up
questions. Educational use over synthetic data — not billing advice.

## Updates

Each fiscal year: `hdh icd load --download --fy 2027 --force`. Loads are
ledgered in `ontology_loads` with checksums and counts; concepts carry
`effective_fy`.
