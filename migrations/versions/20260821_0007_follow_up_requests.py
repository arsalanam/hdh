"""follow-ups become orders: backfill, then drop the scalar (#59).

§9 Q5 of the service-requests design made the ``FOLLOW_UP``
``ServiceRequest`` the source of truth for a return visit, with
``Visit.follow_up_days`` derived from it. This is the half that moves
existing data: every visit that asked for a return gets the order it
should always have had, and only then does the column go.

The backfill runs through SQLAlchemy rather than hand-written SQL because
two things differ per backend and both are easy to get quietly wrong:
enum columns store their NAME, and date arithmetic has no common spelling
(``date(x, '+N days')`` on SQLite, ``x + N * INTERVAL '1 day'`` on
PostgreSQL). Letting the types do the binding keeps one code path.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-21
"""

from datetime import date, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

COLUMN = "follow_up_days"


def _as_date(value) -> date:
    """SQLite hands back a string where PostgreSQL hands back a date."""
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def upgrade() -> None:
    """Create a FOLLOW_UP order per visit that wanted one, then drop it."""
    from hdh.core.models import Base, RequestOrigin, RequestStatus, ServiceKind

    bind = op.get_bind()
    inspector = inspect(bind)
    # The baseline revision is a no-op — tables come from create_all — so a
    # revision run against an empty database must find nothing to do rather
    # than raise (same guard as 0005 and 0006).
    if not inspector.has_table("visits") or not inspector.has_table("service_requests"):
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns("visits")}:
        return  # already migrated

    visits = bind.execute(
        sa.text(
            f"SELECT id, patient_id, provider_id, visit_date, {COLUMN} AS days "
            f"FROM visits WHERE {COLUMN} IS NOT NULL AND {COLUMN} > 0"
        )
    ).all()

    requests = Base.metadata.tables["service_requests"]
    rows = []
    for row in visits:
        requested = _as_date(row.visit_date)
        rows.append(
            {
                "patient_id": row.patient_id,
                "visit_id": row.id,
                "requester_id": row.provider_id,
                "kind": ServiceKind.FOLLOW_UP,
                "status": RequestStatus.ACTIVE,
                # These were produced by the generator, and saying so is the
                # point of having provenance on the row (design §3).
                "origin": RequestOrigin.GENERATED,
                "display": f"Follow-up visit in {row.days} days",
                "requested_date": requested,
                "occurrence_date": requested + timedelta(days=int(row.days)),
            }
        )
    if rows:
        bind.execute(requests.insert(), rows)

    op.drop_column("visits", COLUMN)


def downgrade() -> None:
    """Put the column back and rebuild it from the orders it became."""
    from hdh.core.models import ServiceKind

    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("visits") or not inspector.has_table("service_requests"):
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns("visits")}:
        op.add_column("visits", sa.Column(COLUMN, sa.Integer(), nullable=True))

    kind = ServiceKind.FOLLOW_UP.name  # enum columns store the NAME
    restored = bind.execute(
        sa.text(
            "SELECT visit_id, requested_date, occurrence_date FROM service_requests "
            "WHERE kind = :kind AND visit_id IS NOT NULL AND occurrence_date IS NOT NULL"
        ),
        {"kind": kind},
    ).all()
    for row in restored:
        days = (_as_date(row.occurrence_date) - _as_date(row.requested_date)).days
        bind.execute(
            sa.text(f"UPDATE visits SET {COLUMN} = :days WHERE id = :vid"),
            {"days": days, "vid": row.visit_id},
        )
    bind.execute(sa.text("DELETE FROM service_requests WHERE kind = :kind"), {"kind": kind})
