"""SOAP-note narrative generation.

Renders each visit as a clinical SOAP note. The default renderer is a
deterministic template (no dependencies); pass ``--llm`` to have Claude
rewrite the note in natural prose (requires the ``agent`` extra).
"""

from .soap import visit_to_soap, patient_soap_notes
