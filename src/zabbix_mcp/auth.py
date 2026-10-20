#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Bearer-token authentication and per-request context for HTTP transports.

One static bearer token, supplied through the environment (or Vault), is
all the server knows about. It can carry an expiry so a leaked or
forgotten token stops working by itself; rotate it by changing the value
in Vault and restarting.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import time
from datetime import datetime, timezone

from mcp.server.auth.provider import AccessToken

# Client IP of the current request (the TCP peer, or the first X-Forwarded-For
# entry when the peer is a trusted proxy).
current_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_client_ip", default=None)
# Scheme + authority the current client actually reached us on ("https://mcp.example.com"),
# taken from the request Host (or X-Forwarded-Host/-Proto from a trusted proxy). Used to
# build report download links: the address the caller used is the only one it is known to
# be able to reach, whereas the local bind may be a loopback address behind a proxy.
# Never used for an authorization decision - the client controls Host, so a forged value
# can only mislead the client that sent it.
current_request_base: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_request_base", default=None)


def parse_expiry(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM:SS`` and the same with a
    trailing ``Z`` (``datetime.fromisoformat`` only learned ``Z`` in
    Python 3.11). A naive timestamp is read as UTC. Raises ValueError for
    anything else; callers treat that as "not valid", never as "no expiry".
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class BearerTokenVerifier:
    """Verifier for the single static bearer token."""

    def __init__(self, expected_token: str, expires_at: datetime | None = None) -> None:
        self._expected_token = expected_token
        self._expires_at = expires_at

    @property
    def expired(self) -> bool:
        return self._expires_at is not None and datetime.now(timezone.utc) >= self._expires_at

    async def verify_token(self, token: str) -> AccessToken | None:
        # Constant-time comparison to prevent timing attacks.
        if not hmac.compare_digest(token.encode(), self._expected_token.encode()):
            return None
        if self.expired:
            return None
        expires = int(self._expires_at.timestamp()) if self._expires_at else int(time.time()) + 86400
        return AccessToken(
            token=token,
            client_id="mcp-client",
            scopes=["all"],
            expires_at=expires,
        )


def caller_fingerprint() -> str | None:
    """Stable, non-reversible id of the bearer token behind the current call.

    ``action_prepare`` stores it and ``action_confirm`` compares, so a
    confirmation token is only good for the caller that prepared the action.
    None when there is no authenticated caller (stdio, or auth disabled).
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    access = get_access_token()
    if access is None:
        return None
    return hashlib.sha256(access.token.encode()).hexdigest()[:16]
