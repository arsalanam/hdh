"""An identity becomes a chart actor with a provider_id (AU2).

AU1 established who a person is; this links that to which provider they
write as, so an audit event carries a `provider_id` and not only a name.
Enforcement (may they) is AU3 and not here.

The whole chain is exercised against SQLite with a fake identity — no
Keycloak — the same discipline as the rest of the suite.
"""

from __future__ import annotations

import pytest

from hdh.core.identity import Identity, provider_for, resolve_actor
from hdh.core.identity.accounts import link


@pytest.fixture()
def chart(tmp_path):
    from hdh.core.models import Base, Provider, Specialty, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    bootstrap_schema()
    engine = get_engine(str(tmp_path / "chart.db"))
    Base.metadata.create_all(engine)
    session = get_session(engine)
    spec = Specialty(code="FM", name="Family Medicine")
    session.add(spec)
    session.flush()
    provider = Provider(identifier="HDH-DEMO-dr.chen", name="Dr. Grace Chen", specialty_id=spec.id)
    session.add(provider)
    session.commit()
    yield session, provider
    session.close()
    engine.dispose()


def _identity(subject="s-chen", username="dr.chen", roles=("clinician",), provider_id=None):
    return Identity(subject=subject, username=username, roles=frozenset(roles), provider_id=provider_id)


# ── the link ─────────────────────────────────────────────────────────────


def test_link_then_lookup(chart):
    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    assert provider_for(session, "s-chen") == provider.id


def test_an_unlinked_subject_has_no_provider(chart):
    session, _provider = chart
    assert provider_for(session, "nobody") is None


def test_link_is_idempotent_on_subject(chart):
    """A second login must update the row, not add one — the subject is the
    key, and a subject identifies one person."""
    from hdh.core.models import UserAccount

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    link(session, "s-chen", "dr.chen", provider.id)
    assert session.query(UserAccount).filter_by(subject="s-chen").count() == 1


def test_a_moved_provider_updates_in_place(chart):
    from hdh.core.models import Provider, UserAccount

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    other = Provider(identifier="HDH-DEMO-other", name="Dr. Other")
    session.add(other)
    session.flush()
    link(session, "s-chen", "dr.chen", other.id)
    assert session.query(UserAccount).filter_by(subject="s-chen").count() == 1
    assert provider_for(session, "s-chen") == other.id


def test_the_username_is_kept_current(chart):
    """Stored beside the subject for readability, and it is the mutable
    half — a rename updates it while the subject anchors attribution."""
    from hdh.core.models import UserAccount

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    link(session, "s-chen", "grace.chen", provider.id)  # renamed
    row = session.query(UserAccount).filter_by(subject="s-chen").first()
    assert row.username == "grace.chen"


# ── the resolver ─────────────────────────────────────────────────────────


def test_resolve_actor_carries_the_linked_provider(chart):
    from hdh.core.models import EditSource

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    actor = resolve_actor(session, _identity(), EditSource.AGENT)
    assert actor.name == "dr.chen"
    assert actor.provider_id == provider.id
    assert actor.source == EditSource.AGENT


def test_an_unlinked_identity_is_still_an_actor(chart):
    """Attribution by name works before an account is tied to a profile —
    provider_id is None, not an error."""
    from hdh.core.models import EditSource

    session, _provider = chart
    actor = resolve_actor(session, _identity(subject="unlinked"), EditSource.CLI)
    assert actor.name == "dr.chen"
    assert actor.provider_id is None


def test_the_source_comes_from_the_caller_not_the_identity(chart):
    """The same person is a different EditSource depending on the surface,
    so the caller supplies it."""
    from hdh.core.models import EditSource

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    assert resolve_actor(session, _identity(), EditSource.CLI).source == EditSource.CLI
    assert resolve_actor(session, _identity(), EditSource.AGENT).source == EditSource.AGENT


def test_an_identity_that_already_knows_its_provider_is_trusted(chart):
    """If auth resolved the provider already, the resolver does not re-query."""
    from hdh.core.models import EditSource

    session, provider = chart
    actor = resolve_actor(session, _identity(provider_id=provider.id), EditSource.AGENT)
    assert actor.provider_id == provider.id


# ── cli_actor: signed-in identity, or the OS user ───────────────────────


def test_cli_actor_uses_the_signed_in_identity(chart, tmp_path):
    from hdh.core.identity import FakeProvider, cli_actor, save
    from hdh.core.models import EditSource

    session, provider = chart
    link(session, "s-chen", "dr.chen", provider.id)
    fake = FakeProvider(now=1000.0)
    fake.add("dr.chen", "pw", _identity())
    store = tmp_path / "session.json"
    save(fake.authenticate("dr.chen", "pw"), store)
    actor = cli_actor(session, provider=fake, now=1000.0, path=store)
    assert actor.name == "dr.chen"
    assert actor.provider_id == provider.id
    assert actor.source == EditSource.CLI


def test_cli_actor_falls_back_to_the_os_user_when_not_signed_in(chart, tmp_path):
    """Reads and CLI writes stay open to a developer who has not logged in
    (§2.7); the write is still attributed, to the OS user."""
    from hdh.core.identity import FakeProvider, cli_actor
    from hdh.core.models import EditSource

    session, _provider = chart
    fake = FakeProvider(now=1000.0)
    actor = cli_actor(session, provider=fake, now=1000.0, path=tmp_path / "none.json")
    assert actor.source == EditSource.CLI
    assert actor.name  # some OS user name, not empty


# ── seed ──────────────────────────────────────────────────────────────────


def test_seed_links_every_demo_account(chart):
    from hdh.core.identity.demo import DEMO_ACCOUNTS
    from hdh.core.identity.seed import seed_demo_identities
    from hdh.core.models import UserAccount

    session, _provider = chart
    n = seed_demo_identities(session)
    assert n == len(DEMO_ACCOUNTS)
    for account in DEMO_ACCOUNTS:
        assert provider_for(session, account.subject) is not None
    assert session.query(UserAccount).count() == len(DEMO_ACCOUNTS)


def test_seed_is_idempotent(chart):
    from hdh.core.identity.seed import seed_demo_identities
    from hdh.core.models import Provider, UserAccount

    session, _provider = chart
    seed_demo_identities(session)
    providers_after_first = session.query(Provider).count()
    seed_demo_identities(session)
    assert session.query(UserAccount).count() == 5
    assert session.query(Provider).count() == providers_after_first, "no duplicate providers"


def test_the_seeded_subjects_match_the_realm(chart):
    """The realm pins these subjects; the seed links on them. If the two
    lists diverged, a demo user would log in and the chart would not know
    who they were — the exact failure the shared source of truth prevents."""
    from hdh.core.identity.demo import DEMO_ACCOUNTS
    from hdh.core.identity.seed import seed_demo_identities

    session, _provider = chart
    seed_demo_identities(session)
    chen = next(a for a in DEMO_ACCOUNTS if a.username == "dr.chen")
    # a login yields this subject; the resolver must find the link
    from hdh.core.models import EditSource

    actor = resolve_actor(session, _identity(subject=chen.subject), EditSource.AGENT)
    assert actor.provider_id is not None
