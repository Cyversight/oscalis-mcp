"""Make the check scripts runnable with any Python: re-run them inside the project's virtualenv when needed.

The scripts import ``oscalis`` (pydantic, PyYAML). Git and editors run them with whatever ``python`` is on PATH, which
usually lacks those packages. Call ``ensure_environment()`` before importing ``oscalis``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def venv_python() -> Path | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = REPO / rel
        if candidate.is_file():
            return candidate
    return None


def running_in_venv() -> bool:
    """True when this interpreter is the project's virtualenv.

    Compares ``sys.prefix``, not executables: on Linux and macOS ``.venv/bin/python`` is a symlink to the base
    interpreter, so resolving both paths would make the base Python look like the venv.
    """
    venv = REPO / ".venv"
    return venv.is_dir() and Path(sys.prefix).resolve() == venv.resolve()


def ensure_environment() -> None:
    """Return if the dependencies import. Otherwise re-run this script with the project's virtualenv and exit."""
    try:
        import pydantic  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        pass
    else:
        return
    py = venv_python()
    if py is None or running_in_venv():
        print(
            "error: pydantic/pyyaml are not installed for this Python or in the project .venv.\n"
            "       Run: uv sync --extra dev",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(subprocess.run([str(py), *sys.argv], check=False).returncode)
