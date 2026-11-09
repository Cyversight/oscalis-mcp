"""Crosswalk graph: edge derivation, composition, confidence scoring, and traversal rules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from oscalis.core.graph import CAVEATS, Hop, OscalGraph, score_path
from oscalis.core.oscal import OscalValidationError, validate_document
from oscalis.tools.crosswalk import crosswalk_control
from tests.conftest import catalog_doc, crosswalk_doc, ctl

MakeGraph = Callable[..., OscalGraph]


def hop(relation: str, conf: float, method: str = "direct", src: str = "a:n1", dst: str = "b:n1") -> Hop:
    return Hop(src, f"crosswalk:{relation}", dst, conf, method, "test", False)


# ----------------------------------------------------------------------------- scoring (pure)


def test_single_hop_keeps_edge_values() -> None:
    assert score_path([hop("related", 0.65, "intersecting")]) == ("crosswalk:related", 0.65, "intersecting")


def test_two_hops_multiply_and_decay() -> None:
    rel, conf, method = score_path([hop("related", 0.7, "intersecting"), hop("related", 0.6, "intersecting")])
    assert (rel, conf, method) == ("crosswalk:related", 0.38, "intersecting")  # 0.7 * 0.6 * 0.9


def test_direct_hops_become_transitive_and_are_capped() -> None:
    rel, conf, method = score_path([hop("satisfies", 1.0), hop("satisfies", 1.0)])
    assert method == "transitive"
    assert conf == 0.80  # 1.0 * 1.0 * 0.9 = 0.90, capped at 0.80
    assert rel == "crosswalk:satisfies"


def test_weakest_method_wins() -> None:
    _r, _c, method = score_path([hop("related", 0.95), hop("related", 0.4, "advisory"), hop("related", 0.95)])
    assert method == "advisory"
    _r, _c, method = score_path([hop("related", 0.95), hop("related", 0.6, "intersecting")])
    assert method == "intersecting"


def test_three_hop_decay() -> None:
    _r, conf, _m = score_path([hop("related", 0.9)] * 3)
    assert conf == round(0.9**3 * 0.9**2, 2)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("equivalent", "equivalent", "equivalent"),
        ("equivalent", "satisfies", "satisfies"),
        ("equivalent", "superset", "superset"),
        ("equivalent", "subset", "subset"),
        ("satisfies", "satisfies", "satisfies"),
        ("satisfies", "superset", "satisfies"),
        ("satisfies", "subset", "related"),
        ("superset", "satisfies", "related"),
        ("superset", "superset", "superset"),
        ("superset", "subset", "related"),
        ("subset", "subset", "subset"),
        ("subset", "superset", "related"),
        ("related", "equivalent", "related"),
        ("related", "satisfies", "related"),
        ("superset", "related", "related"),
    ],
)
def test_composition_table(a: str, b: str, expected: str) -> None:
    # use intersecting hops so the multi-hop equivalence rule does not interfere
    rel, _c, _m = score_path([hop(a, 0.6, "intersecting"), hop(b, 0.6, "intersecting")])
    want = expected if expected != "equivalent" else "related"  # non-direct equivalence degrades
    assert rel == f"crosswalk:{want}"


def test_multi_hop_equivalent_requires_all_direct_and_equivalent() -> None:
    rel, _c, _m = score_path([hop("equivalent", 0.95), hop("equivalent", 0.95)])
    assert rel == "crosswalk:equivalent"
    rel, _c, _m = score_path([hop("equivalent", 0.95), hop("equivalent", 0.6, "intersecting")])
    assert rel == "crosswalk:related"


# ----------------------------------------------------------------------------- loading


def build_chain(make_graph: MakeGraph, **settings: Any) -> OscalGraph:
    """a:1 -> b:1 -> c:1, plus a:1 -> c:1 directly (lower confidence), and an island a:9."""
    return make_graph(
        catalogs={
            "a": catalog_doc([ctl("n1"), ctl("n2"), ctl("n9")]),
            "b": catalog_doc([ctl("n1"), ctl("n2")]),
            "c": catalog_doc([ctl("n1"), ctl("n2")]),
        },
        crosswalks={
            "a__b": crosswalk_doc(
                "a",
                "b",
                [
                    ("n1", "equivalent", "n1", "direct", 0.95),
                    ("n2", "related", "n2", "intersecting", 0.60),
                ],
            ),
            "b__c": crosswalk_doc(
                "b",
                "c",
                [
                    ("n1", "equivalent", "n1", "direct", 0.95),
                    ("n2", "satisfies", "n2", "intersecting", 0.60),
                ],
            ),
            "a__c": crosswalk_doc("a", "c", [("n1", "related", "n1", "advisory", 0.30)]),
        },
        **settings,
    )


def test_inverse_edges_are_derived_except_satisfies(make_graph: MakeGraph) -> None:
    g = make_graph(
        catalogs={
            "a": catalog_doc([ctl("n1"), ctl("n2"), ctl("n3"), ctl("n4")]),
            "b": catalog_doc([ctl("n1"), ctl("n2"), ctl("n3"), ctl("n4")]),
        },
        crosswalks={
            "a__b": crosswalk_doc(
                "a",
                "b",
                [
                    ("n1", "equivalent", "n1", "direct", 0.95),
                    ("n2", "superset", "n2", "direct", 0.95),
                    ("n3", "satisfies", "n3", "direct", 0.95),
                    ("n4", "related", "n4", "advisory", 0.30),
                ],
            )
        },
    )
    back = {e.src: (e.relation, e.derived) for n in ("b:n1", "b:n2", "b:n3", "b:n4") for e in g.edges_from(n)}
    assert back["b:n1"] == ("crosswalk:equivalent", True)
    assert back["b:n2"] == ("crosswalk:subset", True)
    assert back["b:n4"] == ("crosswalk:related", True)
    assert "b:n3" not in back  # satisfies is never auto-inverted
    assert g.edges_from("a:n3")[0].derived is False


def test_explicit_edge_beats_derived_and_duplicates_keep_highest_confidence(make_graph: MakeGraph) -> None:
    g = make_graph(
        catalogs={"a": catalog_doc([ctl("n1")]), "b": catalog_doc([ctl("n1")])},
        crosswalks={
            "a__b": crosswalk_doc("a", "b", [("n1", "related", "n1", "advisory", 0.30)]),
            "b__a": crosswalk_doc("b", "a", [("n1", "related", "n1", "intersecting", 0.60)]),
            "a__b2": crosswalk_doc("a", "b", [("n1", "related", "n1", "intersecting", 0.55)]),
        },
    )
    fwd = [e for e in g.edges_from("a:n1") if e.dst == "b:n1"]
    assert len(fwd) == 1 and fwd[0].confidence == 0.55 and not fwd[0].derived
    back = [e for e in g.edges_from("b:n1") if e.dst == "a:n1"]
    assert len(back) == 1 and back[0].confidence == 0.60 and not back[0].derived


# ----------------------------------------------------------------------------- traversal


def test_shortest_path_wins_even_when_longer_path_scores_higher(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    res = {r.node: r for r in g.traverse("a:n1", min_confidence=0.0)}
    assert len(res["c:n1"].hops) == 1  # direct a->c, even though a->b->c would score 0.81
    assert res["c:n1"].confidence == 0.30
    assert res["b:n1"].relation == "crosswalk:equivalent"


def test_transitive_path_when_no_shortcut(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    res = {r.node: r for r in g.traverse("a:n2", min_confidence=0.0)}
    r = res["c:n2"]
    assert [h.dst for h in r.hops] == ["b:n2", "c:n2"]
    assert r.relation == "crosswalk:related"  # related o satisfies = related
    assert r.confidence == round(0.6 * 0.6 * 0.9, 2)
    assert r.method == "intersecting"


def test_min_confidence_filters_and_max_hops_limits(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    assert {r.node for r in g.traverse("a:n2", min_confidence=0.5)} == {"b:n2"}
    assert {r.node for r in g.traverse("a:n2", min_confidence=0.0, max_hops=1)} == {"b:n2"}
    assert {r.node for r in g.traverse("a:n2", min_confidence=0.3)} == {"b:n2", "c:n2"}


def test_default_min_confidence_comes_from_settings(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph, min_confidence=0.9)
    assert {r.node for r in g.traverse("a:n1")} == {"b:n1"}


def test_hard_cap_of_four_hops(make_graph: MakeGraph) -> None:
    cats = {f"k{i}": catalog_doc([ctl("n1")]) for i in range(7)}
    xws = {
        f"k{i}__k{i + 1}": crosswalk_doc(f"k{i}", f"k{i + 1}", [("n1", "equivalent", "n1", "direct", 1.0)])
        for i in range(6)
    }
    g = make_graph(catalogs=cats, crosswalks=xws)
    nodes = {r.node for r in g.traverse("k0:n1", max_hops=10, min_confidence=0.0)}
    assert nodes == {"k1:n1", "k2:n1", "k3:n1", "k4:n1"}


def test_start_node_never_returned_and_no_revisits(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    results = g.traverse("a:n1", min_confidence=0.0)
    nodes = [r.node for r in results]
    assert "a:n1" not in nodes and len(nodes) == len(set(nodes))


def test_target_catalog_filter_still_traverses_through_other_catalogs(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    only_c = g.traverse("a:n2", min_confidence=0.0, target_catalogs=["c"])
    assert [r.node for r in only_c] == ["c:n2"]
    assert len(only_c[0].hops) == 2


def test_results_sorted_by_confidence_desc(make_graph: MakeGraph) -> None:
    g = build_chain(make_graph)
    confs = [r.confidence for r in g.traverse("a:n1", min_confidence=0.0)]
    assert confs == sorted(confs, reverse=True)


def test_satisfies_direction_is_respected(graph: OscalGraph) -> None:
    out = graph.traverse("800-53r5.2:sc-28", min_confidence=0.0)
    assert any(r.node == "csf-v2:pr.ds-01" and r.relation == "crosswalk:satisfies" for r in out)
    assert graph.traverse("csf-v2:pr.ds-01", min_confidence=0.0) == []  # no implicit reverse


# ----------------------------------------------------------------------------- seed canonical chain


def test_seed_chain_ai_rmf_to_80053_to_csf(graph: OscalGraph) -> None:
    out = graph.traverse("ai-rmf:measure_2.7")
    by_node = {r.node: r for r in out}
    assert set(by_node) == {"800-53r5.2:sc-7", "800-53r5.2:ra-3", "csf-v2:de.cm-01"}
    two_hop = by_node["csf-v2:de.cm-01"]
    assert two_hop.confidence == 0.38 and len(two_hop.hops) == 2


def test_tool_adds_caveats_provenance_and_baseline_status(graph: OscalGraph) -> None:
    res = crosswalk_control(
        graph, "AI-RMF".lower(), "MEASURE_2.7", ["800-53r5.2"], profiles=["fedramp-moderate", "800-53b-low"]
    )
    ra3 = next(m for m in res["mappings"] if m["target"]["control_id"] == "ra-3")
    assert ra3["caveat"] == CAVEATS["intersecting"]
    assert ra3["hop_count"] == 1 and "path" not in ra3  # a one-hop mapping is its own hop
    assert "analyst-authored" in res["refs"][ra3["ref"]]  # the reference resolves to the fixture crosswalk's provenance
    assert ra3["baselines"]["fedramp-moderate"]["parameters"]["ra-03_odp.01"]["values"]
    assert ra3["baselines"]["800-53b-low"]["in_baseline"] is True
    assert all("confidence" in m and "method" in m for m in res["mappings"])


def test_multi_hop_mappings_list_every_hop(graph: OscalGraph) -> None:
    res = crosswalk_control(graph, "ai-rmf", "measure_2.7", ["csf-v2"])
    two_hop = next(m for m in res["mappings"] if m["target"]["control_id"] == "de.cm-01")
    assert two_hop["hop_count"] == 2 and "ref" not in two_hop  # provenance is per hop here
    first, second = two_hop["path"]
    assert (first["src"], first["dst"]) == ("ai-rmf:measure_2.7", "800-53r5.2:sc-7")
    assert (second["src"], second["dst"]) == ("800-53r5.2:sc-7", "csf-v2:de.cm-01")
    for hop in two_hop["path"]:
        assert set(hop) >= {"src", "relation", "dst", "confidence", "method", "ref"}
        assert hop["ref"] in res["refs"]


def test_every_ref_resolves_and_each_source_text_is_stored_once(graph: OscalGraph) -> None:
    res = crosswalk_control(graph, "ai-rmf", "measure_2.7", min_confidence=0.0)
    used = {m["ref"] for m in res["mappings"] if "ref" in m} | {
        h["ref"] for m in res["mappings"] for h in m.get("path", [])
    }
    assert used and used <= set(res["refs"])
    assert len(set(res["refs"].values())) == len(res["refs"])  # no source text is stored twice
    assert set(res["refs"]) == used  # and nothing unused is sent


def test_derived_inverse_is_reported_only_when_true(graph: OscalGraph) -> None:
    forward = crosswalk_control(graph, "ai-rmf", "measure_2.7", ["800-53r5.2"])
    assert not any("derived_inverse" in m for m in forward["mappings"])
    back = crosswalk_control(
        graph, "800-53r5.2", "sc-7", ["ai-rmf"]
    )  # reached through an automatically derived inverse
    hit = next(m for m in back["mappings"] if m["target"]["control_id"] == "measure_2.7")
    assert hit["derived_inverse"] is True


def test_an_empty_result_has_an_empty_refs_table(graph: OscalGraph) -> None:
    res = crosswalk_control(graph, "csf-v2", "pr.ds-01", min_confidence=1.0)
    assert res["mappings"] == [] and res["refs"] == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_hops": 9}, "max_hops"),
        ({"min_confidence": 1.5}, "min_confidence"),
        ({"target_catalogs": ["nope"]}, "Valid catalogs"),
        ({"profiles": ["nope"]}, "Valid profiles"),
        ({"projection": "everything"}, "projection"),
        ({"limit": 0}, "limit"),
        ({"offset": -1}, "offset"),
    ],
)
def test_tool_rejects_bad_arguments(graph: OscalGraph, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        crosswalk_control(graph, "ai-rmf", "measure_2.7", **kwargs)


def test_unknown_control_is_reported(graph: OscalGraph) -> None:
    with pytest.raises(ValueError, match="not found in catalog 'ai-rmf'"):
        crosswalk_control(graph, "ai-rmf", "does-not-exist")


# ----------------------------------------------------------------------------- loader enforces the authoring rules


def _two_catalogs() -> dict[str, dict[str, Any]]:
    return {"a": catalog_doc([ctl("x-1"), ctl("x-2")]), "b": catalog_doc([ctl("y-1"), ctl("y-2")])}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m, p: m.update(method="intersecting", confidence=0.95), "outside intersecting range"),
        (lambda m, p: m.update(method="advisory", confidence=0.1), "outside advisory range"),
        (lambda m, p: p.pop("href"), "require provenance.href"),
        (lambda m, p: m.update(confidence=True), "confidence"),
        (lambda m, p: m.update(confidence="0.95"), "confidence"),
    ],
)
def test_loader_refuses_a_crosswalk_the_validator_rejects(
    make_graph: MakeGraph, mutate: Callable[[dict[str, Any], dict[str, Any]], Any], message: str
) -> None:
    doc = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "direct", 0.95)])
    mapping = doc["mapping-collection"]["mappings"][0]
    mutate(mapping["maps"][0], mapping["provenance"])
    with pytest.raises(OscalValidationError, match=message):
        make_graph(catalogs=_two_catalogs(), crosswalks={"a__b": doc})


def test_loader_refuses_duplicate_uuids_and_old_oscal_versions(make_graph: MakeGraph) -> None:
    doc = crosswalk_doc(
        "a", "b", [("x-1", "related", "y-1", "direct", 0.95), ("x-2", "related", "y-2", "direct", 0.95)]
    )
    maps = doc["mapping-collection"]["mappings"][0]["maps"]
    maps[1]["uuid"] = maps[0]["uuid"]
    with pytest.raises(OscalValidationError, match="duplicate UUID"):
        make_graph(catalogs=_two_catalogs(), crosswalks={"a__b": doc})
    old = crosswalk_doc("a", "b", [("x-1", "related", "y-1", "direct", 0.95)])
    old["mapping-collection"]["metadata"]["oscal-version"] = "1.0.0"
    with pytest.raises(OscalValidationError, match="oscal-version"):
        make_graph(catalogs=_two_catalogs(), crosswalks={"a__b": old})


def test_a_mapping_repeated_in_one_file_is_an_error_everywhere(make_graph: MakeGraph, tmp_path: Any) -> None:
    from oscalis.core.integrity import verify_crosswalks

    doc = crosswalk_doc(
        "a", "b", [("x-1", "related", "y-1", "intersecting", 0.5), ("x-1", "related", "y-1", "intersecting", 0.7)]
    )
    assert any("more than once" in e for e in validate_document(doc)[1])
    with pytest.raises(OscalValidationError, match="more than once"):
        make_graph(catalogs=_two_catalogs(), crosswalks={"a__b": doc})
    assert any("more than once" in f.message for f in verify_crosswalks(tmp_path))  # the files make_graph wrote


def test_integrity_hook_also_checks_confidence_ranges(tmp_path: Any) -> None:
    from oscalis.core.integrity import verify_crosswalks
    from tests.conftest import write_json

    for key, cat in _two_catalogs().items():
        write_json(tmp_path / "catalogs" / f"{key}.json", cat)
    write_json(
        tmp_path / "crosswalks" / "a__b.json", crosswalk_doc("a", "b", [("x-1", "related", "y-1", "advisory", 0.9)])
    )
    assert any("outside advisory range" in f.message for f in verify_crosswalks(tmp_path))


def test_derived_inverse_matches_the_stored_edge_when_a_mapping_is_in_two_files(make_graph: MakeGraph) -> None:
    weak = crosswalk_doc("a", "b", [("x-1", "equivalent", "y-1", "intersecting", 0.6)])
    strong = crosswalk_doc("a", "b", [("x-1", "equivalent", "y-1", "direct", 0.95)])
    g = make_graph(catalogs=_two_catalogs(), crosswalks={"a__b_1": weak, "a__b_2": strong})
    forward = [e for e in g.edges_from("a:x-1") if e.dst == "b:y-1"]
    backward = [e for e in g.edges_from("b:y-1") if e.dst == "a:x-1"]
    assert [(e.confidence, e.method) for e in forward] == [(0.95, "direct")]
    assert [(e.confidence, e.method) for e in backward] == [(0.95, "direct")]


def test_floor_applies_before_choosing_the_shortest_path(make_graph: MakeGraph) -> None:
    """Traversal rule 3: a weak direct edge (0.25) does not hide a longer path that meets the floor (0.80)."""
    g = make_graph(
        catalogs={
            "a": catalog_doc([ctl("x-1")]),
            "b": catalog_doc([ctl("y-1")]),
            "c": catalog_doc([ctl("z-1")]),
        },
        crosswalks={
            "a__c": crosswalk_doc("a", "c", [("x-1", "related", "z-1", "advisory", 0.25)]),
            "a__b": crosswalk_doc("a", "b", [("x-1", "equivalent", "y-1", "direct", 1.0)]),
            "b__c": crosswalk_doc("b", "c", [("y-1", "equivalent", "z-1", "direct", 1.0)]),
        },
    )
    weak = {r.node: r for r in g.traverse("a:x-1", min_confidence=0.0)}["c:z-1"]
    assert (len(weak.hops), weak.relation, weak.confidence) == (1, "crosswalk:related", 0.25)
    strong = {r.node: r for r in g.traverse("a:x-1", min_confidence=0.30)}["c:z-1"]
    assert (len(strong.hops), strong.relation, strong.confidence) == (2, "crosswalk:equivalent", 0.80)
