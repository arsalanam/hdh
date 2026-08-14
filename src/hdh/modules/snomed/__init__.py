"""SNOMED CT US Edition module (design docs/design/snomed-module.md).

Licensed-data ground rules (design §2): hdh ships the loader and a
synthetic RF2 fixture, NEVER SNOMED CT content — users load from their
own UMLS credential (``UMLS_API_KEY`` in ``.env``) or an RF2 directory
they are licensed to hold. Concepts land in the shared ontology tables
(``ontology='snomed_ct'``); descriptions and the is-a transitive closure
get their own entities (declared in ``schema/entities/``). The closure
table is PRIVATE to this module's OntologyService — no other module
queries it (design §8).
"""
