"""Oscalis MCP server: tool and resource registrations.

stdio transport carries protocol messages on stdout, so all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar, cast

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from oscalis import __version__
from oscalis.core.auth import ASGIApp, BearerAuthMiddleware, check_bind, is_loopback, validate_token
from oscalis.core.config import Settings
from oscalis.core.graph import OscalGraph, SqliteFeatureError, UnknownKeyError
from oscalis.tools import ai_governance as ai_tools
from oscalis.tools import baseline_delta as delta_tools
from oscalis.tools import crosswalk as crosswalk_tools
from oscalis.tools import get_control as control_tools
from oscalis.tools import profile_controls as profile_tools
from oscalis.tools.common import to_json

log = logging.getLogger("oscalis")

INSTRUCTIONS = (
    "Query NIST OSCAL catalogs (800-53, CSF, AI RMF), resolved profiles/baselines, and a "
    "crosswalk graph. Start with list_catalogs. Tools return bounded views: use projection='summary' to scan, "
    "'prose_only' to read requirement text, and 'full_oscal' only for a few controls. Crosswalk results carry a "
    "confidence and method; 'intersecting' and 'advisory' mappings are not compliance evidence."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False)

F = TypeVar("F", bound=Callable[..., Any])


def _guard(error_type: type[Exception]) -> Callable[[F], F]:
    """Convert domain errors into the SDK's client-visible error type.

    The SDK masks every other exception as a generic failure, which would hide messages such as the list
    of valid catalog keys from the agent.
    """

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (ValueError, KeyError) as exc:  # UnknownKeyError and OscalValidationError are ValueErrors
                raise error_type(str(exc.args[0]) if isinstance(exc, KeyError) else str(exc)) from exc

        return cast(F, wrapper)

    return decorate


tool_guard = _guard(ToolError)
resource_guard = _guard(ResourceError)


def create_server(settings: Settings | None = None, graph: OscalGraph | None = None) -> MCPServer:
    """Build the MCP server. The index is loaded once here, so the first tool call is already warm.

    Pass either ``settings`` (the graph is loaded from them) or a prebuilt ``graph``, which carries its own
    settings. Passing both is ambiguous because the tools read limits from the graph, so it is rejected.
    """
    if graph is not None and settings is not None:
        raise ValueError("pass settings or graph, not both (a graph carries its own settings)")
    g = graph or OscalGraph.load(settings or Settings.from_env())
    mcp = MCPServer("oscalis-mcp", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def list_catalogs() -> str:
        """List loaded catalogs and profiles (baselines) with control counts and OSCAL versions."""
        return to_json(control_tools.list_catalogs(g))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def get_control(
        catalog: str, control_id: str, projection: str = "summary", profiles: list[str] | None = None
    ) -> str:
        """Fetch one control. projection: summary (default) | prose_only | full_oscal.

        catalog is a catalog key from list_catalogs (for example '800-53r5.2'). control_id is
        case-insensitive (for example 'ac-2.1' or 'GV.PO-01').

        Pass profiles (for example ['govramp-moderate']) to also get baseline membership, the profile's
        parameter values, and its additional requirements. Requirement text is included for prose_only and
        full_oscal; summary returns only a count.
        """
        return to_json(control_tools.get_control(g, catalog, control_id, projection, profiles))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def search_controls(
        query: str,
        catalog: str | None = None,
        projection: str = "summary",
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """Full-text search over control titles, statements, and guidance. Results are paginated."""
        return to_json(control_tools.search_controls(g, query, catalog, projection, limit, offset))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def crosswalk_control(
        catalog: str,
        control_id: str,
        target_catalogs: list[str] | None = None,
        max_hops: int | None = None,
        min_confidence: float | None = None,
        profiles: list[str] | None = None,
        projection: str = "summary",
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """Find controls mapped to the given control, directly or transitively (up to max_hops).

        Each result has a relation, confidence, method (direct | transitive | intersecting | advisory) and
        the full hop path. Pass profiles (for example ['fedramp-moderate']) to also get baseline membership
        and parameter values for each result. Results below min_confidence (default 0.30) are omitted.
        """
        return to_json(
            crosswalk_tools.crosswalk_control(
                g, catalog, control_id, target_catalogs, max_hops, min_confidence, profiles, projection, limit, offset
            )
        )

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def baseline_delta(
        profile_a: str,
        profile_b: str,
        projection: str = "summary",
        limit: int | None = None,
        offset: int = 0,
        include_parameter_changes: bool = True,
    ) -> str:
        """Compare two profiles: controls added/removed going from A to B, and changed parameter values."""
        return to_json(
            delta_tools.baseline_delta(g, profile_a, profile_b, projection, limit, offset, include_parameter_changes)
        )

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def list_profile_controls(
        profile: str,
        projection: str = "summary",
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """List the controls a profile (baseline) selects, for example '800-53b-privacy'. Results are paginated.

        profile is a profile key from list_catalogs. Use get_control with profiles for one control's parameter
        values and requirements in that baseline.
        """
        return to_json(profile_tools.list_profile_controls(g, profile, projection, limit, offset))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    @tool_guard
    def ai_governance_map(
        framework: str = "ai-rmf",
        control_id: str | None = None,
        target_catalogs: list[str] | None = None,
        profiles: list[str] | None = None,
        max_hops: int | None = None,
        min_confidence: float | None = None,
        projection: str = "summary",
        limit: int | None = None,
        offset: int = 0,
    ) -> str:
        """Map NIST AI RMF subcategories ('ai-rmf') to security controls.

        With control_id: crosswalk of that control (default target NIST SP 800-53). Without it: a page of
        the framework's controls with the number of mapped targets each.
        """
        return to_json(
            ai_tools.ai_governance_map(
                g, framework, control_id, target_catalogs, profiles, max_hops, min_confidence, projection, limit, offset
            )
        )

    @mcp.resource("oscal://catalogs", mime_type="application/json")
    @resource_guard
    def catalogs_resource() -> str:
        """Index of loaded catalogs and profiles."""
        return to_json(control_tools.list_catalogs(g))

    @mcp.resource("oscal://catalog/{key}/control/{control_id}", mime_type="application/json")
    @resource_guard
    def control_resource(key: str, control_id: str) -> str:
        """Full OSCAL JSON for one control (without nested enhancements)."""
        result = control_tools.get_control(g, key, control_id, "full_oscal")
        return json.dumps(result["oscal"], separators=(",", ":"), ensure_ascii=False)

    @mcp.resource("oscal://profile/{key}", mime_type="application/json")
    @resource_guard
    def profile_resource(key: str) -> str:
        """Resolved profile: member control ids and parameter settings."""
        profile = g.require_profile(key)
        members = sorted(g.profile_members(profile))
        params = g.profile_param_values(profile)
        return to_json(
            {
                "profile": profile,
                "controls": [f"{cat}:{cid}" for cat, cid in members],
                "parameters": {f"{cat}:{pid}": v for (cat, pid), v in sorted(params.items())},
            }
        )

    return mcp


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="oscalis-mcp", description="Oscalis MCP server")
    p.add_argument("--transport", choices=("stdio", "sse"), help="default: $OSCALIS_TRANSPORT or stdio")
    p.add_argument("--host", help="SSE bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="SSE port (default 8000)")
    p.add_argument("--data-dir", help="directory containing catalogs/, profiles/, crosswalks/")
    return p.parse_args(argv)


def loopback_transport_security(host: str) -> TransportSecuritySettings | None:
    """DNS-rebinding protection (Host and Origin checks) for any loopback bind address.

    The SDK enables it only for the exact strings "127.0.0.1", "localhost" and "::1". Other loopback spellings, such as
    127.0.0.2, LOCALHOST or 0:0:0:0:0:0:0:1, pass ``check_bind`` without a token, so without this a web page could reach
    the server through DNS rebinding. Non-loopback binds need a token and get no Host check, as in the SDK.
    """
    if not is_loopback(host):
        return None
    name = host.strip().strip("[]")
    own = {f"[{n}]" if ":" in n else n for n in (name, name.lower())}
    hosts = ["127.0.0.1", "localhost", "[::1]", *sorted(own)]
    unique = list(dict.fromkeys(hosts))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in unique],
        allowed_origins=[f"http://{h}:*" for h in unique],
    )


def build_sse_app(server: MCPServer, settings: Settings) -> ASGIApp:
    """The SSE ASGI app, wrapped in bearer-token auth when a token is configured."""
    app = server.sse_app(host=settings.host, transport_security=loopback_transport_security(settings.host))
    token = validate_token(settings.auth_token)
    if token is None:
        return cast(ASGIApp, app)
    return BearerAuthMiddleware(cast(ASGIApp, app), token)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    base = Settings.from_env()
    settings = replace(
        base,
        transport=args.transport or base.transport,
        host=args.host or base.host,
        port=args.port or base.port,
        data_dir=Path(args.data_dir) if args.data_dir else base.data_dir,
    )
    try:
        if settings.transport == "sse":  # fail on an unsafe bind before spending time loading data
            check_bind(settings.host, validate_token(settings.auth_token))
        server = create_server(settings)
    except (ValueError, UnknownKeyError, SqliteFeatureError) as exc:  # AuthConfigError is a ValueError
        log.error("startup failed: %s", exc)
        raise SystemExit(2) from exc
    if settings.transport == "sse":
        authenticated = validate_token(settings.auth_token) is not None
        log.info(
            "serving SSE on http://%s:%d/sse (%s)",
            settings.host,
            settings.port,
            "bearer token required" if authenticated else "no authentication: loopback only",
        )
        uvicorn.run(build_sse_app(server, settings), host=settings.host, port=settings.port, log_level="info")
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
