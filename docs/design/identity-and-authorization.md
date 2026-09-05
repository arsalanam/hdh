# Identity — Who Is Asking, Before What May They Do (Draft)

**Where it lives:** `hdh.core.identity` (the seam) · Keycloak (the provider,
as a dependency container) · **Completes:**
[attribution-and-audit.md](attribution-and-audit.md) — that design asks *who
changed this*; this one makes "who" a real person rather than a string

---

## 1. The proposal, and why it is the right shape

Attribution today bottoms out in hardcoded strings. `careplan/persist.py`
writes every event as `actor_name="care-plan review"`, orders write "entered
at the CLI", and `chart_audit_events.provider_id` — a column that has existed
since the trail was built — is never populated. The `Actor` contract
anticipated this explicitly:

> Injected — never inferred from ambient state. Provider-level attribution is
> deliberate: a real user/account concept arrives with authentication.

The proposal: **Keycloak as a dependency container, a user/authorization
seam in core, provider profiles linked to accounts, and the agent operating
*as* the logged-in user** — so a care plan created by one clinician and
amended by another carries both identities in the trail, and the guardrail
can refuse an action the current user is not authorized to take.

The interaction: `hdh login` prompts for user id and password; every
subsequent command — CLI or agent — runs as that person until `hdh logout`.

This answers two of #162's open questions directly:

- **Q3 (does the generator need to declare itself?)** — yes, as a *service
  account*: `system:generator`, `system:pipeline`, `system:eval`, issued by
  client-credentials grant. Bulk construction is then an authenticated actor
  like any other, not a hole in the gate.
- **Q1's real substance** — role granularity, not entity granularity, is
  what distinguishes a name correction from a registered-GP change.

---

## 2. Decisions

### 2.1 Keycloak is the provider behind a seam, not the seam

`hdh.core.identity` defines what the rest of the system consumes:

```python
@dataclass(frozen=True)
class Identity:
    subject: str          # Keycloak's stable `sub`
    username: str         # what the audit trail shows
    provider_id: int | None   # link into the providers table
    roles: frozenset[str]
    is_service: bool      # a system:* account
```

Everything else — chartedit, careplan persist, refills, the agent pipeline —
takes an `Identity` and never sees a token, an OIDC claim, or a Keycloak
URL. Keycloak is the implementation because building password storage,
session management and role administration by hand is both more work and
worse; the seam is there so the implementation stays swappable and so tests
run against a fake identity provider with zero containers.

The container joins `docker-compose.deps.yml` beside PostgreSQL and Redis,
with a realm import file (`deps/keycloak-realm.json`) holding the demo
realm, roles and seed users — so `just deps` brings up a working login with
no clicking through an admin console.

### 2.2 `hdh login` is the resource-owner password flow

Exactly the UX proposed: type a user id, get prompted for a password. That
is OAuth2 ROPC (Direct Access Grants) against our own realm.

Stated honestly: OAuth 2.1 deprecates ROPC, because it teaches users to type
passwords into third-party clients. Neither concern applies to a first-party
CLI against its own local realm — but the deprecation is real, so the login
command is one function behind the seam and a move to the device flow later
changes nothing else.

Tokens (access + refresh) land in `~/.hdh/session.json` with owner-only
permissions. Refresh is silent; `hdh logout` deletes the file and revokes
the refresh token. `hdh whoami` says who you are and what roles you hold.

### 2.3 Enforcement lives in core; the guardrail is the polite refusal

Two checks, deliberately redundant:

- **The agent guardrail** asks, before executing a mutating tool, whether
  the current identity holds the permission — and refuses with the missing
  permission named, so the user learns *why* rather than watching a
  stack trace.
- **The mutation path itself** — `chartedit.apply_edits`,
  `persist_reviewed_plan`, `decide`, `amend_plan`, `record_fill` — requires
  an `Identity` and checks the same permission again.

The second is the boundary; the first is manners. This repository's whole
history says why both: every convention that only some call sites observed
has drifted. An agent that forgot to ask the guardrail must still hit the
wall in core.

### 2.4 The LangGraph state carries the user id, never the token

A care-plan review can pause for days and resume across restarts. The
state therefore stores `subject`/`username` for attribution — and
**authorization is re-resolved at action time** against the live session.

Storing a token in a checkpoint would mean a revoked user's paused review
could still approve a plan a week later; storing only the id means resuming
a session re-authenticates, and a user whose roles changed mid-review is
checked against what they hold *now*. Attribution is historical;
authorization never is.

This is also what makes the proposed scenario work: user A builds and saves
a plan (trail: `create` by A), logs out; user B logs in, amends it (trail:
`amend`/supersede by B). The supersede model already keeps both records —
it was only ever missing real names to put in them.

