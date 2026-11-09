"""baseline_delta across programs whose files are shaped differently (FedRAMP OSCAL 1.0.4 vs GovRAMP).

FedRAMP sets some values on legacy parameter ids (`at-2_prm_1`) and files each control's text as one `item` note.
GovRAMP uses the newer ids and splits the text into `requirement` and `guidance`. A comparison must not report the
same value or sentence as both removed and added.
"""

from __future__ import annotations

from typing import Any

from oscalis.core.graph import OscalGraph
from oscalis.tools.baseline_delta import _canon, _expand_aggregates, baseline_delta

CAT = "800-53r5.2"


def delta(graph: OscalGraph, a: str, b: str, **kw: Any) -> dict[str, Any]:
    return baseline_delta(graph, a, b, projection="prose_only", limit=100, **kw)


def all_items(graph: OscalGraph, a: str, b: str, section: str) -> list[dict[str, Any]]:
    """Every item of a section, following next_offset (pages are shortened to fit the size budget)."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = baseline_delta(graph, a, b, projection="prose_only", limit=100, offset=offset)
        items += page[section]["items"]
        if not page[section].get("next_offset"):
            return items
        offset = page[section]["next_offset"]


# ----------------------------------------------------------------------------- parameters


def test_the_catalog_records_which_legacy_parameters_aggregate_which(shipped: OscalGraph) -> None:
    aggregates = shipped.param_aggregates()
    assert aggregates[(CAT, "at-2_prm_1")] == ("at-02_odp.01", "at-02_odp.02")
    assert (CAT, "at-02_odp.01") not in aggregates


def test_expand_aggregates_moves_values_onto_covered_ids_and_direct_values_win() -> None:
    aggregates: dict[tuple[str, str], tuple[str, ...]] = {(CAT, "x_prm_1"): ("x_odp.01", "x_odp.02")}
    values = {
        (CAT, "x_prm_1"): {"control_id": "x", "values": [], "constraints": ["annually"]},
        (CAT, "x_odp.02"): {"control_id": "x", "values": ["monthly"], "constraints": []},
    }
    out = _expand_aggregates(values, aggregates)
    assert set(out) == {(CAT, "x_odp.01"), (CAT, "x_odp.02")}
    assert out[(CAT, "x_odp.01")]["constraints"] == ["annually"] and out[(CAT, "x_odp.01")]["via"] == "x_prm_1"
    assert out[(CAT, "x_odp.02")]["values"] == ["monthly"] and "via" not in out[(CAT, "x_odp.02")]


def test_same_value_on_a_legacy_id_is_not_a_change(shipped: OscalGraph) -> None:
    """FedRAMP sets AT-2 on at-2_prm_1, GovRAMP on at-02_odp.01/.02, both "at least annually"."""
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    assert not [c for c in changes if c["control_id"] == "at-2"]
    assert not [c for c in changes if c["param_id"].endswith("_prm_1")]  # legacy ids no longer appear


def test_a_real_difference_on_a_legacy_id_says_where_the_value_came_from(shipped: OscalGraph) -> None:
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    via = [c for c in changes if (c["a"] or {}).get("via")]
    assert via and all(c["param_id"] in shipped.param_aggregates()[(CAT, c["a"]["via"])] for c in via)


def test_formatting_differences_in_values_are_ignored() -> None:
    assert _canon("Selection: disables") == _canon("disables")
    assert _canon(r"\*See Additional Requirements") == _canon("*See Additional Requirements")
    assert _canon("at least annually.") == _canon("At least  annually")
    assert _canon("Requirement: Do X") == _canon("Do X")
    assert _canon("at least monthly") != _canon("at least weekly")


# ----------------------------------------------------------------------------- notes


def test_same_sentence_under_a_different_label_is_not_a_change(shipped: OscalGraph) -> None:
    """AC-7: FedRAMP `item` and GovRAMP `requirement` both say "In alignment with NIST SP 800-63B"."""
    notes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "requirement_changes")
    assert not [n for n in notes if n["control_id"] == "ac-7"]


def test_an_unpaired_note_shows_only_the_lines_that_are_really_missing(shipped: OscalGraph) -> None:
    """CA-7: both require monthly scans; only FedRAMP's multi-agency ConMon sentence is missing from GovRAMP."""
    notes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "requirement_changes")
    removed = "\n".join(n["a"] for n in notes if n["control_id"] == "ca-7" and n["change"] == "removed")
    assert "CSOs with more than one agency ATO" in removed
    assert "Operating System, Database, Web Application" not in removed


def test_a_real_wording_difference_is_still_reported(shipped: OscalGraph) -> None:
    notes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "requirement_changes")
    ca5 = {(n["change"], n["a"] or n["b"]) for n in notes if n["control_id"] == "ca-5"}
    assert ("removed", "Reference FedRAMP-POAM-Template") in ca5 and ("added", "Reference GovRAMP-POAM-Template") in ca5
    assert not any("monthly" in text for _change, text in ca5)  # the shared POA&M frequency is not a difference


