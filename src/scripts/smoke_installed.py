"""Smoke test of an *installed* Oscalis: start it the way an MCP client would and ask it real questions.

Unlike the unit tests, this does not import the source tree. It launches the ``oscalis-mcp`` command from an empty
working directory, so it fails if the data files were not packaged or the install is otherwise broken. CI runs it on
Linux, macOS and Windows against a freshly built wheel.

Usage:
  python src/scripts/smoke_installed.py                      # uses `oscalis-mcp` on PATH
  python src/scripts/smoke_installed.py --command /path/to/oscalis-mcp
  python src/scripts/smoke_installed.py --python /path/to/venv/python   # runs `python -m oscalis.server`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from typing import Any

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = {
    "list_catalogs",
    "get_control",
    "search_controls",
    "crosswalk_control",
    "baseline_delta",
    "list_profile_controls",
    "ai_governance_map",
}
EXPECTED_CATALOGS = {"800-53r5.2": 1196, "csf-v2": 219, "ai-rmf": 91}
EXPECTED_PROFILE_COUNT = 18


class SmokeFailure(Exception):
    """An installed Oscalis did not behave as expected."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


async def call(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    expect(not result.is_error, f"{name} returned an error: {result.content}")
    doc = json.loads(result.content[0].text)  # type: ignore[union-attr]
    expect(isinstance(doc, dict), f"{name} did not return a JSON object")
    return doc  # type: ignore[no-any-return]


async def run_checks(params: StdioServerParameters) -> list[str]:
    done: list[str] = []
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        expect(tools == EXPECTED_TOOLS, f"tools differ: {sorted(tools ^ EXPECTED_TOOLS)}")
        done.append(f"{len(tools)} tools registered")

        listing = await call(client, "list_catalogs", {})
        counts = {c["key"]: c["control_count"] for c in listing["catalogs"]}
        expect(counts == EXPECTED_CATALOGS, f"catalogs differ: {counts}")
        expect(len(listing["profiles"]) == EXPECTED_PROFILE_COUNT, f"{len(listing['profiles'])} profiles loaded")
        done.append(f"{len(counts)} catalogs and {len(listing['profiles'])} profiles loaded")

        control = await call(client, "get_control", {"catalog": "800-53r5.2", "control_id": "ac-2"})
        expect(control["title"] == "Account Management", f"unexpected ac-2: {control}")
        done.append("get_control ac-2")

        found = await call(client, "search_controls", {"query": "access control", "limit": 5})
        expect(found["count"] > 0, "full-text search returned nothing")
        done.append("full-text search")

        mapped = await call(
            client,
            "crosswalk_control",
            {"catalog": "csf-v2", "control_id": "pr.ds-01", "target_catalogs": ["800-53r5.2"]},
        )
        expect(len(mapped["mappings"]) > 0, "crosswalk returned no mappings")
        done.append(f"crosswalk pr.ds-01 -> {len(mapped['mappings'])} controls")

        delta = await call(client, "baseline_delta", {"profile_a": "govramp-moderate", "profile_b": "govramp-high"})
        expect(delta["summary"]["added_in_b"] > 0, "baseline_delta found no difference")
        done.append("baseline_delta govramp-moderate -> govramp-high")

        members = await call(client, "list_profile_controls", {"profile": "800-53b-privacy", "limit": 100})
        expect(members["total"] == members["count"] == 96, f"privacy baseline lists {members['total']} controls")
        done.append("list_profile_controls 800-53b-privacy -> 96 controls")
    return done


def build_params(command: str | None, python: str | None, cwd: str) -> StdioServerParameters:
    env = {"PYTHONIOENCODING": "utf-8"}
    if python:
        return StdioServerParameters(command=python, args=["-m", "oscalis.server"], cwd=cwd, env=env)
    exe = command or shutil.which("oscalis-mcp")
    if exe is None:
        raise SystemExit("error: `oscalis-mcp` is not on PATH. Pass --command or --python.")
    return StdioServerParameters(command=exe, args=[], cwd=cwd, env=env)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--command", help="path to the oscalis-mcp executable (default: from PATH)")
    p.add_argument("--python", help="Python of an environment that has oscalis installed (runs -m oscalis.server)")
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory() as empty:  # nothing from the source tree is reachable from here
        params = build_params(args.command, args.python, empty)
        try:
            done = asyncio.run(run_checks(params))
        except SmokeFailure as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
    for line in done:
        print(f"ok  {line}")
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
