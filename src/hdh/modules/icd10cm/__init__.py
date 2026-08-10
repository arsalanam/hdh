"""ICD-10-CM clinical ontology module (design: docs/design/icd10cm-ontology-module.md).

The full ICD-10-CM catalog as a knowledge graph: concepts (chapters through
codes) and typed edges (hierarchy, laterality, axis/episode variants, coding
rules, cross-ontology mappings), delivered as a schema-registry module.

Milestone A ships the persistence tier only — three new entities plus a
``Diagnosis.concept_id`` bridge, all declared in ``schema/`` JSON and
materialized by the registry (design §3.2). The loader, CLI, and retrieval
service arrive in later milestones.

Depends on ``ontology_module`` (the SNOMED starter columns): its
``maps_to`` successor edges will link into the same concepts table.
"""
