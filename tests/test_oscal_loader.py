"""OSCAL parsing, validation, profile resolution, and index loading."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from oscalis.core.config import Settings
from oscalis.core.graph import OscalGraph, UnknownKeyError
from oscalis.core.oscal import (
    OscalValidationError,
    flatten_catalog,
    load_document,
    parse_catalog,
    validate_document,
)
from tests.conftest import FIXTURE_DATA, catalog_doc, crosswalk_doc, ctl, profile_doc, write_json

MakeGraph = Callable[..., OscalGraph]


# ----------------------------------------------------------------------------- catalogs


def test_seed_catalogs_are_valid_oscal() -> None:
    for path in sorted(FIXTURE_DATA.rglob("*.json")):
        _kind, errors = validate_document(load_document(path))
        assert errors == [], f"{path.name}: {errors}"


def test_flatten_lowercases_ids_and_tracks_enhancements(graph: OscalGraph) -> None:
    ac2 = graph.get_control("800-53r5.2", "AC-2")
    assert ac2 is not None
    assert ac2["control_id"] == "ac-2"
    assert ac2["enhancement_ids"] == ["ac-2.1"]
    enh = graph.get_control("800-53r5.2", "ac-2.1")
    assert enh is not None
    assert enh["parent_id"] == "ac-2"
    assert enh["depth"] == 1
    csf = graph.get_control("csf-v2", "GV.PO-01")
    assert csf is not None and csf["control_id"] == "gv.po-01"


def test_prose_substitutes_parameters(graph: OscalGraph) -> None:
    ac2 = graph.get_control("800-53r5.2", "ac-2")
    assert ac2 is not None
    assert "[Assignment: system account types]" in ac2["prose"]
    assert "{{" not in ac2["prose"]


def test_select_parameter_rendering() -> None:
    doc = catalog_doc(
        [
            ctl(
                "x-1",
                parts=[{"name": "statement", "prose": "Use {{ insert: param, x-1_prm }} auth."}],
                params=[{"id": "x-1_prm", "select": {"how-many": "one-or-more", "choice": ["MFA", "PKI"]}}],
            )
        ]
    )
    flat = flatten_catalog(parse_catalog(doc))
    assert "[Selection (one or more): MFA; PKI]" in flat.controls[0].prose


def test_full_oscal_excludes_nested_controls(graph: OscalGraph) -> None:
    ac2 = graph.get_control("800-53r5.2", "ac-2", full=True)
    assert ac2 is not None
    assert "controls" not in ac2["oscal"]
    assert ac2["oscal"]["id"] == "ac-2"


def test_rejects_non_oscal_control_key() -> None:
    doc = catalog_doc([{"control_id": "x-1", "title": "bad"}])
    _kind, errors = validate_document(doc)
    assert any("control_id" in e for e in errors)
    assert any("Field required" in e for e in errors)  # `id` is missing


@pytest.mark.parametrize("bad", ["not-a-uuid", "12345678-1234-1234-1234-123456789012", ""])
def test_rejects_invalid_uuid(bad: str) -> None:
    doc = catalog_doc([ctl("x-1")])
    doc["catalog"]["uuid"] = bad
    _kind, errors = validate_document(doc)
    assert any("uuid" in e.lower() for e in errors)


def test_rejects_missing_uuid() -> None:
    doc = catalog_doc([ctl("x-1")])
    del doc["catalog"]["uuid"]
    _kind, errors = validate_document(doc)
    assert any(e.startswith("uuid") for e in errors)


def test_rejects_a_nested_uuid_with_a_trailing_newline() -> None:
    """Nested UUIDs (parties, back-matter) are only checked by the validator, not by the pydantic models."""
    doc = catalog_doc([ctl("x-1")])
    doc["catalog"]["metadata"]["parties"] = [{"uuid": str(uuid.uuid4()) + "\n", "type": "organization"}]
    _kind, errors = validate_document(doc)
    assert any("metadata.parties[0].uuid: invalid UUID" in e for e in errors)


def test_rejects_duplicate_uuid_and_missing_backmatter_uuid() -> None:
    doc = catalog_doc([ctl("x-1")])
    dup = str(uuid.uuid4())
    doc["catalog"]["uuid"] = dup
    doc["catalog"]["back-matter"] = {"resources": [{"uuid": dup}, {"title": "no uuid"}]}
    _kind, errors = validate_document(doc)
    assert any("duplicate UUID" in e for e in errors)
    assert any("missing required uuid" in e for e in errors)


def test_rejects_duplicate_control_ids_case_insensitively() -> None:
    doc = catalog_doc([ctl("AC-1"), ctl("ac-1")])
    _kind, errors = validate_document(doc)
    assert any("duplicate control id" in e for e in errors)


def test_rejects_dangling_link_but_accepts_part_ids() -> None:
    ok = catalog_doc(
        [
            ctl("a-1", parts=[{"id": "a-1_smt", "name": "statement", "prose": "p"}]),
            ctl("a-2", links=[{"href": "#a-1_smt", "rel": "related"}, {"href": "#a-1", "rel": "related"}]),
        ]
    )
    assert validate_document(ok)[1] == []
    bad = catalog_doc([ctl("a-1", links=[{"href": "#zz-9", "rel": "related"}])])
    assert any("unknown id '#zz-9'" in e for e in validate_document(bad)[1])


def test_enforces_minimum_oscal_version() -> None:
    old = catalog_doc([ctl("x-1")], oscal="1.0.4")
    assert any("below required minimum 1.1.0" in e for e in validate_document(old)[1])
    assert validate_document(old, min_oscal_version=(1, 0, 0))[1] == []
    prefixed = catalog_doc([ctl("x-1")], oscal="v1.2.3")
    assert validate_document(prefixed)[1] == []


def test_requires_groups_or_controls() -> None:
    doc = catalog_doc([ctl("x-1")])
    del doc["catalog"]["groups"]
    assert any("at least one of" in e for e in validate_document(doc)[1])


def test_top_level_must_be_one_known_model() -> None:
    assert validate_document({"catalog": {}, "profile": {}})[1]
    assert validate_document({"something-else": {}})[1]


def test_load_document_handles_bom_yaml_and_bad_input(tmp_path: Path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + b'{"catalog": {}}')
    assert "catalog" in load_document(bom)
    y = tmp_path / "c.yaml"
    y.write_text("catalog:\n  uuid: x\n", encoding="utf-8")
    assert load_document(y)["catalog"]["uuid"] == "x"
    with pytest.raises(OscalValidationError):
        load_document(write_json(tmp_path / "list.json", [1]))  # type: ignore[arg-type]
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(OscalValidationError):
        load_document(broken)
    with pytest.raises(OscalValidationError):
        load_document(tmp_path / "x.txt")


# ----------------------------------------------------------------------------- profiles


def test_seed_baselines_resolve(graph: OscalGraph) -> None:
    sizes = {p["key"]: p["control_count"] for p in graph.list_profiles()}
    assert sizes == {"800-53b-low": 7, "800-53b-moderate": 10, "800-53b-high": 10, "fedramp-moderate": 10}
    low = graph.profile_members("800-53b-low")
    assert ("800-53r5.2", "ac-6") not in low
    assert ("800-53r5.2", "ac-6") in graph.profile_members("800-53b-moderate")


def test_profile_chaining_inherits_members_and_sets_parameters(graph: OscalGraph) -> None:
    assert graph.profile_members("fedramp-moderate") == graph.profile_members("800-53b-moderate")
    status = graph.profile_status("800-53r5.2", "ra-3", ["fedramp-moderate", "800-53b-low"])
    assert status["fedramp-moderate"]["in_baseline"] is True
    assert status["fedramp-moderate"]["parameters"]["ra-03_odp.01"]["values"] == ["illustrative: at least annually"]
    assert status["800-53b-low"]["parameters"] == {}


def test_profile_params_only_for_members(graph: OscalGraph) -> None:
    status = graph.profile_status("800-53r5.2", "ac-2", ["fedramp-moderate"])
    assert "ac-02_odp.03" in status["fedramp-moderate"]["parameters"]


@pytest.fixture
def tree_catalog() -> dict[str, Any]:
    return catalog_doc(
        [
            ctl("a-1", controls=[ctl("a-1.1", controls=[ctl("a-1.1.1")]), ctl("a-1.2")]),
            ctl("a-2"),
            ctl("a-3"),
        ]
    )


def test_with_child_controls_includes_descendants(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc(
        [{"href": "cat.json", "include-controls": [{"with-ids": ["a-1"], "with-child-controls": "yes"}]}]
    )
    g = make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof})
    assert {cid for _c, cid in g.profile_members("p")} == {"a-1", "a-1.1", "a-1.1.1", "a-1.2"}


def test_include_without_child_flag_excludes_descendants(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"with-ids": ["a-1", "A-2"]}]}])
    g = make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof})
    assert {cid for _c, cid in g.profile_members("p")} == {"a-1", "a-2"}


def test_exclude_controls_removes_from_include_all(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "cat.json", "include-all": {}, "exclude-controls": [{"with-ids": ["a-2", "a-1.1"]}]}])
    g = make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof})
    assert {cid for _c, cid in g.profile_members("p")} == {"a-1", "a-1.1.1", "a-1.2", "a-3"}


def test_import_by_backmatter_uuid_and_alias(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    rid = str(uuid.uuid4())
    prof = profile_doc(
        [{"href": f"#{rid}", "include-all": {}}],
        **{
            "back-matter": {
                "resources": [
                    {
                        "uuid": rid,
                        "rlinks": [
                            {"href": "https://example.test/x/UPSTREAM_catalog.xml"},
                            {"href": "https://example.test/x/UPSTREAM_catalog.json"},
                        ],
                    }
                ]
            }
        },
    )
    g = make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof}, href_aliases={"UPSTREAM_catalog.json": "cat"})
    assert len(g.profile_members("p")) == 6


def test_profile_cycle_is_detected(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    a = profile_doc([{"href": "b.json", "include-all": {}}])
    b = profile_doc([{"href": "a.json", "include-all": {}}])
    with pytest.raises(OscalValidationError, match="cycle"):
        make_graph(catalogs={"cat": tree_catalog}, profiles={"a": a, "b": b})


def test_unresolvable_import_names_the_candidates(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "missing.json", "include-all": {}}])
    with pytest.raises(OscalValidationError, match="does not resolve"):
        make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof})


def members(g: OscalGraph, profile: str) -> set[str]:
    return {cid for _c, cid in g.profile_members(profile)}


def multi_family_catalog() -> dict[str, Any]:
    return catalog_doc(
        [
            ctl("ac-1"),
            ctl("ac-2", controls=[ctl("ac-2.1"), ctl("ac-2.2")]),
            ctl("ac-3"),
            ctl("au-1"),
            ctl("au-2", controls=[ctl("au-2.1")]),
        ]
    )


def test_matching_selects_by_wildcard_pattern(make_graph: MakeGraph) -> None:
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"matching": [{"pattern": "ac-*"}]}]}])
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"ac-1", "ac-2", "ac-2.1", "ac-2.2", "ac-3"}


def test_matching_is_case_insensitive_and_supports_single_character_wildcards(make_graph: MakeGraph) -> None:
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"matching": [{"pattern": "AU-?"}]}]}])
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"au-1", "au-2"}  # '?' is one character, so au-2.1 is not matched


def test_matching_combines_with_with_ids_and_multiple_patterns(make_graph: MakeGraph) -> None:
    prof = profile_doc(
        [
            {
                "href": "cat.json",
                "include-controls": [{"with-ids": ["au-1"], "matching": [{"pattern": "ac-1"}, {"pattern": "ac-3"}]}],
            }
        ]
    )
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"au-1", "ac-1", "ac-3"}


def test_matching_with_child_controls_adds_enhancements_of_matches_only(make_graph: MakeGraph) -> None:
    prof = profile_doc(
        [{"href": "cat.json", "include-controls": [{"matching": [{"pattern": "ac-2"}], "with-child-controls": "yes"}]}]
    )
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"ac-2", "ac-2.1", "ac-2.2"}


def test_child_expansion_applies_only_to_the_selector_that_asked_for_it(make_graph: MakeGraph) -> None:
    """A control picked by an earlier selector without child expansion must not gain enhancements from a later one."""
    prof = profile_doc(
        [
            {
                "href": "cat.json",
                "include-controls": [
                    {"with-ids": ["ac-2"], "with-child-controls": "no"},
                    {"with-ids": ["au-2"], "with-child-controls": "yes"},
                ],
            }
        ]
    )
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"ac-2", "au-2", "au-2.1"}  # ac-2.1 and ac-2.2 stay out


def test_exclude_by_pattern_removes_from_include_all(make_graph: MakeGraph) -> None:
    prof = profile_doc(
        [{"href": "cat.json", "include-all": {}, "exclude-controls": [{"matching": [{"pattern": "au-*"}]}]}]
    )
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"ac-1", "ac-2", "ac-2.1", "ac-2.2", "ac-3"}


def test_pattern_matching_nothing_warns(make_graph: MakeGraph) -> None:
    prof = profile_doc(
        [{"href": "cat.json", "include-controls": [{"with-ids": ["ac-1"], "matching": [{"pattern": "zz-*"}]}]}]
    )
    g = make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})
    assert members(g, "p") == {"ac-1"} and any("zz-*" in w for w in g.warnings)


@pytest.mark.parametrize("bad", [{"pattern": ""}, {"pattern": "   "}, {"other": "x"}, {"pattern": 5}])
def test_matching_without_a_usable_pattern_is_an_error(make_graph: MakeGraph, bad: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"matching": [bad]}]}])
    with pytest.raises(OscalValidationError, match="pattern"):
        make_graph(catalogs={"cat": multi_family_catalog()}, profiles={"p": prof})


def test_unknown_selector_ids_warn(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"with-ids": ["a-1", "zz-9"]}]}])
    g = make_graph(catalogs={"cat": tree_catalog}, profiles={"p": prof})
    assert any("zz-9" in w for w in g.warnings)


def test_key_used_by_catalog_and_profile_is_rejected(make_graph: MakeGraph, tree_catalog: dict[str, Any]) -> None:
    prof = profile_doc([{"href": "same.json", "include-all": {}}])
    with pytest.raises(OscalValidationError, match="both a catalog and a profile"):
        make_graph(catalogs={"same": tree_catalog}, profiles={"same": prof})


# ----------------------------------------------------------------------------- crosswalk files


def test_crosswalk_rules_on_authoring() -> None:
    doc = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "intersecting", 0.99)])
    errs = validate_document(doc)[1]
    assert any("outside intersecting range 0.50-0.70" in e for e in errs)

    doc = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "advisory", 0.5)])
    assert any("outside advisory range" in e for e in validate_document(doc)[1])

    doc = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "direct", 0.95)])
    del doc["mapping-collection"]["mappings"][0]["provenance"]["href"]
    assert any("require provenance.href" in e for e in validate_document(doc)[1])

    doc = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "transitive", 0.8)])  # derived-only method
    assert validate_document(doc)[1]

    doc = crosswalk_doc("a", "b", [("x-1", "bogus", "y-1", "direct", 0.95)])
    assert validate_document(doc)[1]

    doc = crosswalk_doc("a", "a", [("x-1", "related", "y-1", "direct", 0.95)])
    assert any("identical" in e for e in validate_document(doc)[1])


def test_load_fails_on_dangling_crosswalk_edge(make_graph: MakeGraph) -> None:
    a = catalog_doc([ctl("x-1")])
    b = catalog_doc([ctl("y-1")])
    xw = crosswalk_doc("a", "b", [("x-1", "equivalent", "y-404", "direct", 0.95)])
    with pytest.raises(OscalValidationError, match="unknown node 'b:y-404'"):
        make_graph(catalogs={"a": a, "b": b}, crosswalks={"a__b": xw})


def test_load_fails_on_unknown_catalog_in_crosswalk(make_graph: MakeGraph) -> None:
    a = catalog_doc([ctl("x-1")])
    xw = crosswalk_doc("a", "ghost", [("x-1", "equivalent", "y-1", "direct", 0.95)])
    with pytest.raises(OscalValidationError, match="ghost"):
        make_graph(catalogs={"a": a}, crosswalks={"a__ghost": xw})


# ----------------------------------------------------------------------------- index behavior


def test_unknown_catalog_error_lists_valid_keys(graph: OscalGraph) -> None:
    with pytest.raises(UnknownKeyError) as exc:
        graph.require_catalog("nope")
    assert "800-53r5.2" in str(exc.value) and "ai-rmf" in str(exc.value)


def test_index_is_read_only_after_load(graph: OscalGraph) -> None:
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        graph._conn.execute("DELETE FROM controls")


def test_search_is_safe_against_fts_syntax(graph: OscalGraph) -> None:
    rows, total = graph.search('least" ( privilege* ', "800-53r5.2", 10, 0)
    assert total >= 1 and rows[0]["control_id"] == "ac-6"
    # operators are plain words, not FTS syntax: 'OR' must not widen the match
    assert graph.search("least OR zzzzqqqq", "800-53r5.2", 10, 0)[1] == 0
    with pytest.raises(ValueError, match="at least one letter"):
        graph.search('"*()', None, 10, 0)


def test_file_backed_index(tmp_path: Path) -> None:
    db = tmp_path / "idx" / "oscalis.sqlite"
    g = OscalGraph.load(Settings(data_dir=FIXTURE_DATA, db_path=db))
    assert db.is_file()
    assert g.get_control("800-53r5.2", "ac-2") is not None


def test_empty_data_dir_loads_with_no_catalogs(tmp_path: Path) -> None:
    g = OscalGraph.load(Settings(data_dir=tmp_path))
    assert g.list_catalogs() == [] and g.edge_count() == 0


def test_validation_does_not_mutate_input() -> None:
    doc = catalog_doc([ctl("x-1")])
    before = copy.deepcopy(doc)
    validate_document(doc)
    assert doc == before


# ----------------------------------------------------------------------------- parameter rendering


def test_parameters_inside_selection_choices_are_rendered() -> None:
    """AC-11 a: one choice of the selection inserts another parameter."""
    from oscalis.core.oscal import Param, render_text

    params = {
        "ac-11_odp.01": Param(
            id="ac-11_odp.01",
            select={
                "how-many": "one-or-more",
                "choice": ["after {{ insert: param, ac-11_odp.02 }} of inactivity", "on user request"],
            },
        ),
        "ac-11_odp.02": Param(id="ac-11_odp.02", label="time period"),
    }
    got = render_text("Lock the device [{{ insert: param, ac-11_odp.01 }}].", params)
    assert got == (
        "Lock the device [[Selection (one or more): after [Assignment: time period] of inactivity; on user request]]."
    )


def test_a_parameter_inserted_inside_itself_does_not_recurse() -> None:
    from oscalis.core.oscal import Param, render_text

    loop = {"p": Param(id="p", select={"choice": ["again {{ insert: param, p }}"]})}
    assert render_text("{{ insert: param, p }}", loop) == "[Selection (one): again [parameter p]]"


def test_shipped_catalog_prose_has_no_raw_insert_tags() -> None:
    from oscalis.core.oscal import flatten_catalog, load_document, parse_catalog

    repo = Path(__file__).resolve().parent.parent
    flat = flatten_catalog(parse_catalog(load_document(repo / "data" / "catalogs" / "800-53r5.2.json")))
    leftovers = [c.control_id for c in flat.controls if "{{" in c.prose]
    assert leftovers == []


# ----------------------------------------------------------------------------- AI RMF part layout


def _part(
    name: str, prose: str | None = None, cls: str | None = None, parts: list[Any] | None = None
) -> dict[str, Any]:
    p: dict[str, Any] = {"name": name}
    if prose is not None:
        p["prose"] = prose
    if cls is not None:
        p["class"] = cls
    if parts is not None:
        p["parts"] = parts
    return p


def _flat(control: dict[str, Any]) -> Any:
    return next(c for c in flatten_catalog(parse_catalog(catalog_doc([control]))).controls)


def test_ai_rmf_outcome_is_the_prose_and_suggested_actions_are_guidance() -> None:
    """NIST's AI RMF catalog puts the outcome in `overview` and the Playbook's advice in a suggested-action part."""
    control = ctl(
        "govern_1.1",
        parts=[
            _part("overview", "Legal and regulatory requirements involving AI are understood."),
            _part(
                "statement",
                cls="suggested_action",
                parts=[_part("item", "Maintain awareness.", "suggested_action"), _part("item", "Align efforts.")],
            ),
            _part("guidance", "AI systems may be subject to requirements.", "about"),
        ],
    )
    flat = _flat(control)
    assert flat.prose == "Legal and regulatory requirements involving AI are understood."
    assert flat.guidance == (
        "AI systems may be subject to requirements.\nSuggested actions:\n- Maintain awareness.\n- Align efforts."
    )


def test_a_real_statement_wins_over_an_overview() -> None:
    """Only a catalog with no requirement statement falls back to `overview`."""
    control = ctl(
        "x-1", parts=[_part("overview", "Background only."), _part("statement", "The organization shall do X.")]
    )
    flat = _flat(control)
    assert flat.prose == "The organization shall do X." and "Suggested actions" not in flat.guidance


def test_empty_suggested_actions_add_nothing() -> None:
    control = ctl("x-1", parts=[_part("overview", "Outcome."), _part("statement", cls="suggested_action", parts=[])])
    flat = _flat(control)
    assert flat.prose == "Outcome." and flat.guidance == ""
