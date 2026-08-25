"""Care-plan generation: concerns, goals, interventions, outcomes.

Milestone 1 is the data model only — six entities entering the schema
registry declaratively, exactly as `interchange` and `comprehension` do.
No generation, no retrieval, no agent surface yet.

**The foreign keys are the design.** A care plan is a four-part graph and
every part traces upward: a goal answers a concern, an intervention serves
a goal, an outcome measures a goal. Those columns are `nullable: false`,
so an orphan is not a validation failure to be caught later — it cannot be
written at all. See design §5, and §8 on why this is deliberately not
something an LLM is asked to get right.

Two deviations from the 2026-08-06 design, both because the codebase moved
underneath it:

- ``measure_loinc`` is ``measure_system`` + ``measure_code``. When the
  design was written LOINC was the only plausible vocabulary; the chart now
  pairs a system with every code it stores (`Prescription`,
  `ServiceRequest`), and a column named after one vocabulary would be the
  only place in hdh that bakes one in.
- ``PlanIntervention`` carries ``request_id``. A medication or referral the
  plan proposes is a *proposal*, and `ServiceRequest` already models that
  exactly — DRAFT until a human releases it. The service-requests module
  did not exist when this was designed; linking rather than duplicating is
  the point of it existing now.
"""