### 2.5 Roles are few, permissions are data

Realm roles, proposed:

| role | intent |
|---|---|
| `prescriber` | medications: orders, refills, plan approval |
| `clinician` | full chart write, care-plan authoring and decisions |
| `nurse` | vitals, immunizations, functional status, notes |
| `clerk` | the person-record tables: demographics, contacts, coverage |
| `admin` | user↔provider linking; no clinical writes |

Role → permission mapping lives in core as **data** (entity × action, the
same grain as chartedit's registry), not scattered in code — the same
pattern as rubrics, prompts and semantics, and for the same reason: it will
be tuned, and tuning code is how drift starts.

One deliberate split the careplan module anticipated in its own docstring
(*"the split is also where a permission boundary would go if one ever
arrives"*): **saving a plan and deciding a plan are different permissions.**
An author who cannot approve their own work is the ordinary clinical
arrangement, and now expressible.

### 2.6 The identity system is outside the agent's reach

No agent tool creates users, edits roles, or reads credentials. The
Keycloak admin console and realm file are the only ways in. The agent
consumes identity; it never administers it. (Same boundary the proposal
drew — stated here so a future "add a user-management tool" PR has to argue
with a design document rather than a habit.)

### 2.7 Reads stay open, writes require login

Synthetic data, demo system: browsing a chart anonymously stays possible,
and gating reads would make every quickstart step grow a login. Every
**mutation** requires an identity. If a deployment ever wants closed reads,
that is one flag on the seam, not a redesign.

---

## 3. Milestones

Interleaved with #162's A-series — identity is what makes A1/A2 worth
doing, since an edit path that records "cli" learns nothing new.

| | | depends on |
|---|---|---|
| **AU1** | Keycloak in `just deps` + realm import; `hdh login / logout / whoami`; `core.identity` with the fake provider for tests | — |
| **AU2** | `user_accounts` (subject ↔ provider_id) + seed linking; `Actor` built from `Identity`; hardcoded actor strings die; `provider_id` finally populated | AU1 |
| **AU3** | permissions as data; enforcement in chartedit/persist/refills; guardrail pre-check tool; service accounts for generator/pipeline/eval | AU2 |
| **AU4** | user id into agent pipeline + LangGraph state; re-auth on resume; `hdh chart history --actor` and "what did X do today" | AU2 (A4 lands here too) |

A2 from #162 (the fill writes a trail) should not wait for any of this — it
is one event write with the actor already in hand.

---

## 4. Decisions (2026-09-05)

The four questions §3 left open, answered.

### 4.1 The generator grows named clinicians per practice

Demo accounts do not link to the three thin seeded `providers`; the
generator produces named clinicians and the realm's demo users map onto
them, so "Dr. Chen approved this plan" is a person who also appears as a
`registered_provider_id` and in `Procedure.provider_id`.

*The consequence, so it is planned rather than discovered:* this is a
generator change, and a generator change moves what the pinned seed
produces — cohort version bump, `compare` refusal, re-baseline. That
composes cleanly with #156, which requires a re-baseline anyway: land the
named-clinician generator change **before or with** the re-baseline, so one
bump covers both.

### 4.2 Roles carry basic permission sets; author-approves-own is allowed

Every role gets a small permission set as data. For now the `clinician`
role holds **create, view, edit and approve** — one user can author and
approve the same plan.

Author≠approver stays *expressible* and unenforced: because the mapping is
data at the entity×action grain, tightening it later — removing `approve`
from the authoring role, or adding a not-own-work rule — is a configuration
change and a test, not a redesign. That is the payoff §2.5 was bought for.

### 4.3 Sessions: 8-hour access, 3-day refresh, both from env

| | default | env |
|---|---|---|
| access-token / SSO session | 8 hours | `HDH_AUTH_SESSION_HOURS` |
| refresh-token idle | 3 days | `HDH_AUTH_REFRESH_DAYS` |

Applied to the realm at `just deps` import time, so changing them is an
`.env` edit and a container restart — no admin-console clicking. Generous
by design; see 4.4 for what that generosity buys.

### 4.4 `hdh agent` refuses outright without login

No anonymous agent sessions, ever — nobody builds the habit, and every
agent action has an actor from the first turn. The refusal names the fix:

```
hdh agent: not logged in. Run `hdh login` first.
```

The generous session and refresh defaults in 4.3 are the other half of this
decision: refusing outright is only reasonable when login is a
once-a-morning event rather than a recurring interruption.

Direct CLI *reads* (`hdh show`, `hdh stats`) stay open per §2.7 — the
question and the answer were about the agent, and gating a read-only
quickstart would tax the first five minutes of every new user for no
attribution gain. Every mutation requires login regardless of surface.
