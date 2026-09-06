"""The Keycloak provider's wire behaviour, without a Keycloak (AU1).

The container is exercised by `just deps` and a human; what a unit test can
pin without one is the parts that go wrong silently: decoding an access
token's claims into an `Identity`, and turning each kind of HTTP failure
into an `AuthError` a person can read rather than a stack trace.

`_post` is stubbed, so nothing here opens a socket.
"""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest

from hdh.core.identity.identity import AuthError
from hdh.core.identity.keycloak import KeycloakConfig, KeycloakProvider, _identity_from_token


def _jwt(claims: dict) -> str:
    """A JWT-shaped string: header.payload.signature, payload real, rest
    ignored — the provider decodes the payload and does not verify."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def _token_response(claims, **over):
    resp = {
        "access_token": _jwt(claims),
        "refresh_token": "refresh-xyz",
        "expires_in": 28800,
        "refresh_expires_in": 259200,
    }
    resp.update(over)
    return resp


# ── claims become an Identity ────────────────────────────────────────────


def test_identity_from_a_realistic_token():
    who = _identity_from_token(
        _jwt(
            {
                "sub": "abc-123",
                "preferred_username": "dr.chen",
                "realm_access": {"roles": ["clinician", "prescriber", "offline_access"]},
            }
        )
    )
    assert who.subject == "abc-123"
    assert who.username == "dr.chen"
    assert who.has_role("clinician")
    assert who.provider_id is None  # filled in by AU2, not here


def test_a_service_account_is_marked():
    who = _identity_from_token(_jwt({"sub": "s1", "preferred_username": "service-account-hdh-cli"}))
    assert who.is_service


def test_a_token_with_no_roles_is_fine():
    who = _identity_from_token(_jwt({"sub": "s1", "preferred_username": "clerk.diaz"}))
    assert who.roles == frozenset()


def test_an_unreadable_token_is_an_auth_error():
    with pytest.raises(AuthError):
        _identity_from_token("not-a-jwt")


# ── HTTP failures become readable AuthErrors ─────────────────────────────


def _provider_raising(err):
    provider = KeycloakProvider(KeycloakConfig(), now=1000.0)

    def _post(_endpoint, _form):
        raise err

    provider._post = _post  # type: ignore[method-assign]
    return provider


def _http_error(code, body):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="", hdrs=None, fp=_BytesFP(json.dumps(body).encode())
    )


class _BytesFP:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_bad_credentials_map_to_a_plain_message(monkeypatch):
    """A 401 must read as 'invalid username or password', not a JSON body."""
    provider = KeycloakProvider(KeycloakConfig(), now=1000.0)

    def _urlopen(*_a, **_k):
        raise _http_error(401, {"error": "invalid_grant", "error_description": "Invalid user credentials"})

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    with pytest.raises(AuthError) as e:
        provider.authenticate("dr.chen", "wrong")
    assert "Invalid user credentials" in str(e.value)


def test_a_down_server_says_so_and_names_the_fix(monkeypatch):
    provider = KeycloakProvider(KeycloakConfig(base_url="http://localhost:8080"), now=1000.0)

    def _urlopen(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    with pytest.raises(AuthError) as e:
        provider.authenticate("dr.chen", "x")
    assert "not reachable" in str(e.value)
    assert "just deps" in str(e.value)


# ── a successful exchange builds the session ─────────────────────────────


def test_authenticate_builds_a_session_with_expiries(monkeypatch):
    provider = KeycloakProvider(KeycloakConfig(), now=1000.0)
    claims = {"sub": "abc", "preferred_username": "dr.chen", "realm_access": {"roles": ["clinician"]}}
    provider._post = lambda _e, _f: _token_response(claims)  # type: ignore[method-assign]
    session = provider.authenticate("dr.chen", "dr.chen")
    assert session.identity.username == "dr.chen"
    assert session.access_expires_at == 1000.0 + 28800
    assert session.refresh_expires_at == 1000.0 + 259200


def test_config_reads_the_env(monkeypatch):
    monkeypatch.setenv("HDH_AUTH_URL", "https://sso.example/")
    monkeypatch.setenv("HDH_AUTH_REALM", "prod")
    monkeypatch.setenv("HDH_AUTH_CLIENT", "hdh")
    cfg = KeycloakConfig.from_env()
    assert cfg.base_url == "https://sso.example"  # trailing slash trimmed
    assert cfg.realm == "prod"
    assert "prod" in cfg._endpoint("token")


def _render_module():
    """Load scripts/render_keycloak_realm.py by path — `scripts` is not an
    importable package, and adding an `__init__.py` to make it one would
    turn every ad-hoc script into importable API."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "render_keycloak_realm.py"
    spec = importlib.util.spec_from_file_location("_render_keycloak_realm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the rendered realm matches what the provider expects ─────────────────


def test_the_rendered_realm_has_the_client_and_roles_the_design_names(tmp_path, monkeypatch):
    """The provider authenticates against client `hdh-cli` with direct
    access grants; the realm must actually define that, or login fails with
    a confusing error at the wire."""
    monkeypatch.setenv("HDH_AUTH_SESSION_HOURS", "8")
    monkeypatch.setenv("HDH_AUTH_REFRESH_DAYS", "3")
    realm = _render_module().build_realm()

    client = next(c for c in realm["clients"] if c["clientId"] == "hdh-cli")
    assert client["directAccessGrantsEnabled"] and client["publicClient"]
    role_names = {r["name"] for r in realm["roles"]["realm"]}
    assert {"prescriber", "clinician", "nurse", "clerk", "admin"} <= role_names
    assert realm["accessTokenLifespan"] == 8 * 3600
    assert realm["ssoSessionIdleTimeout"] == 3 * 86400


def test_a_clinician_seed_user_can_author_and_approve(monkeypatch):
    """Decision §4.2: clinician holds create/view/edit/approve. The seed
    user carries the clinician role that AU3 will map to those."""
    realm = _render_module().build_realm()
    chen = next(u for u in realm["users"] if u["username"] == "dr.chen")
    assert "clinician" in chen["realmRoles"]
