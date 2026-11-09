"""Install-anywhere behaviour: where data is found, and a clear failure when SQLite lacks FTS5."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from oscalis.core.config import Settings, choose_data_dir, default_data_dir
from oscalis.core.graph import SqliteFeatureError, require_fts5

# ----------------------------------------------------------------------------- data location


def test_packaged_data_wins_when_present(tmp_path: Path) -> None:
    packaged, repo = tmp_path / "pkg" / "data", tmp_path / "repo" / "data"
    packaged.mkdir(parents=True)
    repo.mkdir(parents=True)
    assert choose_data_dir(packaged, repo) == packaged


def test_source_checkout_falls_back_to_repo_data(tmp_path: Path) -> None:
    assert choose_data_dir(tmp_path / "missing", tmp_path / "repo" / "data") == tmp_path / "repo" / "data"


def test_default_data_dir_holds_the_catalogs() -> None:
    assert (default_data_dir() / "catalogs" / "800-53r5.2.json").is_file()
    assert Settings().data_dir == default_data_dir()


def test_env_var_still_overrides_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OSCALIS_DATA_DIR", str(tmp_path))
    assert Settings.from_env().data_dir == tmp_path


# ----------------------------------------------------------------------------- FTS5


def test_this_pythons_sqlite_has_fts5() -> None:
    require_fts5(sqlite3.connect(":memory:"))  # does not raise


class _NoFts5:
    """A connection whose SQLite was built without FTS5."""

    def execute(self, sql: str, *args: Any) -> None:
        raise sqlite3.OperationalError("no such module: fts5")


def test_missing_fts5_raises_a_helpful_error() -> None:
    with pytest.raises(SqliteFeatureError) as exc:
        require_fts5(cast(sqlite3.Connection, _NoFts5()))
    message = str(exc.value)
    assert "FTS5" in message and sqlite3.sqlite_version in message and "standard Python build" in message


def test_server_exits_cleanly_when_fts5_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from oscalis import server

    def boom(*_a: Any, **_k: Any) -> None:
        raise SqliteFeatureError("no FTS5")

    monkeypatch.setattr(server, "create_server", boom)
    with pytest.raises(SystemExit) as exc:
        server.main([])
    assert exc.value.code == 2  # a configuration problem, not a crash with a traceback


# ----------------------------------------------------------------------------- hook bootstrap


def _load_bootstrap() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "checks" / "_bootstrap.py"
    spec = importlib.util.spec_from_file_location("hook_bootstrap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_repo(tmp_path: Path) -> Path:
    """A repo whose `.venv/bin/python` points at the base interpreter, as `uv venv` makes it on Linux and macOS."""
    import os
    import shutil
    import sys

    bindir = tmp_path / ".venv" / "bin"
    bindir.mkdir(parents=True)
    try:
        os.symlink(sys.executable, bindir / "python")
    except OSError:  # Windows without symlink privilege
        shutil.copy(sys.executable, bindir / "python")
    return bindir / "python"


def test_hook_bootstrap_reruns_in_a_symlinked_venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess
    import sys

    boot = _load_bootstrap()
    venv_py = _fake_repo(tmp_path)
    monkeypatch.setattr(boot, "REPO", tmp_path)
    monkeypatch.setitem(sys.modules, "pydantic", None)  # this Python lacks the dependencies
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(boot.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        boot.ensure_environment()
    assert exc.value.code == 0
    assert calls and calls[0][0] == str(venv_py)


def test_hook_bootstrap_stops_when_the_venv_itself_lacks_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    boot = _load_bootstrap()
    _fake_repo(tmp_path)
    monkeypatch.setattr(boot, "REPO", tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.setitem(sys.modules, "pydantic", None)
    monkeypatch.setattr(boot.subprocess, "run", lambda *a, **k: pytest.fail("must not re-run itself"))
    with pytest.raises(SystemExit) as exc:
        boot.ensure_environment()
    assert exc.value.code == 2  # no endless re-exec loop


# ----------------------------------------------------------------------------- line endings of generated files


def test_every_script_text_write_forces_lf() -> None:
    """Files the importers write must have the same bytes on Windows, Linux and macOS.

    ``Path.write_text`` without ``newline`` turns each "\\n" into "\\r\\n" on Windows. The GovRAMP importer cannot run
    in tests (its workbooks are not in the repository), so this checks the source of every script instead.
    """
    import ast

    scripts = sorted((Path(__file__).resolve().parents[1] / "src" / "scripts").glob("*.py"))
    assert scripts
    missing: list[str] = []
    for script in scripts:
        for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text":
                newline = next((k.value for k in node.keywords if k.arg == "newline"), None)
                if not (isinstance(newline, ast.Constant) and newline.value == "\n"):
                    missing.append(f"{script.name}:{node.lineno}")
    assert not missing, f"write_text without newline='\\n': {missing}"
