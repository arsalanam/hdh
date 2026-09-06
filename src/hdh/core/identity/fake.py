"""An in-memory identity provider, so tests need no Keycloak.

The same reason `stub_extractor` exists for comprehension and `stub_grader`
for the rubric: `just qa` must run with no container and no network. This
issues tokens that are opaque strings rather than real JWTs — nothing in
the seam decodes them, because the `Identity` is carried alongside, not
extracted from the token.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hdh.core.identity.identity import AuthError, AuthSession, Identity

#: Generous, matching the deployed defaults' spirit (design §4.3). Tests
#: that care about expiry override these when constructing the provider.
_ACCESS_TTL = 8 * 3600
_REFRESH_TTL = 3 * 24 * 3600


@dataclass
class FakeProvider:
    """A provider backed by a dict of accounts.

    ``now`` is injected rather than read from the clock so expiry is
    testable without waiting; ``AuthSession`` carries epoch seconds and the
    caller decides what "now" means.
    """

    accounts: dict[str, tuple[str, Identity]] = field(default_factory=dict)
    now: float = 0.0
    access_ttl: int = _ACCESS_TTL
    refresh_ttl: int = _REFRESH_TTL
    _issued: int = 0
    _revoked: set[str] = field(default_factory=set)

    def add(self, username: str, password: str, identity: Identity) -> None:
        self.accounts[username] = (password, identity)

    def _session(self, identity: Identity) -> AuthSession:
        self._issued += 1
        tag = f"{identity.subject}:{self._issued}"
        return AuthSession(
            identity=identity,
            access_token=f"access:{tag}",
            refresh_token=f"refresh:{tag}",
            access_expires_at=self.now + self.access_ttl,
            refresh_expires_at=self.now + self.refresh_ttl,
        )

    def authenticate(self, username: str, password: str) -> AuthSession:
        record = self.accounts.get(username)
        if record is None or record[0] != password:
            # One message for both cases, on purpose: telling a caller which
            # of the username or the password was wrong tells an attacker
            # which usernames exist.
            raise AuthError("invalid username or password")
        return self._session(record[1])

    def refresh(self, refresh_token: str) -> AuthSession:
        if refresh_token in self._revoked:
            raise AuthError("the session has expired — sign in again")
        for _password, identity in self.accounts.values():
            if refresh_token.startswith(f"refresh:{identity.subject}:"):
                return self._session(identity)
        raise AuthError("the session has expired — sign in again")

    def logout(self, refresh_token: str) -> None:
        self._revoked.add(refresh_token)
