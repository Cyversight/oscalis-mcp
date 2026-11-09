#!/usr/bin/env python3
"""Build GovRAMP profiles (data/profiles/govramp-*.json) from the GovRAMP resource workbooks.

Reads the Controls Matrix, Core, Ready, Federal Overlay and CJIS workbooks from a folder and writes OSCAL profiles
that import the NIST SP 800-53 catalog. Parameter text such as ``AC-2 (h) (1) [twenty-four (24) hours]`` is mapped to
the catalog's parameter ids; additional requirements and unmapped parameter text are kept as ``alters`` notes.

Profiles written:
  govramp-low, govramp-moderate, govramp-high             Authorized-status baselines
  govramp-core                                            Core Verification subset of Moderate (60 controls)
  govramp-ready-low / -moderate / -high                   Ready-status subsets of each baseline
  govramp-moderate-cjis                                   Moderate with the CJIS 6.0 overlay applied
  govramp-low-federal / -moderate-federal / -high-federal baseline plus the FedRAMP alignment overlay

Requires the real NIST catalog in data/catalogs/ (run src/scripts/ingest_catalogs.py first) and openpyxl.

Usage:
  python src/scripts/import_govramp.py --source <folder> [--dry-run] [--report reports/govramp-import-report.json]

The workbooks are NOT stored in this repository, and Oscalis does not need them to run. The generated profiles in
data/profiles/ are what the server reads. Run this script only to regenerate them after GovRAMP publishes new workbooks.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from oscalis.core.config import Settings  # noqa: E402
from oscalis.core.govramp import (  # noqa: E402
    REASONS,
    build_index,
    is_not_selected,
    normalize_control_id,
    repair_text,
)
from oscalis.core.govramp_profiles import (  # noqa: E402
    BuildStats,
    ControlRow,
    catalog_import,
    collect,
    count_note_parts,
    profile_document,
    profile_import_all,
    profile_import_ids,
)
from oscalis.core.graph import OscalGraph  # noqa: E402
from oscalis.core.integrity import LOCK_FILE  # noqa: E402
from oscalis.core.oscal import load_document, parse_catalog, validate_document  # noqa: E402

warnings.filterwarnings("ignore", module="openpyxl")

CATALOG_HREF = "800-53r5.2.json"
CATALOG_KEY = "800-53r5.2"
GENERATOR = "src/scripts/import_govramp.py"

# ----------------------------------------------------------------------------- workbook reading


@dataclass
class Sheet:
    path: Path
    name: str
    headers: list[str]
    rows: list[tuple[Any, ...]]

    def col(self, *prefixes: str, nth: int = 0, exact: bool = False) -> int | None:
        hits = [
            i
            for i, h in enumerate(self.headers)
            if h and any((h.lower() == p.lower()) if exact else h.lower().startswith(p.lower()) for p in prefixes)
        ]
        return hits[nth] if len(hits) > nth else None

    def need(self, *prefixes: str, exact: bool = False) -> int:
        """Like `col`, but a missing column stops the import instead of reading every row as blank."""
        idx = self.col(*prefixes, exact=exact)
        if idx is None:
            raise SystemExit(
                f"error: {self.path.name} / {self.name}: no column {' or '.join(map(repr, prefixes))}. "
                f"Headers found: {[h for h in self.headers if h]}"
            )
        return idx


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_sheet(path: Path, name: str) -> Sheet:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if name not in wb.sheetnames:
            raise SystemExit(f"error: {path.name}: no sheet {name!r}. Sheets: {wb.sheetnames}")
        ws = wb[name]
        all_rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    head_i = next((i for i, r in enumerate(all_rows[:6]) if "ID" in [str(c).strip() for c in r if c]), None)
    if head_i is None:
        raise SystemExit(f"error: {path.name} / {name}: no header row with an 'ID' cell in the first 6 rows")
    headers = [(str(c).strip().split("\n")[0].strip() if c else "") for c in all_rows[head_i]]
    return Sheet(path, name, headers, all_rows[head_i + 1 :])


def cell(row: tuple[Any, ...], idx: int | None) -> str | None:
    if idx is None or idx >= len(row) or row[idx] is None:
        return None
    text = str(row[idx]).strip()
    return text or None


def yes(value: str | None) -> bool:
    return (value or "").strip().lower() == "yes"


@dataclass
class ExtraRow:
    """A matrix row whose id is not a NIST control (CJIS-only requirements)."""

    raw_id: str
    name: str | None
    description: str | None
    requirement: str | None
    authorized: bool


@dataclass
class MatrixRead:
    rows: list[ControlRow] = field(default_factory=list)
    extras: list[ExtraRow] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)  # id fixed using the SORT ID column
    not_in_catalog: list[str] = field(default_factory=list)
    not_selected: list[str] = field(default_factory=list)  # authorized = no


def read_matrix(sheet: Sheet, catalog_ids: set[str]) -> MatrixRead:
    id_c, sort_c = sheet.need("ID", exact=True), sheet.col("SORT ID")
    ready_c, auth_c, core_c = (
        sheet.col("Required for Ready"),
        sheet.need("Required for Authorized"),
        sheet.col("Required for Core"),
    )
    param_c = sheet.col("GovRAMP-Defined", "Assigned GovRAMP", "StateRAMP-Defined")
    req_c = sheet.col("Additional GovRAMP", "Additional StateRAMP")
    desc_c = sheet.col("NIST Control Description")
    name_c = sheet.col("Control Name")
    out = MatrixRead()
    for r in sheet.rows:
        raw = cell(r, id_c)
        if not raw:
            continue
        nid, sid = normalize_control_id(raw), normalize_control_id(cell(r, sort_c) or "")
        if nid is None:
            out.extras.append(ExtraRow(raw, cell(r, name_c), cell(r, desc_c), cell(r, req_c), yes(cell(r, auth_c))))
            continue
        if sid and sid != nid:
            out.corrections.append(f"{raw} -> {sid} (SORT ID column)")
            nid = sid
        if nid not in catalog_ids:
            out.not_in_catalog.append(raw)
            continue
        authorized = yes(cell(r, auth_c))
        if not authorized:
            out.not_selected.append(nid)
        out.rows.append(
            ControlRow(nid, authorized, yes(cell(r, ready_c)), yes(cell(r, core_c)), cell(r, param_c), cell(r, req_c))
        )
    return out


def ids_in(sheet: Sheet, catalog_ids: set[str]) -> set[str]:
    id_c = sheet.col("ID", exact=True)
    found = {normalize_control_id(cell(r, id_c) or "") for r in sheet.rows}
    return {i for i in found if i and i in catalog_ids}


# ----------------------------------------------------------------------------- reporting


@dataclass
class Report:
    sources: list[dict[str, Any]] = field(default_factory=list)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    mapping: dict[str, dict[str, int]] = field(default_factory=dict)
    unmapped: list[dict[str, str]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def record_stats(rep: Report, profile: str, stats: BuildStats) -> None:
    c = stats.mapping
    rep.mapping[profile] = {k: int(v) for k, v in sorted(c.items())}
    for cid, u in stats.unmapped:
        rep.unmapped.append(
            {
                "profile": profile,
                "control": cid,
                "locator": ".".join(u.entry.locator) + (f"-{u.entry.ordinal}" if u.entry.ordinal else ""),
                "reason": u.reason,
                "reason_text": REASONS.get(u.reason, u.reason),
                "text": repair_text(u.entry.raw)[0][:240],
            }
        )


# ----------------------------------------------------------------------------- building


def find_one(root: Path, pattern: str, *, top_only: bool = False) -> list[Path]:
    """Files under `root` whose name matches `pattern`, ignoring case on every platform.

    `Path.glob` ignores case on Windows but not on Linux or macOS, and its `case_sensitive` flag needs Python 3.12.
    """
    wanted = pattern.lower()
    candidates = root.iterdir() if top_only else root.rglob("*")
    hits = sorted(p for p in candidates if p.is_file() and fnmatch.fnmatchcase(p.name.lower(), wanted))
    if not hits:
        raise SystemExit(f"error: no file matching {pattern!r} under {root}")
    return hits


def extra_notes(extras: list[ExtraRow]) -> list[dict[str, Any]]:
    """Attach CJIS-only rows to the NIST control they extend, as 'cjis-addition' notes."""
    alters: dict[str, list[dict[str, Any]]] = {}
    for e in extras:
        if not e.authorized:
            continue
        sub = re.match(r"^IA-5\s*\(([a-z])\)$", e.raw_id.strip(), re.I)
        if sub:
            target, locator = "ia-5", sub[1].lower()
            text = (e.description or "").replace("CJIS Addition:", "").strip()
        elif e.raw_id.strip().upper() == "IA-0":
            target, locator = "ac-3", "ia-0"
            tail = f"\n\nGovRAMP note: {e.requirement}" if e.requirement else ""
            text = f"{e.name or 'IA-0'}\n\n{e.description or ''}{tail}".strip()
        else:
            continue
        text, _ = repair_text(text)
        alters.setdefault(target, []).append(
            {
                "name": "cjis-addition",
                "ns": "urn:oscalis:ns:govramp",
                "prose": text,
                "props": [{"name": "locator", "value": locator}],
            }
        )
    return [
        {"control-id": cid, "adds": [{"position": "ending", "parts": parts}]} for cid, parts in sorted(alters.items())
    ]


def merge_alters(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        for a in group:
            merged.setdefault(a["control-id"], []).extend(a["adds"])
    return [{"control-id": cid, "adds": adds} for cid, adds in sorted(merged.items())]


def version_of(path: Path) -> str:
    m = re.search(r"[Vv](\d+(?:\.\d+)*)", path.name)
    return f"matrix-v{m[1]}" if m else "unversioned"


@dataclass
class Built:
    key: str
    doc: dict[str, Any]
    members: int
    set_parameters: int
    notes: int
    sources: list[str]


def build(source: Path, data_dir: Path, rep: Report) -> list[Built]:
    catalog = parse_catalog(load_document(data_dir / "catalogs" / f"{CATALOG_KEY}.json"))
    index = build_index(catalog)
    catalog_ids = set(index)

    low_p = find_one(source, "GovRAMP-Controls-Matrix_Low_Rev5_*.xlsx")[0]
    mod_all = find_one(source, "GovRAMP-Controls-Matrix_Mod_Rev5_V*.xlsx")
    mod_all = [p for p in mod_all if "CJIS" not in p.name]
    high_p = find_one(source, "GovRAMP-Controls-Matrix_High_Rev5_*.xlsx")[0]
    cjis_p = find_one(source, "GovRAMP-Controls-Matrix_Mod_Rev5_*CJIS.xlsx")[0]
    core_p = find_one(source, "GovRAMP Core Controls.xlsx", top_only=True)[0]
    ready_p = find_one(source, "GovRAMP Ready Minimum*.xlsx", top_only=True)[0]
    fed_p = find_one(source, "GovRAMP Federal Overlay.xlsx", top_only=True)[0]
    cjo_p = find_one(source, "GovRAMP CJIS-Aligned*.xlsx", top_only=True)[0]
    mod_p = mod_all[0]

    copies = {sha256(p) for p in mod_all}
    rep.checks["moderate_matrix_copies"] = {
        "files": len(mod_all),
        "identical": len(copies) == 1,
        "note": "the Moderate matrix appears at the folder root and in the service-provider package",
    }
    if len(copies) != 1:
        rep.warnings.append("Moderate matrix copies differ; using " + str(mod_p.relative_to(source)))

    for label, p in (
        ("low matrix", low_p),
        ("moderate matrix", mod_p),
        ("high matrix", high_p),
        ("moderate+CJIS matrix", cjis_p),
        ("core workbook", core_p),
        ("ready workbook", ready_p),
        ("federal overlay", fed_p),
        ("CJIS overlay", cjo_p),
    ):
        rep.sources.append({"role": label, "file": p.relative_to(source).as_posix(), "sha256": sha256(p)})

    matrices: dict[str, MatrixRead] = {
        "low": read_matrix(load_sheet(low_p, "12_Low Controls"), catalog_ids),
        "moderate": read_matrix(load_sheet(mod_p, "12_Mod Controls"), catalog_ids),
        "high": read_matrix(load_sheet(high_p, "12_High Controls"), catalog_ids),
        "moderate-cjis": read_matrix(load_sheet(cjis_p, "12_Mod Controls"), catalog_ids),
    }
    paths = {"low": low_p, "moderate": mod_p, "high": high_p, "moderate-cjis": cjis_p}
    newest = max(p.stat().st_mtime for p in (low_p, mod_p, high_p, cjis_p, core_p, ready_p, fed_p))
    modified = datetime.fromtimestamp(newest, UTC).strftime("%Y-%m-%dT00:00:00Z")

    for name, m in matrices.items():
        rep.checks[f"matrix:{name}"] = {
            "controls_in_matrix": len(m.rows),
            "authorized": sum(r.authorized for r in m.rows),
            "ready": sum(r.ready for r in m.rows),
            "id_corrections": m.corrections,
            "ids_not_in_nist_catalog": m.not_in_catalog,
            "non_nist_rows_kept_as_notes": [e.raw_id for e in m.extras if e.authorized],
            "not_selected": m.not_selected,
        }

    built: list[Built] = []

    def add(
        key: str,
        title: str,
        version: str,
        remarks: str,
        imports: list[dict[str, Any]],
        stats: BuildStats | None,
        set_params: list[dict[str, Any]] | None,
        alters: list[dict[str, Any]] | None,
        members: int,
        sources: list[Path],
    ) -> None:
        doc = profile_document(key, title, version, remarks, modified, imports, set_params, alters)
        if members == 0 and all(imp.get("href") == CATALOG_HREF for imp in imports):
            # A profile that selects straight from the catalog and selects nothing means the workbook was misread.
            raise SystemExit(f"error: {key} would select no controls; check the workbook layout")
        built.append(
            Built(
                key,
                doc,
                members,
                len(set_params or []),
                count_note_parts(alters),
                [p.relative_to(source).as_posix() for p in sources],
            )
        )
        if stats:
            record_stats(rep, key, stats)

    base_remarks = (
        "Generated by Oscalis from GovRAMP workbooks ({src}). Control membership, parameter values and additional "
        "requirements are GovRAMP's. Parameter text was mapped onto NIST SP 800-53 Rev 5 parameter ids by "
        "{gen}; entries that could not be placed safely are kept as 'parameter-text' notes. NIST control text "
        "comes from the NIST catalog, not from the workbooks."
    )

    level_titles = {"low": "Low", "moderate": "Moderate", "high": "High"}
    level_rows: dict[str, list[ControlRow]] = {}
    for lvl in ("low", "moderate", "high"):
        m = matrices[lvl]
        member_rows = [r for r in m.rows if r.authorized]
        level_rows[lvl] = member_rows
        ids = [r.control_id for r in member_rows]
        sp, al, st = collect(member_rows, index)
        add(
            f"govramp-{lvl}",
            f"GovRAMP Rev. 5 {level_titles[lvl]} baseline (Authorized)",
            version_of(paths[lvl]),
            base_remarks.format(src=paths[lvl].name, gen=GENERATOR),
            [catalog_import(CATALOG_HREF, ids)],
            st,
            sp,
            al,
            len(ids),
            [paths[lvl]],
        )

    mod_core = [r.control_id for r in level_rows["moderate"] if r.core]
    add(
        "govramp-core",
        "GovRAMP Core Verification (60 foundational Moderate controls)",
        version_of(mod_p),
        "Subset of govramp-moderate flagged 'Required for Core' in the Controls Matrix. Parameters and requirements "
        "are inherited from govramp-moderate.",
        [profile_import_ids("govramp-moderate.json", mod_core)],
        None,
        None,
        None,
        len(mod_core),
        [mod_p, core_p],
    )

    for lvl in ("low", "moderate", "high"):
        ready = [r.control_id for r in level_rows[lvl] if r.ready]
        add(
            f"govramp-ready-{lvl}",
            f"GovRAMP Ready status, {level_titles[lvl]} (minimum mandatory controls)",
            version_of(paths[lvl]),
            f"Subset of govramp-{lvl} flagged 'Required for Ready'. Parameters and requirements are inherited.",
            [profile_import_ids(f"govramp-{lvl}.json", ready)],
            None,
            None,
            None,
            len(ready),
            [paths[lvl]] + ([ready_p] if lvl == "moderate" else []),
        )

    cj = matrices["moderate-cjis"]
    cj_rows = [r for r in cj.rows if r.authorized]
    cj_ids = [r.control_id for r in cj_rows]
    sp, al, st = collect(cj_rows, index)
    al = merge_alters(al, extra_notes(cj.extras))
    add(
        "govramp-moderate-cjis",
        "GovRAMP Moderate with CJIS-aligned overlay (CJIS Security Policy 6.0)",
        version_of(cjis_p),
        base_remarks.format(src=cjis_p.name, gen=GENERATOR)
        + " CJIS-only requirements with no NIST control (IA-0, IA-5 (j)-(o)) are attached as 'cjis-addition' notes.",
        [catalog_import(CATALOG_HREF, cj_ids)],
        st,
        sp,
        al,
        len(cj_ids),
        [cjis_p, cjo_p],
    )

    fed_titles = {"low": "Low Overlay", "moderate": "Moderate Overlay", "high": "High Overlay"}
    tab_rows: dict[str, tuple[list[ControlRow], list[str]]] = {}
    for lvl in ("low", "moderate", "high"):
        sheet = load_sheet(fed_p, fed_titles[lvl])
        id_c, auth_c, ready_c = (
            sheet.need("ID", exact=True),
            sheet.need("Required for Authorized"),
            sheet.col("Required for Ready"),
        )
        gov_c, ov_c = sheet.need("GovRAMP-Defined"), sheet.col("Overlay", exact=True)
        freq_c = sheet.col("FedRAMP", nth=1)  # second FedRAMP column: requirements and guidance
        rows: list[ControlRow] = []
        extra_ids: list[str] = []
        for r in sheet.rows:
            nid = normalize_control_id(cell(r, id_c) or "")
            if not nid or nid not in catalog_ids:
                continue
            if is_not_selected(cell(r, gov_c)):
                if yes(cell(r, auth_c)):
                    extra_ids.append(nid)
                    rows.append(ControlRow(nid, True, yes(cell(r, ready_c)), False, None, cell(r, freq_c)))
                continue
            overlay, req = cell(r, ov_c), cell(r, freq_c)
            if overlay or req:
                rows.append(ControlRow(nid, True, yes(cell(r, ready_c)), False, overlay, req))
        tab_rows[lvl] = (rows, extra_ids)

    # The workbook states that each level includes the lower levels' deltas, so the tabs cascade:
    # Moderate = Low + Moderate tabs, High = Low + Moderate + High tabs. Later tabs override earlier ones.
    order = ("low", "moderate", "high")
    for n, lvl in enumerate(order):
        used = order[: n + 1]
        rows = [r for u in used for r in tab_rows[u][0]]
        extra_ids = sorted({i for u in used for i in tab_rows[u][1]})
        sp, al, st = collect(rows, index, prefix="federal-")
        imports = [profile_import_all(f"govramp-{lvl}.json")]
        if extra_ids:
            imports.append(catalog_import(CATALOG_HREF, extra_ids))
        add(
            f"govramp-{lvl}-federal",
            f"GovRAMP {level_titles[lvl]} with Federal (FedRAMP Rev. 5) alignment overlay",
            "federal-overlay-v1.0",
            f"Chained on govramp-{lvl}, with the overlay tabs {', '.join(used)} applied in that order (the workbook "
            "states that each level includes the lower levels' deltas). Adds controls FedRAMP requires that GovRAMP "
            "does not select, sets the overlay parameter where FedRAMP is the higher watermark, and attaches FedRAMP "
            "requirements and guidance. " + base_remarks.format(src=fed_p.name, gen=GENERATOR),
            imports,
            st,
            sp,
            al,
            len(extra_ids),
            [fed_p],
        )
        rep.checks[f"federal-overlay:{lvl}"] = {
            "tabs_applied": list(used),
            "rows_used": len(rows),
            "controls_added": extra_ids,
        }
    cross_checks(rep, matrices, core_p, ready_p, cjo_p, catalog_ids)
    return built


def cross_checks(
    rep: Report, matrices: dict[str, MatrixRead], core_p: Path, ready_p: Path, cjo_p: Path, catalog_ids: set[str]
) -> None:
    mod = matrices["moderate"]
    core_flag = {r.control_id for r in mod.rows if r.core}
    core_wb = ids_in(load_sheet(core_p, "GovRAMP Mod Core Controls"), catalog_ids)
    ready_flag = {r.control_id for r in mod.rows if r.ready}
    ready_wb = ids_in(load_sheet(ready_p, "Mod Impact_Ready_Mandates"), catalog_ids)
    cj_matrix = {r.control_id for r in matrices["moderate-cjis"].rows}
    cj_overlay = ids_in(load_sheet(cjo_p, "GovRAMP Mod & CJIS 6.0 Overlay"), catalog_ids)
    rep.checks["core_matrix_vs_core_workbook"] = {
        "matrix": len(core_flag),
        "workbook": len(core_wb),
        "differences": sorted(core_flag ^ core_wb),
    }
    rep.checks["ready_matrix_vs_ready_workbook"] = {
        "matrix": len(ready_flag),
        "workbook": len(ready_wb),
        "differences": sorted(ready_flag ^ ready_wb),
    }
    rep.checks["cjis_matrix_vs_cjis_overlay_workbook"] = {
        "matrix": len(cj_matrix),
        "workbook": len(cj_overlay),
        "only_in_matrix": sorted(cj_matrix - cj_overlay),
        "only_in_overlay_workbook": sorted(cj_overlay - cj_matrix),
    }


# ----------------------------------------------------------------------------- FedRAMP corroboration


def _norm(s: str) -> str:
    s = re.sub(r"\(\s*\d+\s*\)", "", s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"\b(the|a|an|of|and|or)\b", " ", s)).strip()


def corroborate(built: list[Built], data_dir: Path, rep: Report) -> None:
    """Compare mapped Moderate parameters with FedRAMP's published values on the same OSCAL parameter ids."""
    fed_path = data_dir / "profiles" / "fedramp-moderate.json"
    gov = next((b for b in built if b.key == "govramp-moderate"), None)
    if gov is None or not fed_path.is_file():
        rep.checks["fedramp_corroboration"] = {"skipped": "fedramp-moderate profile not found; run ingest_catalogs.py"}
        return
    fed_doc = json.loads(fed_path.read_text(encoding="utf-8"))["profile"]
    fed: dict[str, str] = {}
    for sp in fed_doc.get("modify", {}).get("set-parameters", []):
        fed[sp["param-id"]] = "; ".join([c["description"] for c in sp.get("constraints", [])] + sp.get("values", []))
    ours = {
        sp["param-id"]: "; ".join(sp["values"]) for sp in gov.doc["profile"].get("modify", {}).get("set-parameters", [])
    }
    both = sorted(set(ours) & set(fed))
    same = [p for p in both if _norm(ours[p]) == _norm(fed[p])]
    differ = [p for p in both if _norm(ours[p]) != _norm(fed[p])]
    by_ctrl: dict[str, dict[str, str]] = {}
    for p, t in fed.items():
        by_ctrl.setdefault(re.sub(r"_odp.*$", "", p), {})[p] = t
    elsewhere_unset: list[str] = []
    clashes: list[str] = []
    for p, t in ours.items():
        hits = [
            q
            for q, ft in by_ctrl.get(re.sub(r"_odp.*$", "", p), {}).items()
            if _norm(ft) == _norm(t) and len(_norm(t)) > 6
        ]
        if hits and p not in hits:
            (clashes if p in fed else elsewhere_unset).append(p)
    rep.checks["fedramp_corroboration"] = {
        "govramp_parameters": len(ours),
        "fedramp_parameters": len(fed),
        "set_by_both": len(both),
        "same_wording_on_same_id": len(same),
        "different_wording_on_same_id": len(differ),
        "wording_matches_another_parameter_and_fedramp_leaves_ours_unset": sorted(elsewhere_unset),
        "wording_matches_another_parameter_and_fedramp_has_different_value_here": sorted(clashes),
        "different_wording_ids": differ,
    }


