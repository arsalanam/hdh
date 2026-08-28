"""The return leg — and the only genuinely dangerous part of the round trip.

Sending an order is easy. Accepting a result is where a naive importer
quietly corrupts a chart: a result for an order nobody placed, a second
copy of one already filed, a result for an order a clinician cancelled
this morning. None of those are errors the sender will tell you about.

So this holds the same line the comprehension pipeline holds. A result
that cannot be matched to an OPEN order belonging to the right patient is
never written; it becomes a ``RejectedResult`` for a human to decide about
(design §6). Everything that IS written goes through ``chartedit`` with
``origin=EXTERNAL``, so ``hdh chart history`` shows a partner's writes
beside the pipeline's and the agent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from hdh.modules.interchange.bundles import read_bundles, read_result_bundle
from hdh.modules.interchange.contracts import InboundResult

#: The only statuses a result may land against. DRAFT is excluded on
#: purpose: nobody released that order, so nothing should be coming back
#: for it, and a result that does is worth a human's attention.
OPEN_STATUSES = ("ACTIVE",)


@dataclass
class ImportOutcome:
    """What one import run did, and what it refused to do."""

    filed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    completed_requests: set[int] = field(default_factory=set)

    @property
    def needs_review(self) -> bool:
        return bool(self.rejected)


def _actor(partner: str):
    from hdh.core.chartedit.contracts import Actor
    from hdh.core.models import EditSource

    # EditSource has no EXTERNAL member — provenance of the ROW is
    # ServiceRequest/LabResult-level (`origin`), while EditSource says which
    # surface performed the write. A partner import is performed by the
    # pipeline on the partner's behalf, and the actor name carries who.
    return Actor(name=f"partner:{partner}", source=EditSource.PIPELINE)


def _reported_on(request) -> date:
    """When the result came back.

    The request's own occurrence date when it has one, else today. A
    fulfilment date invented as "now" for a backdated import would make the
    chart claim the result arrived when the file was read.
    """
    return getattr(request, "occurrence_date", None) or date.today()


def rejected_table():
    """The review queue's table.

    Registry-created entities are reached through ``Base.metadata`` rather
    than an importable class — the class is built at bootstrap, so nothing
    can import it statically (same pattern as comprehension's NoteRecord).
    """
    from hdh.core.models import Base

    return Base.metadata.tables["rejected_results"]


def _reject(session, partner: str, reason: str, detail: str, item, source: Path | None) -> None:
    from sqlalchemy import insert

    session.execute(
        insert(rejected_table()).values(
            partner=partner,
            reason=reason,
            detail=detail[:300],
            request_id=getattr(item, "request_id", None),
            source_file=str(source) if source else None,
            received_at=datetime.now(),
            payload={
                "name": getattr(item, "name", None),
                "value": getattr(item, "value", None),
                "value_text": getattr(item, "value_text", None),
            },
        )
    )


def _file_lab(session, request, item: InboundResult):
    from hdh.core.models import LabResult, LabStatus

    status = {
        "low": LabStatus.LOW,
        "high": LabStatus.HIGH,
        "critical": LabStatus.CRITICAL,
    }.get((item.abnormal or "").lower(), LabStatus.NORMAL)
    row = LabResult(
        visit_id=request.visit_id,
        request_id=request.id,
        test_name=item.name,
        value=item.value,
        value_text=item.value_text,
        comparator=item.comparator,
        unit=item.unit,
        reference_low=item.reference_low,
        reference_high=item.reference_high,
        status=status,
        loinc_code=item.loinc_code,
    )
    session.add(row)
    return row


def _file_dispense(session, request, item: InboundResult):
    from hdh.core.models import Prescription

    row = Prescription(
        visit_id=request.visit_id,
        request_id=request.id,
        drug_name=item.name,
        drug_class="",
        dose="",
        frequency=item.detail.get("sig", "") if item.detail else "",
        duration_days=item.detail.get("days_supply") if item.detail else None,
        is_new=True,
    )
    session.add(row)
    return row


def import_results(session, inbox: Path, dry_run: bool = False) -> ImportOutcome:
    """Read every result bundle in ``inbox`` and chart what is safe to."""
    from hdh.core.chartedit import record_creation
    from hdh.core.models import ServiceRequest

    outcome = ImportOutcome()
    for path, payload in read_bundles(inbox):
        try:
            partner, results = read_result_bundle(payload)
        except (ValueError, KeyError, TypeError) as err:
            _reject(session, "unknown", "unreadable", f"{path.name}: {err}", None, path)
            outcome.rejected.append(f"{path.name}: unreadable — {err}")
            continue

        for item in results:
            request = (
                session.query(ServiceRequest)
                .filter(ServiceRequest.id == item.request_id)
                .execution_options(include_voided=True)
                .one_or_none()
            )
            if request is None:
                _reject(session, partner, "unknown_request", f"no order #{item.request_id}", item, path)
                outcome.rejected.append(f"{item.name}: no order #{item.request_id}")
                continue
            if request.voided_at is not None or request.status.name not in OPEN_STATUSES:
                state = "voided" if request.voided_at is not None else request.status.value
                _reject(
                    session,
                    partner,
                    "order_not_open",
                    f"order #{request.id} is {state}",
                    item,
                    path,
                )
                outcome.rejected.append(f"{item.name}: order #{request.id} is {state}")
                continue
            if request.visit_id is None:
                # A result has to live on an encounter. An order placed
                # outside one has nowhere to put it, and inventing a visit
                # to hold a lab is not a decision an importer should make.
                _reject(
                    session,
                    partner,
                    "no_encounter",
                    f"order #{request.id} has no visit to file against",
                    item,
                    path,
                )
                outcome.rejected.append(f"{item.name}: order #{request.id} has no encounter")
                continue
            if _already_filed(session, request, item):
                _reject(
                    session,
                    partner,
                    "duplicate_result",
                    f"{item.name} already filed against order #{request.id}",
                    item,
                    path,
                )
                outcome.rejected.append(f"{item.name}: duplicate for order #{request.id}")
                continue

            row = (
                _file_lab(session, request, item)
                if item.kind == "lab"
                else _file_dispense(session, request, item)
            )
            session.flush()
            record_creation(
                session,
                _actor(partner),
                "LabResult" if item.kind == "lab" else "Prescription",
                row,
                reason=f"returned by {partner} for order #{request.id}",
            )
            outcome.filed.append(f"{item.name} → order #{request.id}")
            outcome.completed_requests.add(request.id)

    # An order whose results have arrived is finished. Done last so a
    # partial bundle does not close an order that still owes results.
    #
    # Through `fulfil` rather than by setting the status here: the status and
    # the date have to move together. This previously set COMPLETED without
    # stamping `end_date`, which left every served request looking open to
    # anything reading dates — and `end_date` is what says a request may no
    # longer be acted upon (requests-and-read-models.md).
    from hdh.core.fulfilment import fulfil

    for request_id in sorted(outcome.completed_requests):
        request = session.get(ServiceRequest, request_id)
        if request is not None:
            fulfil(session, request, _reported_on(request), note="results imported")

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return outcome


def _already_filed(session, request, item: InboundResult) -> bool:
    """Has this exact result already landed against this order?

    Re-sending is normal in real interfaces — a partner retries, or a file
    is replayed — and the honest answer is to refuse the second copy rather
    than double the patient's potassium.

    This ASKS THE DATABASE rather than reading ``request.lab_results``,
    and the difference is not stylistic. A session created by the
    generator carries ``expire_on_commit = False`` (a bulk-load
    optimisation that outlives the load), so a relationship loaded before
    the first import stays empty afterwards — and every replayed bundle
    would be filed again. Same family as the identity-map caveat recorded
    in ``chartedit/visibility.py``.
    """
    from hdh.core.models import LabResult, Prescription

    name = item.name.strip().lower()
    if item.kind == "lab":
        rows = session.query(LabResult.test_name).filter(LabResult.request_id == request.id).all()
    else:
        rows = session.query(Prescription.drug_name).filter(Prescription.request_id == request.id).all()
    return any((value or "").strip().lower() == name for (value,) in rows)
