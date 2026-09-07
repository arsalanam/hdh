"""What a role may do, as data (AU3).

Permissions are ``domain:action`` strings, and a role grants a set of them.
The map lives here as data — not scattered through the write paths — for the
reason rubrics, prompts and schema semantics do: it will be tuned, and
tuning code is how drift starts. Adjusting who may approve a plan is an edit
to :data:`ROLE_PERMISSIONS`, not a hunt through call sites.

Grain: coarse, by domain, on purpose (decision §4.2 — "basic permissions").
A nurse and a clerk differ by *domain* (clinical chart vs the person record),
which this captures; finer entity-level scoping (a nurse may touch vitals but
not a diagnosis) is a later refinement that stays a data change here rather
than a redesign.

This module answers "may they" and nothing else — it resolves no identity,
opens no session, and enforces at no particular call site. Where the check
is wired is the enforcing module's decision.
"""

from __future__ import annotations

from hdh.core.identity.identity import Identity

# ── the vocabulary ───────────────────────────────────────────────────────
#
# Every permission a role can be granted. A catalogue, so a typo in a role
# map or a `require` call fails loudly against a known set rather than
# silently never matching.

DOMAINS = ("chart", "person", "careplan", "medication", "identity")
ACTIONS = ("view", "create", "edit", "void", "author", "approve", "reject", "fill", "admin")

PERMISSIONS: frozenset[str] = frozenset(
    {
        "chart:create",
        "chart:edit",
        "chart:void",
        "person:create",
        "person:edit",
        "careplan:author",
        "careplan:approve",
        "careplan:reject",
        "careplan:amend",
        "medication:create",
        "medication:fill",
        "medication:void",
        "identity:admin",
    }
)


# ── who may do what ────────────────────────────────────────────────────────
#
# The load-bearing artifact. Read it as the answer to "what can this role
# touch?", and change it here when that answer changes.

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # Full clinical write, and care-plan author AND approve — decision §4.2
    # deliberately lets a clinician approve their own work for now. That the
    # two are separate permissions is what makes tightening it later (drop
    # `careplan:approve` from this set) a one-line data change.
    "clinician": frozenset(
        {
            "chart:create",
            "chart:edit",
            "chart:void",
            "person:create",
            "person:edit",
            "careplan:author",
            "careplan:approve",
            "careplan:reject",
            "careplan:amend",
            "medication:create",
            "medication:fill",
            "medication:void",
        }
    ),
    # Medications and the approval of a plan's clinical content — a
    # prescriber signs off drugs and plans, but is not the general chart
    # editor a clinician is.
    "prescriber": frozenset(
        {
            "medication:create",
            "medication:fill",
            "medication:void",
            "careplan:approve",
            "careplan:reject",
        }
    ),
    # The clinical chart, but not prescribing or plan approval.
    "nurse": frozenset({"chart:create", "chart:edit"}),
    # The person record — demographics, contacts, coverage — and nothing
    # clinical.
    "clerk": frozenset({"person:create", "person:edit"}),
    # User/provider administration; explicitly NO clinical writes, so an
    # admin account cannot quietly edit a chart.
    "admin": frozenset({"identity:admin"}),
}


class Unauthorized(RuntimeError):
    """An identity attempted something its roles do not permit.

    Carries the missing permission so the refusal can name it — a user
    turned away should learn *what* they lack, not just *that* they were
    refused.
    """

    def __init__(self, permission: str, identity: Identity | None = None) -> None:
        self.permission = permission
        who = f" as {identity.username}" if identity is not None else ""
        super().__init__(f"not authorized{who}: this action needs '{permission}'")


class NotAuthenticated(RuntimeError):
    """A write was attempted with nobody signed in.

    Distinct from :class:`Unauthorized`: that is "you may not", this is "we
    do not yet know who you are". Writes require a login (design §2.7 / §4.4)
    — reads do not — so this turns into "run `hdh login`", not "wrong role".
    """

    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(
            f"making changes requires signing in. Run `hdh login` "
            f"(this action needs '{permission}'; your roles decide what you may change)."
        )


def permissions_for(roles) -> frozenset[str]:
    """Every permission granted by any of these roles, unioned.

    Unknown roles contribute nothing rather than raising: a realm can carry
    roles hdh does not map (``offline_access``), and those simply grant no
    hdh permission.
    """
    granted: set[str] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def may(identity: Identity, permission: str) -> bool:
    """Whether this identity holds the permission.

    A service account (``is_service``) holds everything: the generator, the
    pipeline and the eval harness act as the system and are trusted by
    construction (design §1, Q3). Their attribution still records which
    system account it was.
    """
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r} — not in the catalogue")
    if identity.is_service:
        return True
    return permission in permissions_for(identity.roles)


def require(identity: Identity, permission: str) -> None:
    """Raise :class:`Unauthorized` unless the identity holds the permission."""
    if not may(identity, permission):
        raise Unauthorized(permission, identity)
