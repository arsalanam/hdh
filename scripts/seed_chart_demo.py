"""Populate the M3-M5 chart fields for ONE patient, so they can be seen.

The generator does not write functional status, second identifiers,
secondary coverage, coded procedures or immunisation refusals — design
decision 6.4 defers that — so a chart straight out of `hdh generate` shows
those sections correct and empty. Anyone testing the milestone would
reasonably conclude none of it works.

This writes a realistic set for a single MRN and touches nothing else.
Re-running replaces what a previous run added rather than duplicating it,
so it is safe to run repeatedly while testing.

    uv run python scripts/seed_chart_demo.py            # MRN57649249
    MRN=MRN12345678 uv run python scripts/seed_chart_demo.py

Then:

    hdh show --mrn <MRN>

It is a demo aid, not part of the generator. When the generator populates
these fields this script stops being necessary.
"""

import os
import pathlib
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")
for line in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from hdh.core.schema_registry import bootstrap_schema  # noqa: E402

bootstrap_schema()
from hdh.core.models import (  # noqa: E402
    Allergy,
    AllergySeverity,
    FunctionalStatus,
    Immunization,
    Patient,
    PatientCoverage,
    PatientIdentifier,
    Procedure,
    get_engine,
    get_session,
)

MRN = os.environ.get("MRN", "MRN57649249")
s = get_session(get_engine())
p = s.query(Patient).filter(Patient.mrn == MRN).first()
if p is None:
    raise SystemExit(f"no patient {MRN} — set MRN=<one that exists>")

# idempotent: drop what a previous run of THIS script added
for row in list(p.functional_status):
    s.delete(row)
for row in [r for r in p.immunizations if (r.status or "completed") != "completed"]:
    s.delete(row)
for row in [r for r in p.procedures if r.code]:
    s.delete(row)
for row in [r for r in p.identifiers if r.kind != "mrn"]:
    s.delete(row)
for row in [r for r in p.coverages if (r.rank or 1) > 1]:
    s.delete(row)
for row in [r for r in p.allergies if r.criticality or r.verification]:
    s.delete(row)
s.commit()

p.preferred_name = p.preferred_name or "Bob"
p.pronouns = p.pronouns or "he/him"

s.add_all(
    [
        # M4 — functional status
        FunctionalStatus(
            patient_id=p.id,
            domain="mobility",
            level="assisted",
            aid="walking frame",
            assessed_date=date(2026, 8, 1),
        ),
        FunctionalStatus(
            patient_id=p.id, domain="vision", level="aided", aid="glasses", assessed_date=date(2026, 8, 1)
        ),
        FunctionalStatus(
            patient_id=p.id,
            domain="iadl",
            level="dependent",
            detail="cannot manage own medicines",
            assessed_date=date(2026, 8, 1),
        ),
        # M5 — a refusal, a coded procedure with a side, surgical history
        Immunization(
            patient_id=p.id,
            vaccine="Influenza, seasonal",
            status="refused",
            reason="declines every year",
            recorded_date=date(2026, 10, 1),
        ),
        Procedure(
            patient_id=p.id,
            description="Total knee replacement",
            code="609588000",
            code_standard="SNOMED",
            body_site="knee",
            laterality="left",
            performed_date=date(2019, 7, 4),
        ),
        # M5 — allergy with criticality distinct from severity
        Allergy(
            patient_id=p.id,
            substance="Penicillin",
            drug_code="7980",
            code_standard="RxNorm",
            reaction="rash",
            severity=AllergySeverity.MILD,
            criticality="high",
            verification="confirmed",
            clinical_status="active",
            noted_date=date(2020, 1, 5),
            last_happened=date(1994, 6, 2),
        ),
        Allergy(patient_id=p.id, substance="Egg", clinical_status="resolved", reaction="hives"),
        # M3 — a second identifier and a secondary payer
        PatientIdentifier(patient_id=p.id, kind="national", value="NHS-1234567890", issuer="NHS England"),
        PatientCoverage(patient_id=p.id, rank=2, payer_name="Secondary Health", member_id="SEC-99"),
    ]
)
s.commit()
print(
    f"seeded {MRN}: 3 functional domains, 1 refusal, 1 coded procedure, 2 allergies, "
    f"1 extra identifier, 1 secondary payer"
)
s.close()
