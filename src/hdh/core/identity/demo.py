"""The demo identities, in one place so the realm and the chart cannot drift.

`scripts/render_keycloak_realm.py` writes these as Keycloak users; the
identity seed writes the matching `user_accounts` rows and provider
profiles. Both read this list, so a username that logs in is always a
username the chart can attribute — the failure where a login succeeds and
then nothing knows who logged in cannot happen for the demo set.

The subjects are fixed UUIDs rather than left for Keycloak to mint, because
`user_accounts` links on the subject and the seed has to know it before
anyone logs in. Fixing them in the realm import is what makes that possible.

Named people, not role labels (decision §4.1): "Dr. Chen approved this"
should read like a person. When the generator grows named clinicians per
practice (§4.1, with #156's re-baseline), these link onto real generated
providers; until then the seed creates a provider profile per demo user so
`provider_id` is populated rather than NULL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DemoAccount:
    username: str
    subject: str  # stable Keycloak `sub`, pinned in the realm import
    first_name: str
    last_name: str
    roles: tuple[str, ...]
    specialty_code: str  # for the seeded provider profile


DEMO_ACCOUNTS: tuple[DemoAccount, ...] = (
    DemoAccount(
        "dr.chen", "0a11e5e0-0000-4000-a000-000000000001", "Grace", "Chen", ("clinician", "prescriber"), "FM"
    ),
    DemoAccount("dr.okafor", "0a11e5e0-0000-4000-a000-000000000002", "Ada", "Okafor", ("prescriber",), "IM"),
    DemoAccount("nurse.reed", "0a11e5e0-0000-4000-a000-000000000003", "Sam", "Reed", ("nurse",), "FM"),
    DemoAccount("clerk.diaz", "0a11e5e0-0000-4000-a000-000000000004", "Robin", "Diaz", ("clerk",), "FM"),
    DemoAccount("admin", "0a11e5e0-0000-4000-a000-000000000005", "System", "Administrator", ("admin",), "FM"),
)


def display_name(account: DemoAccount) -> str:
    """The provider-profile name, matching the chart's 'Dr. Chen' convention."""
    title = "Dr. " if any(r in ("clinician", "prescriber") for r in account.roles) else ""
    return f"{title}{account.first_name} {account.last_name}"
