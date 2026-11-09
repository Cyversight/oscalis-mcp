"""The CSF crosswalk importer, and how the importers share data/sources.lock.json."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import openpyxl
import pytest

from tests.conftest import REPO


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", REPO / "src" / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def csf() -> ModuleType:
    return load("import_csf_crosswalk")


def workbook(rows: list[tuple[Any, ...]], *, generated: str = "2026-01-02") -> bytes:
    """A minimal CSF 2.0 Reference Tool export."""
    wb = openpyxl.Workbook()
    intro = wb.active
    assert intro is not None
    intro.title = "Introduction"
    intro.append(["Title", "The NIST Cybersecurity Framework (CSF) 2.0"])
    intro.append(["Generated Date", f"{generated} 10:00:00"])
    sheet = wb.create_sheet("CSF 2.0")
    sheet.append([None, "title row", None, None, None])
    sheet.append(["Function", "Category", "Subcategory", "Implementation Examples", "Informative References"])
    for r in rows:
        sheet.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def refs(*controls: str) -> str:
    return "\n".join(f"SP 800-53 Rev 5.2.0: {c}" for c in controls)


def sub(code: str, text: str = "An outcome.") -> tuple[Any, ...]:
    return (None, None, f"{code}: {text}", "Ex1: ...", None)


# ----------------------------------------------------------------------------- reading the workbook


def test_reads_only_rev_5_2_0_references_and_normalizes_ids(csf: ModuleType) -> None:
    other = "SP 800-53 Rev 5.1.1: AC-02\nCCMv4.0: BCR-08\nISO/IEC 27001:2022: Annex A Controls: 5.1\n"
    row = (None, None, "PR.DS-01: Data-at-rest is protected", "Ex1", other + refs("SC-28", "AC-02 (01)", "SC-28"))
    got, generated = csf.read_references(workbook([row]))
    assert got == {"pr.ds-01": ["sc-28", "ac-2.1"]}  # Rev 5.1.1 ignored, duplicates dropped, ids normalized
    assert generated == "2026-01-02"


def test_subcategories_without_references_and_non_subcategory_rows_are_skipped(csf: ModuleType) -> None:
    rows = [
        ("GOVERN (GV): ...", None, None, None, "CRI Profile v2.0: GV"),
        (None, "Organizational Context (GV.OC): ...", None, None, refs("PM-11")),
        sub("GV.OC-01") + ()[:0],
        (None, None, "RC.IM-01: [Withdrawn: Incorporated into ID.IM-03]", "", None),
        (None, None, "GV.OC-02: Stakeholders", "Ex1", refs("PM-9")),
    ]
    got, _ = csf.read_references(workbook(rows))
    assert got == {"gv.oc-02": ["pm-9"]}


def test_unexpected_layouts_fail_loudly(csf: ModuleType) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = "Other"  # type: ignore[union-attr]
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="no 'CSF 2.0' sheet"):
        csf.read_references(buf.getvalue())

    wb = openpyxl.Workbook()
    wb.active.title = "CSF 2.0"  # type: ignore[union-attr]
    wb.active.append(["a", "b", "c", "d", "e"])  # type: ignore[union-attr]
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="unexpected workbook layout"):
        csf.read_references(buf.getvalue())

    wb = openpyxl.Workbook()
    wb.active.title = "CSF 2.0"  # type: ignore[union-attr]
    wb.active.append(["a", "b"])  # type: ignore[union-attr]  # fewer columns than the layout needs
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ValueError, match="unexpected workbook layout"):
        csf.read_references(buf.getvalue())


# ----------------------------------------------------------------------------- building the crosswalk


def test_every_edge_is_a_partial_overlap_never_direct(csf: ModuleType) -> None:
    data = {"pr.ds-01": ["sc-28", "sc-12"], "gv.po-01": ["ac-1"]}
    doc = csf.build_document(data, "2026-01-02", "ab" * 32, "2026-01-02T00:00:00Z")
    maps = doc["mapping-collection"]["mappings"][0]["maps"]
    assert {m["relationship"] for m in maps} == {"crosswalk:related"}
    assert {m["method"] for m in maps} == {"intersecting"}
    assert all(0.50 <= m["confidence"] <= 0.70 for m in maps)
    assert sum(len(m["targets"]) for m in maps) == 3


def test_generated_document_passes_the_projects_own_validation(csf: ModuleType) -> None:
    from oscalis.core.oscal import validate_document

    doc = csf.build_document({"pr.ds-01": ["sc-28"]}, "2026-01-02", "cd" * 32, "2026-01-02T00:00:00Z")
    assert validate_document(doc)[1] == []
    meta = doc["mapping-collection"]["metadata"]
    assert "cd" * 32 in meta["remarks"] and "heuristic" in meta["remarks"]
    prov = doc["mapping-collection"]["mappings"][0]["provenance"]
    assert prov["href"].startswith("https://www.nist.gov/") and "export 2026-01-02" in prov["version"]


def test_output_is_deterministic(csf: ModuleType) -> None:
    data = {"pr.ds-01": ["sc-28", "sc-12"], "gv.po-01": ["ac-1"]}
    a = csf.build_document(data, "d", "00", "t")
    b = csf.build_document(dict(reversed(list(data.items()))), "d", "00", "t")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.parametrize(
    ("breadth", "citations", "expected"),
    [(1, 1, 0.70), (3, 1, 0.70), (4, 1, 0.65), (8, 1, 0.65), (9, 1, 0.60), (15, 1, 0.60), (16, 1, 0.55), (25, 1, 0.55),
     (26, 1, 0.50), (40, 1, 0.50), (1, 13, 0.65), (9, 13, 0.55), (26, 13, 0.50), (3, 12, 0.70)],
)  # fmt: skip
def test_confidence_is_narrow_when_few_controls_or_few_citations(
    csf: ModuleType, breadth: int, citations: int, expected: float
) -> None:
    assert csf.confidence(breadth, citations) == expected


def test_confidence_never_leaves_the_range_the_rules_allow_for_intersecting(csf: ModuleType) -> None:
    values = {csf.confidence(b, c) for b in range(1, 80) for c in range(1, 40)}
    assert min(values) >= 0.50 and max(values) <= 0.70


def test_a_control_cited_widely_scores_lower_than_a_specific_one(csf: ModuleType) -> None:
    data = {f"gv.oc-{i:02d}": ["ir-4"] for i in range(1, 14)}  # ir-4 cited by 13 outcomes
    data["pr.ds-01"] = ["ir-4", "sc-28"]  # sc-28 cited once
    doc = csf.build_document(data, "d", "00", "t")
    by_target = {}
    for m in doc["mapping-collection"]["mappings"][0]["maps"]:
        if m["sources"][0]["id-ref"] == "pr.ds-01":
            for t in m["targets"]:
                by_target[t["id-ref"]] = m["confidence"]
    assert by_target["sc-28"] > by_target["ir-4"]


# ----------------------------------------------------------------------------- safety


@pytest.mark.parametrize(
    "url", ["http://csrc.nist.gov/x", "https://evil.example/x", "https://csrc.nist.gov.evil.example/x", "file:///x"]
)
def test_fetch_only_talks_to_nist(csf: ModuleType, url: str) -> None:
    with pytest.raises(ValueError, match="refusing to fetch"):
        csf.fetch(url)


def test_main_rejects_ids_that_are_not_in_the_catalogs(
    csf: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    shutil.copytree(REPO / "tests" / "fixtures" / "data", tmp_path / "d")
    book = tmp_path / "book.xlsx"
    book.write_bytes(workbook([(None, None, "GV.PO-01: Policy", "Ex1", refs("AC-01", "ZZ-99"))]))
    assert csf.main(["--source", str(book), "--data-dir", str(tmp_path / "d")]) == 1
    assert "zz-99" in capsys.readouterr().err
    assert not (tmp_path / "d" / "crosswalks" / "csf-v2__800-53r5.2.json").exists()


def test_main_requires_the_catalogs_to_exist(
    csf: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = tmp_path / "book.xlsx"
    book.write_bytes(workbook([(None, None, "GV.PO-01: Policy", "Ex1", refs("AC-01"))]))
    assert csf.main(["--source", str(book), "--data-dir", str(tmp_path / "empty")]) == 1
    assert "not found" in capsys.readouterr().err


# ----------------------------------------------------------------------------- shared lock file


def test_main_writes_the_crosswalk_and_records_provenance(csf: ModuleType, tmp_path: Path) -> None:
    import shutil

    data = tmp_path / "d"
    shutil.copytree(REPO / "tests" / "fixtures" / "data", data)
    (data / "crosswalks" / "csf-v2__800-53r5.2.json").unlink(missing_ok=True)
    book = tmp_path / "book.xlsx"
    book.write_bytes(workbook([(None, None, "GV.PO-01: Policy", "Ex1", refs("AC-01", "AC-02"))]))
    assert csf.main(["--source", str(book), "--data-dir", str(data)]) == 0
    out = json.loads((data / "crosswalks" / "csf-v2__800-53r5.2.json").read_text(encoding="utf-8"))
    assert sum(len(m["targets"]) for m in out["mapping-collection"]["mappings"][0]["maps"]) == 2
    lock = json.loads((data / "sources.lock.json").read_text(encoding="utf-8"))
    entry = next(e for e in lock["generated"] if e["path"] == "crosswalks/csf-v2__800-53r5.2.json")
    assert entry["generator"].endswith("import_csf_crosswalk.py") and len(entry["sources"][0]["sha256"]) == 64
    # LF on every platform, so Windows and Linux runs write identical bytes.
    for written in ("crosswalks/csf-v2__800-53r5.2.json", "sources.lock.json"):
        assert b"\r" not in (data / written).read_bytes(), written


def test_running_the_csf_importer_twice_does_not_duplicate_its_lock_entry(csf: ModuleType, tmp_path: Path) -> None:
    import shutil

    data = tmp_path / "d"
    shutil.copytree(REPO / "tests" / "fixtures" / "data", data)
    book = tmp_path / "book.xlsx"
    book.write_bytes(workbook([(None, None, "GV.PO-01: Policy", "Ex1", refs("AC-01"))]))
    for _ in range(2):
        assert csf.main(["--source", str(book), "--data-dir", str(data)]) == 0
    lock = json.loads((data / "sources.lock.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in lock["generated"]].count("crosswalks/csf-v2__800-53r5.2.json") == 1


def test_the_shipped_lock_file_records_every_generated_artifact() -> None:
    """Both importers write to one list. Each must keep the other's entries (a regression that really happened)."""
    lock = json.loads((REPO / "data" / "sources.lock.json").read_text(encoding="utf-8"))
    paths = {e["path"] for e in lock["generated"]}
    assert "crosswalks/csf-v2__800-53r5.2.json" in paths
    assert {f"profiles/govramp-{x}.json" for x in ("low", "moderate", "high", "core", "moderate-cjis")} <= paths
    assert len(paths) == len([e["path"] for e in lock["generated"]])  # no duplicates
    for artifact in paths:
        assert (REPO / "data" / artifact).is_file(), f"lock lists {artifact} but the file is missing"


