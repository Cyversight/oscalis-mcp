#!/usr/bin/env python3
"""Quality gate: lint, strict typing, tests across every MCP primitive, and data validation.

Runs the same on Windows, Linux and macOS. No bash is needed.

  ruff check                    style and security lint
  mypy --strict                 static typing (configured in pyproject.toml)
  pytest                        full suite, including tests/test_mcp_endpoints.py, which fails if any registered
                                MCP tool or resource has no test (see test_every_primitive_is_covered)
  validate-oscal-schemas.py     OSCAL structure of everything under data/
  verify-crosswalk-integrity.py every crosswalk edge points at a real control

Usage:
  python checks/pre-tool-eval.py [--fast]     (--fast skips pytest)

Runs the checks with the project's .venv when it exists, else with `uv run --extra dev python`, else with the Python
that started this script. Stops at the first failing stage and exits with that stage's exit code.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _bootstrap import REPO, venv_python  # noqa: E402

Runner = Callable[[Sequence[str]], int]
Which = Callable[[str], str | None]


def choose_python(which: Which = shutil.which) -> list[str]:
    """The command that runs Python with the dev dependencies installed."""
    venv = venv_python()
    if venv is not None:
        return [str(venv)]
    uv = which("uv")
    if uv is not None:
        return [uv, "run", "--extra", "dev", "python"]
    return [sys.executable]


def stages(py: Sequence[str], *, fast: bool) -> list[tuple[str, list[str]]]:
    out = [
        ("ruff", [*py, "-m", "ruff", "check", "src", "tests", "checks"]),
        ("mypy --strict", [*py, "-m", "mypy", "--strict"]),
    ]
    if not fast:
        out.append(("pytest (all MCP primitives)", [*py, "-m", "pytest", "-q"]))
    out += [
        ("OSCAL schema validation", [*py, str(HERE / "validate-oscal-schemas.py")]),
        ("crosswalk integrity", [*py, str(HERE / "verify-crosswalk-integrity.py")]),
    ]
    return out


def run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), cwd=REPO, check=False).returncode


def main(argv: Sequence[str] | None = None, *, runner: Runner = run, python: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fast", action="store_true", help="skip pytest")
    args = parser.parse_args(argv)
    py = list(python) if python is not None else choose_python()
    for name, cmd in stages(py, fast=args.fast):
        print(f"\n==> {name}", file=sys.stderr, flush=True)
        code = runner(cmd)
        if code:
            print(f"\npre-tool-eval: '{name}' failed (exit {code})", file=sys.stderr)
            return code
    print("\npre-tool-eval: all checks passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
