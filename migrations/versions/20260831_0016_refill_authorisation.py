"""medication orders carry their refill authorisation.

`docs/design/medication-orders-and-refills.md` §4.1, milestone A. Two
columns on `service_requests`, both nullable and both meaningful only on a
MEDICATION order:

- ``refills_authorised`` — how many refills beyond the first supply. NULL on
  an order that authorised none, and on every other kind.
- ``valid_until`` — the last date the authorisation may be filled on.
  Distinct from ``end_date``, which says the request's life is over: an
  order can expire while still open, and "expired" is a different refusal
  from "closed".

Refills *remaining* is not stored. It is arithmetic over
``medication_dispenses`` (§4.3), because a counter decremented in two places
drifts — which `Prescription.refills` already demonstrates.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-31
"""

from alembic import op
from sqlalchemy import Column, Date, Integer, inspect

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

TABLE = "service_requests"
COLUMNS = (("refills_authorised", Integer), ("valid_until", Date))


def _present(inspector) -> set[str]:
    if TABLE not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    present = _present(inspect(op.get_bind()))
    if not present:
        return
    for name, kind in COLUMNS:
        if name not in present:
            op.add_column(TABLE, Column(name, kind))


def downgrade() -> None:
    present = _present(inspect(op.get_bind()))
    if not present:
        return
    # batch_alter_table so SQLite recreates the table rather than refusing
    # the drop, matching 0014 and 0015.
    with op.batch_alter_table(TABLE) as batch:
        for name, _kind in COLUMNS:
            if name in present:
                batch.drop_column(name)
