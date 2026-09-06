"""The Keycloak-backed identity provider (OIDC resource-owner flow).

Uses stdlib ``urllib`` rather than a client library on purpose: identity is
a *core* concern, and core carries no HTTP dependency (httpx arrives only
with the optional ``[agent]`` extra). The token endpoint is a plain
form-POST, which urllib handles without one.

The access token's claims are decoded WITHOUT signature verification. For a
first-party CLI talking to its own realm over localhost that is acceptable —
the token just arrived from the server we asked, over the connection we
opened. A deployment that fetches tokens across a trust boundary must verify
against the realm's JWKS; that belongs behind this same seam and changes no
caller. Stated here so the shortcut is a decision, not an oversight.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from hdh.core.identity.identity import AuthError, AuthSession, Identity

#: Config, all env-overridable so a deployment points at its own realm
#: without a code change. Defaults match docker-compose.deps.yml.
DEFAULT_URL = "http://localhost:8080"
DEFAULT_REALM = "hdh"
DEFAULT_CLIENT = "hdh-cli"


@dataclass(frozen=True)
class KeycloakConfig:
    """Where the realm lives and which client the CLI presents as.

    Every field is env-overridable (:meth:`from_env`) so a deployment points
    at its own realm without a code change; the defaults match
    docker-compose.deps.yml.
    """

    base_url: str = DEFAULT_URL
    realm: str = DEFAULT_REALM
    client_id: str = DEFAULT_CLIENT
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> KeycloakConfig:
        return cls(
            base_url=os.environ.get("HDH_AUTH_URL", DEFAULT_URL).rstrip("/"),
            realm=os.environ.get("HDH_AUTH_REALM", DEFAULT_REALM),
            client_id=os.environ.get("HDH_AUTH_CLIENT", DEFAULT_CLIENT),
        )

    def _endpoint(self, name: str) -> str:
        return f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/{name}"


class KeycloakProvider:
    """Talks OIDC to a Keycloak realm; returns the same `AuthSession` the
    fake does, so no caller can tell which one it has."""

    def __init__(self, config: KeycloakConfig | None = None, now=None) -> None:
        self.config = config or KeycloakConfig.from_env()
        # `now` injected for the same reason it is elsewhere: expiry is a
        # comparison against a clock the test controls.
        self._now = now

    def _clock(self) -> float:
        if self._now is not None:
            return self._now() if callable(self._now) else self._now
        import time

        return time.time()

    def _post(self, endpoint: str, form: dict) -> dict:
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(endpoint, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                body = response.read().decode()
                # The logout endpoint answers 204 with no body; only the
                # token endpoint returns JSON. An empty body is success with
                # nothing to say, not a malformed response.
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as err:
            detail = _error_detail(err)
            if err.code in (400, 401):
                raise AuthError(detail or "invalid username or password") from None
            raise AuthError(f"the identity service rejected the request ({err.code})") from None
        except urllib.error.URLError:
            raise AuthError(
                f"the identity service at {self.config.base_url} is not reachable — "
                "is it running? (`just deps`)"
            ) from None

    def _session(self, token_response: dict) -> AuthSession:
        access = token_response["access_token"]
        now = self._clock()
        return AuthSession(
            identity=_identity_from_token(access),
            access_token=access,
            refresh_token=token_response["refresh_token"],
            access_expires_at=now + float(token_response.get("expires_in", 0)),
            refresh_expires_at=now + float(token_response.get("refresh_expires_in", 0)),
        )

    def authenticate(self, username: str, password: str) -> AuthSession:
        """A session from a username and password (OIDC resource-owner flow).

        Raises :class:`AuthError` on bad credentials or an unreachable
        server, never a raw HTTP error.
        """
        return self._session(
            self._post(
                self.config._endpoint("token"),
                {
                    "grant_type": "password",
                    "client_id": self.config.client_id,
                    "username": username,
                    "password": password,
                },
            )
        )

    def refresh(self, refresh_token: str) -> AuthSession:
        """A fresh session from a valid refresh token, or :class:`AuthError`."""
        return self._session(
            self._post(
                self.config._endpoint("token"),
                {
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "refresh_token": refresh_token,
                },
            )
        )

    def logout(self, refresh_token: str) -> None:
        """Revoke the refresh token, best effort — never raises on a network
        failure, so the caller can clear the local session regardless."""
        try:
            self._post(
                self.config._endpoint("logout"),
                {"client_id": self.config.client_id, "refresh_token": refresh_token},
            )
        except AuthError:
            # Best effort: the local session is cleared by the caller
            # regardless, so a server we cannot reach must not block logout.
            pass


def _error_detail(err: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(err.read().decode())
        return body.get("error_description") or body.get("error") or ""
    except Exception:  # noqa: BLE001 - a non-JSON error body is not itself an error
        return ""


def _identity_from_token(access_token: str) -> Identity:
    """Build the `Identity` from the access token's claims.

    Decodes the JWT payload segment only — see the module docstring on why
    verification is skipped for the first-party localhost case. Roles come
    from `realm_access.roles`; a `system:*` username marks a service
    account. `provider_id` is None here and is filled in by AU2's
    account↔provider linking.
    """
    claims = _decode_claims(access_token)
    username = claims.get("preferred_username") or claims.get("sub", "unknown")
    roles = frozenset((claims.get("realm_access") or {}).get("roles") or ())
    return Identity(
        subject=claims.get("sub", username),
        username=username,
        roles=roles,
        provider_id=None,
        is_service=username.startswith("system:") or "service-account" in username,
    )


def _decode_claims(access_token: str) -> dict:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload).decode())
    except (IndexError, ValueError, json.JSONDecodeError):
        raise AuthError("the identity service returned a token that could not be read") from None
