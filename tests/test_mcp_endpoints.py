"""MCP surface: every registered tool and resource is exercised through a real in-memory client.

`test_every_primitive_is_covered` fails when a tool or resource is registered without a case here, which is
what `checks/pre-tool-eval.py` relies on to enforce "tests cover all MCP primitives".
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult

from oscalis.core.config import Settings
from oscalis.core.graph import OscalGraph
from oscalis.server import _parse_args, create_server
from tests.conftest import run


@pytest.fixture(scope="module")
def server(graph: OscalGraph) -> MCPServer:
    return create_server(graph=graph)


def with_client(server: MCPServer, fn: Callable[[Client], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            return await fn(client)

    return run(go())


def call(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> CallToolResult:
    result: CallToolResult = with_client(server, lambda c: c.call_tool(name, args or {}))
    return result


def ok(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and parse its result. Also enforces the wire contract that every tool result must satisfy."""
    result = call(server, name, args)
    assert not result.is_error, result.content
    assert len(result.content) == 1, "a result must be a single text block"
    assert result.structured_content is None, "no duplicate structured copy: it would double the tokens"
    text = result.content[0].text  # type: ignore[union-attr]
    assert "\n" not in text and ": " not in text.split('"')[0], "output must be compact JSON, not indented"
    out: dict[str, Any] = json.loads(text)
    assert isinstance(out, dict)
    return out


def error_text(server: MCPServer, name: str, args: dict[str, Any]) -> str:
    result = call(server, name, args)
    assert result.is_error
    return str(result.content[0].text)  # type: ignore[union-attr]


# One minimal valid call per tool / resource. Adding a tool without adding it here fails the coverage test.
TOOL_CASES: dict[str, dict[str, Any]] = {
    "list_catalogs": {},
    "get_control": {"catalog": "800-53r5.2", "control_id": "ac-2"},
    "search_controls": {"query": "privilege"},
    "crosswalk_control": {"catalog": "ai-rmf", "control_id": "measure_2.7"},
    "baseline_delta": {"profile_a": "800-53b-low", "profile_b": "800-53b-moderate"},
    "list_profile_controls": {"profile": "800-53b-low"},
    "ai_governance_map": {"framework": "ai-rmf", "control_id": "map_1.1"},
}
RESOURCE_CASES: dict[str, str] = {
    "oscal://catalogs": "catalogs",
    "oscal://catalog/{key}/control/{control_id}": "oscal://catalog/800-53r5.2/control/ac-2.1",
    "oscal://profile/{key}": "oscal://profile/fedramp-moderate",
}


def test_every_primitive_is_covered(server: MCPServer) -> None:
    tools = {t.name for t in with_client(server, lambda c: c.list_tools()).tools}
    assert tools == set(TOOL_CASES), f"tools without a test case: {tools ^ set(TOOL_CASES)}"
    res = with_client(server, lambda c: c.list_resources()).resources
    tpl = with_client(server, lambda c: c.list_resource_templates()).resource_templates
    registered = {str(r.uri) for r in res} | {t.uri_template for t in tpl}
    assert registered == set(RESOURCE_CASES), f"resources without a test case: {registered ^ set(RESOURCE_CASES)}"


@pytest.mark.parametrize("name", sorted(TOOL_CASES))
def test_tool_succeeds_and_is_declared_read_only(server: MCPServer, name: str) -> None:
    out = ok(server, name, TOOL_CASES[name])
    assert isinstance(out, dict)
    tool = next(t for t in with_client(server, lambda c: c.list_tools()).tools if t.name == name)
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.description


@pytest.mark.parametrize("uri", sorted(set(RESOURCE_CASES.values())))
def test_resource_reads_valid_json(server: MCPServer, uri: str) -> None:
    if not uri.startswith("oscal://"):
        uri = "oscal://" + uri
    result = with_client(server, lambda c: c.read_resource(uri))
    body = json.loads(result.contents[0].text)
    assert body


def test_control_resource_is_full_oscal_without_nested_controls(server: MCPServer) -> None:
    result = with_client(server, lambda c: c.read_resource("oscal://catalog/800-53r5.2/control/ac-2"))
    body = json.loads(result.contents[0].text)
    assert body["id"] == "ac-2" and "controls" not in body


def test_profile_resource_lists_members_and_parameters(server: MCPServer) -> None:
    result = with_client(server, lambda c: c.read_resource("oscal://profile/fedramp-moderate"))
    body = json.loads(result.contents[0].text)
    assert "800-53r5.2:ra-3" in body["controls"]
    assert "800-53r5.2:ra-03_odp.01" in body["parameters"]


