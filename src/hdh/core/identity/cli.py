"""`hdh login`, `hdh logout`, `hdh whoami` (AU1).

These take no database session and open no engine — identity is resolved
before the chart is touched, which is why they are dispatched ahead of
`get_engine` in the CLI. The password is read with `getpass`, never taken
as an argument, so it cannot land in shell history or a process list.
"""

from __future__ import annotations

import time


def register_cli(subparsers) -> None:
    login = subparsers.add_parser("login", help="Sign in as a provider (prompts for a password)")
    login.add_argument("username", nargs="?", help="Your user id; prompted for if omitted")

    subparsers.add_parser("logout", help="Sign out and revoke the session")
    subparsers.add_parser("whoami", help="Show who you are signed in as, and your roles")


def cmd_login(args, provider=None, path=None) -> int:
    """Authenticate and store the session. Returns a process exit code.

    ``path`` is injectable for the same reason ``provider`` is: a test must
    write its session somewhere other than the developer's real
    ``~/.hdh/session.json``.
    """
    import getpass

    from hdh.core.identity import SESSION_PATH, AuthError, default_provider, save

    provider = provider or default_provider()
    path = path or SESSION_PATH
    username = args.username or input("User id: ").strip()
    if not username:
        print("login needs a user id.")
        return 1
    password = getpass.getpass("Password: ")

    try:
        session = provider.authenticate(username, password)
    except AuthError as err:
        print(f"login failed: {err}")
        return 1

    save(session, path)
    identity = session.identity
    roles = ", ".join(sorted(identity.roles)) or "no roles"
    print(f"signed in as {identity.username} ({roles}).")
    return 0


def cmd_logout(args, provider=None, path=None) -> int:
    """Revoke the refresh token and delete the local session."""
    from hdh.core.identity import SESSION_PATH, clear, default_provider, load

    path = path or SESSION_PATH
    session = load(path)
    if session is None:
        print("not signed in.")
        return 0
    # Revoke server-side first, then clear locally regardless — a server we
    # cannot reach must not strand the user in a session they cannot end.
    provider = provider or default_provider()
    provider.logout(session.refresh_token)
    clear(path)
    print(f"signed out {session.identity.username}.")
    return 0


def cmd_whoami(args, provider=None, now: float | None = None, path=None) -> int:
    """Report the current identity, refreshing a stale token if it can."""
    from hdh.core.identity import SESSION_PATH, current_session, default_provider

    provider = provider or default_provider()
    path = path or SESSION_PATH
    session = current_session(provider, time.time() if now is None else now, path)
    if session is None:
        print("not signed in. Run `hdh login`.")
        return 1

    identity = session.identity
    print(f"user     : {identity.username}")
    print(f"subject  : {identity.subject}")
    print(f"roles    : {', '.join(sorted(identity.roles)) or 'none'}")
    if identity.is_service:
        print("account  : service (system)")
    if identity.provider_id is not None:
        print(f"provider : #{identity.provider_id}")
    return 0
