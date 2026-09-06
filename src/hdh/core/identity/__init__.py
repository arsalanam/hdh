"""Identity and authentication (design identity-and-authorization.md, AU1).

The public surface: an ``Identity`` (who), an ``AuthSession`` (the tokens
behind it), the ``IdentityProvider`` protocol, and the on-disk session
helpers. Keycloak is the default provider; ``FakeProvider`` is what the
tests use, so nothing here needs a container.

Enforcement (does this identity hold the permission) arrives in AU3.
Account↔provider linking (AU2) is here: ``resolve_actor`` turns an identity
into the chart ``Actor`` that carries a ``provider_id``.
"""

from __future__ import annotations

from hdh.core.identity.accounts import cli_actor, link, provider_for, resolve_actor
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
    "cli_actor",
    "clear",
    "current_identity",
    "current_session",
    "default_provider",
    "link",
    "load",
    "provider_for",
    "resolve_actor",
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
