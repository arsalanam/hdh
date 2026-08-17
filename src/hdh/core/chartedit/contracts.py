"""Typed contracts for chart amendment (design chart-maintenance.md §3.2).

Every mutation is a request object with a typed outcome — never
positional booleans, never a bare dict. The persisted enums
(:class:`EditSource`, :class:`AuditAction`) live with the other persisted
enums in :mod:`hdh.core.models`; what lives here is the request/response
vocabulary the edit API speaks.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from hdh.core.models import EditSource


class EditAction(str, enum.Enum):
    """What the caller is asking for. Narrower than ``AuditAction``:
    creation is not something this API performs — rows are created by the
    generator or by comprehension, and only ever amended or voided here."""

    AMEND = "amend"
    VOID = "void"


@dataclass(frozen=True)
class Actor:
    """Who is making the change. Injected — never inferred from ambient
    state. Provider-level attribution is deliberate: a real user/account
    concept arrives with authentication (design §7 Q3)."""

    name: str
    source: EditSource
    provider_id: int | None = None


@dataclass(frozen=True)
class ChartEdit:
    """One proposed mutation of one chart row.

    ``changes`` is meaningful for AMEND only. ``reason`` is mandatory for
    clinical entities (§7 Q4) — the registry decides which those are, and
    the API refuses the edit without one."""

    entity: str
    row_id: int
    action: EditAction
    changes: Mapping[str, object] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class EditOutcome:
    """What actually happened — reportable verbatim by CLI and agent."""

    edit: ChartEdit
    applied: bool
    detail: str
    audit_id: int | None = None
    before: Mapping[str, object] = field(default_factory=dict)
    after: Mapping[str, object] = field(default_factory=dict)
