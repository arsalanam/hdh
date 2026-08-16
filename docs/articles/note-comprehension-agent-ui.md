# Note Comprehension, or: The Agent Tier Is the New EHR UI

![A free-text progress note flows through a five-stage pipeline (only extraction is an LLM; everything else is deterministic) into a coded, reconciled patient chart — with one unmapped item branching out to a human review queue. Tagline: the note is the interface.](assets/note-comprehension-hero.png)

*An introduction to the doctor-note comprehension module in
[hdh](https://github.com/arsalanam/hdh), and the roadmap it opens.
Everything below runs on synthetic data — hdh generates medically
realistic family-medicine charts with zero PHI.*

## A provider talks to the chart

Here is the whole demo. A provider opens a chat and types:

> Can you add the following note to the patient chart for patient
> MRN67606524, provided yesterday by Dr. Priya Sharma: *Patient seen in
> clinic for evaluation of elevated blood pressure readings taken at home
> over the past month. She reports occasional headaches in the mornings
> but denies chest pain… blood pressure is 152/94 mmHg… she meets
> criteria for essential hypertension… Start Lisinopril 10mg once daily
> for hypertension. Return in 30 days…*

Seconds later, the agent answers with a reconciliation report:

- **Essential hypertension (I10)** — already on the problem list;
  referenced, not duplicated.
- **Lisinopril 10mg once daily** — new medication added.
- **Vitals** — BP 152/94, HR 88, weight 82.5 kg — recorded.
- ⚠️ **"headaches"** — recognized and coded to SNOMED CT, but no billing
  mapping exists; **not written**, queued for human review.

No forms. No dropdowns. No twelve-click encounter workflow. The note
itself — the thing clinicians actually produce — became the structured,
coded, billable chart update. And the one item the system wasn't sure
about was *refused*, not guessed.

## The design rule that makes it safe

Large language models are superb at reading clinical prose and terrible
at being audited. So the pipeline is built on one house rule:

**The LLM classifies. Deterministic code decides.**

The model does exactly one job: find the clinical mentions in the text
and type them (problem, medication, lab/vital, procedure, allergy). It
never assigns a code, never decides whether a condition is negated or
historical, and never touches the database. Everything after extraction
is deterministic, testable code:

- **Verbatim grounding.** Every extracted mention must match the note
  text character-for-character at a specific span. A mention that can't
  prove where it came from is rejected, and the model retries with
  precise feedback. No fuzzy repair, ever.
- **Closed schemas.** Five mention types, a fixed table of attribute
  kinds, one relation kind. The model cannot invent a category; a new
  kind is a reviewed schema change, not a free string.
- **Ontology-grounded coding.** Codes come from a deterministic funnel
  over real terminologies — SNOMED CT for problems and procedures, LOINC
  for vitals and labs, the drug catalog for medications, ICD-10-CM for
  billing via curated cross-ontology mappings. The model never "recalls"
  a code from training data.
- **Rules-first context.** Negation ("denies chest pain"), history,
  family history, and uncertainty are decided by deterministic rules over
  the note's sections and trigger words — so "denies chest pain" is
  provably skipped, not probabilistically skipped.
- **Refuse, don't guess.** Anything that can't be resolved with
  confidence — an unmapped symptom, a low-confidence link — goes to a
  human review queue and is never written to the chart. The reconciler
  reports four verdicts per entity: `new`, `confirmed`, `review`,
  `skipped`.

During live testing, the agent once tried to work around a review item
by writing raw SQL into the database. The read-only guardrail refused
it, the review item stayed unwritten, and the session survived. That is
the point: the boundaries hold even when the agent is being creative.

## Why this matters: two consumers, one chart

An EHR chart has always had two audiences. Humans read narratives;
automated systems — billing, quality measures, decision support,
registries, and now AI agents — need codes and structure. Traditional
EHRs resolve this tension by making the *clinician* do the structuring,
one click at a time, which is how we got the documentation-burden
crisis.

Note comprehension inverts it. The clinician produces what they were
always going to produce — a note — and the system derives the structure,
with provenance: every coded entry points back to the exact span of text
it came from, the original note is stored on the visit verbatim, and the
same visit round-trips back out as a SOAP note or a FHIR document
bundle. The chart stays usable by humans, by AI, and by the decidedly
non-AI automation that actually runs healthcare.

## The thesis: the agent tier is the new UI

Once free text can safely become structured chart data, the screen full
of forms stops being the interface. The interface is a conversation
backed by **tools with contracts**:

- the agent holds *published, guarded tools* — chart a note, query a
  cohort by ontology subtree, normalize a term, look up a code — each
  one a narrow API that validates its inputs and rolls back on failure;
- the ontologies are the shared vocabulary between the human, the agent,
  and every downstream system;
- the guardrails and review queues are where clinical accountability
  lives — the human is *in* the loop by design, not by exception.

In that world the EHR's "UI tier" is an agent tier. Screens become one
optional client among many; the real product is the governed set of
capabilities the agent can exercise on the chart.

## The roadmap

The comprehension module is merged and live-tested in hdh today
(design docs:
[the service](../design/notes-comprehension-service.md) and
[the extraction contract](../design/comprehension-extraction-schema.md)).
What comes next is the rest of the chart-maintenance surface:

1. **Comprehensive testing & eval discipline** — a replay corpus where
   every live model failure becomes a permanent regression fixture, plus
   a measured baseline (recall, precision, linking, assertion accuracy)
   that no future prompt or model change may silently regress.
2. **Amend / delete + an append-only audit log** — the sanctioned edit
   path. When a charted item is wrong, the provider tells the agent to
   fix it; the audit trail records who changed what, when, and why. This
   is what makes agent-maintained charts *credible*, not just possible.
3. **Broader billing coverage** — curated symptom mappings so common
   complaints (headache, fatigue, dizziness) chart automatically instead
   of queueing for review, while unmapped items keep refusing to guess.
4. **Orders through the agent** — the same comprehension of "basic
   metabolic panel to be drawn before the next visit" becoming a real
   service request, with the same verdict-and-review discipline.
5. **Care plans** — concerns linked to interventions, goals tracked
   across visits: the structure comprehension extracts today becoming
   the substrate a longitudinal care-plan agent reasons over.

Each step follows the same pattern: a narrow, contract-first capability;
deterministic code wherever a decision has consequences; the LLM only
where language understanding is genuinely the job; and a human review
path wherever confidence runs out.

## Try it

hdh is open source (MIT):
[github.com/arsalanam/hdh](https://github.com/arsalanam/hdh). Generate a
synthetic panel, load the ontologies, and paste a note at the agent —
the README's *"Chart free-text notes through the agent"* section has the
five-minute version. Feedback, issues, and skepticism are all welcome;
the review queue was built for exactly that.
