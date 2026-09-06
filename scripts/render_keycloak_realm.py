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

#: One demo account per role. Named people, not role labels, because AU2
#: links them to generated clinicians and "Dr. Chen approved this" should
#: read like a person (decision §4.1).
USERS = [
    ("dr.chen", "Grace", "Chen", ["clinician", "prescriber"]),
    ("dr.okafor", "Ada", "Okafor", ["prescriber"]),
    ("nurse.reed", "Sam", "Reed", ["nurse"]),
    ("clerk.diaz", "Robin", "Diaz", ["clerk"]),
    ("admin", "System", "Administrator", ["admin"]),
]


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
                "username": username,
                "enabled": True,
                "firstName": first,
                "lastName": last,
                "email": f"{username}@hdh.local",
                "emailVerified": True,
                "credentials": [{"type": "password", "value": username, "temporary": False}],
                "realmRoles": roles,
            }
            for username, first, last, roles in USERS
        ],
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build_realm(), indent=2) + "\n", encoding="utf-8")
    session_seconds, refresh_seconds = _seconds()
    print(
        f"wrote {OUT.relative_to(pathlib.Path.cwd())} "
        f"(access {session_seconds}s, refresh {refresh_seconds}s, "
        f"{len(USERS)} users, {len(ROLES)} roles)"
    )


if __name__ == "__main__":
    main()
