#!/usr/bin/env python3
"""Validate OSCAL catalogs, profiles, and crosswalk mappings before they are committed or persisted.

Checks (see src/oscalis/core/oscal.py and integrity.py):
  * strict OSCAL key sets: non-compliant control keys (for example `control_id`) are rejected
  * required UUIDs present, well-formed (RFC 4122 v4/v5), and unique
  * back-matter resources carry UUIDs; profile `#uuid` imports and control `#id` links resolve
  * metadata.oscal-version >= 1.1.0 (upstream files pinned in data/sources.lock.json may be older)
  * crosswalk relationships, methods, and confidence ranges follow the crosswalk rules (README, Crosswalks)
  * if NIST's official JSON schemas exist in data/schemas/, catalogs and profiles are also checked against them

Usage:
  validate-oscal-schemas.py                 # validate everything under data/
  validate-oscal-schemas.py FILE_OR_DIR...  # validate specific paths (used by the git pre-commit hook)
  validate-oscal-schemas.py --require-official   # fail if the official schemas are not installed

Exit codes: 0 ok, 1 validation errors, 2 usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import ensure_environment  # noqa: E402

ensure_environment()
sys.path.insert(0, str(REPO / "src"))

from oscalis.core.integrity import collect_files, has_errors, validate_files  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories (default: data/)")
    parser.add_argument("--data-dir", type=Path, default=REPO / "data")
    parser.add_argument(
        "--schema-dir", type=Path, default=None, help="official OSCAL schemas (default: <data-dir>/schemas)"
    )
    parser.add_argument("--require-official", action="store_true", help="fail if official schemas are missing")
    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir.resolve()
    targets = [p.resolve() for p in args.paths] or [data_dir]
    # Only validate files that live under data/; ignore unrelated staged files.
    files = [f for f in collect_files(targets) if data_dir in f.parents]
    if not files:
        print("validate-oscal-schemas: nothing to validate", file=sys.stderr)
        return 0

    schema_dir = (args.schema_dir or data_dir / "schemas").resolve()
    findings = validate_files(
        files,
        data_dir,
        schema_dir=schema_dir if schema_dir.is_dir() or args.require_official else None,
        require_official=args.require_official,
    )
    for f in findings:
        print(f, file=sys.stderr)
    errors = sum(1 for f in findings if f.severity == "error")
    print(f"validate-oscal-schemas: {len(files)} file(s) checked, {errors} error(s)", file=sys.stderr)
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
