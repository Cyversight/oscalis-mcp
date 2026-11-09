"""GovRAMP parsing, parameter mapping, profile notes, and the shipped real data."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from oscalis.core.govramp import (
    build_index,
    clean_value,
    is_not_selected,
    map_cell,
    normalize_control_id,
    parse_param_cell,
    parse_requirements,
    repair_text,
)
from oscalis.core.govramp_profiles import ControlRow, collect, profile_document
from oscalis.core.graph import OscalGraph
from oscalis.core.integrity import verify_crosswalks
from oscalis.core.oscal import OscalValidationError, parse_catalog, validate_document
from tests.conftest import REPO, catalog_doc, ctl, profile_doc

MakeGraph = Callable[..., OscalGraph]


# ----------------------------------------------------------------------------- catalog builders


def label(value: str) -> list[dict[str, str]]:
    return [{"name": "label", "value": value}]


def item(letter: str, prose: str, *subs: dict[str, Any]) -> dict[str, Any]:
    part: dict[str, Any] = {"name": "item", "props": label(letter + "."), "prose": prose}
    if subs:
        part["parts"] = list(subs)
    return part


def odp(pid: str, lab: str | None = "time period", **extra: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"id": pid}
    if lab:
        p["label"] = lab
    p.update(extra)
    return p


def tag(pid: str) -> str:
    return "{{ insert: param, " + pid + " }}"


def statement(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": "statement", "parts": list(items)}]


def build(controls: list[dict[str, Any]]) -> Any:
    return build_index(parse_catalog(catalog_doc(controls)))


@pytest.fixture
def index() -> Any:
    """Controls shaped like the real 800-53 structures GovRAMP locators have to resolve against."""
    ac2 = ctl(
        "ac-2",
        params=[odp("ac-02_odp.06"), odp("ac-02_odp.07"), odp("ac-02_odp.08"), odp("ac-02_odp.10", "frequency")],
        parts=statement(
            item(
                "h",
                "Notify within:",
                item("1", tag("ac-02_odp.06")),
                item("2", tag("ac-02_odp.07")),
                item("3", tag("ac-02_odp.08")),
            ),
            item("j", "Review " + tag("ac-02_odp.10")),
        ),
    )
    # composite parameter that aggregates two ODPs (one GovRAMP value fills both)
    at2 = ctl(
        "at-2",
        params=[
            {
                "id": "at-2_prm_1",
                "label": "organization-defined frequency",
                "props": [
                    {"name": "aggregates", "value": "at-02_odp.01"},
                    {"name": "aggregates", "value": "at-02_odp.02"},
                ],
            },
            odp("at-02_odp.01", "frequency"),
            odp("at-02_odp.02", "frequency"),
            odp("at-02_odp.06", "frequency"),
            odp("at-02_odp.07", "events"),
        ],
        parts=statement(
            item("a", "Train", item("1", "initially and " + tag("at-2_prm_1") + " thereafter")),
            item("c", "Update " + tag("at-02_odp.06") + " and following " + tag("at-02_odp.07")),
        ),
    )
    # selection ODP that wraps a nested time-period ODP
    ac11 = ctl(
        "ac-11",
        params=[
            odp(
                "ac-11_odp.01",
                None,
                select={
                    "how-many": "one-or-more",
                    "choice": [
                        "initiating a device lock after " + tag("ac-11_odp.02") + " of inactivity",
                        "requiring the user to initiate a device lock",
                    ],
                },
            ),
            odp("ac-11_odp.02"),
        ],
        parts=statement(item("a", "Prevent access by " + tag("ac-11_odp.01"))),
    )
    sc28 = ctl(
        "sc-28",
        params=[
            odp("sc-28_odp.01", None, select={"how-many": "one-or-more", "choice": ["confidentiality", "integrity"]}),
            odp("sc-28_odp.02", "information at rest"),
        ],
        parts=statement(item("a", "Protect the " + tag("sc-28_odp.01") + " of " + tag("sc-28_odp.02"))),
    )
    sole = ctl("au-11", params=[odp("au-11_odp")], parts=statement(item("a", "Retain " + tag("au-11_odp"))))
    ac1 = ctl(
        "ac-1",
        params=[
            odp("ac-01_odp.05", "frequency"),
            odp("ac-01_odp.06", "events"),
            odp("ac-01_odp.07", "frequency"),
            odp("ac-01_odp.08", "events"),
        ],
        parts=statement(
            item(
                "c",
                "Review",
                item("1", "Policy " + tag("ac-01_odp.05") + " and following " + tag("ac-01_odp.06")),
                item("2", "Procedures " + tag("ac-01_odp.07") + " and following " + tag("ac-01_odp.08")),
            )
        ),
    )
    ac23 = ctl(
        "ac-2.3",
        params=[odp("ac-02.03_odp.01"), odp("ac-02.03_odp.02")],
        parts=statement(
            item("a", "Disable after " + tag("ac-02.03_odp.01")), item("d", "Inactive for " + tag("ac-02.03_odp.02"))
        ),
    )
    return build([ac2, at2, ac11, sc28, sole, ac1, ac23])


def placed(index: Any, cell: str, cid: str) -> dict[str, tuple[str, ...]]:
    result = map_cell(index, cell, cid)
    assert not result.unmapped, result.unmapped
    return {m.param_id: m.values for m in result.mapped}


# ----------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC-02", "ac-2"),
        ("AC-2 (1)", "ac-2.1"),
        ("ac-02 (01)", "ac-2.1"),
        ("SC-7 (24)", "sc-7.24"),
        ("IA-5 (j)", None),
        ("IA-0", None),
        ("", None),
        ("not an id", None),
    ],
)
def test_normalize_control_id(raw: str, expected: str | None) -> None:
    assert normalize_control_id(raw) == expected


def test_parse_param_cell_locators_ordinals_and_multiple_values() -> None:
    cell = (
        "AC-2 (h) (1) [twenty-four (24) hours]\nAC-1 (c) (2)-1 [at least annually] [significant changes]\nCM-2 (3) [x]"
    )
    a, b, c = parse_param_cell(cell)
    assert (a.control_id, a.locator, a.ordinal, a.values) == ("ac-2", ("h", "1"), None, ("twenty-four (24) hours",))
    assert (b.control_id, b.locator, b.ordinal, b.values) == (
        "ac-1",
        ("c", "2"),
        1,
        ("at least annually", "significant changes"),
    )
    assert (c.control_id, c.locator) == ("cm-2.3", ())  # (3) is the enhancement, not a locator


def test_parse_param_cell_pipe_separator_and_blank() -> None:
    assert len(parse_param_cell("AC-2 (h) [a] | AC-2 (j) [b]")) == 2
    assert parse_param_cell(None) == [] and parse_param_cell("  ") == []
    assert parse_param_cell("AC-2 (h) no brackets here") == []


def test_clean_value_strips_selection_and_assignment_prefix() -> None:
    assert clean_value("Selection: disables") == "disables"
    assert clean_value("Selection (one or more): a; b") == "a; b"
    assert clean_value("Assignment: no more than 96 hours") == "no more than 96 hours"
    assert clean_value("ordinary value") == "ordinary value"


def test_not_selected_marker() -> None:
    assert is_not_selected("Control Not Selected by GovRAMP")
    assert not is_not_selected("AC-2 [x]") and not is_not_selected(None)


# ----------------------------------------------------------------------------- mapping


def test_locator_maps_to_the_parameter_inside_that_item(index: Any) -> None:
    got = placed(
        index, "AC-2 (h) (1) [twenty-four (24) hours]\nAC-2 (h) (2) [eight (8) hours]\nAC-2 (j) [quarterly]", "ac-2"
    )
    assert got == {
        "ac-02_odp.06": ("twenty-four (24) hours",),
        "ac-02_odp.07": ("eight (8) hours",),
        "ac-02_odp.10": ("quarterly",),
    }


def test_composite_parameter_fans_one_value_out_to_every_aggregated_odp(index: Any) -> None:
    got = placed(index, "AT-2 (a) (1) [at least annually]", "at-2")
    assert got == {"at-02_odp.01": ("at least annually",), "at-02_odp.02": ("at least annually",)}
    assert map_cell(index, "AT-2 (a) (1) [at least annually]", "at-2").counts()["fan-out"] == 2


def test_fewer_values_than_blanks_skips_the_events_parameter(index: Any) -> None:
    got = placed(
        index, "AC-1 (c) (1) [at least every 3 years]\nAC-1 (c) (2) [at least annually] [significant changes]", "ac-1"
    )
    assert got["ac-01_odp.05"] == ("at least every 3 years",)  # frequency
    assert "ac-01_odp.06" not in got  # events left alone
    assert got["ac-01_odp.07"] == ("at least annually",)
    assert got["ac-01_odp.08"] == ("significant changes",)


def test_ordinal_picks_the_nth_blank(index: Any) -> None:
    got = placed(index, "AC-1 (c) (1)-1 [every 3 years]\nAC-1 (c) (1)-2 [on policy change]", "ac-1")
    assert got == {"ac-01_odp.05": ("every 3 years",), "ac-01_odp.06": ("on policy change",)}


def test_ordinal_beyond_available_blanks_is_reported_not_guessed(index: Any) -> None:
    result = map_cell(index, "AC-1 (c) (1)-3 [x]", "ac-1")
    assert not result.mapped and result.unmapped[0].reason == "ordinal-out-of-range"


def test_selection_wrapping_a_nested_parameter_sends_the_value_to_the_inner_one(index: Any) -> None:
    assert placed(index, "AC-11 (a) [fifteen (15) minutes]", "ac-11") == {"ac-11_odp.02": ("fifteen (15) minutes",)}


def test_selection_value_that_is_a_choice_stays_on_the_selection(index: Any) -> None:
    got = placed(index, "AC-11 (a) [requiring the user to initiate a device lock]", "ac-11")
    assert got == {"ac-11_odp.01": ("requiring the user to initiate a device lock",)}


def test_unlocated_value_is_matched_to_a_selection_by_its_choices(index: Any) -> None:
    assert placed(index, "SC-28 [confidentiality AND integrity]", "sc-28") == {
        "sc-28_odp.01": ("confidentiality AND integrity",)
    }


def test_unlocated_value_without_evidence_is_not_placed(index: Any) -> None:
    result = map_cell(index, "SC-28 [something else entirely]", "sc-28")
    assert not result.mapped and result.unmapped[0].reason == "no-locator-multi"


def test_sole_parameter_takes_an_unlocated_value(index: Any) -> None:
    result = map_cell(index, "AU-11 [a time period in compliance with government requirements]", "au-11")
    assert [(m.param_id, m.method) for m in result.mapped] == [("au-11_odp", "sole-parameter")]


def test_unlocated_entry_takes_the_blank_no_located_entry_claimed(index: Any) -> None:
    got = placed(index, "AC-2 (3) [24 hours for user accounts]\nAC-2 (3) (d) [ninety (90) days]", "ac-2.3")
    assert got == {"ac-02.03_odp.02": ("ninety (90) days",), "ac-02.03_odp.01": ("24 hours for user accounts",)}


def test_unknown_locator_is_reported(index: Any) -> None:
    result = map_cell(index, "AC-2 (z) [x]", "ac-2")
    assert result.unmapped[0].reason == "locator-not-found"


def test_pointer_values_carry_no_parameter(index: Any) -> None:
    result = map_cell(index, "AC-2 (h) (1) [see additional GovRAMP Requirements and Guidance]", "ac-2")
    assert not result.mapped and result.unmapped[0].reason == "refers-to-requirements"


def test_selection_prefix_must_agree_with_the_blank_kind(index: Any) -> None:
    result = map_cell(index, "AU-11 [Selection: disables]", "au-11")
    assert not result.mapped and result.unmapped[0].reason == "kind-mismatch"


def test_two_entries_cannot_claim_the_same_blank(index: Any) -> None:
    result = map_cell(index, "AC-2 (h) (1) [one hour]\nAC-2 (h) (1) [two hours]", "ac-2")
    assert len(result.mapped) == 1 and result.unmapped[0].reason == "conflict"


def test_cell_with_no_readable_values_is_kept_as_unparsed(index: Any) -> None:
    result = map_cell(index, "Control required, but no organization-defined restrictions provided.", "ac-2")
    assert result.unmapped[0].reason == "unparsed"


def test_control_missing_from_catalog_is_reported(index: Any) -> None:
    result = map_cell(index, "ZZ-9 [x]", "zz-9")
    assert result.unmapped[0].reason == "locator-not-found"


# ----------------------------------------------------------------------------- requirement text


def test_requirements_split_by_label_and_locator() -> None:
    cell = (
        "AC-2 (3) Requirement: The provider defines the period.\nSecond line.\n"
        "AC-2 (3) (d) Requirement: Device identifiers.\n"
        "AC-2 (3) Guidance: See NIST."
    )
    notes = {(n.kind, n.locator): n.text for n in parse_requirements(cell)}
    assert notes[("requirement", "")] == "The provider defines the period.\nSecond line."
    assert notes[("requirement", "d")] == "Device identifiers."
    assert notes[("guidance", "")] == "See NIST."


def test_unlabelled_requirement_text_is_kept() -> None:
    assert [(n.kind, n.text) for n in parse_requirements("Plain text with no label.")] == [
        ("requirement", "Plain text with no label.")
    ]
    assert parse_requirements(None) == [] and parse_requirements("   ") == []


def test_repair_text_fixes_the_lost_apostrophe() -> None:
    fixed, n = repair_text("technology doesn\ufffdt allow\u00a0it")
    assert fixed == "technology doesn't allow it" and n == 1


# ----------------------------------------------------------------------------- profile documents


def test_collect_builds_valid_oscal_profile_content(index: Any) -> None:
    rows = [
        ControlRow("ac-2", True, True, True, "AC-2 (h) (1) [one hour]", "AC-2 Requirement: Do the thing."),
        ControlRow("au-11", True, False, False, "AU-11 [x]", None),
    ]
    sp, al, stats = collect(rows, index)
    assert sp == [{"param-id": "ac-02_odp.06", "values": ["one hour"]}, {"param-id": "au-11_odp", "values": ["x"]}]
    assert al[0]["control-id"] == "ac-2" and al[0]["adds"][0]["parts"][0]["name"] == "requirement"
    assert stats.controls == 2 and stats.notes == 1
    doc = profile_document(
        "govramp-test",
        "t",
        "1",
        "r",
        "2026-01-01T00:00:00Z",
        [{"href": "cat.json", "include-controls": [{"with-ids": ["ac-2", "au-11"]}]}],
        sp,
        al,
    )
    assert validate_document(doc)[1] == []
    assert (
        doc["profile"]["uuid"]
        == profile_document("govramp-test", "t", "1", "r", "x", [], None, None)["profile"]["uuid"]
    )


def test_unmapped_text_survives_as_a_parameter_text_note(index: Any) -> None:
    sp, al, _stats = collect([ControlRow("ac-2", True, False, False, "AC-2 (z) [kept as text]", None)], index)
    assert sp == []
    part = al[0]["adds"][0]["parts"][0]
    assert part["name"] == "parameter-text" and "kept as text" in part["prose"]


def test_note_prefix_keeps_overlay_notes_separate(index: Any) -> None:
    _sp, al, _ = collect(
        [ControlRow("ac-2", True, False, False, None, "AC-2 Requirement: x")], index, prefix="federal-"
    )
    assert al[0]["adds"][0]["parts"][0]["name"] == "federal-requirement"


# ----------------------------------------------------------------------------- notes in the graph


def _notes_profile(extra_alters: list[dict[str, Any]]) -> dict[str, Any]:
    doc = profile_doc([{"href": "cat.json", "include-all": {}}])
    doc["profile"]["modify"] = {"alters": extra_alters}
    return doc


def _alter(cid: str, name: str, text: str, locator: str = "") -> dict[str, Any]:
    part: dict[str, Any] = {"name": name, "prose": text}
    if locator:
        part["props"] = [{"name": "locator", "value": locator}]
    return {"control-id": cid, "adds": [{"position": "ending", "parts": [part]}]}


def test_alters_become_per_control_notes_and_chain(make_graph: MakeGraph) -> None:
    cat = catalog_doc([ctl("a-1"), ctl("a-2")])
    base = _notes_profile([_alter("a-1", "requirement", "base text"), _alter("a-2", "guidance", "g")])
    child = profile_doc([{"href": "base.json", "include-controls": [{"with-ids": ["a-1"]}]}])
    child["profile"]["modify"] = {"alters": [_alter("a-1", "requirement", "child text", "d")]}
    g = make_graph(catalogs={"cat": cat}, profiles={"base": base, "child": child})
    status = g.profile_status("cat", "a-1", ["base", "child"], include_notes=True)
    assert [n["text"] for n in status["base"]["notes"]] == ["base text"]
    assert {n["locator"]: n["text"] for n in status["child"]["notes"]} == {
        "": "base text",
        "d": "child text",
    }  # inherited + own
    assert g.profile_status("cat", "a-2", ["child"])["child"]["note_count"] == 0  # a-2 was not selected


def test_note_text_is_withheld_without_include_notes(make_graph: MakeGraph) -> None:
    cat = catalog_doc([ctl("a-1")])
    g = make_graph(catalogs={"cat": cat}, profiles={"p": _notes_profile([_alter("a-1", "requirement", "secret text")])})
    plain = g.profile_status("cat", "a-1", ["p"])["p"]
    assert plain["note_count"] == 1 and "notes" not in plain


def test_alter_for_a_control_outside_the_profile_is_ignored(make_graph: MakeGraph) -> None:
    cat = catalog_doc([ctl("a-1"), ctl("a-2")])
    prof = profile_doc([{"href": "cat.json", "include-controls": [{"with-ids": ["a-1"]}]}])
    prof["profile"]["modify"] = {"alters": [_alter("a-2", "requirement", "x")]}
    g = make_graph(catalogs={"cat": cat}, profiles={"p": prof})
    assert g.profile_notes("p") == {}


# ----------------------------------------------------------------------------- shipped data


GOVRAMP_PROFILES = {
    "govramp-low": 153,
    "govramp-moderate": 319,
    "govramp-high": 406,
    "govramp-core": 60,
    "govramp-ready-low": 29,
    "govramp-ready-moderate": 80,
    "govramp-ready-high": 90,
    "govramp-moderate-cjis": 332,
    "govramp-low-federal": 153,
    "govramp-moderate-federal": 321,
    "govramp-high-federal": 408,
}


def test_shipped_data_loads_without_warnings(shipped: OscalGraph) -> None:
    assert shipped.warnings == []
    assert {c["key"] for c in shipped.list_catalogs()} >= {"800-53r5.2", "csf-v2", "ai-rmf"}


@pytest.mark.parametrize(("key", "count"), sorted(GOVRAMP_PROFILES.items()))
def test_shipped_govramp_profile_resolves_to_the_expected_size(shipped: OscalGraph, key: str, count: int) -> None:
    resolved = {p["key"]: p["control_count"] for p in shipped.list_profiles()}
    assert resolved[key] == count


def test_shipped_baselines_nest_as_the_programs_define_them(shipped: OscalGraph) -> None:
    low, mod, high = (shipped.profile_members(f"govramp-{x}") for x in ("low", "moderate", "high"))
    core, ready = shipped.profile_members("govramp-core"), shipped.profile_members("govramp-ready-moderate")
    assert core <= ready <= mod
    assert shipped.profile_members("govramp-ready-low") <= low
    assert shipped.profile_members("govramp-ready-high") <= high
    assert len(low & mod) >= 150 and mod <= high | mod


def test_shipped_govramp_parameters_match_known_values(shipped: OscalGraph) -> None:
    status = shipped.profile_status("800-53r5.2", "ac-2", ["govramp-moderate"], include_notes=True)["govramp-moderate"]
    assert status["in_baseline"] is True
    assert status["parameters"]["ac-02_odp.06"]["values"] == ["twenty-four (24) hours"]
    assert status["parameters"]["ac-02_odp.07"]["values"] == ["eight (8) hours"]
    sc28 = shipped.profile_status("800-53r5.2", "sc-28", ["govramp-high"])["govramp-high"]
    assert sc28["parameters"]["sc-28_odp.01"]["values"] == ["confidentiality AND integrity"]


def test_shipped_federal_overlay_adds_the_piv_controls_and_overrides_parameters(shipped: OscalGraph) -> None:
    mod, fed = shipped.profile_members("govramp-moderate"), shipped.profile_members("govramp-moderate-federal")
    assert {c for _, c in fed - mod} == {"ia-2.12", "ia-8.1"} and mod <= fed
    gov = shipped.profile_status("800-53r5.2", "au-11", ["govramp-moderate", "govramp-moderate-federal"])
    assert (
        gov["govramp-moderate"]["parameters"]["au-11_odp"]["values"]
        != gov["govramp-moderate-federal"]["parameters"]["au-11_odp"]["values"]
    )
    assert "M-21-31" in gov["govramp-moderate-federal"]["parameters"]["au-11_odp"]["values"][0]


def test_shipped_cjis_profile_keeps_cjis_only_requirements_as_notes(shipped: OscalGraph) -> None:
    ia5 = shipped.profile_status(
        "800-53r5.2", "ia-5", ["govramp-moderate-cjis", "govramp-moderate"], include_notes=True
    )
    names = {(n["name"], n["locator"]) for n in ia5["govramp-moderate-cjis"]["notes"]}
    assert {("cjis-addition", c) for c in "jklmno"} <= names
    assert not any(n["name"] == "cjis-addition" for n in ia5["govramp-moderate"]["notes"])
    assert (
        shipped.profile_status("800-53r5.2", "ac-3", ["govramp-moderate-cjis"], include_notes=True)[
            "govramp-moderate-cjis"
        ]["note_count"]
        >= 1
    )


def test_shipped_data_has_no_dangling_crosswalk_edges() -> None:
    assert [f for f in verify_crosswalks(REPO / "data") if f.severity == "error"] == []


def test_importer_refuses_to_run_without_the_nist_catalog(tmp_path: Any) -> None:
    import runpy
    import sys

    sys.path.insert(0, str(REPO / "src"))
    main = runpy.run_path(str(REPO / "src" / "scripts" / "import_govramp.py"))["main"]
    assert main(["--source", str(tmp_path), "--data-dir", str(tmp_path)]) == 2
    with pytest.raises(OscalValidationError):
        raise OscalValidationError(["sentinel"])


def test_importer_finds_workbooks_regardless_of_name_case(tmp_path: Any) -> None:
    """Path.glob ignores case only on Windows; the importer must behave the same on Linux and macOS."""
    import runpy
    import sys

    sys.path.insert(0, str(REPO / "src"))
    find_one = runpy.run_path(str(REPO / "src" / "scripts" / "import_govramp.py"))["find_one"]
    (tmp_path / "sub").mkdir()
    nested = tmp_path / "sub" / "govramp-controls-matrix_LOW_rev5_v1.xlsx"
    top = tmp_path / "GOVRAMP CORE CONTROLS.XLSX"
    nested.touch()
    top.touch()
    assert find_one(tmp_path, "GovRAMP-Controls-Matrix_Low_Rev5_*.xlsx") == [nested]
    assert find_one(tmp_path, "GovRAMP Core Controls.xlsx", top_only=True) == [top]
    with pytest.raises(SystemExit):
        find_one(tmp_path, "GovRAMP-Controls-Matrix_Low_Rev5_*.xlsx", top_only=True)  # nested file is not top-level


# ----------------------------------------------------------------------------- regressions found by mutation checks


def test_selection_with_several_nested_parameters_keeps_the_value_on_the_selection() -> None:
    """SI-5 item c: the choices insert three different parameters, so one value cannot be split among them."""
    si5 = ctl(
        "si-5",
        params=[
            odp(
                "si-05_odp.02",
                None,
                select={
                    "how-many": "one-or-more",
                    "choice": [
                        tag("si-05_odp.03"),
                        tag("si-05_odp.04"),
                        tag("si-05_odp.05"),
                        "other personnel or roles",
                    ],
                },
            ),
            odp("si-05_odp.03", "personnel or roles"),
            odp("si-05_odp.04", "elements"),
            odp("si-05_odp.05", "external organizations"),
        ],
        parts=statement(item("c", "Disseminate to: " + tag("si-05_odp.02"))),
    )
    got = placed(build([si5]), "SI-5 (c) [to include system security personnel and administrators]", "si-5")
    assert got == {"si-05_odp.02": ("to include system security personnel and administrators",)}


def test_selection_with_one_nested_parameter_sends_a_non_choice_value_inward() -> None:
    pl4 = ctl(
        "pl-4",
        params=[
            odp(
                "pl-04_odp.02",
                None,
                select={"how-many": "one-or-more", "choice": [tag("pl-04_odp.03"), "when the rules are revised"]},
            ),
            odp("pl-04_odp.03", "frequency"),
        ],
        parts=statement(item("d", "Re-acknowledge " + tag("pl-04_odp.02"))),
    )
    assert placed(build([pl4]), "PL-4 (d) [at least annually]", "pl-4") == {"pl-04_odp.03": ("at least annually",)}


def cjis_control() -> Any:
    """AT-2 item c holds a frequency blank then an events blank, as in the NIST catalog."""
    at2 = ctl(
        "at-2",
        params=[odp("at-02_odp.06", "frequency"), odp("at-02_odp.07", "events")],
        parts=statement(item("c", "Update " + tag("at-02_odp.06") + " and following " + tag("at-02_odp.07"))),
    )
    return build([at2])


def test_cjis_sub_items_map_in_order_when_everything_lines_up() -> None:
    cell = (
        "AT-2 (c) (1) [at least annually]\nAT-2 (c) (2) [changes in the operating environment, or security incidents]"
    )
    result = map_cell(cjis_control(), cell, "at-2")
    assert not result.unmapped
    assert {m.param_id: (m.values, m.method) for m in result.mapped} == {
        "at-02_odp.06": (("at least annually",), "cjis-subitem"),
        "at-02_odp.07": (("changes in the operating environment, or security incidents",), "cjis-subitem"),
    }


def test_cjis_sub_items_are_refused_when_the_numbering_has_a_gap() -> None:
    cell = "AT-2 (c) (1) [at least annually]\nAT-2 (c) (3) [changes in the operating environment]"
    result = map_cell(cjis_control(), cell, "at-2")
    assert not result.mapped and {u.reason for u in result.unmapped} == {"locator-not-found"}


def test_cjis_sub_items_are_refused_when_there_are_fewer_entries_than_blanks() -> None:
    result = map_cell(cjis_control(), "AT-2 (c) (1) [at least annually]", "at-2")
    assert not result.mapped and result.unmapped[0].reason == "locator-not-found"


def test_cjis_sub_items_are_refused_when_a_value_does_not_suit_its_blank() -> None:
    swapped = "AT-2 (c) (1) [changes in the operating environment]\nAT-2 (c) (2) [at least annually]"
    result = map_cell(cjis_control(), swapped, "at-2")
    assert not result.mapped and {u.reason for u in result.unmapped} == {"locator-not-found"}


def test_cjis_sub_items_are_refused_when_an_entry_carries_several_values() -> None:
    cell = "AT-2 (c) (1) [at least annually] [and on request]\nAT-2 (c) (2) [changes in the operating environment]"
    assert not map_cell(cjis_control(), cell, "at-2").mapped


def test_a_profile_does_not_inherit_notes_for_controls_it_did_not_select(make_graph: MakeGraph) -> None:
    cat = catalog_doc([ctl("a-1"), ctl("a-2")])
    base = _notes_profile([_alter("a-1", "requirement", "one"), _alter("a-2", "requirement", "two")])
    child = profile_doc([{"href": "base.json", "include-controls": [{"with-ids": ["a-1"]}]}])
    g = make_graph(catalogs={"cat": cat}, profiles={"base": base, "child": child})
    assert set(g.profile_notes("child")) == {("cat", "a-1")}
    assert g.profile_status("cat", "a-2", ["child"])["child"] == {
        "in_baseline": False,
        "parameters": {},
        "note_count": 0,
    }
