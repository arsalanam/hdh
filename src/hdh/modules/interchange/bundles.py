"""Outbox and inbox: FHIR Bundles in a directory (design §6, §9 Q3).

Files rather than HTTP, deliberately. A bundle you can open and read is
worth a great deal while the round trip is being built, it diffs in tests,
and it needs no server lifecycle in CI. Staged directories are also close
to how real lab interfaces behave. HTTP stays available later behind the
same :class:`PartnerAdapter`, which is the point of having the protocol.

The identifier is what makes the return leg work: every order carries its
``ServiceRequest`` id, and every result quotes it back. Matching on that is
the happy path — and everything that is not the happy path is the subject
of :mod:`hdh.modules.interchange.importer`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from hdh.modules.interchange.contracts import InboundResult, OutboundOrder

ORDER_SYSTEM = "urn:hdh:service-request"
BUNDLE_VERSION = "1"


def _isoformat(value):
    return value.isoformat() if isinstance(value, date) else value


def order_bundle(orders: list[OutboundOrder]) -> dict:
    """A transaction Bundle of ServiceRequest resources."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "meta": {"tag": [{"system": "urn:hdh:interchange", "code": f"order-v{BUNDLE_VERSION}"}]},
        "entry": [
            {
                "resource": {
                    "resourceType": "ServiceRequest",
                    "id": str(order.request_id),
                    "identifier": [{"system": ORDER_SYSTEM, "value": str(order.request_id)}],
                    "status": "active",
                    "intent": "order",
                    "code": {
                        "text": order.display,
                        **(
                            {"coding": [{"system": order.code_system, "code": order.code}]}
                            if order.code
                            else {}
                        ),
                    },
                    "subject": {"identifier": {"system": "urn:hdh:mrn", "value": order.patient_mrn}},
                    "authoredOn": _isoformat(order.requested_date),
                    "occurrenceDateTime": _isoformat(order.occurrence_date),
                    # A real requisition carries diagnosis codes for medical
                    # necessity; here they also let a partner answer in a way
                    # that fits the patient (design §9 Q4).
                    "reasonCode": [
                        {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code}]}
                        for code in order.diagnoses
                    ],
                    "patientInstruction": order.sig,
                    "category": [{"text": order.kind}],
                }
            }
            for order in orders
        ],
    }


def read_order_bundle(payload: dict) -> list[OutboundOrder]:
    """Orders back out of a bundle — the partner's side of the wire."""
    orders = []
    for entry in payload.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "ServiceRequest":
            continue
        code = (resource.get("code") or {}).get("coding") or [{}]
        subject = ((resource.get("subject") or {}).get("identifier") or {}).get("value", "")
        orders.append(
            OutboundOrder(
                request_id=int(resource["id"]),
                kind=((resource.get("category") or [{}])[0]).get("text", ""),
                display=(resource.get("code") or {}).get("text", ""),
                patient_mrn=subject,
                requested_date=date.fromisoformat(resource["authoredOn"]),
                code_system=code[0].get("system"),
                code=code[0].get("code"),
                occurrence_date=(
                    date.fromisoformat(resource["occurrenceDateTime"])
                    if resource.get("occurrenceDateTime")
                    else None
                ),
                sig=resource.get("patientInstruction"),
                diagnoses=tuple(
                    coding["code"]
                    for reason in resource.get("reasonCode", [])
                    for coding in reason.get("coding", [])
                    if coding.get("code")
                ),
            )
        )
    return orders


def result_bundle(partner: str, results: list[InboundResult]) -> dict:
    """A collection Bundle of Observations (labs) / MedicationDispense."""
    entries = []
    for item in results:
        if item.kind == "dispense":
            entries.append(
                {
                    "resource": {
                        "resourceType": "MedicationDispense",
                        "status": "completed",
                        "medicationCodeableConcept": {"text": item.name},
                        "basedOn": [{"identifier": {"system": ORDER_SYSTEM, "value": str(item.request_id)}}],
                        "note": [{"text": json.dumps(item.detail)}] if item.detail else [],
                    }
                }
            )
            continue
        resource = {
            "resourceType": "Observation",
            "status": "final",
            "code": {
                "text": item.name,
                **(
                    {"coding": [{"system": "http://loinc.org", "code": item.loinc_code}]}
                    if item.loinc_code
                    else {}
                ),
            },
            "basedOn": [{"identifier": {"system": ORDER_SYSTEM, "value": str(item.request_id)}}],
            "interpretation": [{"text": item.abnormal}] if item.abnormal else [],
        }
        if item.value_text is not None:
            # The qualitative case: "no growth", "positive". There is no
            # number here, and inventing one is what §3 was about.
            resource["valueString"] = item.value_text
        else:
            quantity = {"value": item.value, "unit": item.unit}
            if item.comparator:
                quantity["comparator"] = item.comparator
            resource["valueQuantity"] = quantity
        if item.reference_low is not None or item.reference_high is not None:
            resource["referenceRange"] = [
                {
                    "low": {"value": item.reference_low},
                    "high": {"value": item.reference_high},
                }
            ]
        entries.append({"resource": resource})
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": {
            "tag": [
                {"system": "urn:hdh:interchange", "code": f"result-v{BUNDLE_VERSION}"},
                {"system": "urn:hdh:partner", "code": partner},
            ]
        },
        "entry": entries,
    }


def read_result_bundle(payload: dict) -> tuple[str, list[InboundResult]]:
    """(partner, results). Raises ValueError on anything unreadable — the
    importer turns that into a review item rather than a crash."""
    tags = {tag.get("system"): tag.get("code") for tag in (payload.get("meta") or {}).get("tag", [])}
    partner = tags.get("urn:hdh:partner") or "unknown"
    results: list[InboundResult] = []
    for entry in payload.get("entry", []):
        resource = entry.get("resource", {})
        kind = {"Observation": "lab", "MedicationDispense": "dispense"}.get(resource.get("resourceType"))
        if kind is None:
            continue
        based_on = resource.get("basedOn") or []
        raw_id = ((based_on[0] if based_on else {}).get("identifier") or {}).get("value")
        if raw_id is None or not str(raw_id).isdigit():
            raise ValueError("result does not quote a service-request identifier")
        code_block = resource.get("code") or resource.get("medicationCodeableConcept") or {}
        quantity = resource.get("valueQuantity") or {}
        ref = (resource.get("referenceRange") or [{}])[0]
        results.append(
            InboundResult(
                request_id=int(raw_id),
                kind=kind,
                name=code_block.get("text", ""),
                value=quantity.get("value"),
                value_text=resource.get("valueString"),
                comparator=quantity.get("comparator"),
                unit=quantity.get("unit"),
                reference_low=(ref.get("low") or {}).get("value"),
                reference_high=(ref.get("high") or {}).get("value"),
                abnormal=((resource.get("interpretation") or [{}])[0]).get("text"),
                loinc_code=next((c.get("code") for c in code_block.get("coding", []) if c.get("code")), None),
                detail=json.loads((resource.get("note") or [{}])[0].get("text", "{}"))
                if resource.get("note")
                else {},
            )
        )
    return partner, results


def write_bundle(directory: Path, name: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_bundles(directory: Path) -> list[tuple[Path, dict]]:
    """Every *.json in the directory, oldest name first (stable ordering)."""
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        out.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return out
