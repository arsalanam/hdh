"""Which database features a caller is standing on.

hdh's core is portable and stays portable — generation, the chart, exports
and the basic agent flow all run on SQLite, which is what makes a first
look cost nothing. Advanced modules are not portable, and the decision
recorded in ARCHITECTURE §4a is that they should **say so** rather than
quietly do less.

That distinction was learned rather than assumed. `termsearch` carries
four retrieval rungs and only the first is portable, so on SQLite the
funnel silently becomes substring matching: "SOB" never reaches *Dyspnea*,
a misspelling never recovers, and word order stops mattering. The code
paid for two paths and the user got a quarter of the feature with no
warning.

So a module that needs PostgreSQL calls :func:`require_postgresql` at the
point it needs it, and the failure names what is missing and how to fix
it — the same posture as every other refusal in this project: an honest
stop beats a quiet degradation.
"""

from __future__ import annotations


class DatabaseFeatureError(RuntimeError):
    """A required database capability is not available here."""


def dialect_of(session) -> str:
    """The bound dialect's name — ``postgresql``, ``sqlite``, …"""
    return session.get_bind().dialect.name


def is_postgresql(session) -> bool:
    """Is this session on PostgreSQL?"""
    return dialect_of(session) == "postgresql"


def require_postgresql(session, feature: str, *, hint: str = "") -> None:
    """Raise unless the session is on PostgreSQL.

    ``feature`` names the capability in the user's terms, not the
    implementation's — the reader needs to know what they cannot do, not
    which index type is missing.

    Raises:
        DatabaseFeatureError: with the dialect found, the feature refused,
            and how to get a PostgreSQL database.
    """
    if is_postgresql(session):
        return
    found = dialect_of(session)
    guidance = hint or ("Start one with `just deps` and set HDH_DB_URL (see the Clinician's Guide, Part 3).")
    raise DatabaseFeatureError(f"{feature} requires PostgreSQL — this session is on {found}. {guidance}")
