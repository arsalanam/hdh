"""Provider-realm authentication for the agent API (design agentic-ui-module §7).

The verification seam now lives in :mod:`hdh.core.identity.web_auth` so the
agent API and the media I/O service share one implementation without a
module-to-module dependency. These names are re-exported here for the agent
API's own use and for back-compat with earlier imports.
"""

from __future__ import annotations

from hdh.core.identity.web_auth import (
    Authenticator,
    AuthError,
    _bearer,
    keycloak_authenticator,
)

__all__ = ["AuthError", "Authenticator", "_bearer", "keycloak_authenticator"]
