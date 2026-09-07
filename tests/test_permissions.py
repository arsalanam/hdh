"""What a role may do, and CLI writes gated by it (AU3).

The permission map is the load-bearing artifact — these tests pin who may do
what, so a change to `ROLE_PERMISSIONS` that widens or narrows a role fails
loudly here rather than in production. Enforcement is wired at the CLI write
paths (chartedit, orders); the agent-side paths enforce in AU4, when the
identity reaches the graph.
"""

from __future__ import annotations

import pytest

from hdh.core.identity import Identity, may, permissions_for, require
from hdh.core.identity.permissions import (
    PERMISSIONS,
    ROLE_PERMISSIONS,
    NotAuthenticated,
    Unauthorized,
)


def _who(*roles, service=False):
    return Identity("s", "someone", frozenset(roles), is_service=service)


# ── the map is coherent ──────────────────────────────────────────────────


def test_every_granted_permission_is_in_the_catalogue():
    """A typo in a role map is a permission that silently never matches. The
    catalogue is what makes it a loud error instead."""
    for role, perms in ROLE_PERMISSIONS.items():
        unknown = perms - PERMISSIONS
        assert not unknown, f"{role} grants permissions not in the catalogue: {unknown}"


def test_permissions_for_unions_roles():
    who = _who("nurse", "clerk")
    assert permissions_for(who.roles) == permissions_for(frozenset({"nurse"})) | permissions_for(
        frozenset({"clerk"})
    )


def test_an_unmapped_role_grants_nothing():
    """A realm role hdh does not know (offline_access) simply grants no hdh
    permission — it is not an error."""
    assert permissions_for(frozenset({"offline_access"})) == frozenset()


# ── who may do what (the decisions, pinned) ──────────────────────────────


def test_clinician_authors_and_approves():
    """Decision §4.2: a clinician may approve their own work, for now."""
    who = _who("clinician")
    assert may(who, "careplan:author")
    assert may(who, "careplan:approve")
    assert may(who, "chart:edit")
    assert may(who, "chart:void")


def test_prescriber_prescribes_and_approves_but_is_not_the_chart_editor():
    who = _who("prescriber")
    assert may(who, "medication:create")
    assert may(who, "medication:fill")
    assert may(who, "careplan:approve")
    assert not may(who, "chart:void")
    assert not may(who, "careplan:author")


def test_nurse_edits_the_chart_but_neither_prescribes_nor_approves():
    who = _who("nurse")
    assert may(who, "chart:edit")
    assert not may(who, "medication:create")
    assert not may(who, "careplan:approve")


def test_clerk_touches_the_person_record_and_nothing_clinical():
    who = _who("clerk")
    assert may(who, "person:edit")
    assert not may(who, "chart:edit")
    assert not may(who, "medication:create")


def test_admin_has_no_clinical_write():
    """An admin administers users; it must not be able to quietly edit a
    chart."""
    who = _who("admin")
    assert not may(who, "chart:edit")
    assert not may(who, "chart:void")
    assert not may(who, "careplan:approve")
    assert may(who, "identity:admin")


# ── service accounts ─────────────────────────────────────────────────────


def test_a_service_account_holds_everything():
    """The generator, pipeline and eval act as the system and are trusted by
    construction (§1 Q3). Attribution still records which system it was."""
    sysacct = _who(service=True)
    for perm in PERMISSIONS:
        assert may(sysacct, perm)


# ── require / errors ─────────────────────────────────────────────────────


def test_require_passes_when_permitted():
    require(_who("clinician"), "chart:edit")  # no raise


def test_require_names_the_missing_permission():
    with pytest.raises(Unauthorized) as e:
        require(_who("nurse"), "careplan:approve")
    assert e.value.permission == "careplan:approve"
    assert "careplan:approve" in str(e.value)
    assert "nurse" not in str(e.value) or "someone" in str(e.value)  # names the user, not the role


def test_an_unknown_permission_is_a_loud_error():
    with pytest.raises(ValueError):
        may(_who("clinician"), "chart:teleport")


def test_not_authenticated_says_log_in():
    err = NotAuthenticated("chart:edit")
    assert "hdh login" in str(err)
    assert "chart:edit" in str(err)


# ── authorize_cli: login required, permission checked ────────────────────


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "c.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    yield session
    session.close()
    engine.dispose()


def _logged_in(tmp_path, identity):
    from hdh.core.identity import FakeProvider, save

    fake = FakeProvider(now=1000.0)
    fake.add(identity.username, "pw", identity)
    store = tmp_path / "session.json"
    save(fake.authenticate(identity.username, "pw"), store)
    return fake, store


def test_authorize_cli_refuses_when_not_signed_in(chart, tmp_path):
    from hdh.core.identity import FakeProvider, authorize_cli

    with pytest.raises(NotAuthenticated):
        authorize_cli(
            chart, "chart:edit", provider=FakeProvider(now=1000.0), now=1000.0, path=tmp_path / "none.json"
        )


def test_authorize_cli_allows_a_permitted_identity(chart, tmp_path):
    from hdh.core.identity import authorize_cli

    fake, store = _logged_in(tmp_path, Identity("s", "dr.chen", frozenset({"clinician"})))
    identity = authorize_cli(chart, "chart:edit", provider=fake, now=1000.0, path=store)
    assert identity.username == "dr.chen"


def test_authorize_cli_refuses_a_permitted_login_lacking_the_role(chart, tmp_path):
    from hdh.core.identity import authorize_cli

    fake, store = _logged_in(tmp_path, Identity("s", "nurse.reed", frozenset({"nurse"})))
    with pytest.raises(Unauthorized):
        authorize_cli(chart, "careplan:approve", provider=fake, now=1000.0, path=store)
