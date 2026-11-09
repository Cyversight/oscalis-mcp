"""SSE authentication: the ASGI middleware, token validation, the bind guard, and a live server."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from oscalis.core.auth import (
    MIN_TOKEN_LENGTH,
    AuthConfigError,
    BearerAuthMiddleware,
    Message,
    Receive,
    Scope,
    Send,
    check_bind,
    is_loopback,
    validate_token,
)
from oscalis.core.config import Settings
from oscalis.server import build_sse_app, create_server, main
from tests.conftest import FIXTURE_DATA, REPO

TOKEN = "t0ken-" + "x" * 20


class Recorder:
    """Stand-in ASGI app that records whether it was reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


async def drive(app: Any, headers: list[tuple[bytes, bytes]], scope_type: str = "http") -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": scope_type, "headers": headers, "path": "/sse"}, receive, send)
    return sent


def status_of(messages: list[Message]) -> int | None:
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    return None if start is None else int(start["status"])


def call(headers: list[tuple[bytes, bytes]]) -> tuple[int | None, Recorder, list[Message]]:
    inner = Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    messages = asyncio.run(drive(app, headers))
    return status_of(messages), inner, messages


# ----------------------------------------------------------------------------- middleware


def test_correct_token_reaches_the_app() -> None:
    status, inner, _ = call([(b"authorization", f"Bearer {TOKEN}".encode())])
    assert status == 200 and inner.calls == ["http"]


@pytest.mark.parametrize(
    "headers",
    [
        [],  # no header at all
        [(b"authorization", b"")],
        [(b"authorization", b"Bearer")],
        [(b"authorization", b"Bearer wrong-token-value-here")],
        [(b"authorization", f"Basic {TOKEN}".encode())],  # right secret, wrong scheme
        [(b"authorization", TOKEN.encode())],  # no scheme
        [(b"authorization", f"Bearer {TOKEN}x".encode())],  # one character too long
        [(b"authorization", f"Bearer {TOKEN[:-1]}".encode())],  # one character too short
        [(b"authorization", f"Bearer {TOKEN.upper()}".encode())],  # tokens are case-sensitive
        [(b"x-api-key", TOKEN.encode())],  # token in the wrong header
    ],
)
def test_missing_or_wrong_credentials_never_reach_the_app(headers: list[tuple[bytes, bytes]]) -> None:
    status, inner, messages = call(headers)
    assert status == 401 and inner.calls == []
    start = messages[0]
    assert dict(start["headers"])[b"www-authenticate"].startswith(b"Bearer")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert TOKEN.encode() not in body and b"unauthorized" in body


def test_scheme_and_header_name_are_case_insensitive() -> None:
    status, _, _ = call([(b"Authorization", f"bearer {TOKEN}".encode())])
    assert status == 200


def test_surrounding_whitespace_in_the_credential_is_tolerated() -> None:
    assert call([(b"authorization", f"Bearer   {TOKEN}  ".encode())])[0] == 200


def test_every_http_path_and_method_is_protected() -> None:
    inner = Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    for path in ("/sse", "/messages/", "/anything"):

        async def go(p: str = path) -> int | None:
            sent: list[Message] = []

            async def receive() -> Message:
                return {"type": "http.request"}

            async def send(m: Message) -> None:
                sent.append(m)

            await app({"type": "http", "method": "POST", "path": p, "headers": []}, receive, send)
            return status_of(sent)

        assert asyncio.run(go()) == 401
    assert inner.calls == []


def test_lifespan_events_pass_through_so_the_server_can_start() -> None:
    inner = Recorder()
    asyncio.run(drive(BearerAuthMiddleware(inner, TOKEN), [], scope_type="lifespan"))
    assert inner.calls == ["lifespan"]


def test_websocket_without_credentials_is_closed_with_policy_violation() -> None:
    inner = Recorder()
    messages = asyncio.run(drive(BearerAuthMiddleware(inner, TOKEN), [], scope_type="websocket"))
    assert messages == [{"type": "websocket.close", "code": 1008}] and inner.calls == []


def test_empty_token_is_rejected_at_construction() -> None:
    with pytest.raises(AuthConfigError):
        BearerAuthMiddleware(Recorder(), "")


