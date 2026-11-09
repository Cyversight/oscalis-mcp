#!/usr/bin/env python3
"""Build data/crosswalks/csf-v2__800-53r5.2.json from NIST's published CSF 2.0 informative references.

Source: the CSF 2.0 Reference Tool download (an Excel workbook) on https://www.nist.gov/cyberframework/informative-references.
It lists, for each CSF 2.0 subcategory, the SP 800-53 Rev 5.2.0 controls NIST published as informative references.
NIST data is public domain.

What the source does and does not say:
  * It says a control is an informative reference for an outcome. It gives NO relationship type (no "equal to",
    "subset of"), so these are partial-overlap mappings: relation ``crosswalk:related``, method ``intersecting``.
    Nothing here is labelled ``direct``.
  * Confidence (0.50-0.70, the range the rules allow for ``intersecting``) is an Oscalis heuristic computed only from
    NIST's own data: an outcome that cites few controls, and a control cited by few outcomes, are tighter fits.

Usage:
  python src/scripts/import_csf_crosswalk.py                 # download, then write
  python src/scripts/import_csf_crosswalk.py --source FILE   # use a saved workbook (offline)
  python src/scripts/import_csf_crosswalk.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
import uuid
import warnings
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openpyxl

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from oscalis.core.govramp import normalize_control_id  # noqa: E402
from oscalis.core.integrity import LOCK_FILE  # noqa: E402
from oscalis.core.oscal import iter_controls, load_document, parse_catalog, validate_document  # noqa: E402

warnings.filterwarnings("ignore", module="openpyxl")

URL = "https://csrc.nist.gov/extensions/nudp/services/json/csf/download?olirids=all"
ALLOWED_HOST = "csrc.nist.gov"
MAX_BYTES = 32 * 1024 * 1024
SOURCE_CATALOG, TARGET_CATALOG = "csf-v2", "800-53r5.2"
OUT_NAME = f"{SOURCE_CATALOG}__{TARGET_CATALOG}.json"
NAMESPACE = uuid.UUID("5d0a8e4c-2b7f-5c19-8a63-71e9f0b4d2a6")
GENERATOR = "src/scripts/import_csf_crosswalk.py"

_SUB = re.compile(r"^([A-Z]{2}\.[A-Z]{2}-\d{2}):")
_REF = re.compile(r"SP 800-53 Rev 5\.2\.0:\s*([A-Za-z]{2}-\d+(?:\s*\(\s*\d+\s*\))?)")


def fetch(url: str) -> bytes:
    if not url.startswith("https://") or url.split("/")[2] != ALLOWED_HOST:
        raise ValueError(f"refusing to fetch {url!r}: only https://{ALLOWED_HOST}/ is allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "oscalis-mcp-import/0.1"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        data: bytes = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"response exceeds {MAX_BYTES} bytes")
    return data


def read_references(raw: bytes) -> tuple[dict[str, list[str]], str]:
    """Return ({'pr.ds-01': ['sc-28', ...]}, export date). Withdrawn subcategories carry no references."""
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        if "CSF 2.0" not in wb.sheetnames:
            raise ValueError(f"workbook has no 'CSF 2.0' sheet (sheets: {wb.sheetnames})")
        rows = [tuple(r) for r in wb["CSF 2.0"].iter_rows(values_only=True)]
        generated = ""
        if "Introduction" in wb.sheetnames:
            for r in wb["Introduction"].iter_rows(values_only=True):
                if r and str(r[0]).strip() == "Generated Date" and len(r) > 1 and r[1]:
                    generated = str(r[1])[:10]
    finally:
        wb.close()
    header = next((i for i, r in enumerate(rows) if r and len(r) > 4 and str(r[2]).strip() == "Subcategory"), None)
    if header is None or str(rows[header][4]).strip() != "Informative References":
        raise ValueError("unexpected workbook layout: no Subcategory / Informative References header row")
    out: dict[str, list[str]] = {}
    for r in rows[header + 1 :]:
        m = _SUB.match(str(r[2] or ""))
        if not m or not r[4]:
            continue
        controls: list[str] = []
        for ref in _REF.findall(str(r[4])):
            cid = normalize_control_id(ref)
            if cid and cid not in controls:
                controls.append(cid)
        if controls:
            out[m.group(1).lower()] = controls
    return out, generated


def confidence(breadth: int, citations: int) -> float:
    """Heuristic in [0.50, 0.70] from NIST's own counts.

    breadth: how many controls NIST cites for the subcategory. citations: how many subcategories cite the control.
    """
    base = (
        0.70 if breadth <= 3 else 0.65 if breadth <= 8 else 0.60 if breadth <= 15 else 0.55 if breadth <= 25 else 0.50
    )
    penalty = 0.05 if citations > 12 else 0.0
    return round(max(0.50, base - penalty), 2)


def build_document(refs: dict[str, list[str]], generated: str, sha: str, modified: str) -> dict[str, Any]:
    citations: dict[str, int] = defaultdict(int)
    for controls in refs.values():
        for c in controls:
            citations[c] += 1
    maps: list[dict[str, Any]] = []
    for sub in sorted(refs):
        by_conf: dict[float, list[str]] = defaultdict(list)
        for c in refs[sub]:
            by_conf[confidence(len(refs[sub]), citations[c])].append(c)
        for conf in sorted(by_conf, reverse=True):
            maps.append(
                {
                    "uuid": str(uuid.uuid5(NAMESPACE, f"{sub}:{conf}")),
                    "relationship": "crosswalk:related",
                    "method": "intersecting",
                    "confidence": conf,
                    "sources": [{"type": "control", "id-ref": sub}],
                    "targets": [{"type": "control", "id-ref": c} for c in sorted(by_conf[conf])],
                    "remarks": (
                        "NIST lists these SP 800-53 Rev 5.2.0 controls as informative references for this CSF 2.0 "
                        "outcome. The source gives no relationship type, so this is a partial-overlap mapping."
                    ),
                }
            )
    return {
        "mapping-collection": {
            "uuid": str(uuid.uuid5(NAMESPACE, "mapping-collection")),
            "metadata": {
                "title": "NIST CSF 2.0 informative references to SP 800-53 Rev 5.2.0",
                "last-modified": modified,
                "version": f"csf2.0-export-{generated or 'undated'}",
                "oscal-version": "1.1.3",
                "remarks": (
                    "Generated by Oscalis from the NIST CSF 2.0 Reference Tool download (public domain). "
                    f"Source SHA-256 {sha}. Confidence is an Oscalis heuristic, not a NIST value. "
                    f"Regenerate with {GENERATOR}."
                ),
            },
            "mappings": [
                {
                    "uuid": str(uuid.uuid5(NAMESPACE, "mapping")),
                    "source-resource": {"catalog": SOURCE_CATALOG},
                    "target-resource": {"catalog": TARGET_CATALOG},
                    "provenance": {
                        "document": "NIST CSF 2.0 Informative References (SP 800-53 Rev 5.2.0)",
                        "version": f"export {generated or 'undated'}",
                        "href": "https://www.nist.gov/cyberframework/informative-references",
                    },
                    "maps": maps,
                }
            ],
        }
    }


def check_against_catalogs(doc: dict[str, Any], data_dir: Path) -> list[str]:
    """Every id must exist in the loaded catalogs. Returns problems."""
    problems: list[str] = []
    for key in (SOURCE_CATALOG, TARGET_CATALOG):
        if not (data_dir / "catalogs" / f"{key}.json").is_file():
            return [f"catalog {key!r} not found in {data_dir / 'catalogs'}; run ingest_catalogs.py first"]
    ids = {
        key: {
            c.id.lower() for c, *_ in iter_controls(parse_catalog(load_document(data_dir / "catalogs" / f"{key}.json")))
        }
        for key in (SOURCE_CATALOG, TARGET_CATALOG)
    }
    for mp in doc["mapping-collection"]["mappings"][0]["maps"]:
        for s in mp["sources"]:
            if s["id-ref"] not in ids[SOURCE_CATALOG]:
                problems.append(f"{SOURCE_CATALOG}:{s['id-ref']}")
        for t in mp["targets"]:
            if t["id-ref"] not in ids[TARGET_CATALOG]:
                problems.append(f"{TARGET_CATALOG}:{t['id-ref']}")
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, help="saved CSF 2.0 Reference Tool workbook (default: download)")
    ap.add_argument("--data-dir", type=Path, default=REPO / "data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    data_dir = args.data_dir.resolve()

    raw = args.source.read_bytes() if args.source else fetch(URL)
    sha = hashlib.sha256(raw).hexdigest()
    refs, generated = read_references(raw)
    if not refs:
        print("error: no SP 800-53 Rev 5.2.0 references found in the workbook", file=sys.stderr)
        return 1
    doc = build_document(refs, generated, sha, datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z"))
    _kind, errors = validate_document(doc)
    if errors:
        print(f"error: generated crosswalk is not valid: {errors[:3]}", file=sys.stderr)
        return 1
    problems = check_against_catalogs(doc, data_dir)
    if problems:
        print(f"error: {len(problems)} id(s) not found in the catalogs: {problems[:10]}", file=sys.stderr)
        return 1

    edges = sum(len(m["targets"]) for m in doc["mapping-collection"]["mappings"][0]["maps"])
    print(f"{'DRY RUN: ' if args.dry_run else ''}{len(refs)} subcategories, {edges} edges, source sha256={sha[:12]}")
    if args.dry_run:
        return 0
    out = data_dir / "crosswalks" / OUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    lock_path = data_dir / LOCK_FILE
    lock: dict[str, Any] = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {"files": []}
    generated_entries = [e for e in lock.get("generated", []) if e.get("path") != f"crosswalks/{OUT_NAME}"]
    generated_entries.append(
        {
            "path": f"crosswalks/{OUT_NAME}",
            "key": OUT_NAME.removesuffix(".json"),
            "generator": GENERATOR,
            "generated": datetime.now(UTC).date().isoformat(),
            "sources": [{"file": URL, "sha256": sha}],
        }
    )
    lock["generated"] = generated_entries
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
