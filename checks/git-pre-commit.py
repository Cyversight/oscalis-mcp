#!/usr/bin/env python3
"""The checks behind the git pre-commit hook. `.githooks/pre-commit` is a small sh wrapper that runs this file.

1. Refuse to commit AI_HANDOFF.md, which is private, even after `git add -f`.
2. When OSCAL data files under data/ are staged, validate them and check crosswalk integrity.

Written in Python so it behaves the same on Windows, Linux and macOS. Staged paths are resolved to absolute paths
before they are compared, so the checks also work when the git repository is rooted in a parent folder.

Exit codes: 0 ok, 1 refused or validation failed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from runpy import run_path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _bootstrap import REPO, ensure_environment  # noqa: E402

PRIVATE_FILES = ("AI_HANDOFF.md",)
DATA_SUFFIXES = frozenset({".json", ".yaml", ".yml"})


def _key(path: Path) -> str:
    """Comparable form of a path: case-insensitive on Windows, exact elsewhere."""
    return os.path.normcase(str(path))


def staged(root: Path) -> list[Path]:
    """Absolute paths of everything staged in the git repository that contains ``root``."""
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    raw = subprocess.run(["git", "-C", top, "diff", "--cached", "--name-only", "-z"], capture_output=True, check=True)
    names = raw.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [(Path(top) / name).resolve() for name in names if name]


def private_files(paths: Sequence[Path], root: Path = REPO) -> list[Path]:
    blocked = {_key(root / name) for name in PRIVATE_FILES}
    return [p for p in paths if _key(p) in blocked]


def data_files(paths: Sequence[Path], root: Path = REPO) -> list[Path]:
    """Staged OSCAL data files that still exist (a deleted or renamed-away file has nothing to validate)."""
    prefix = _key(root / "data") + os.sep
    return [p for p in paths if _key(p).startswith(prefix) and p.suffix.lower() in DATA_SUFFIXES and p.is_file()]


def run_validators(files: Sequence[Path], data_dir: Path = REPO / "data") -> int:
    ensure_environment()  # re-runs this script inside .venv when pydantic or PyYAML is missing
    validate = run_path(str(HERE / "validate-oscal-schemas.py"))["main"]
    verify = run_path(str(HERE / "verify-crosswalk-integrity.py"))["main"]
    failed = int(validate([*(str(f) for f in files), "--data-dir", str(data_dir)])) != 0
    failed = int(verify(["--data-dir", str(data_dir)])) != 0 or failed
    return 1 if failed else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    list_staged: Callable[[], list[Path]] | None = None,
    validate: Callable[[Sequence[Path]], int] = run_validators,
) -> int:
    del argv  # git passes no arguments to a pre-commit hook
    paths = list_staged() if list_staged is not None else staged(REPO)
    for p in private_files(paths):
        print(
            f"error: {p.name} is private and must not be committed. Run: git rm --cached {os.path.relpath(p)}",
            file=sys.stderr,
        )
        return 1
    files = data_files(paths)
    return validate(files) if files else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
