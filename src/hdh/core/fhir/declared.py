"""Generic FHIR emitter for registry-declared flat entities (design §7).

A schema module's *new* entity JSON may carry a ``fhir`` hint block::

    "fhir": {
      "resourceType": "Observation",
      "patient_link": "patient_id",
      "fields": {"status_text": "valueCodeableConcept.text",
                 "recorded_date": "effectiveDateTime"},
      "set": {"status": "final", "code": {"coding": [...]}},
      "id_fields": ["recorded_date"],
      "subject_field": "subject"
    }

``fields`` maps flat columns onto dotted FHIR paths (None values are
skipped; dates become ISO strings); ``set`` contributes literal FHIR
fragments for the parts that aren't per-row data (statuses, codings);
``patient_link`` names the FK column matching ``Patient.id``. A stable
content-hash id (from ``id_fields``, defaulting to every mapped column)
and the patient reference (``subject_field``, default ``subject``) are
added automatically. The payload is validated through the official
``fhir.resources`` R4B class — the same conformance bar as the
hand-written emitters.

This is the mapping-DSL idea at the right altitude (design §7): per-
entity hints for trivially flat shapes, while resources with real logic
keep real emitters. Module enrichers apply to declared resources too —
the bundle groups by resource type, not by who emitted.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from sqlalchemy.orm import object_session

from hdh.core.fhir import ExportContext


def _plain(value: Any) -> Any:
    """A JSON-friendly scalar (dates → ISO strings; pydantic does the rest)."""
    return value.isoformat() if isinstance(value, date) else value


def _put(payload: dict, dotted: str, value: Any) -> None:
    """Set ``payload[a][b][c] = value`` for path ``"a.b.c"``; skip None."""
    if value is None:
        return
    node = payload
    *parents, leaf = dotted.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


class DeclaredEntityEmitter:
    """Exports one registry-declared flat entity via its ``fhir`` hint."""

    def __init__(self, entity_name: str, entity_class: Any, hint: dict) -> None:
        self.entity_name = entity_name
        self.entity_class = entity_class
        self.hint = hint
        self.resource_type: str = hint["resourceType"]

    def emit(self, ctx: ExportContext) -> list[tuple[Any, Any]]:
        """Build one typed resource per row linked to the patient."""
        from fhir.resources.R4B import get_fhir_model_class

        session = object_session(ctx.patient)
        if session is None:
            return []
        cls = self.entity_class
        link = getattr(cls, self.hint["patient_link"])
        model_class = get_fhir_model_class(self.resource_type)
        fields: dict[str, str] = self.hint.get("fields", {})
        id_fields = self.hint.get("id_fields") or sorted(fields)
        rows = session.query(cls).filter(link == ctx.patient.id).order_by(*cls.__mapper__.primary_key)
        out: list[tuple[Any, Any]] = []
        for row in rows:
            payload: dict = copy.deepcopy(self.hint.get("set", {}))
            for column, path in fields.items():
                _put(payload, path, _plain(getattr(row, column)))
            payload["id"] = ctx.rid(self.resource_type, *(getattr(row, f) for f in id_fields))
            payload[self.hint.get("subject_field", "subject")] = {"reference": f"Patient/{ctx.mrn}"}
            out.append((model_class.model_validate(payload), row))
        return out