def test_unknown_resource_keys_report_valid_values(server: MCPServer) -> None:
    async def go(c: Client) -> str:
        try:
            await c.read_resource("oscal://profile/nope")
        except Exception as exc:
            return str(exc)
        return ""

    assert "Valid profiles" in with_client(server, go)


# ----------------------------------------------------------------------------- tool behavior


def test_list_catalogs(server: MCPServer) -> None:
    out = ok(server, "list_catalogs")
    assert {c["key"] for c in out["catalogs"]} == {"800-53r5.2", "ai-rmf", "csf-v2"}
    assert {p["key"] for p in out["profiles"]} >= {"800-53b-low", "fedramp-moderate"}


def test_list_catalogs_entries_carry_exactly_the_useful_fields(server: MCPServer) -> None:
    out = ok(server, "list_catalogs")
    wanted = {"key", "title", "version", "oscal_version", "control_count"}
    assert all(set(row) == wanted | {"withdrawn_count"} for row in out["catalogs"])  # no `source` file name
    assert all(set(row) == wanted for row in out["profiles"])
    assert all(row["control_count"] > 0 for row in out["catalogs"] + out["profiles"])


def test_the_graph_keeps_the_source_file_even_though_the_tool_omits_it(graph: OscalGraph) -> None:
    assert all(row["source"] == row["key"] + ".json" for row in graph.list_catalogs() + graph.list_profiles())


def test_the_catalogs_resource_matches_the_tool(server: MCPServer) -> None:
    body = json.loads(with_client(server, lambda c: c.read_resource("oscal://catalogs")).contents[0].text)
    assert body == ok(server, "list_catalogs")


def test_projections_are_progressively_larger(server: MCPServer) -> None:
    args = {"catalog": "800-53r5.2", "control_id": "ac-2"}
    sizes = {
        p: len(json.dumps(ok(server, "get_control", {**args, "projection": p})))
        for p in ("summary", "prose_only", "full_oscal")
    }
    assert sizes["summary"] < sizes["prose_only"] < sizes["full_oscal"]
    summary = ok(server, "get_control", args)
    assert "prose" not in summary and "oscal" not in summary
    assert summary["projection"] == "summary" and summary["truncated"] is False
    assert "oscal" in ok(server, "get_control", {**args, "projection": "full_oscal"})
    assert "[Assignment:" in ok(server, "get_control", {**args, "projection": "prose_only"})["prose"]


def test_get_control_is_case_insensitive_and_reports_misses(server: MCPServer) -> None:
    assert ok(server, "get_control", {"catalog": "csf-v2", "control_id": " GV.PO-01 "})["control_id"] == "gv.po-01"
    msg = error_text(server, "get_control", {"catalog": "800-53r5.2", "control_id": "zz-99"})
    assert "zz-99" in msg and "search_controls" in msg


def test_unknown_catalog_error_reaches_the_agent(server: MCPServer) -> None:
    msg = error_text(server, "get_control", {"catalog": "iso-27001", "control_id": "a.5.1"})
    assert "unknown catalog" in msg and "800-53r5.2" in msg and "ai-rmf" in msg


def test_invalid_projection_is_rejected(server: MCPServer) -> None:
    assert "projection" in error_text(
        server, "get_control", {"catalog": "csf-v2", "control_id": "gv.po-01", "projection": "all"}
    )


def test_missing_required_argument_is_an_error(server: MCPServer) -> None:
    assert call(server, "get_control", {"catalog": "csf-v2"}).is_error


def test_search_ranks_pagination_and_filters(server: MCPServer) -> None:
    out = ok(server, "search_controls", {"query": "access", "limit": 2})
    assert out["count"] == 2 and out["limit"] == 2 and out["total"] > 2
    assert out["truncated"] is True and out["next_offset"] == 2 and "offset=2" in out["hint"]
    page2 = ok(server, "search_controls", {"query": "access", "limit": 2, "offset": 2})
    assert {r["control_id"] for r in out["results"]}.isdisjoint({r["control_id"] for r in page2["results"]})
    only = ok(server, "search_controls", {"query": "access", "catalog": "csf-v2"})
    assert {r["catalog"] for r in only["results"]} == {"csf-v2"}
    assert ok(server, "search_controls", {"query": "zzzzqqqq"})["results"] == []


def test_search_matches_guidance_and_prefixes(server: MCPServer) -> None:
    out = ok(server, "search_controls", {"query": "authenticat", "catalog": "800-53r5.2"})
    assert "ia-2" in {r["control_id"] for r in out["results"]}


def test_token_budget_caps_full_oscal_lists(graph: OscalGraph) -> None:
    tight = create_server(replace(graph.settings, max_full_items=2, max_limit=3))
    out = ok(tight, "search_controls", {"query": "information", "projection": "full_oscal", "limit": 50})
    assert out["limit"] == 2 and out["count"] <= 2
    assert "capped at 2" in out["hint"]
    summary = ok(tight, "search_controls", {"query": "information", "limit": 50})
    assert summary["limit"] == 3  # max_limit applies to every projection


