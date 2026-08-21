"""What the edit API is allowed to know about each chart entity
(design chart-maintenance.md §3.1–§3.2).

Per-entity knowledge lives behind a registry so a new amendable entity is
a **registration**, not another branch in a growing ``if``. Each spec
declares the fields it will accept, how to reach the owning patient (the
audit trail is patient-centric), and whether a reason is mandatory.

``VisitNote`` is deliberately absent: notes are the source record, never
mutated — corrections are addenda.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class AmendableEntity(Protocol):
    """The contract the edit API needs from one chart entity."""

    entity: str
    amendable_fields: frozenset[str]
    reason_required: bool

    def load(self, session, row_id: int):
        """The row, or None — including already-voided rows, so voiding
        twice reports honestly instead of pretending the row vanished."""
        ...

    def patient_id(self, row) -> int | None:
        """Whose chart this row belongs to."""
        ...

    def coerce(self, field: str, value: object) -> object:
        """Turn CLI/agent text into the column's type, or raise ValueError."""
        ...

    def describe(self, row) -> str:
        """A short human label for outcome lines."""
        ...


def _label(value: object) -> str:
    """Render a label field the way a person wrote it, not the way Python
    spells it — an outcome line reading "ServiceKind.LAB" is noise where
    "lab" is the word the clinician used."""
    return value.value if isinstance(value, enum.Enum) else str(value)


@dataclass(frozen=True)
class _Spec:
    """One registered entity. Frozen: the registry is configuration."""

    entity: str
    model_name: str
    amendable_fields: frozenset[str]
    reason_required: bool
    patient_path: tuple[str, ...]  # attribute hops from row to patient_id
    label_fields: tuple[str, ...]

    def _model(self):
        from hdh.core import models

        return getattr(models, self.model_name)

    def load(self, session, row_id: int):
        """Load including voided rows — the caller decides what that means."""
        from sqlalchemy import select

        model = self._model()
        return session.execute(
            select(model).where(model.id == row_id).execution_options(include_voided=True)
        ).scalar_one_or_none()

    def patient_id(self, row) -> int | None:
        current = row
        for hop in self.patient_path:
            if current is None:
                return None
            current = getattr(current, hop)
        return current

    def coerce(self, field: str, value: object) -> object:
        """Coerce to the column's Python type — enums by value, dates from
        ISO text, numbers from digits. Unknown shapes raise ValueError."""
        import enum as enum_mod
        from datetime import date, datetime

        column = getattr(self._model(), field).property.columns[0]
        python_type = getattr(column.type, "python_type", None)
        if value is None or python_type is None:
            return value
        try:
            target = python_type
        except NotImplementedError:  # pragma: no cover - exotic columns
            return value
        if isinstance(value, target):
            return value
        text = str(value)
        if isinstance(target, type) and issubclass(target, enum_mod.Enum):
            try:
                return target(text.lower())
            except ValueError:
                return target[text.upper()]
        if target is bool:
            if text.lower() in ("true", "yes", "1"):
                return True
            if text.lower() in ("false", "no", "0"):
                return False
            raise ValueError(f"{field}: expected a boolean, got {value!r}")
        if target is datetime:
            return datetime.fromisoformat(text)
        if target is date:
            return date.fromisoformat(text)
        return target(text)

    def describe(self, row) -> str:
        parts = [_label(getattr(row, name)) for name in self.label_fields if getattr(row, name, None)]
        return f"{self.entity} #{row.id} ({' · '.join(parts)})" if parts else f"{self.entity} #{row.id}"


REGISTRY: Mapping[str, _Spec] = {
    spec.entity: spec
    for spec in (
        _Spec(
            entity="Condition",
            model_name="Condition",
            amendable_fields=frozenset(
                {
                    "status",
                    "controlled",
                    "chronic",
                    "icd10_code",
                    "description",
                    "onset_date",
                    "resolved_date",
                }
            ),
            reason_required=True,
            patient_path=("patient_id",),
            label_fields=("icd10_code", "description"),
        ),
        _Spec(
            entity="Prescription",
            model_name="Prescription",
            amendable_fields=frozenset({"dose", "frequency", "duration_days", "refills", "is_new"}),
            reason_required=True,
            patient_path=("visit", "patient_id"),
            label_fields=("drug_name", "dose"),
        ),
        _Spec(
            entity="Vital",
            model_name="Vital",
            amendable_fields=frozenset(
                {
                    "bp_systolic",
                    "bp_diastolic",
                    "heart_rate",
                    "respiratory_rate",
                    "temperature_f",
                    "oxygen_sat",
                    "weight_kg",
                    "height_cm",
                    "bmi",
                    "pain_scale",
                }
            ),
            reason_required=False,  # transcription fixes (§7 Q4)
            patient_path=("visit", "patient_id"),
            label_fields=(),
        ),
        _Spec(
            entity="LabResult",
            model_name="LabResult",
            amendable_fields=frozenset({"value", "unit", "status", "reference_low", "reference_high"}),
            reason_required=True,
            patient_path=("visit", "patient_id"),
            label_fields=("test_name",),
        ),
        _Spec(
            entity="Allergy",
            model_name="Allergy",
            amendable_fields=frozenset({"substance", "reaction", "severity", "noted_date"}),
            reason_required=True,
            patient_path=("patient_id",),
            label_fields=("substance",),
        ),
        _Spec(
            entity="Visit",
            model_name="Visit",
            amendable_fields=frozenset({"visit_date", "visit_type", "provider_id", "chief_complaint"}),
            reason_required=True,
            patient_path=("patient_id",),
            label_fields=("visit_date",),
        ),
        _Spec(
            # An order is a clinical instruction, so changing one needs a
            # reason like any other. `origin` and `requested_date` are
            # deliberately NOT amendable: provenance and the moment of
            # asking are what make the row auditable, and a chart that can
            # rewrite them cannot answer "who ordered this, and when".
            entity="ServiceRequest",
            model_name="ServiceRequest",
            amendable_fields=frozenset(
                {
                    "status",
                    "display",
                    "code_system",
                    "code",
                    "reason_condition_id",
                    "occurrence_date",
                    "end_date",
                    "quantity",
                    "route",
                    "sig",
                    "stop_reason",
                }
            ),
            reason_required=True,
            patient_path=("patient_id",),
            label_fields=("kind", "display"),
        ),
    )
}

#: Entities whose rows a void cascades to when a Visit is voided.
VISIT_OWNED: tuple[str, ...] = ("Vital", "Prescription", "LabResult", "Condition")


def spec_for(entity: str) -> _Spec:
    """The registered spec, or a ValueError naming what IS registered."""
    try:
        return REGISTRY[entity]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise ValueError(f"{entity!r} is not an amendable entity (registered: {known})") from None
