"""What crosses the boundary, and who is allowed to know what.

The point of milestone C is the ROUND TRIP: an order leaves, a result
returns, and it lands on the chart — with no real integration. So the
partner side is deliberately kept ignorant of hdh: it receives an
:class:`OutboundOrder` and returns :class:`InboundResult` values, and it
never sees a session, a model, or the database. Swapping a mock for a real
lab is then a matter of writing one adapter.

Everything here is frozen. The wire is a contract, not a scratchpad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OutboundOrder:
    """One request, as a partner sees it.

    ``diagnoses`` carries the patient's active ICD-10 codes. That is not a
    convenience for the mock — a real lab requisition carries diagnosis
    codes for medical necessity, and it is what lets a partner return
    results that fit the patient instead of generic noise (design §9 Q4).
    """

    request_id: int
    kind: str  # ServiceKind value: lab | medication | referral | procedure | follow_up
    display: str
    patient_mrn: str
    requested_date: date
    code_system: str | None = None
    code: str | None = None
    occurrence_date: date | None = None
    sig: str | None = None
    diagnoses: tuple[str, ...] = ()  # active ICD-10 codes


@dataclass(frozen=True)
class InboundResult:
    """One thing a partner sends back.

    A result is not always a number. ``value_text`` and ``comparator``
    carry the qualitative and censored answers OMOP made room for — a
    rapid strep reads "positive", not 0.0 (design §3).
    """

    request_id: int
    kind: str  # "lab" | "dispense"
    name: str  # test name, or the dispensed drug
    value: float | None = None
    value_text: str | None = None
    comparator: str | None = None
    unit: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    abnormal: str | None = None  # "normal" | "low" | "high" | "critical"
    loinc_code: str | None = None
    detail: dict = field(default_factory=dict)


@runtime_checkable
class PartnerAdapter(Protocol):
    """A lab, a pharmacy, or one day something real.

    An adapter is a MODULE rather than a script (design §6) precisely so
    that replacing the mock with an interface to a real partner touches
    nothing else: same protocol, same bundles, same importer.
    """

    name: str

    def handles(self, order: OutboundOrder) -> bool:
        """Is this order mine to fulfil?"""
        ...

    def fulfil(self, order: OutboundOrder) -> tuple[InboundResult, ...]:
        """Produce whatever comes back. May be empty — a partner that
        cannot do the test says so by returning nothing, and the importer
        records that rather than inventing a result."""
        ...