def test_crosswalk_tool_output_shape(server: MCPServer) -> None:
    out = ok(
        server,
        "crosswalk_control",
        {"catalog": "ai-rmf", "control_id": "MEASURE_2.7", "profiles": ["fedramp-moderate"]},
    )
    assert out["source"]["control_id"] == "measure_2.7"
    assert out["min_confidence"] == 0.3 and out["max_hops"] == 3
    two_hop = next(m for m in out["mappings"] if m["hop_count"] == 2)
    assert two_hop["confidence"] == 0.38 and two_hop["method"] == "intersecting" and "caveat" in two_hop
    assert out["mappings"][0]["confidence"] >= out["mappings"][-1]["confidence"]


def test_crosswalk_min_confidence_and_hops_arguments(server: MCPServer) -> None:
    base = {"catalog": "ai-rmf", "control_id": "measure_2.7"}
    assert ok(server, "crosswalk_control", {**base, "max_hops": 1})["total"] == 2
    assert ok(server, "crosswalk_control", {**base, "min_confidence": 0.55})["total"] == 1
    assert ok(server, "crosswalk_control", {**base, "min_confidence": 0.0})["total"] >= 4
    assert ok(server, "crosswalk_control", {**base, "target_catalogs": ["csf-v2"]})["total"] == 1


def test_crosswalk_bad_arguments(server: MCPServer) -> None:
    assert "max_hops" in error_text(
        server, "crosswalk_control", {"catalog": "ai-rmf", "control_id": "map_1.1", "max_hops": 7}
    )


def test_baseline_delta_added_removed_and_parameters(server: MCPServer) -> None:
    up = ok(server, "baseline_delta", {"profile_a": "800-53b-low", "profile_b": "800-53b-moderate"})
    assert up["summary"] == {
        "added_in_b": 3,
        "removed_in_b": 0,
        "common": 7,
        "parameter_changes": 0,
        "requirement_changes": 0,
    }
    assert [i["control_id"] for i in up["added_in_b"]["items"]] == ["ac-2.1", "ac-6", "sc-28"]
    down = ok(server, "baseline_delta", {"profile_a": "800-53b-moderate", "profile_b": "800-53b-low"})
    assert down["summary"]["removed_in_b"] == 3 and down["summary"]["added_in_b"] == 0
    fr = ok(server, "baseline_delta", {"profile_a": "800-53b-moderate", "profile_b": "fedramp-moderate"})
    assert fr["summary"]["added_in_b"] == 0 and fr["summary"]["parameter_changes"] == 3
    change = fr["parameter_changes"]["items"][0]
    assert change["a"] is None and change["b"] is not None and change["control_id"] == "ac-2"
    same = ok(server, "baseline_delta", {"profile_a": "800-53b-high", "profile_b": "800-53b-high"})
    assert same["summary"]["added_in_b"] == same["summary"]["removed_in_b"] == 0


def test_baseline_delta_pagination_projection_and_toggle(server: MCPServer) -> None:
    out = ok(server, "baseline_delta", {"profile_a": "800-53b-low", "profile_b": "800-53b-moderate", "limit": 2})
    assert out["added_in_b"]["count"] == 2 and out["added_in_b"]["truncated"] is True
    assert out["truncated"] is True and out["added_in_b"]["next_offset"] == 2 and "hint" in out
    page = ok(
        server, "baseline_delta", {"profile_a": "800-53b-low", "profile_b": "800-53b-moderate", "limit": 2, "offset": 2}
    )
    assert [i["control_id"] for i in page["added_in_b"]["items"]] == ["sc-28"]
    prose = ok(
        server,
        "baseline_delta",
        {"profile_a": "800-53b-low", "profile_b": "800-53b-moderate", "projection": "prose_only"},
    )
    assert "prose" in prose["added_in_b"]["items"][0]
    no_params = ok(
        server,
        "baseline_delta",
        {"profile_a": "800-53b-moderate", "profile_b": "fedramp-moderate", "include_parameter_changes": False},
    )
    assert no_params["summary"]["parameter_changes"] == 0


def test_baseline_delta_unknown_profile(server: MCPServer) -> None:
    msg = error_text(server, "baseline_delta", {"profile_a": "nope", "profile_b": "800-53b-low"})
    assert "unknown profile" in msg and "fedramp-moderate" in msg


