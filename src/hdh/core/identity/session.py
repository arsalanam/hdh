"""The on-disk session: ``~/.hdh/session.json``, owner-readable only.

The tokens live here and only here. A checkpoint or the agent's graph state
keeps the user id and re-resolves authorization against a live session
(design §2.4), because a token in a durable store outlives the authority it
represents.

``current_session`` is the one function callers use: it loads, silently
refreshes an expired access token while the refresh token is still good,
and returns ``None`` when there is no usable session — which the caller
turns into "run `hdh login` first".
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from hdh.core.identity.identity import AuthError, AuthSession, Identity, IdentityProvider

SESSION_PATH = Path.home() / ".hdh" / "session.json"


def save(session: AuthSession, path: Path = SESSION_PATH) -> None:
    """Write the session, readable by its owner and nobody else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject": session.identity.subject,
        "username": session.identity.username,
        "roles": sorted(session.identity.roles),
        "provider_id": session.identity.provider_id,
        "is_service": session.identity.is_service,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "access_expires_at": session.access_expires_at,
        "refresh_expires_at": session.refresh_expires_at,
    }
    # Write then chmod, and write to a temp name first so a crash mid-write
    # cannot leave a half-file that reads as a corrupt session.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0o600; a no-op on Windows ACLs
    except OSError:
        pass
    tmp.replace(path)


def load(path: Path = SESSION_PATH) -> AuthSession | None:
    """The stored session, or None if there is none or it is unreadable.

    A corrupt file is treated as no session rather than an error: the fix is
    the same either way — log in again — and a stack trace helps nobody.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AuthSession(
            identity=Identity(
                subject=raw["subject"],
                username=raw["username"],
                roles=frozenset(raw.get("roles") or ()),
                provider_id=raw.get("provider_id"),
                is_service=bool(raw.get("is_service", False)),
            ),
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            access_expires_at=float(raw["access_expires_at"]),
            refresh_expires_at=float(raw["refresh_expires_at"]),
        )
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def clear(path: Path = SESSION_PATH) -> None:
    """Delete the session file. Safe to call when there is none."""
    path.unlink(missing_ok=True)


def current_session(
    provider: IdentityProvider,
    now: float,
    path: Path = SESSION_PATH,
) -> AuthSession | None:
    """The usable session, refreshing a stale access token in passing.

    Returns None when there is no session, or the refresh token has also
    expired — both mean "log in again". ``now`` is passed rather than read
    so the refresh boundary is testable.
    """
    session = load(path)
    if session is None:
        return None
    if now < session.access_expires_at:
        return session
    if now >= session.refresh_expires_at:
        clear(path)  # nothing left to refresh from; do not leave a dead file
        return None
    try:
        refreshed = provider.refresh(session.refresh_token)
    except AuthError:
        clear(path)
        return None
    save(refreshed, path)
    return refreshed


def current_identity(
    provider: IdentityProvider,
    now: float,
    path: Path = SESSION_PATH,
) -> Identity | None:
    """Just the actor, for callers that do not touch tokens."""
    session = current_session(provider, now, path)
    return session.identity if session else None
