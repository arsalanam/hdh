"""Seed the demo accounts into the chart database (AU2).

For each demo identity: ensure a provider profile exists, and link the
Keycloak subject to it in ``user_accounts``. Idempotent — re-running
reconciles rather than duplicating — so it is safe to run on every
``just deps`` or after a cohort rebuild.

The provider profiles are created here rather than by the generator because
the named-clinician generator change (§4.1) is deferred to #156's
re-baseline. When it lands, this reconciles onto the generated providers
instead of minting its own; until then it is what makes ``provider_id``
populated rather than NULL for the demo set.
"""

from __future__ import annotations

from hdh.core.identity.demo import DEMO_ACCOUNTS, DemoAccount, display_name

#: A stable, obviously-synthetic identifier so a re-run finds the same row
#: and nobody mistakes these for real NPIs.
_IDENTIFIER_PREFIX = "HDH-DEMO-"


def _provider_for(session, account: DemoAccount):
    """The provider profile for a demo account, created if absent."""
    from hdh.core.models import Provider, Specialty

    identifier = f"{_IDENTIFIER_PREFIX}{account.username}"
    provider = session.query(Provider).filter_by(identifier=identifier).first()
    if provider is not None:
        return provider

    specialty = session.query(Specialty).filter_by(code=account.specialty_code).first()
    provider = Provider(
        identifier=identifier,
        name=display_name(account),
        specialty_id=specialty.id if specialty else None,
    )
    session.add(provider)
    session.flush()
    return provider


def seed_demo_identities(session) -> int:
    """Ensure a provider profile and account link for every demo user.

    Returns the number of accounts linked. Clerks and admins get a provider
    profile too — attribution wants a name for them, even where they hold no
    clinical write role.
    """
    from hdh.core.identity.accounts import link

    for account in DEMO_ACCOUNTS:
        provider = _provider_for(session, account)
        link(session, account.subject, account.username, provider.id)
    session.commit()
    return len(DEMO_ACCOUNTS)
