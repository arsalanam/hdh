# SNOMED CT guide

The `snomed` module loads the **SNOMED CT US Edition** into the shared
ontology tables and serves it through the `OntologyService` protocol: a
DAG hierarchy backed by a transitive-closure table, a 1M-row synonym
index, and defining-attribute edges (method, finding site, …) that carry
intervention semantics. Design: `docs/design/snomed-module.md`.

## Licensing — read this first

SNOMED CT is **licensed** (free for US affiliate use under the UMLS
Metathesaurus License, sign up at <https://uts.nlm.nih.gov>). hdh ships
the loader and a synthetic test fixture — **never SNOMED CT content**.
The release you download with your own UTS API key is cached per-user in
`~/.hdh/snomed/<release>/` and must not be committed or redistributed.

## Load

```bash
# .env:  UMLS_API_KEY=...   (from your UTS profile; see .env.example)
hdh snomed load --download            # UTS fetch (~1 GB, cached) + full load
hdh snomed load --source <rf2-dir>    # any legitimately obtained RF2 Snapshot
hdh snomed status                     # ledger + catalog counts
```

The eight-stage pipeline (acquire → parse → build → load → closure →
accelerate → verify → finalize) writes ~386k concepts, ~1M terms, and
~7.8M closure rows; a failing stage aborts before the ledger, so a failed
load is never recorded as complete. Re-running is free — the cache skips
the download and the ledger guard refuses double loads without `--force`.
PostgreSQL (`just deps`) is the intended home at this scale: loads use
COPY, and the accelerate stage builds FTS + trigram indexes over the term
index.

## Query

```bash
hdh snomed search "heart attack"          # synonym-aware funnel (FTS → trigram)
hdh snomed lookup 73211009                # FSN, semantic tag, ancestors, terms
hdh snomed subsumes 64572001 73211009     # one closure hit: True
hdh snomed attributes 43810009            # thrombectomy: method → Removal, ...
hdh snomed bench                          # honest latencies vs targets
```

From Python, dispatch through the protocol — never query the closure
table directly (it is module-private; the quality gate enforces the same
rule for ICD's `path`):

```python
from hdh.core.ontology import get_ontology_service

svc = get_ontology_service("snomed_ct", session)
svc.subsumes("64572001", "73211009")            # True
svc.normalize("glimmer fever")                   # ranked Candidates
svc.normalize("removal", {"semantic_tags": ["procedure"]})   # tag-fit rerank
svc.attributes("43810009")                       # {"method": (...), ...}
```

## Agent integration

With the catalog loaded, `hdh agent` automatically gains
`snomed_normalize` / `snomed_lookup` / `snomed_subsumes` /
`snomed_descendants` (published API: `build_snomed_tools`), so cohort
questions resolve by graph semantics:

```bash
hdh agent "Which patients have a disorder under cerebrovascular disease?"
```

## Updates

The US Edition publishes twice yearly (March 1 / September 1). Load the
new release with `--download` (new cache directory, new ledger row);
`--force` replaces in place. The closure is derived data — always safe to
rebuild.