# ----------------------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("::1", True),
        ("[::1]", True),
        ("127.5.5.5", True),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped loopback
        ("[::ffff:127.0.0.1]", True),
        ("::ffff:10.0.0.5", False),  # IPv4-mapped, but not loopback
        ("::ffff:0.0.0.0", False),
        ("0.0.0.0", False),  # noqa: S104
        ("::", False),
        ("192.168.1.10", False),
        ("10.0.0.5", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_ipv4_mapped_loopback_does_not_depend_on_the_python_patch_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older 3.11/3.12 builds say ``IPv6Address('::ffff:127.0.0.1').is_loopback`` is False. CI caught this on the
    runners' Python 3.11. Simulate that behaviour so the test fails here too if the unwrapping is removed."""
    import ipaddress

    def old_is_loopback(self: ipaddress.IPv6Address) -> bool:
        return int(self) == 1  # only ::1, as older releases did

    monkeypatch.setattr(ipaddress.IPv6Address, "is_loopback", property(old_is_loopback))
    assert ipaddress.ip_address("::ffff:127.0.0.1").is_loopback is False  # the simulation is in effect
    assert is_loopback("::ffff:127.0.0.1") is True
    assert is_loopback("::1") is True and is_loopback("::ffff:10.0.0.5") is False


def test_token_validation() -> None:
    assert validate_token(None) is None and validate_token("") is None
    assert validate_token(TOKEN) == TOKEN
    with pytest.raises(AuthConfigError, match=str(MIN_TOKEN_LENGTH)):
        validate_token("short")
    with pytest.raises(AuthConfigError, match="whitespace"):
        validate_token("a" * 20 + " " + "b" * 5)
    with pytest.raises(AuthConfigError, match="whitespace"):
        validate_token(" " + "a" * 20)


def test_bind_guard() -> None:
    check_bind("127.0.0.1", None)  # loopback without a token is allowed
    check_bind("0.0.0.0", TOKEN)  # noqa: S104  # exposed, but authenticated
    with pytest.raises(AuthConfigError, match="refusing to listen"):
        check_bind("0.0.0.0", None)  # noqa: S104
    with pytest.raises(AuthConfigError, match="refusing to listen"):
        check_bind("192.168.1.10", None)


def test_token_is_read_from_the_environment_and_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSCALIS_AUTH_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert settings.auth_token == TOKEN and TOKEN not in repr(settings)
    monkeypatch.delenv("OSCALIS_AUTH_TOKEN")
    assert Settings.from_env().auth_token is None


def test_build_sse_app_wraps_only_when_a_token_is_set() -> None:
    server = create_server(Settings(data_dir=FIXTURE_DATA))
    assert not isinstance(build_sse_app(server, Settings(data_dir=FIXTURE_DATA)), BearerAuthMiddleware)
    protected = Settings(data_dir=FIXTURE_DATA, auth_token=TOKEN)  # noqa: S106
    assert isinstance(build_sse_app(server, protected), BearerAuthMiddleware)
    with pytest.raises(AuthConfigError):
        build_sse_app(server, Settings(data_dir=FIXTURE_DATA, auth_token="short"))  # noqa: S106


def test_main_refuses_an_exposed_unauthenticated_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OSCALIS_AUTH_TOKEN", raising=False)
    started: list[str] = []
    monkeypatch.setattr("oscalis.server.uvicorn.run", lambda *a, **k: started.append("started"))
    with pytest.raises(SystemExit) as exc:
        main(["--transport", "sse", "--host", "0.0.0.0", "--data-dir", str(FIXTURE_DATA)])  # noqa: S104
    assert exc.value.code == 2 and started == []


def test_main_starts_an_authenticated_exposed_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSCALIS_AUTH_TOKEN", TOKEN)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("oscalis.server.uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))
    main(["--transport", "sse", "--host", "0.0.0.0", "--port", "9123", "--data-dir", str(FIXTURE_DATA)])  # noqa: S104
    assert isinstance(captured["app"], BearerAuthMiddleware)
    assert (captured["host"], captured["port"]) == ("0.0.0.0", 9123)  # noqa: S104


# ----------------------------------------------------------------------------- live server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server() -> Iterator[tuple[int, str]]:
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "OSCALIS_AUTH_TOKEN": TOKEN, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "oscalis.server",
            "--transport",
            "sse",
            "--port",
            str(port),
            "--data-dir",
            str(Path(FIXTURE_DATA)),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not start")
        yield port, TOKEN
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_server_rejects_anonymous_and_accepts_the_token(live_server: tuple[int, str]) -> None:
    import urllib.error
    import urllib.request

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    port, token = live_server
    url = f"http://127.0.0.1:{port}/sse"
    with pytest.raises(urllib.error.HTTPError) as anon:
        urllib.request.urlopen(url, timeout=10)  # noqa: S310
    assert anon.value.code == 401 and anon.value.headers["WWW-Authenticate"].startswith("Bearer")

    req = urllib.request.Request(url, headers={"Authorization": "Bearer nope-nope-nope-nope"})  # noqa: S310
    with pytest.raises(urllib.error.HTTPError) as wrong:
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    assert wrong.value.code == 401

    async def authenticated() -> list[str]:
        headers = {"Authorization": f"Bearer {token}"}
        async with sse_client(url, headers=headers) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return [t.name for t in (await session.list_tools()).tools]

    assert "get_control" in asyncio.run(authenticated())


# ----------------------------------------------------------------------------- DNS-rebinding protection


def _post_with_host(app: Any, host_header: str) -> int:
    from starlette.testclient import TestClient

    response = TestClient(app).post(
        "/messages/?session_id=" + "0" * 32,
        headers={"Host": host_header, "Content-Type": "application/json"},
        content=b"{}",
    )
    return response.status_code


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "LOCALHOST", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"])
def test_every_loopback_bind_checks_the_host_header(bind: str) -> None:
    """A forged Host (DNS rebinding from a web page) is refused for any loopback spelling check_bind accepts."""
    server = create_server(Settings(data_dir=FIXTURE_DATA))
    app = build_sse_app(server, Settings(data_dir=FIXTURE_DATA, host=bind))
    assert _post_with_host(app, "evil.example:8000") == 421
    own = f"[{bind.lower()}]" if ":" in bind else bind.lower()
    assert _post_with_host(app, f"{own}:8000") == 404  # host accepted; the made-up session id is then not found


def test_loopback_security_settings_cover_the_bound_host_only() -> None:
    from oscalis.server import loopback_transport_security

    assert loopback_transport_security("0.0.0.0") is None  # noqa: S104  # non-loopback needs a token instead
    settings = loopback_transport_security("127.0.0.2")
    assert settings is not None and settings.enable_dns_rebinding_protection
    assert "127.0.0.2:*" in settings.allowed_hosts and "http://127.0.0.2:*" in settings.allowed_origins
