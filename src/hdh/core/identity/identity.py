"""Who is asking, and the token bundle that proves it.

The seam the rest of hdh consumes. Everything downstream — chartedit,
careplan persist, refills, the agent pipeline — takes an ``Identity`` and
never sees a token, an OIDC claim, or a Keycloak URL. Keycloak is one
implementation of :class:`IdentityProvider`; :class:`FakeProvider` is
another, and it is what the tests run against, so `just qa` needs no
container and no network.

Design: docs/design/identity-and-authorization.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class AuthError(RuntimeError):
    """Authentication failed, or a session could not be established.

    Carries a message fit to show a user — "invalid username or password",
    "the identity service is not reachable" — never a stack trace or a raw
    OIDC error body.
    """


@dataclass(frozen=True)
class Identity:
    """A resolved actor: who they are and what they may do.

    ``provider_id`` links to the ``providers`` table and is ``None`` until
    AU2 wires account↔provider linking — an identity is usable for
    attribution and authorization before it is tied to a clinical profile.

    ``is_service`` marks a ``system:*`` account (the generator, the
    pipeline, the eval harness). A service identity is authenticated like
    any other; the flag exists so bulk-construction paths can be recognised
    rather than mistaken for a person.
    """

    subject: str
    username: str
    roles: frozenset[str] = field(default_factory=frozenset)
    provider_id: int | None = None
    is_service: bool = False

    def has_role(self, role: str) -> bool:
        return role in self.roles


@dataclass(frozen=True)
class AuthSession:
    """An authenticated session: the identity plus the tokens behind it.

    Persisted to ``~/.hdh/session.json``; the tokens live only there, never
    in a checkpoint or the agent's graph state — those keep the id and
    re-resolve authorization against a live session (design §2.4).

    Expiries are epoch seconds, so staleness is a comparison against the
    clock rather than a decoded claim every reader has to trust.
    """

    identity: Identity
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float


@runtime_checkable
class IdentityProvider(Protocol):
    """What a login backend must do. Keycloak is one; the fake is another."""

    def authenticate(self, username: str, password: str) -> AuthSession:
        """Exchange a username and password for a session, or raise
        :class:`AuthError`."""
        ...

    def refresh(self, refresh_token: str) -> AuthSession:
        """A fresh session from a valid refresh token, or :class:`AuthError`."""
        ...

    def logout(self, refresh_token: str) -> None:
        """Revoke the refresh token. Best effort: a logout must succeed
        locally even if the server cannot be reached, so this never raises
        on a network failure — the local session is cleared regardless."""
        ...
