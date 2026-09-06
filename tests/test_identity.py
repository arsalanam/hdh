"""Identity, the session store, and login/logout/whoami (AU1).

Everything here runs against `FakeProvider` — no Keycloak, no network — the
same way comprehension tests run against `stub_extractor`. That is the point
of the seam: the container is one implementation, and `just qa` needs none.

The Keycloak provider's own wire behaviour (JWT claim decode, error mapping)
is unit-tested without a server further down.
"""

from __future__ import annotations

import json

import pytest

from hdh.core.identity import (
    AuthError,
    FakeProvider,
    Identity,
    current_identity,
    current_session,
)
from hdh.core.identity import session as sessionmod


@pytest.fixture()
def provider():
    p = FakeProvider(now=1000.0)
    p.add("dr.chen", "dr.chen", Identity("s-chen", "dr.chen", frozenset({"clinician", "prescriber"})))
    p.add("nurse.reed", "nurse.reed", Identity("s-reed", "nurse.reed", frozenset({"nurse"})))
    return p


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "session.json"


# ── the Identity itself ──────────────────────────────────────────────────


def test_has_role():
    who = Identity("s1", "dr.chen", frozenset({"clinician"}))
    assert who.has_role("clinician")
    assert not who.has_role("admin")


# ── authentication through the seam ──────────────────────────────────────


def test_authenticate_returns_a_session_with_the_identity(provider):
    session = provider.authenticate("dr.chen", "dr.chen")
    assert session.identity.username == "dr.chen"
    assert session.identity.has_role("clinician")
    assert session.access_token and session.refresh_token


def test_a_wrong_password_is_refused(provider):
    with pytest.raises(AuthError):
        provider.authenticate("dr.chen", "wrong")


def test_an_unknown_user_and_a_wrong_password_fail_alike(provider):
    """Distinguishing them tells an attacker which usernames exist."""
    unknown, wrong = None, None
    try:
        provider.authenticate("ghost", "x")
    except AuthError as e:
        unknown = str(e)
    try:
        provider.authenticate("dr.chen", "x")
    except AuthError as e:
        wrong = str(e)
    assert unknown == wrong


# ── the session file ─────────────────────────────────────────────────────


def test_save_then_load_round_trips(provider, store):
    session = provider.authenticate("dr.chen", "dr.chen")
    sessionmod.save(session, store)
    loaded = sessionmod.load(store)
    assert loaded is not None
    assert loaded.identity.username == "dr.chen"
    assert loaded.identity.roles == frozenset({"clinician", "prescriber"})
    assert loaded.refresh_token == session.refresh_token


def test_the_session_file_holds_no_surprises(provider, store):
    """A reader auditing what is stored should find tokens and identity, and
    nothing they did not expect — no password, for instance."""
    sessionmod.save(provider.authenticate("dr.chen", "dr.chen"), store)
    raw = json.loads(store.read_text(encoding="utf-8"))
    assert "dr.chen" not in json.dumps({k: v for k, v in raw.items() if "token" not in k and k != "username"})
    assert set(raw) == {
        "subject",
        "username",
        "roles",
        "provider_id",
        "is_service",
        "access_token",
        "refresh_token",
        "access_expires_at",
        "refresh_expires_at",
    }


def test_loading_a_missing_file_is_none(store):
    assert sessionmod.load(store) is None


def test_a_corrupt_file_reads_as_no_session(store):
    """The fix is the same either way — log in again — so a broken file is
    'no session', not a traceback."""
    store.write_text("{ not json", encoding="utf-8")
    assert sessionmod.load(store) is None


def test_clear_removes_it(provider, store):
    sessionmod.save(provider.authenticate("dr.chen", "dr.chen"), store)
    sessionmod.clear(store)
    assert sessionmod.load(store) is None
    sessionmod.clear(store)  # idempotent


# ── current_session: refresh in passing, or None ────────────────────────


def test_a_live_session_is_returned_unchanged(provider, store):
    sessionmod.save(provider.authenticate("dr.chen", "dr.chen"), store)
    got = current_session(provider, now=1000.0, path=store)
    assert got is not None and got.identity.username == "dr.chen"


def test_a_stale_access_token_is_refreshed(provider, store):
    session = provider.authenticate("dr.chen", "dr.chen")  # access expires at 1000+8h
    sessionmod.save(session, store)
    # past the access expiry, before the refresh expiry
    got = current_session(provider, now=1000.0 + 8 * 3600 + 1, path=store)
    assert got is not None
    assert got.access_token != session.access_token, "a new access token was issued"
    assert sessionmod.load(store).access_token == got.access_token, "and persisted"


def test_an_expired_refresh_token_yields_no_session(provider, store):
    sessionmod.save(provider.authenticate("dr.chen", "dr.chen"), store)
    got = current_session(provider, now=1000.0 + 3 * 86400 + 1, path=store)
    assert got is None
    assert sessionmod.load(store) is None, "the dead session file is cleared"


def test_no_session_file_yields_no_identity(provider, store):
    assert current_identity(provider, now=1000.0, path=store) is None


# ── the CLI handlers, against the fake ───────────────────────────────────


class _Args:
    def __init__(self, username=None):
        self.username = username


def test_login_stores_a_session(provider, store, monkeypatch):
    from hdh.core.identity import cli

    monkeypatch.setattr("getpass.getpass", lambda *_: "dr.chen")
    code = cli.cmd_login(_Args("dr.chen"), provider=provider, path=store)
    assert code == 0
    assert sessionmod.load(store).identity.username == "dr.chen"


def test_login_with_a_bad_password_fails_and_stores_nothing(provider, store, monkeypatch):
    from hdh.core.identity import cli

    monkeypatch.setattr("getpass.getpass", lambda *_: "wrong")
    code = cli.cmd_login(_Args("dr.chen"), provider=provider, path=store)
    assert code == 1
    assert sessionmod.load(store) is None


def test_whoami_reports_the_signed_in_user(provider, store, monkeypatch, capsys):
    from hdh.core.identity import cli

    sessionmod.save(provider.authenticate("dr.chen", "dr.chen"), store)
    code = cli.cmd_whoami(_Args(), provider=provider, now=1000.0, path=store)
    assert code == 0
    out = capsys.readouterr().out
    assert "dr.chen" in out
    assert "clinician" in out


def test_whoami_when_signed_out_says_so(provider, store, monkeypatch, capsys):
    from hdh.core.identity import cli

    code = cli.cmd_whoami(_Args(), provider=provider, now=1000.0, path=store)
    assert code == 1
    assert "not signed in" in capsys.readouterr().out.lower()


def test_logout_clears_and_revokes(provider, store, monkeypatch):
    from hdh.core.identity import cli

    session = provider.authenticate("dr.chen", "dr.chen")
    sessionmod.save(session, store)
    code = cli.cmd_logout(_Args(), provider=provider, path=store)
    assert code == 0
    assert sessionmod.load(store) is None
    # revoked server-side: a refresh with the old token now fails
    with pytest.raises(AuthError):
        provider.refresh(session.refresh_token)


def test_logout_when_signed_out_is_harmless(provider, store, monkeypatch):
    from hdh.core.identity import cli

    assert cli.cmd_logout(_Args(), provider=provider, path=store) == 0
