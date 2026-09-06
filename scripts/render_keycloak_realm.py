"""Render deps/keycloak-realm.json from the environment (AU1).

Keycloak's own `${ENV}` substitution into realm-import files is fragile
across versions and does not reliably reach numeric session fields, so the
realm is built here instead — from `HDH_AUTH_SESSION_HOURS` and
`HDH_AUTH_REFRESH_DAYS` — and written as a concrete file the container
imports. `just deps` runs this before `docker compose up`, so changing a
session lifetime is one `.env` edit and a restart (design §4.3).

The realm: roles one per clinical function, a public direct-access client
for the CLI, and one demo user per role. Passwords are the username — this
is a synthetic, local, demo realm and says so; nobody signs into anything
real with it.

    uv run python scripts/render_keycloak_realm.py

Committed output keeps `docker compose up` working without the render step;
re-running picks up changed env.
"""

from __future__ import annotations

import json
import os
import pathlib

from hdh.core.identity.demo import DEMO_ACCOUNTS

OUT = pathlib.Path(__file__).resolve().parents[1] / "deps" / "keycloak-realm.json"

#: Realm role -> the permission grain AU3 will enforce. Recorded here as
#: documentation only; enforcement is data in core.identity later, not in
#: Keycloak. `clinician` holds approve as well as author (decision §4.2).
ROLES = {
    "prescriber": "medications, refills, plan approval",
    "clinician": "full chart write, care-plan authoring AND approval",
    "nurse": "vitals, immunizations, functional status, notes",
    "clerk": "demographics, contacts, coverage — the person record",
    "admin": "user/provider administration; no clinical writes",
}


# The demo users come from core.identity.demo — the one source of truth the
# DB seed also reads, so a username that logs in is always one the chart can
# attribute. Their subjects are pinned there (not minted by Keycloak) so the
# seed can link on them before anyone logs in.
def _demo_accounts():
    return DEMO_ACCOUNTS


def _seconds() -> tuple[int, int]:
    hours = float(os.environ.get("HDH_AUTH_SESSION_HOURS", "8"))
    days = float(os.environ.get("HDH_AUTH_REFRESH_DAYS", "3"))
    return int(hours * 3600), int(days * 86400)


def build_realm() -> dict:
    session_seconds, refresh_seconds = _seconds()
    return {
        "realm": "hdh",
        "enabled": True,
        # The access-token / SSO session lifetime, and the refresh (idle)
        # lifetime — the two the design makes env-configurable.
        "accessTokenLifespan": session_seconds,
        "ssoSessionMaxLifespan": refresh_seconds,
        "ssoSessionIdleTimeout": refresh_seconds,
        "roles": {"realm": [{"name": name, "description": desc} for name, desc in ROLES.items()]},
        "clients": [
            {
                "clientId": "hdh-cli",
                "enabled": True,
                "publicClient": True,  # a CLI keeps no secret
                "directAccessGrantsEnabled": True,  # the resource-owner password flow
                "standardFlowEnabled": False,
                "serviceAccountsEnabled": False,
            }
        ],
        "users": [
            {
                # `id` pins the Keycloak subject so `user_accounts` can link
                # on it before first login (see core.identity.demo).
                "id": acct.subject,
                "username": acct.username,
                "enabled": True,
                "firstName": acct.first_name,
                "lastName": acct.last_name,
                "email": f"{acct.username}@hdh.local",
                "emailVerified": True,
                "credentials": [{"type": "password", "value": acct.username, "temporary": False}],
                "realmRoles": list(acct.roles),
            }
            for acct in _demo_accounts()
        ],
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build_realm(), indent=2) + "\n", encoding="utf-8")
    session_seconds, refresh_seconds = _seconds()
    print(
        f"wrote {OUT.relative_to(pathlib.Path.cwd())} "
        f"(access {session_seconds}s, refresh {refresh_seconds}s, "
        f"{len(_demo_accounts())} users, {len(ROLES)} roles)"
    )


if __name__ == "__main__":
    main()