def test_ai_governance_map_for_one_control(server: MCPServer) -> None:
    out = ok(
        server, "ai_governance_map", {"framework": "ai-rmf", "control_id": "map_1.1", "profiles": ["fedramp-moderate"]}
    )
    assert out["framework"] == "ai-rmf" and out["function_or_group"] == "MAP"
    assert [m["target"]["control_id"] for m in out["mappings"]] == ["ra-3"]  # defaults to 800-53 targets
    assert out["mappings"][0]["baselines"]["fedramp-moderate"]["in_baseline"] is True
    assert "not as compliance evidence" in out["guidance"] or "compliance evidence" in out["guidance"]


def test_ai_governance_map_listing_mode(server: MCPServer) -> None:
    out = ok(server, "ai_governance_map", {"framework": "ai-rmf", "limit": 5})
    assert out["target_catalogs"] == ["800-53r5.2"] and out["count"] == 5 and out["truncated"] is True
    everything = ok(server, "ai_governance_map", {"limit": 100})  # framework defaults to ai-rmf
    mapped = {c["control_id"]: c["mapped_target_count"] for c in everything["controls"]}
    assert mapped["map_1.1"] == 1 and mapped["govern_1"] == 0


def test_ai_governance_map_rejects_non_ai_framework(server: MCPServer) -> None:
    msg = error_text(server, "ai_governance_map", {"framework": "iso-42001"})
    assert "framework must be one of" in msg and "ai-rmf" in msg


# ----------------------------------------------------------------------------- server plumbing


def test_server_advertises_instructions_and_name(server: MCPServer) -> None:
    async def go(c: Client) -> tuple[str, str]:
        assert c.server_info is not None
        return c.server_info.name, c.instructions or ""

    name, instructions = with_client(server, go)
    assert name == "oscalis-mcp"
    assert "projection" in instructions


def test_create_server_rejects_ambiguous_arguments(graph: OscalGraph) -> None:
    with pytest.raises(ValueError, match="not both"):
        create_server(graph.settings, graph)


def test_both_transports_can_be_built(server: MCPServer) -> None:
    assert server.sse_app() is not None
    assert server.streamable_http_app() is not None


def test_cli_transport_flags() -> None:
    args = _parse_args(["--transport", "sse", "--host", "0.0.0.0", "--port", "9100"])  # noqa: S104
    assert (args.transport, args.host, args.port) == ("sse", "0.0.0.0", 9100)  # noqa: S104
    with pytest.raises(SystemExit):
        _parse_args(["--transport", "carrier-pigeon"])


def test_settings_from_env_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSCALIS_TRANSPORT", "sse")
    monkeypatch.setenv("OSCALIS_MAX_HOPS", "2")
    s = Settings.from_env()
    assert s.transport == "sse" and s.max_hops == 2
    monkeypatch.setenv("OSCALIS_MAX_HOPS", "9")
    with pytest.raises(ValueError, match="OSCALIS_MAX_HOPS"):
        Settings.from_env()
    monkeypatch.setenv("OSCALIS_MAX_HOPS", "x")
    with pytest.raises(ValueError, match="integer"):
        Settings.from_env()
    monkeypatch.delenv("OSCALIS_MAX_HOPS")
    monkeypatch.setenv("OSCALIS_TRANSPORT", "websocket")
    with pytest.raises(ValueError, match="OSCALIS_TRANSPORT"):
        Settings.from_env()


# ----------------------------------------------------------------------------- profiles and notes over MCP
# These run against the real shipped data (NIST, FedRAMP, GovRAMP) because the behaviour is about that data.


@pytest.fixture(scope="module")
def shipped_server(shipped: OscalGraph) -> MCPServer:
    return create_server(graph=shipped)


def test_get_control_with_profiles_returns_baseline_parameters_and_notes(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server,
        "get_control",
        {
            "catalog": "800-53r5.2",
            "control_id": "IA-5.1",
            "projection": "prose_only",
            "profiles": ["govramp-moderate", "fedramp-moderate"],
        },
    )
    gov, fed = out["baselines"]["govramp-moderate"], out["baselines"]["fedramp-moderate"]
    assert gov["in_baseline"] and fed["in_baseline"]
    assert gov["note_count"] == len(gov["notes"]) >= 2
    assert {n["name"] for n in gov["notes"]} >= {"requirement", "guidance"}
    assert all(set(n) == {"name", "locator", "text"} and n["text"] for n in gov["notes"])
    assert fed["note_count"] == 0 or "notes" in fed  # FedRAMP's alters add other kinds of content


def test_summary_projection_withholds_note_text_but_counts_it(shipped_server: MCPServer) -> None:
    args = {"catalog": "800-53r5.2", "control_id": "ia-5.1", "profiles": ["govramp-moderate"]}
    summary = ok(shipped_server, "get_control", args)["baselines"]["govramp-moderate"]
    full = ok(shipped_server, "get_control", {**args, "projection": "prose_only"})["baselines"]["govramp-moderate"]
    assert "notes" not in summary and summary["note_count"] == full["note_count"] > 0
    assert len(json.dumps(summary)) < len(json.dumps(full))


