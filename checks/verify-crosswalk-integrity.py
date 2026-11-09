#!/usr/bin/env python3
"""Verify referential integrity of the crosswalk graph.

Every edge in data/crosswalks/*.json must point at a control id that exists in one of the active catalogs
in data/catalogs/ (a catalog's key is its file name without extension). Source and target
catalogs named by a mapping must be active catalogs. Crosswalk documents must also parse as strict OSCAL
mapping collections.

Usage:
  verify-crosswalk-integrity.py [--data-dir DIR]

Exit codes: 0 ok, 1 integrity errors.
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

from oscalis.core.integrity import has_errors, verify_crosswalks  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=REPO / "data")
    args = parser.parse_args(argv)

    findings = verify_crosswalks(args.data_dir.resolve())
    for f in findings:
        print(f, file=sys.stderr)
    errors = sum(1 for f in findings if f.severity == "error")
    print(f"verify-crosswalk-integrity: {errors} error(s), {len(findings) - errors} warning(s)", file=sys.stderr)
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
