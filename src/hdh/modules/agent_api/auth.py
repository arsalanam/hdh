"""Provider-realm authentication for the agent API (design agentic-ui-module §7).

The browser logs in against the hdh **provider** realm (Keycloak, OIDC
auth-code + PKCE) and sends the access token as a ``Bearer`` on every request.
This verifies that token's **signature** against the realm's JWKS — the web
trust boundary the CLI's same-trust flow did not need — checks the issuer and
expiry, and builds the same :class:`Identity` the rest of the system uses.

A token minted by any other realm carries a different issuer and is refused
here: **provider and patient realms never mix**, enforced at the door.

``authenticator`` is injectable so the HTTP surface is testable without a live
Keycloak — tests pass a fake that maps a header to an Identity (or rejects it).
"""

from __future__ import annotations

from collections.abc import Callable

from hdh.core.identity import Identity

#: (Authorization header value or None) → Identity; raises AuthError otherwise.
Authenticator = Callable[[str | None], Identity]


class AuthError(Exception):
    """A missing, malformed, wrong-realm, or expired token."""


def _bearer(header: str | None) -> str:
    """The token out of an ``Authorization: Bearer <token>`` header."""
    if not header or not header.lower().startswith("bearer "):
        raise AuthError("missing or malformed Authorization header")
    token = header[len("bearer ") :].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def keycloak_authenticator(config=None) -> Authenticator:
    """The real authenticator: verify a provider-realm access token via JWKS.

    Signature (RS256) is checked against the realm's published keys, and the
    issuer must be exactly this realm's — which is what keeps a patient-realm
    token out. Audience is not checked (Keycloak's access tokens carry the
    client in ``azp``, not a fixed ``aud``); issuer + signature + expiry are
    the gate.
    """
    from hdh.core.identity.keycloak import KeycloakConfig

    cfg = config or KeycloakConfig.from_env()
    issuer = f"{cfg.base_url}/realms/{cfg.realm}"
    jwks_url = cfg._endpoint("certs")
    _jwks_client = None  # built lazily, then cached (it caches keys itself)

    def authenticate(header: str | None) -> Identity:
        nonlocal _jwks_client
        import jwt

        from hdh.core.identity.keycloak import identity_from_claims

        token = _bearer(header)
        try:
            if _jwks_client is None:
                _jwks_client = jwt.PyJWKClient(jwks_url)
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=issuer,
                options={"verify_aud": False},
            )
        except AuthError:
            raise
        except Exception as err:  # noqa: BLE001 - any JWT/JWKS failure is a refused token
            raise AuthError(f"token rejected: {type(err).__name__}") from None
        return identity_from_claims(claims)

    return authenticate