def test_get_control_without_profiles_has_no_baselines_key(shipped_server: MCPServer) -> None:
    assert "baselines" not in ok(shipped_server, "get_control", {"catalog": "800-53r5.2", "control_id": "ac-2"})


GOVERN_1_1 = "Legal and regulatory requirements involving AI are understood, managed, and documented."


def test_ai_rmf_prose_is_the_official_outcome(shipped_server: MCPServer) -> None:
    """The outcome sentence, not the Playbook's suggested actions, which NIST files as a `statement` part."""
    out = ok(
        shipped_server, "get_control", {"catalog": "ai-rmf", "control_id": "govern_1.1", "projection": "prose_only"}
    )
    assert out["prose"] == GOVERN_1_1


def test_ai_rmf_suggested_actions_are_labelled_guidance(shipped_server: MCPServer) -> None:
    args = {"catalog": "ai-rmf", "control_id": "govern_1.1", "projection": "full_oscal"}
    guidance = ok(shipped_server, "get_control", args)["guidance"]
    assert "Suggested actions:\n- Maintain awareness of the applicable legal and regulatory" in guidance
    assert guidance.startswith("AI systems may be subject to specific applicable legal")  # NIST's own guidance first


def test_every_ai_rmf_subcategory_has_its_outcome_as_prose(shipped: OscalGraph) -> None:
    rows, total = shipped.list_controls("ai-rmf", 100, 0, top_level_only=False)
    assert total == len(rows) == 91
    subcategories = [
        shipped.get_control("ai-rmf", r["control_id"], full=True) for r in rows if r["class"] == "subcategory"
    ]
    assert len(subcategories) == 72
    for c in subcategories:
        assert c is not None and c["prose"], c
        overview = next(p["prose"] for p in c["oscal"]["parts"] if p["name"] == "overview")
        assert c["prose"] == overview.strip(), c["control_id"]


def test_search_finds_an_ai_rmf_subcategory_by_its_outcome(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server, "search_controls", {"query": "legal regulatory requirements involving AI", "catalog": "ai-rmf"}
    )
    assert "govern_1.1" in [r["control_id"] for r in out["results"][:3]]


def test_get_control_profile_that_does_not_contain_the_control(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server, "get_control", {"catalog": "800-53r5.2", "control_id": "ac-2.12", "profiles": ["govramp-low"]}
    )
    status = out["baselines"]["govramp-low"]
    assert status == {"in_baseline": False, "parameters": {}, "note_count": 0}


def test_get_control_unknown_profile_lists_valid_ones(shipped_server: MCPServer) -> None:
    msg = error_text(
        shipped_server, "get_control", {"catalog": "800-53r5.2", "control_id": "ac-2", "profiles": ["govramp-bogus"]}
    )
    assert "unknown profile" in msg and "govramp-moderate" in msg


def test_crosswalk_notes_follow_the_projection(shipped_server: MCPServer) -> None:
    base = {
        "catalog": "csf-v2",
        "control_id": "pr.ds-01",
        "target_catalogs": ["800-53r5.2"],
        "profiles": ["govramp-moderate"],
    }
    summary = ok(shipped_server, "crosswalk_control", {**base, "limit": 40})
    prose = ok(shipped_server, "crosswalk_control", {**base, "limit": 40, "projection": "prose_only"})
    with_notes = [m for m in prose["mappings"] if m["baselines"]["govramp-moderate"]["note_count"]]
    assert with_notes, "expected at least one mapped control with GovRAMP requirements"
    assert all("notes" in m["baselines"]["govramp-moderate"] for m in prose["mappings"])
    assert all("notes" not in m["baselines"]["govramp-moderate"] for m in summary["mappings"])


def test_baseline_delta_reports_requirement_changes_between_govramp_levels(shipped_server: MCPServer) -> None:
    args = {"profile_a": "govramp-moderate", "profile_b": "govramp-high"}
    summary = ok(shipped_server, "baseline_delta", {**args, "limit": 50})
    assert summary["summary"]["requirement_changes"] > 0
    item = summary["requirement_changes"]["items"][0]
    assert item["change"] in {"added", "removed", "modified"} and set(item) >= {
        "control_id",
        "name",
        "locator",
        "a",
        "b",
    }
    long_a = next((i["a"] for i in summary["requirement_changes"]["items"] if i["a"] and len(i["a"]) > 200), None)
    full = ok(shipped_server, "baseline_delta", {**args, "limit": 50, "projection": "prose_only"})
    assert long_a is None or any(i["a"] and len(i["a"]) > 203 for i in full["requirement_changes"]["items"])
    assert all(not i["a"] or len(i["a"]) <= 203 for i in summary["requirement_changes"]["items"])  # excerpt cap


