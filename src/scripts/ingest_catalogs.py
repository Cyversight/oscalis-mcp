#!/usr/bin/env python3
"""Download published OSCAL catalogs and profiles into data/ and record their provenance.

Sources (all openly published):
  catalogs: NIST SP 800-53 Rev 5, NIST CSF 2.0, NIST AI RMF 1.0  (usnistgov/oscal-content)
  profiles: NIST SP 800-53B Low/Moderate/High/Privacy            (usnistgov/oscal-content)
            FedRAMP Rev 5 Low/Moderate/High                      (oscal-compass-lab/fedramp-automation mirror)
  schemas : NIST OSCAL JSON schemas (optional, --schemas)

Each file is validated with the project's OSCAL validators before it replaces anything on disk.
`data/sources.lock.json` records URL, SHA-256, retrieval date, and OSCAL/document versions.

Not available: GovRAMP baselines (no public OSCAL source found); see README, Known Gaps.

Usage:
  python src/scripts/ingest_catalogs.py                 # catalogs + profiles
  python src/scripts/ingest_catalogs.py --schemas       # also fetch official OSCAL JSON schemas
  python src/scripts/ingest_catalogs.py --only csf-v2 ai-rmf
  python src/scripts/ingest_catalogs.py --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from oscalis.core.integrity import LOCK_FILE, UPSTREAM_FLOOR  # noqa: E402
from oscalis.core.oscal import OscalValidationError, validate_document  # noqa: E402

NIST = "https://raw.githubusercontent.com/usnistgov/oscal-content/main/nist.gov"
FEDRAMP = "https://raw.githubusercontent.com/oscal-compass-lab/fedramp-automation/main/dist/content/rev5/baselines/json"
OSCAL_SCHEMA = "https://github.com/usnistgov/OSCAL/releases/download/v1.1.3"

ALLOWED_HOSTS = ("raw.githubusercontent.com", "github.com")
MAX_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 60

Kind = Literal["catalog", "profile", "schema"]


@dataclass(frozen=True)
class Source:
    key: str
    kind: Kind
    url: str

    @property
    def rel_path(self) -> str:
        folder = {"catalog": "catalogs", "profile": "profiles", "schema": "schemas"}[self.kind]
        return f"{folder}/{self.key}.json"


SOURCES: tuple[Source, ...] = (
    Source("800-53r5.2", "catalog", f"{NIST}/SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json"),
    Source("csf-v2", "catalog", f"{NIST}/CSF/v2.0/json/NIST_CSF_v2.0_catalog.json"),
    Source("ai-rmf", "catalog", f"{NIST}/AI100-1/ver1/json/NIST_AI-RMF_v1_catalog.json"),
    Source("800-53b-low", "profile", f"{NIST}/SP800-53/rev5/json/NIST_SP-800-53_rev5_LOW-baseline_profile.json"),
    Source(
        "800-53b-moderate", "profile", f"{NIST}/SP800-53/rev5/json/NIST_SP-800-53_rev5_MODERATE-baseline_profile.json"
    ),
    Source("800-53b-high", "profile", f"{NIST}/SP800-53/rev5/json/NIST_SP-800-53_rev5_HIGH-baseline_profile.json"),
    Source(
        "800-53b-privacy", "profile", f"{NIST}/SP800-53/rev5/json/NIST_SP-800-53_rev5_PRIVACY-baseline_profile.json"
    ),
    Source("fedramp-low", "profile", f"{FEDRAMP}/FedRAMP_rev5_LOW-baseline_profile.json"),
    Source("fedramp-moderate", "profile", f"{FEDRAMP}/FedRAMP_rev5_MODERATE-baseline_profile.json"),
    Source("fedramp-high", "profile", f"{FEDRAMP}/FedRAMP_rev5_HIGH-baseline_profile.json"),
)
SCHEMAS: tuple[Source, ...] = (
    Source("oscal_catalog_schema", "schema", f"{OSCAL_SCHEMA}/oscal_catalog_schema.json"),
    Source("oscal_profile_schema", "schema", f"{OSCAL_SCHEMA}/oscal_profile_schema.json"),
)


def fetch(url: str) -> bytes:
    if not url.startswith("https://") or url.split("/")[2] not in ALLOWED_HOSTS:
        raise ValueError(f"refusing to fetch {url!r}: only https from {ALLOWED_HOSTS}")
    req = urllib.request.Request(url, headers={"User-Agent": "oscalis-mcp-ingest/0.1"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
        data: bytes = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{url}: response exceeds {MAX_BYTES} bytes")
    return data


def validate_download(source: Source, raw: bytes) -> tuple[dict[str, Any], list[str]]:
    """Return (metadata summary, errors). Schemas only need to be valid JSON."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"not valid JSON: {exc}"]
    if source.kind == "schema":
        return {}, ([] if isinstance(doc, dict) and "definitions" in doc else ["not a JSON schema"])
    _kind, errors = validate_document(doc, min_oscal_version=UPSTREAM_FLOOR)
    meta = doc.get(source.kind, {}).get("metadata", {}) if isinstance(doc, dict) else {}
    summary = {k: meta.get(k) for k in ("title", "version", "oscal-version")}
    return summary, errors


def load_lock(path: Path) -> dict[str, Any]:
    if path.is_file():
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    return {"files": []}


def ingest(sources: list[Source], data_dir: Path) -> int:
    lock_path = data_dir / LOCK_FILE
    lock = load_lock(lock_path)
    entries: dict[str, dict[str, Any]] = {e["path"]: e for e in lock.get("files", [])}
    failures = 0
    for src in sources:
        target = data_dir / src.rel_path
        try:
            raw = fetch(src.url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            print(f"FAIL  {src.key}: download: {exc}", file=sys.stderr)
            failures += 1
            continue
        summary, errors = validate_download(src, raw)
        if errors:
            print(f"FAIL  {src.key}: {len(errors)} validation error(s); existing file kept", file=sys.stderr)
            for e in errors[:5]:
                print(f"        {e}", file=sys.stderr)
            failures += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
        digest = hashlib.sha256(raw).hexdigest()
        entries[src.rel_path] = {
            "path": src.rel_path,
            "key": src.key,
            "url": src.url,
            "sha256": digest,
            "bytes": len(raw),
            "retrieved": datetime.now(UTC).date().isoformat(),
            **summary,
        }
        print(f"OK    {src.key:18} {len(raw):>10,} bytes  sha256={digest[:12]}  {summary.get('version') or ''}")
    lock["files"] = sorted(entries.values(), key=lambda e: str(e["path"]))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=REPO / "data")
    parser.add_argument("--only", nargs="+", metavar="KEY", help="ingest only these keys")
    parser.add_argument("--schemas", action="store_true", help="also fetch the official OSCAL JSON schemas")
    parser.add_argument("--list", action="store_true", help="list sources and exit")
    args = parser.parse_args(argv)

    pool = [*SOURCES, *(SCHEMAS if args.schemas else ())]
    if args.list:
        for s in (*SOURCES, *SCHEMAS):
            print(f"{s.kind:8} {s.key:22} {s.url}")
        return 0
    if args.only:
        known = {s.key for s in (*SOURCES, *SCHEMAS)}
        unknown = sorted(set(args.only) - known)
        if unknown:
            print(f"unknown key(s): {unknown}. Known: {sorted(known)}", file=sys.stderr)
            return 2
        pool = [s for s in (*SOURCES, *SCHEMAS) if s.key in args.only]
    failures = ingest(pool, args.data_dir.resolve())
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OscalValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
