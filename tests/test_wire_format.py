"""Token efficiency: what the server puts on the wire. These tests are a budget as much as a contract.

An agent pays for every character a tool returns. Compact encoding and the size ceilings below keep a future change
from quietly making responses bigger.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from oscalis.core.graph import OscalGraph
from oscalis.server import create_server
from oscalis.tools.common import slim, to_json
from tests.conftest import run

# ----------------------------------------------------------------------------- slim / to_json


def test_slim_drops_only_the_noisy_empty_fields() -> None:
    src: dict[str, Any] = {
        "class": None, "parent_id": None, "group_id": None, "title": "t", "enhancement_count": 0,
        "parameters": {}, "truncated": False, "results": [],
        "a": {"values": [], "constraints": ["x"]}, "b": {"values": ["y"], "constraints": []},
    }  # fmt: skip
    assert slim(src) == {
        "title": "t", "enhancement_count": 0, "parameters": {}, "truncated": False, "results": [],
        "a": {"constraints": ["x"]}, "b": {"values": ["y"]},
    }  # fmt: skip


def test_slim_keeps_a_null_that_carries_meaning() -> None:
    """'profile A has no value' is information. Only class/parent_id/group_id nulls are noise."""
    assert slim({"a": None, "b": {"text": "x"}}) == {"a": None, "b": {"text": "x"}}


def test_slim_never_alters_raw_oscal_and_does_not_mutate_its_input() -> None:
    oscal: dict[str, Any] = {"id": "ac-2", "class": None, "params": [{"values": []}], "props": []}
    src: dict[str, Any] = {"control_id": "ac-2", "class": None, "oscal": oscal}
    before = copy.deepcopy(src)
    out = slim(src)
    assert out == {"control_id": "ac-2", "oscal": oscal}  # `class` dropped outside oscal, untouched inside
    assert src == before


def test_slim_reaches_into_lists() -> None:
    assert slim({"items": [{"parent_id": None, "id": 1}, {"parent_id": "p", "id": 2}]}) == {
        "items": [{"id": 1}, {"parent_id": "p", "id": 2}]
    }


def test_to_json_is_compact_and_keeps_text_as_written() -> None:
    text = to_json({"k": ["a", {"n": 1}], "apostrophe": "doesn\u2019t"})
    assert text == '{"k":["a",{"n":1}],"apostrophe":"doesn\u2019t"}'  # no spaces, no \u2019 escape
    assert "\n" not in text and ", " not in text and ": " not in text


def test_to_json_round_trips() -> None:
    payload = {"x": [1, 2.5, True, None, "é"], "y": {"z": "q"}}
    assert json.loads(to_json(payload)) == payload


# ----------------------------------------------------------------------------- the server's contract


@pytest.fixture(scope="module")
def server(shipped: OscalGraph) -> MCPServer:
    return create_server(graph=shipped)


def with_client(server: MCPServer, fn: Any) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            return await fn(client)

    return run(go())


def test_no_tool_advertises_an_output_schema(server: MCPServer) -> None:
    """An output schema is sent with the tool list on every session, and invites a duplicate structured copy."""
    tools = with_client(server, lambda c: c.list_tools()).tools
    assert tools and all(t.output_schema is None for t in tools)


def text_of(server: MCPServer, name: str, args: dict[str, Any]) -> str:
    result = with_client(server, lambda c: c.call_tool(name, args))
    assert not result.is_error and len(result.content) == 1 and result.structured_content is None
    return str(result.content[0].text)


CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "list_catalogs": ("list_catalogs", {}),
    "get_control": ("get_control", {"catalog": "800-53r5.2", "control_id": "ac-2", "profiles": ["govramp-moderate"]}),
    "search": ("search_controls", {"query": "access control"}),
    "crosswalk": (
        "crosswalk_control",
        {"catalog": "csf-v2", "control_id": "pr.ds-01", "profiles": ["govramp-moderate"]},
    ),
    "delta": ("baseline_delta", {"profile_a": "govramp-moderate", "profile_b": "govramp-high"}),
    "profile": ("list_profile_controls", {"profile": "800-53b-privacy"}),
    "ai": ("ai_governance_map", {"framework": "ai-rmf"}),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_tool_returns_one_compact_json_block(server: MCPServer, case: str) -> None:
    name, args = CASES[case]
    text = text_of(server, name, args)
    assert text == json.dumps(
        json.loads(text), separators=(",", ":"), ensure_ascii=False
    )  # already in canonical compact form


def test_resources_are_compact_too(server: MCPServer) -> None:
    for uri in ("oscal://catalogs", "oscal://catalog/800-53r5.2/control/ac-2", "oscal://profile/govramp-core"):
        body = with_client(server, lambda c, u=uri: c.read_resource(u)).contents[0].text
        assert "\n" not in body and json.loads(body)


# ----------------------------------------------------------------------------- size budgets
# Ceilings in characters, set about 15% above what the shipped data produces today. A rise past one of these means a
# change made a response heavier. Raise a ceiling only on purpose, and say why in the commit.

AC2 = {"catalog": "800-53r5.2", "control_id": "ac-2"}
PR_DS_01 = {"catalog": "csf-v2", "control_id": "pr.ds-01", "target_catalogs": ["800-53r5.2"]}

BUDGETS: dict[str, tuple[str, dict[str, Any], int]] = {
    "get_control summary": ("get_control", AC2, 180),
    "get_control prose_only": ("get_control", {**AC2, "projection": "prose_only"}, 1_900),
    "get_control prose_only + govramp": (
        "get_control",
        {**AC2, "projection": "prose_only", "profiles": ["govramp-moderate"]},
        2_300,
    ),
    "list_catalogs": ("list_catalogs", {}, 3_800),
    "search default (25)": ("search_controls", {"query": "access control"}, 4_600),
    "crosswalk csf pr.ds-01 (14 hits)": ("crosswalk_control", PR_DS_01, 5_500),
    "crosswalk + 2 profiles": (
        "crosswalk_control",
        {**PR_DS_01, "profiles": ["govramp-moderate", "fedramp-moderate"]},
        10_100,
    ),
    "baseline_delta default": (
        "baseline_delta",
        {"profile_a": "govramp-moderate", "profile_b": "govramp-high"},
        10_200,
    ),
    "list_profile_controls default (25)": ("list_profile_controls", {"profile": "800-53b-privacy"}, 3_900),
}


@pytest.mark.parametrize("label", sorted(BUDGETS))
def test_response_stays_within_its_size_budget(server: MCPServer, label: str) -> None:
    name, args, ceiling = BUDGETS[label]
    size = len(text_of(server, name, args))
    assert size <= ceiling, f"{label}: {size:,} characters exceeds the {ceiling:,} budget"


def test_summary_is_far_smaller_than_the_other_projections(server: MCPServer) -> None:
    base = {"catalog": "800-53r5.2", "control_id": "ac-2"}
    sizes = {
        p: len(text_of(server, "get_control", {**base, "projection": p}))
        for p in ("summary", "prose_only", "full_oscal")
    }
    assert sizes["summary"] * 5 < sizes["prose_only"] < sizes["full_oscal"]
    assert sizes["full_oscal"] < 30_000  # was about 45,000 when indented


def test_one_hop_crosswalk_is_much_smaller_than_the_old_shape(server: MCPServer) -> None:
    """Guards the crosswalk reshape: no per-mapping path for one-hop results, provenance stored once."""
    doc = json.loads(
        text_of(
            server,
            "crosswalk_control",
            {"catalog": "csf-v2", "control_id": "pr.ds-01", "target_catalogs": ["800-53r5.2"]},
        )
    )
    assert all("path" not in m for m in doc["mappings"] if m["hop_count"] == 1)
    assert len(doc["refs"]) == 1  # all 14 mappings come from one source document
    assert (
        text_of(
            server,
            "crosswalk_control",
            {"catalog": "csf-v2", "control_id": "pr.ds-01", "target_catalogs": ["800-53r5.2"]},
        ).count("nist.gov/cyberframework")
        == 1
    )


# ----------------------------------------------------------------------------- baseline_delta, full_oscal cap, titles


def test_baseline_delta_parameter_setting_is_not_sent_twice(server: MCPServer) -> None:
    """A setting carries its `values` or its `constraints`, never a joined `text` copy as well."""
    doc = json.loads(text_of(server, "baseline_delta", {"profile_a": "govramp-moderate", "profile_b": "govramp-high"}))
    changes = doc["parameter_changes"]["items"]
    assert changes
    sides = [c[side] for c in changes for side in ("a", "b") if c[side] is not None]
    assert sides and all("text" not in s for s in sides)
    assert all(set(s) <= {"values", "constraints"} and s for s in sides)  # empty lists are slimmed away


def test_baseline_delta_keeps_none_when_a_profile_sets_no_value(server: MCPServer) -> None:
    doc = json.loads(text_of(server, "baseline_delta", {"profile_a": "800-53b-low", "profile_b": "govramp-low"}))
    sides = [c[side] for c in doc["parameter_changes"]["items"] for side in ("a", "b")]
    assert None in sides  # "no value" is information and must survive `slim`


def test_full_oscal_lists_default_to_three_items(server: MCPServer) -> None:
    doc = json.loads(
        text_of(server, "search_controls", {"query": "access control", "projection": "full_oscal", "limit": 25})
    )
    assert doc["count"] == doc["limit"] == 3
    assert doc["truncated"] is True


def test_short_title_drops_boilerplate_and_abbreviates() -> None:
    from oscalis.tools.get_control import short_title

    assert (
        short_title("Electronic (OSCAL) Version of NIST Special Publication 800-53 Revision 5.2.0 LOW IMPACT BASELINE")
        == "NIST SP 800-53 Rev 5.2.0 LOW IMPACT BASELINE"
    )
    assert short_title("Electronic Version of NIST Cybersecurity Framework 2.0") == "NIST Cybersecurity Framework 2.0"
    assert short_title("FedRAMP Rev 5 High Baseline") == "FedRAMP Rev 5 High Baseline"  # nothing to remove
    assert short_title("Electronic Version of ") != ""  # never returns an empty title


def test_list_catalogs_titles_are_short_and_the_index_keeps_the_published_title(
    server: MCPServer, shipped: OscalGraph
) -> None:
    listing = json.loads(text_of(server, "list_catalogs", {}))
    rows = listing["catalogs"] + listing["profiles"]
    assert rows and all(r["title"] and not r["title"].startswith("Electronic") for r in rows)
    resource = json.loads(with_client(server, lambda c: c.read_resource("oscal://catalogs")).contents[0].text)
    assert [r["title"] for r in resource["catalogs"]] == [r["title"] for r in listing["catalogs"]]
    published = [r["title"] for r in shipped.list_catalogs() + shipped.list_profiles()]
    assert any(t.startswith("Electronic") for t in published)  # the index is untouched


def test_oscal_versions_are_written_without_a_leading_v(server: MCPServer) -> None:
    from oscalis.tools.get_control import clean_oscal_version

    assert clean_oscal_version("v1.2.3") == "1.2.3"
    assert clean_oscal_version("1.2.3") == "1.2.3"
    assert clean_oscal_version("1.0.4") == "1.0.4"
    assert clean_oscal_version("vague") == "vague"  # only a "v" followed by a version number is removed
    listing = json.loads(text_of(server, "list_catalogs", {}))
    versions = {r["oscal_version"] for r in listing["catalogs"] + listing["profiles"]}
    assert versions and not any(v.startswith("v") for v in versions)
    assert "1.2.3" in versions  # still the real version, not blanked


# ----------------------------------------------------------------------------- fit_page


def _settings(budget: int) -> Any:
    from oscalis.core.config import Settings

    return Settings(max_response_chars=budget)


def _page(n: int, item_chars: int = 100) -> dict[str, Any]:
    return {"items": ["x" * item_chars] * n, "limit": n}


def test_fit_page_leaves_a_page_that_fits_alone() -> None:
    from oscalis.tools.common import fit_page

    out = fit_page(_settings(10_000), 5, _page)
    assert out == _page(5)  # no hint added


def test_fit_page_shortens_until_the_response_fits_and_says_so() -> None:
    from oscalis.tools.common import fit_page

    built: list[int] = []

    def build(n: int) -> dict[str, Any]:
        built.append(n)
        return _page(n)

    out = fit_page(_settings(4_000), 100, build)
    assert len(to_json(out)) <= 4_000 + 200  # the hint itself may add a little
    assert 1 <= out["limit"] < 100 and out["limit"] == len(out["items"])
    assert "Page shortened to" in out["hint"] and "4,000" in out["hint"]
    assert len(built) <= 4  # proportional shrinking, not one item at a time


def test_fit_page_keeps_an_existing_hint() -> None:
    from oscalis.tools.common import fit_page

    out = fit_page(_settings(4_000), 100, lambda n: {**_page(n), "hint": "Pass offset=9."})
    assert out["hint"].startswith("Pass offset=9. Page shortened")


def test_fit_page_still_returns_one_item_that_is_bigger_than_the_budget() -> None:
    from oscalis.tools.common import fit_page

    out = fit_page(_settings(4_000), 10, lambda n: _page(n, item_chars=9_000))
    assert out["limit"] == 1 and len(out["items"]) == 1


def test_response_budget_is_configurable_within_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from oscalis.core.config import Settings

    assert Settings().max_response_chars == 24_000
    monkeypatch.setenv("OSCALIS_MAX_RESPONSE_CHARS", "60000")
    assert Settings.from_env().max_response_chars == 60_000
    monkeypatch.setenv("OSCALIS_MAX_RESPONSE_CHARS", "100")
    with pytest.raises(ValueError, match="OSCALIS_MAX_RESPONSE_CHARS"):
        Settings.from_env()