def test_baseline_delta_compares_text_across_values_and_constraints(shipped_server: MCPServer) -> None:
    """FedRAMP stores parameter text as constraints and GovRAMP as values. Identical text must not count as a change."""
    out = ok(
        shipped_server,
        "baseline_delta",
        {"profile_a": "fedramp-moderate", "profile_b": "govramp-moderate", "limit": 100},
    )
    changed = {(c["control_id"], c["param_id"]) for c in out["parameter_changes"]["items"]}
    assert ("ac-2", "ac-02_odp.06") not in changed  # both say "twenty-four (24) hours"
    assert out["summary"]["parameter_changes"] < 150  # roughly half of 236 are the same wording


def test_baseline_delta_nested_profiles_are_subsets(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server, "baseline_delta", {"profile_a": "govramp-core", "profile_b": "govramp-moderate", "limit": 1}
    )
    assert out["summary"]["removed_in_b"] == 0 and out["summary"]["added_in_b"] == 259
    assert out["summary"]["parameter_changes"] == 0  # Core inherits Moderate's parameters, so none differ


def test_ai_governance_map_reports_govramp_baselines(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server,
        "ai_governance_map",
        {
            "framework": "ai-rmf",
            "control_id": "map_1.1",
            "profiles": ["govramp-low", "govramp-core"],
            "min_confidence": 0.0,
        },
    )
    ra3 = next(m for m in out["mappings"] if m["target"]["control_id"] == "ra-3")
    assert ra3["baselines"]["govramp-low"]["in_baseline"] is True
    assert ra3["baselines"]["govramp-core"]["in_baseline"] in (True, False)


def test_official_csf_crosswalk_is_traversable_in_both_directions(shipped_server: MCPServer) -> None:
    fwd = ok(
        shipped_server,
        "crosswalk_control",
        {"catalog": "csf-v2", "control_id": "pr.ds-01", "target_catalogs": ["800-53r5.2"]},
    )
    controls = {m["target"]["control_id"] for m in fwd["mappings"]}
    assert "sc-28" in controls and len(controls) >= 10
    assert all(
        m["method"] == "intersecting" and 0.5 <= m["confidence"] <= 0.7 for m in fwd["mappings"] if m["hop_count"] == 1
    )
    back = ok(
        shipped_server,
        "crosswalk_control",
        {"catalog": "800-53r5.2", "control_id": "sc-28", "target_catalogs": ["csf-v2"]},
    )
    assert "pr.ds-01" in {m["target"]["control_id"] for m in back["mappings"]}


# ----------------------------------------------------------------------------- list_profile_controls


def test_list_profile_controls_returns_every_member_in_natural_order(shipped_server: MCPServer) -> None:
    out = ok(shipped_server, "list_profile_controls", {"profile": "800-53b-privacy", "limit": 100})
    ids = [c["control_id"] for c in out["controls"]]
    assert out["total"] == out["count"] == len(ids) == 96 and out["truncated"] is False
    assert {"pt-2", "pm-18", "ra-8", "au-11", "cm-4", "ir-8"} <= set(ids)
    assert ids.index("pm-3") < ids.index("pm-10") < ids.index("pm-20") < ids.index("pm-20.1")  # natural, not text
    assert out["profile"] == {"key": "800-53b-privacy", "title": "NIST SP 800-53 Rev 5.2.0 PRIVACY BASELINE"}
    assert out["projection"] == "summary" and all("prose" not in c for c in out["controls"])


def test_list_profile_controls_pages_without_gaps_or_overlap(shipped_server: MCPServer) -> None:
    args = {"profile": "govramp-moderate", "limit": 40}
    first = ok(shipped_server, "list_profile_controls", args)
    assert first["truncated"] is True and first["next_offset"] == 40 and first["total"] == 319
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = ok(shipped_server, "list_profile_controls", {**args, "offset": offset})
        seen += [c["control_id"] for c in page["controls"]]
        offset = page.get("next_offset")
    assert len(seen) == len(set(seen)) == 319


def test_list_profile_controls_projections_and_full_oscal_cap(shipped_server: MCPServer) -> None:
    prose = ok(shipped_server, "list_profile_controls", {"profile": "fedramp-low", "projection": "prose_only"})
    assert all("prose" in c and "oscal" not in c for c in prose["controls"])
    full = ok(
        shipped_server, "list_profile_controls", {"profile": "fedramp-low", "projection": "full_oscal", "limit": 50}
    )
    # The full_oscal cap is 3, and the size budget shortens the page further: AC-2 alone is about 26,000 characters.
    assert 1 <= full["count"] <= 3 and full["truncated"] is True and "capped" in full["hint"]
    assert full["next_offset"] == full["count"] == full["limit"]
    assert all(c["oscal"]["id"] == c["control_id"] for c in full["controls"])


