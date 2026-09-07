"""Turning an authenticated identity into a chart actor (AU2).

The identity says who a person is and what they may do; the chart needs one
more thing — which provider they write as — and that link lives in
``user_accounts``. ``resolve_actor`` is the bridge: given an ``Identity``
and the surface it came through, it returns the ``Actor`` the audit trail
records, with ``provider_id`` filled from the link.

Enforcement (does this identity hold the permission) is AU3 and lives
elsewhere. This module only answers "who is writing", not "may they".
"""

from __future__ import annotations

from hdh.core.identity.identity import Identity


def provider_for(session, subject: str) -> int | None:
    """The provider a subject writes as, or None if the account is unlinked.

    None is a real answer, not an error: an account can be attributed by
    username before it is tied to a clinical profile.
    """
    from hdh.core.models import UserAccount

    row = session.query(UserAccount).filter_by(subject=subject).first()
    return row.provider_id if row else None


def link(session, subject: str, username: str, provider_id: int | None) -> None:
    """Record (or update) the account↔provider link for a subject.

    Idempotent on ``subject``: a second login with a moved provider_id
    updates the row rather than duplicating it. The username is stored
    beside the subject for readability and refreshed on each call, since it
    is the mutable half.
    """
    from hdh.core.models import UserAccount

    row = session.query(UserAccount).filter_by(subject=subject).first()
    if row is None:
        session.add(UserAccount(subject=subject, username=username, provider_id=provider_id))
    else:
        row.username = username
        row.provider_id = provider_id
    session.flush()


def resolve_actor(session, identity: Identity, source):
    """The chart ``Actor`` for this identity on this surface.

    ``source`` is supplied by the caller because it is a property of the
    surface (a CLI edit vs an agent edit), not of the person — the same
    clinician is a different ``EditSource`` depending on where they act.
    ``provider_id`` comes from the link, or the identity's own value if it
    was already resolved at authentication time, or None.
    """
    from hdh.core.chartedit import Actor

    provider_id = identity.provider_id
    if provider_id is None:
        provider_id = provider_for(session, identity.subject)
    return Actor(name=identity.username, source=source, provider_id=provider_id)


def authorize_cli(session, permission: str, *, provider=None, now=None, path=None):
    """The signed-in identity for a CLI write, required to hold ``permission``.

    Writes require a login (design §2.7 / §4.4): a caller who has not run
    ``hdh login`` is refused with :class:`NotAuthenticated`, and a signed-in
    caller whose roles lack the permission with :class:`Unauthorized`. Both
    name the permission, so the refusal teaches rather than just blocks.

    Returns the ``Identity`` so the caller can build the ``Actor`` from it
    without resolving twice — a CLI write is one authenticate, one actor.
    """
    import time

    from hdh.core.identity import SESSION_PATH, current_identity, default_provider
    from hdh.core.identity.permissions import NotAuthenticated, require

    identity = current_identity(
        provider or default_provider(),
        time.time() if now is None else now,
        path or SESSION_PATH,
    )
    if identity is None:
        raise NotAuthenticated(permission)
    require(identity, permission)
    return identity


def cli_actor(session, provider=None, now=None, path=None):
    """The ``Actor`` a CLI write is attributed to.

    The signed-in identity when there is one, the OS user otherwise — so a
    developer who has not run ``hdh login`` keeps today's behaviour rather
    than being blocked (reads and CLI writes stay open; §2.7). This is the
    one place `core.orders`/`chartedit` touch identity, so the coupling to
    the Keycloak provider lives here and nowhere else.

    A not-logged-in caller hits no network: ``current_identity`` returns
    None from an absent session file without contacting the server.
    """
    import getpass
    import time

    from hdh.core.chartedit import Actor
    from hdh.core.identity import SESSION_PATH, current_identity, default_provider
    from hdh.core.models import EditSource

    identity = None
    try:
        identity = current_identity(
            provider or default_provider(),
            time.time() if now is None else now,
            path or SESSION_PATH,
        )
    except Exception:  # noqa: BLE001 - a broken provider must not block a CLI write
        identity = None

    if identity is not None:
        return resolve_actor(session, identity, EditSource.CLI)

    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - headless environments have no user
        name = "cli"
    return Actor(name=name, source=EditSource.CLI)