# ----------------------------------------------------------------------------- GovRAMP workbook reading


@pytest.fixture(scope="module")
def govramp() -> ModuleType:
    return load("import_govramp")


def matrix_file(tmp_path: Path, headers: list[str], rows: list[list[Any]], sheet: str = "12_Mod Controls") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet
    ws.append(["GovRAMP Controls Matrix"])  # title row above the header, as in the real workbooks
    ws.append(headers)
    for r in rows:
        ws.append(r)
    path = tmp_path / "matrix.xlsx"
    wb.save(path)
    return path


HEADERS = ["SORT ID", "ID", "Control Name", "Required for Ready", "Required for Authorized", "Required for Core"]


def test_matrix_rows_are_read_by_header(govramp: ModuleType, tmp_path: Path) -> None:
    path = matrix_file(tmp_path, HEADERS, [["ac-02", "AC-2", "Account Management", "Yes", "Yes", "No"]])
    read = govramp.read_matrix(govramp.load_sheet(path, "12_Mod Controls"), {"ac-2"})
    assert [(r.control_id, r.authorized, r.ready, r.core) for r in read.rows] == [("ac-2", True, True, False)]


def test_a_renamed_authorized_column_stops_the_import(govramp: ModuleType, tmp_path: Path) -> None:
    """Before: every row read as 'not authorized' and empty profiles were written with exit code 0."""
    headers = [h if h != "Required for Authorized" else "Authorized?" for h in HEADERS]
    path = matrix_file(tmp_path, headers, [["ac-02", "AC-2", "Account Management", "Yes", "Yes", "No"]])
    with pytest.raises(SystemExit, match="no column 'Required for Authorized'"):
        govramp.read_matrix(govramp.load_sheet(path, "12_Mod Controls"), {"ac-2"})


def test_a_missing_sheet_or_header_row_names_the_file(govramp: ModuleType, tmp_path: Path) -> None:
    path = matrix_file(tmp_path, HEADERS, [])
    with pytest.raises(SystemExit, match=r"matrix.xlsx: no sheet '12_Low Controls'"):
        govramp.load_sheet(path, "12_Low Controls")
    headerless = matrix_file(tmp_path, ["SORT", "Name"], [])
    with pytest.raises(SystemExit, match="no header row with an 'ID' cell"):
        govramp.load_sheet(headerless, "12_Mod Controls")


def test_dry_run_creates_nothing(govramp: ModuleType, tmp_path: Path) -> None:
    rep = govramp.Report()
    govramp.write_profiles([], tmp_path, tmp_path, rep, dry_run=True)
    assert list(tmp_path.iterdir()) == []  # not even an empty profiles/ folder