# ----------------------------------------------------------------------------- response size budget

BUDGET = Settings().max_response_chars


def text_size(server: MCPServer, name: str, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    result = call(server, name, args)
    assert not result.is_error, result.content
    text = result.content[0].text  # type: ignore[union-attr]
    return len(text), json.loads(text)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("baseline_delta", {"profile_a": "800-53b-low", "profile_b": "govramp-low", "limit": 100}),
        ("baseline_delta", {"profile_a": "800-53b-low", "profile_b": "govramp-high", "limit": 100}),
        ("search_controls", {"query": "access", "projection": "full_oscal", "limit": 3}),
        ("search_controls", {"query": "access", "projection": "prose_only", "limit": 100}),
        ("list_profile_controls", {"profile": "fedramp-high", "projection": "prose_only", "limit": 100}),
        (
            "crosswalk_control",
            {
                "catalog": "csf-v2",
                "control_id": "pr.ds-01",
                "target_catalogs": ["800-53r5.2"],
                "profiles": ["govramp-moderate", "govramp-high", "govramp-moderate-cjis", "fedramp-high"],
                "projection": "prose_only",
                "limit": 100,
            },
        ),
        ("ai_governance_map", {"framework": "ai-rmf", "projection": "prose_only", "limit": 100}),
    ],
)
def test_list_responses_stay_within_the_size_budget(shipped_server: MCPServer, name: str, args: dict[str, Any]) -> None:
    """Large pages are shortened so they fit a client's per-result limit (Kiro: 30,000 characters)."""
    size, doc = text_size(shipped_server, name, args)
    assert size <= BUDGET or doc["limit"] == 1, f"{name}: {size:,} characters for limit {doc['limit']}"


def test_shortened_delta_pages_cleanly(shipped_server: MCPServer) -> None:
    args = {"profile_a": "800-53b-low", "profile_b": "govramp-low", "limit": 100}
    _size, first = text_size(shipped_server, "baseline_delta", args)
    page = first["limit"]
    assert first["offset"] == 0 and page < 100, "the page should have been shortened"
    assert first["parameter_changes"]["count"] == page and first["parameter_changes"]["next_offset"] == page
    hint = first["hint"]
    assert f"offset={page}" in hint and "Page shortened" in hint
    assert "projection='summary'" not in hint, "the call already used summary"
    _size, second = text_size(shipped_server, "baseline_delta", {**args, "offset": page})
    ids_1 = [c["param_id"] for c in first["parameter_changes"]["items"]]
    ids_2 = [c["param_id"] for c in second["parameter_changes"]["items"]]
    assert ids_1 and ids_2 and not set(ids_1) & set(ids_2)  # no overlap between pages


def test_delta_hint_suggests_summary_only_when_it_would_help(shipped_server: MCPServer) -> None:
    args = {"profile_a": "800-53b-low", "profile_b": "govramp-low", "limit": 5}
    _size, summary = text_size(shipped_server, "baseline_delta", args)
    assert "projection='summary'" not in summary["hint"] and "offset=5" in summary["hint"]
    _size, prose = text_size(shipped_server, "baseline_delta", {**args, "projection": "prose_only"})
    assert "projection='summary'" in prose["hint"]


def test_delta_profile_titles_are_short(shipped_server: MCPServer) -> None:
    _size, doc = text_size(shipped_server, "baseline_delta", {"profile_a": "800-53b-low", "profile_b": "govramp-low"})
    assert doc["profile_a"]["title"] == "NIST SP 800-53 Rev 5.2.0 LOW IMPACT BASELINE"
    assert doc["profile_b"]["title"] == "GovRAMP Rev. 5 Low baseline (Authorized)"


def test_list_profile_controls_rejects_bad_arguments(shipped_server: MCPServer) -> None:
    msg = error_text(shipped_server, "list_profile_controls", {"profile": "iso-27001"})
    assert "iso-27001" in msg and "800-53b-privacy" in msg  # lists the valid profile keys
    assert "projection" in error_text(
        shipped_server, "list_profile_controls", {"profile": "800-53b-low", "projection": "x"}
    )
    assert "limit" in error_text(shipped_server, "list_profile_controls", {"profile": "800-53b-low", "limit": 0})
    assert "offset" in error_text(shipped_server, "list_profile_controls", {"profile": "800-53b-low", "offset": -1})