# ----------------------------------------------------------------------------- output


def write_profiles(built: list[Built], data_dir: Path, source: Path, rep: Report, *, dry_run: bool) -> None:
    out_dir = data_dir / "profiles"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    for b in built:
        _kind, errors = validate_document(b.doc)
        if errors:
            raise SystemExit(f"error: generated profile {b.key} is not valid OSCAL: {errors[:3]}")
        path = out_dir / f"{b.key}.json"
        if not dry_run:
            path.write_text(json.dumps(b.doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        rep.profiles.append(
            {
                "key": b.key,
                "file": f"profiles/{b.key}.json",
                "controls_selected": b.members,
                "set_parameters": b.set_parameters,
                "notes": b.notes,
                "sources": b.sources,
            }
        )
    if dry_run:
        return
    lock_path = data_dir / LOCK_FILE
    lock: dict[str, Any] = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {"files": []}
    src_hash = {s["file"]: s["sha256"] for s in rep.sources}
    # Several importers share this list. Replace only this importer's entries (by path) and keep the others.
    mine = {f"profiles/{b.key}.json" for b in built}
    kept = [e for e in lock.get("generated", []) if e.get("path") not in mine]
    lock["generated"] = kept + [
        {
            "path": f"profiles/{b.key}.json",
            "key": b.key,
            "generator": GENERATOR,
            "generated": datetime.now(UTC).date().isoformat(),
            "sources": [{"file": f, "sha256": src_hash.get(f, "")} for f in b.sources],
        }
        for b in built
    ]
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")


def verify_load(data_dir: Path, built: list[Built], rep: Report) -> None:
    """Load everything through the real graph layer and confirm each profile resolves."""
    graph = OscalGraph.load(Settings(data_dir=data_dir))
    resolved = {p["key"]: p["control_count"] for p in graph.list_profiles()}
    rows: list[dict[str, Any]] = []
    for b in built:
        n = resolved.get(b.key)
        params = graph.profile_param_values(b.key)
        notes = graph.profile_notes(b.key)
        rows.append(
            {
                "key": b.key,
                "resolved_controls": n,
                "resolved_parameters": len(params),
                "controls_with_notes": len(notes),
                "notes": sum(len(v) for v in notes.values()),
            }
        )
    rep.checks["graph_resolution"] = rows
    fed_compare: dict[str, Any] = {}
    for lvl in ("low", "moderate", "high"):
        mine, theirs = f"govramp-{lvl}-federal", f"fedramp-{lvl}"
        if mine in resolved and theirs in resolved:
            ours_m, theirs_m = graph.profile_members(mine), graph.profile_members(theirs)
            fed_compare[lvl] = {
                "govramp_federal": len(ours_m),
                "fedramp": len(theirs_m),
                "common": len(ours_m & theirs_m),
                "only_govramp_federal": sorted(c for _, c in ours_m - theirs_m),
                "only_fedramp": sorted(c for _, c in theirs_m - ours_m),
            }
    rep.checks["federal_overlay_vs_published_fedramp_membership"] = fed_compare
    rep.checks["graph_warnings"] = graph.warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--source",
        type=Path,
        required=True,
        help="folder with the GovRAMP workbooks (not stored in this repository; download them from GovRAMP)",
    )
    ap.add_argument("--data-dir", type=Path, default=REPO / "data")
    ap.add_argument("--report", type=Path, default=REPO / "reports" / "govramp-import-report.json")
    ap.add_argument("--dry-run", action="store_true", help="build and validate, but write nothing")
    args = ap.parse_args(argv)
    source, data_dir = args.source.resolve(), args.data_dir.resolve()
    if not source.is_dir():
        print(
            f"error: source folder not found: {source}\n"
            "       The GovRAMP workbooks are not stored in this repository. Download them from GovRAMP and pass\n"
            "       the folder with --source. The current profiles are already in data/profiles/.",
            file=sys.stderr,
        )
        return 2
    if not (data_dir / "catalogs" / f"{CATALOG_KEY}.json").is_file():
        print(
            "error: data/catalogs/800-53r5.2.json is missing. Run src/scripts/ingest_catalogs.py first.",
            file=sys.stderr,
        )
        return 2

    rep = Report()
    built = build(source, data_dir, rep)
    corroborate(built, data_dir, rep)
    write_profiles(built, data_dir, source, rep, dry_run=args.dry_run)
    if not args.dry_run:
        verify_load(data_dir, built, rep)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(rep.__dict__, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )

    totals: Counter[str] = Counter()
    for stats in rep.mapping.values():
        totals.update(stats)
    print(f"{'DRY RUN: ' if args.dry_run else ''}wrote {len(built)} profiles to {data_dir / 'profiles'}")
    for p in rep.profiles:
        print(
            f"  {p['key']:26} selected={p['controls_selected']:4} "
            f"set-parameters={p['set_parameters']:4} notes={p['notes']:4}"
        )
    print(
        f"parameter entries: {totals['entries']}, parameter values placed: {totals['params']}, "
        f"unmapped entries: {len(rep.unmapped)}"
    )
    if not args.dry_run:
        print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
