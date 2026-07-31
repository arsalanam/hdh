# Narrative guide — SOAP notes

Renders visits as Subjective / Objective / Assessment / Plan notes.

## Usage

```bash
hdh narrative --mrn MRN12345678             # 3 most recent visits (default)
hdh narrative --mrn MRN12345678 --last 10
hdh narrative --mrn MRN12345678 --llm       # Claude rewrites as natural prose
```

Example (template renderer):

```
SOAP NOTE — 2025-05-22  (Dr. James O'Brien, MD)
S: 5-year-old female presents with: Well-child check / routine vaccines.
O: Vitals: BP 114/66 mmHg, HR 76, RR 15, T 98.9°F, SpO2 97%, BMI 19.5, pain 0/10.
A: Well-child visit, routine (Z00.129)
P: Follow up in 365 days.
```

## Two renderers

| | Template (default) | `--llm` |
|---|---|---|
| Dependencies | none | `hdh[agent]` + `ANTHROPIC_API_KEY` |
| Determinism | fully deterministic | model-written prose |
| Guarantees | exact values from the DB | values/codes/dates preserved by instruction; falls back to the template note on refusal |
| Use when | pipelines, tests, bulk generation | demos, LLM-training corpora, realism |

The template covers: chief complaint with age/sex framing, allergies and
chronic history (S); vitals plus abnormal-lab callouts (O); ICD-10 diagnoses
(A); new/continued prescriptions and the follow-up interval (P).

## Python API

```python
from hdh.modules.narrative import visit_to_soap, patient_soap_notes
from hdh.modules.narrative.soap import polish_with_llm

notes = patient_soap_notes(patient, last_n=5)        # list[str]
note = visit_to_soap(patient.visits[-1], patient)
prose = polish_with_llm(note)                        # optional LLM pass
```

## Extending

The four section builders (`_subjective`, `_objective`, `_assessment`,
`_plan`) in `src/hdh/modules/narrative/soap.py` are small pure functions —
adjust style there. Ideas: per-visit-type templates (well-child vs chronic
follow-up), a bulk exporter writing one note file per visit, batching the
LLM pass through the Message Batches API for large corpora.