def test_cross_program_counts_fell(shipped: OscalGraph) -> None:
    summary = delta(shipped, "fedramp-moderate", "govramp-moderate")["summary"]
    assert summary["parameter_changes"] <= 41 and summary["requirement_changes"] <= 107  # was 101 and 188
    assert summary["removed_in_b"] == 4 and summary["added_in_b"] == 0


# ----------------------------------------------------------------------------- comparisons within one program


def test_comparisons_within_govramp_are_unchanged_apart_from_formatting(shipped: OscalGraph) -> None:
    up = delta(shipped, "govramp-moderate", "govramp-high")["summary"]
    assert (up["added_in_b"], up["removed_in_b"], up["parameter_changes"]) == (87, 0, 2)
    assert up["requirement_changes"] == 7  # was 8: AC-8 differed only by a space and a full stop
    fed = delta(shipped, "govramp-moderate", "govramp-moderate-federal")["summary"]
    assert (fed["added_in_b"], fed["parameter_changes"], fed["requirement_changes"]) == (2, 5, 9)
    nist = delta(shipped, "800-53b-low", "govramp-low")["summary"]
    assert (nist["parameter_changes"], nist["requirement_changes"]) == (167, 62)  # was 168: one pointer-only value


# ----------------------------------------------------------------------------- selection vs nested assignment


def test_the_catalog_records_parameters_nested_in_a_selection(shipped: OscalGraph) -> None:
    assert shipped.param_nested()[(CAT, "pe-14_odp.01")] == ("pe-14_odp.02",)


def test_same_value_on_a_selection_or_its_nested_assignment_is_not_a_change(shipped: OscalGraph) -> None:
    """PE-14, PL-4, RA-3, RA-5(2): FedRAMP writes the value on the selection, GovRAMP on the assignment inside it."""
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    assert not {c["control_id"] for c in changes} & {"pe-14", "pl-4", "ra-3", "ra-5.2"}


def test_same_group_keeps_a_real_difference() -> None:
    from oscalis.tools.baseline_delta import _same_group

    nested: dict[tuple[str, str], tuple[str, ...]] = {(CAT, "s"): ("n",)}

    def v(text: str) -> dict[str, Any]:
        return {"control_id": "x", "values": [text], "constraints": []}

    moved = _same_group({(CAT, "s"): v("ASHRAE")}, {(CAT, "n"): v("ASHRAE")}, nested)
    assert moved == {(CAT, "s"), (CAT, "n")}
    assert _same_group({(CAT, "s"): v("ASHRAE")}, {(CAT, "n"): v("other")}, nested) == set()
    assert _same_group({(CAT, "s"): v("x")}, {(CAT, "s"): v("x")}, nested) == set()  # already equal: nothing to do


def test_sa_4_2_is_still_a_real_difference(shipped: OscalGraph) -> None:
    """FedRAMP sets a minimum list on the selection; GovRAMP leaves it organization-defined. Not the same value."""
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    assert any(c["control_id"] == "sa-4.2" for c in changes)


# ----------------------------------------------------------------------------- pointer text and formatting


def test_a_value_that_only_points_at_the_notes_counts_as_unset(shipped: OscalGraph) -> None:
    from oscalis.tools.baseline_delta import _effective

    def v(text: str) -> dict[str, Any]:
        return {"values": [], "constraints": [text]}

    assert _effective(v("see additional FedRAMP Requirements and Guidance")) is None
    assert _effective(v(r"\*See Additional Requirements")) is None
    assert _effective(v("ninety (90) days (See additional requirements and guidance.)")) == "ninety (90) days"
    assert _effective(v("see SC-13")) == "see sc-13"  # a real cross-reference is a value
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    assert not {c["control_id"] for c in changes} & {"ac-8", "ir-8", "mp-4", "ac-2.3"}


def test_typographic_dashes_quotes_and_numbered_labels_are_ignored() -> None:
    assert _canon("criteria \u2013 as required") == _canon("criteria - as required")
    assert _canon("the system\u2019s owner") == _canon("the system's owner")
    assert _canon("CM-6 (a) Requirement 1:  The service provider shall use the DoD STIGs") == _canon(
        "The service provider shall use the DoD STIGs"
    )
    assert _canon("Guidance 2: Do X") == _canon("Do X")
    assert _canon("AC-2 enforces X") == _canon("AC-2 enforces X")  # a control id without a label stays


def test_sc_13_cryptography_difference_is_reported(shipped: OscalGraph) -> None:
    """The cleanup must not hide real differences: GovRAMP accepts FIPS-compliant or modern cryptography."""
    changes = all_items(shipped, "fedramp-moderate", "govramp-moderate", "parameter_changes")
    sc13 = [c for c in changes if c["control_id"] == "sc-13"]
    assert sc13 and "FIPS-validated" in sc13[0]["a"]["constraints"][0]
    assert "modern cryptography" in sc13[0]["b"]["values"][0]
