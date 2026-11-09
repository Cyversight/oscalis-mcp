#!/usr/bin/env python3
"""Single entry point for an editor's "validate on save" hook.

Runs validate-oscal-schemas.py and verify-crosswalk-integrity.py in turn and exits non-zero if either fails. A
single command avoids shell operators such as ``&&``, which Windows PowerShell 5.1 does not support. Works with any
``python`` on PATH: it re-runs itself inside the project's virtualenv when dependencies are missing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from runpy import run_path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _bootstrap import ensure_environment  # noqa: E402

CHECKS = ("validate-oscal-schemas.py", "verify-crosswalk-integrity.py")


def main() -> int:
    ensure_environment()
    failed = 0
    for name in CHECKS:
        code = int(run_path(str(HERE / name))["main"]([]))
        if code:
            print(f"check-data-on-save: {name} failed (exit {code})", file=sys.stderr)
            failed = 1
    if not failed:
        print("check-data-on-save: all data checks passed", file=sys.stderr)
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
