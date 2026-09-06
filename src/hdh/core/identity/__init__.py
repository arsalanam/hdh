"""Identity and authentication (design identity-and-authorization.md, AU1).

The public surface: an ``Identity`` (who), an ``AuthSession`` (the tokens
behind it), the ``IdentityProvider`` protocol, and the on-disk session
helpers. Keycloak is the default provider; ``FakeProvider`` is what the
tests use, so nothing here needs a container.

Enforcement (does this identity hold the permission) and account↔provider
linking arrive in AU3 and AU2 — this milestone establishes the seam and the
`login / logout / whoami` commands, nothing more.
"""

from __future__ import annotations

from hdh.core.identity.fake import FakeProvider
from hdh.core.identity.identity import (
    AuthError,
    AuthSession,
    Identity,
    IdentityProvider,
)
from hdh.core.identity.session import (
    SESSION_PATH,
    clear,
    current_identity,
    current_session,
    load,
    save,
)

__all__ = [
    "SESSION_PATH",
    "AuthError",
    "AuthSession",
    "FakeProvider",
    "Identity",
    "IdentityProvider",
    "clear",
    "current_identity",
    "current_session",
    "default_provider",
    "load",
    "save",
]


def default_provider():
    """The provider a real CLI invocation uses: Keycloak from the env.

    Imported lazily so the module graph does not pull the Keycloak code (or
    anyone's assumptions about a running server) into every `import
    hdh.core.identity` — the fake provider and the session store must stay
    usable with no network in sight.
    """
    from hdh.core.identity.keycloak import KeycloakProvider

    return KeycloakProvider()