# ----------------------------------------------------------------------------- withdrawn controls


def test_withdrawn_control_says_so_and_names_its_successor(shipped_server: MCPServer) -> None:
    ra4 = ok(shipped_server, "get_control", {"catalog": "800-53r5.2", "control_id": "ra-4"})
    assert ra4["status"] == "withdrawn" and ra4["incorporated_into"] == ["ra-3"]
    live = ok(shipped_server, "get_control", {"catalog": "800-53r5.2", "control_id": "ra-3"})
    assert "status" not in live and "incorporated_into" not in live  # live controls carry no extra keys


def test_withdrawn_crosswalk_target_is_flagged(shipped_server: MCPServer) -> None:
    out = ok(
        shipped_server,
        "crosswalk_control",
        {"catalog": "csf-v2", "control_id": "de.ae-06", "target_catalogs": ["800-53r5.2"], "limit": 100},
    )
    ra4 = next(m["target"] for m in out["mappings"] if m["target"]["control_id"] == "ra-4")
    assert ra4["status"] == "withdrawn" and ra4["incorporated_into"] == ["ra-3"]


def test_list_catalogs_counts_withdrawn_controls(shipped_server: MCPServer) -> None:
    rows = {c["key"]: c for c in ok(shipped_server, "list_catalogs")["catalogs"]}
    assert rows["800-53r5.2"]["withdrawn_count"] == 182 and rows["800-53r5.2"]["control_count"] == 1196
    assert rows["ai-rmf"]["withdrawn_count"] == 0


# ----------------------------------------------------------------------------- boundary validation (review fixes)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_controls", {"query": "account", "offset": 2**63}),
        ("ai_governance_map", {"offset": 2**63}),
        ("list_profile_controls", {"profile": "800-53b-low", "offset": 10**7}),
    ],
)
def test_a_huge_offset_is_a_readable_error(server: MCPServer, tool: str, args: dict[str, Any]) -> None:
    """Before the bound, 2**63 overflowed SQLite and the SDK masked it as a generic failure."""
    assert "offset must be at most" in error_text(server, tool, args)


def test_key_lists_are_deduplicated_and_capped(server: MCPServer) -> None:
    args = {"catalog": "800-53r5.2", "control_id": "ac-2"}
    twice = ok(server, "get_control", {**args, "profiles": ["800-53b-low", " 800-53b-low"]})
    assert list(twice["baselines"]) == ["800-53b-low"]
    flood = [f"p{i}" for i in range(26)]
    assert "at most 25 distinct keys" in error_text(server, "get_control", {**args, "profiles": flood})
    assert "at most 25 distinct keys" in error_text(
        server, "crosswalk_control", {"catalog": "ai-rmf", "control_id": "measure_2.7", "target_catalogs": flood}
    )
    repeated = ["800-53b-low"] * 5_000  # one distinct key: allowed, and cheap
    assert ok(server, "crosswalk_control", {"catalog": "ai-rmf", "control_id": "measure_2.7", "profiles": repeated})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"profiles": ["nope"]}, "Valid profiles"),
        ({"target_catalogs": ["nope"], "offset": 100_000}, "Valid catalogs"),
        ({"max_hops": 9, "offset": 100_000}, "max_hops"),
        ({"min_confidence": 2.0}, "min_confidence"),
        ({"profiles": ["800-53b-low"]}, "only with control_id"),
    ],
)
def test_ai_governance_listing_mode_validates_every_argument(
    server: MCPServer, args: dict[str, Any], message: str
) -> None:
    assert message in error_text(server, "ai_governance_map", args)


def test_printed_enhancement_ids_are_accepted(server: MCPServer) -> None:
    for spelling in ("AC-2(1)", "AC-02 (01)", "ac-2.1"):
        assert ok(server, "get_control", {"catalog": "800-53r5.2", "control_id": spelling})["control_id"] == "ac-2.1"
    msg = error_text(server, "get_control", {"catalog": "800-53r5.2", "control_id": "AC-2(99)"})
    assert "not found" in msg and "search_controls" in msg


def test_search_query_length_is_bounded(server: MCPServer) -> None:
    assert "at most 512 characters" in error_text(server, "search_controls", {"query": "a " * 300})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("search_controls", {"query": "account", "projection": "full_oscal"}),
        ("list_profile_controls", {"profile": "800-53b-low", "projection": "full_oscal"}),
        ("baseline_delta", {"profile_a": "800-53b-low", "profile_b": "800-53b-high", "projection": "full_oscal"}),
    ],
)
def test_full_oscal_cap_is_explained_when_limit_is_omitted(server: MCPServer, tool: str, args: dict[str, Any]) -> None:
    assert "full_oscal is capped at 3" in ok(server, tool, args)["hint"]
