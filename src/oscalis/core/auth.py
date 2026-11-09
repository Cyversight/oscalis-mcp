"""Bearer-token protection for the SSE transport.

stdio needs no authentication: the client launches the server as a child process. SSE listens on a network socket, so
anything that can reach the port could read every catalog and profile. This module provides:

* ``BearerAuthMiddleware``: a pure ASGI middleware that requires ``Authorization: Bearer <token>`` on every HTTP
  request. The comparison is constant-time and the token is never logged or echoed.
* ``check_bind``: refuses to listen on a non-loopback address unless a token is configured.

The middleware is plain ASGI (no framework types) so it works with any app and can be tested without a server.
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MIN_TOKEN_LENGTH = 16


class AuthConfigError(ValueError):
    """Raised at startup for an unsafe or invalid authentication configuration."""


def is_loopback(host: str) -> bool:
    """True for localhost and loopback addresses. Wildcards such as 0.0.0.0 and :: are NOT loopback.

    An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is judged by the IPv4 address it carries. Python's own answer
    for these differs between patch releases (older 3.11 and 3.12 builds return False, newer ones True), so the bind
    guard and the DNS-rebinding protection would otherwise depend on which Python happens to be installed.
    """
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def validate_token(token: str | None) -> str | None:
    """Return the token, or None when no token is configured. Rejects tokens too short to resist guessing."""
    if token is None or token == "":
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        raise AuthConfigError(
            f"OSCALIS_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters "
            '(generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))")'
        )
    if token != token.strip() or any(c.isspace() for c in token):
        raise AuthConfigError("OSCALIS_AUTH_TOKEN must not contain whitespace")
    return token


def check_bind(host: str, token: str | None) -> None:
    """Refuse to expose the server on a network interface without authentication."""
    if token is None and not is_loopback(host):
        raise AuthConfigError(
            f"refusing to listen on {host!r} without authentication. Set OSCALIS_AUTH_TOKEN, "
            "or bind to 127.0.0.1 (the default)."
        )


class BearerAuthMiddleware:
    """Reject HTTP requests that do not carry the expected bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise AuthConfigError("a non-empty token is required")
        self.app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)  # lifespan events
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="oscalis-mcp"'),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _authorized(self, scope: Scope) -> bool:
        presented = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, credential = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    presented = credential.strip()
                break
        # compare_digest is constant-time for equal-length inputs; hash-free because both sides are bytes
        return hmac.compare_digest(presented, self._token)
