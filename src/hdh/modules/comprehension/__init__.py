"""Doctor-notes comprehension: free text → coded structured record.

The pipeline spine (master design notes-comprehension-service.md §3;
extraction contract in comprehension-extraction-schema.md): segment →
extract → normalize → contextualize → disambiguate → assemble →
validate. The house rule at every stage: **the LLM classifies,
deterministic code decides** — extraction finds and types, it never
codes and never asserts.

Milestone A ships stages 1–2: the frozen contracts, the deterministic
family-medicine segmenter, whole-note extraction behind an injectable
extractor protocol (stub for tests, LLM for real notes), and the
validator whose rejection reasons are the retry feedback. Every output
obeys the safety property: no span, no mention.
"""
