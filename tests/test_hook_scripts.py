"""The quality gate (checks/pre-tool-eval.py) and the git pre-commit checks (checks/git-pre-commit.py).

Both are Python so they run the same on Windows, Linux and macOS. `.githooks/pre-commit` is a small sh wrapper.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import REPO

HOOKS = REPO / "checks"


def load(name: str) -> ModuleType:
    """Import a hook script by path (its file name has a hyphen, so a normal import cannot reach it)."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return load("pre-tool-eval")


@pytest.fixture(scope="module")
def precommit() -> ModuleType:
    return load("git-pre-commit")


# ----------------------------------------------------------------------------- pre-tool-eval.py


def test_gate_runs_every_stage_in_order_and_passes(gate: ModuleType) -> None:
    calls: list[list[str]] = []

    def runner(cmd: Sequence[str]) -> int:
        calls.append(list(cmd))
        return 0

    assert gate.main([], runner=runner, python=["py"]) == 0
    assert [c[1:3] for c in calls[:3]] == [["-m", "ruff"], ["-m", "mypy"], ["-m", "pytest"]]
    assert calls[0][3:] == ["check", "src", "tests", "checks"]
    assert [Path(c[1]).name for c in calls[3:]] == ["validate-oscal-schemas.py", "verify-crosswalk-integrity.py"]
    assert all(c[0] == "py" for c in calls)


def test_gate_stops_at_the_first_failure_and_returns_its_code(gate: ModuleType) -> None:
    codes = iter([0, 7])
    calls: list[list[str]] = []

    def runner(cmd: Sequence[str]) -> int:
        calls.append(list(cmd))
        return next(codes)

    assert gate.main([], runner=runner, python=["py"]) == 7
    assert len(calls) == 2  # mypy failed, so pytest and the validators never ran


def test_fast_skips_pytest_only(gate: ModuleType) -> None:
    names = [name for name, _cmd in gate.stages(["py"], fast=True)]
    assert names == ["ruff", "mypy --strict", "OSCAL schema validation", "crosswalk integrity"]


def test_gate_prefers_the_venv_then_uv_then_this_python(gate: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "venv_python", lambda: Path("/repo/.venv/bin/python"))
    assert gate.choose_python(lambda _name: "/usr/bin/uv") == [str(Path("/repo/.venv/bin/python"))]
    monkeypatch.setattr(gate, "venv_python", lambda: None)
    assert gate.choose_python(lambda _name: "/usr/bin/uv") == ["/usr/bin/uv", "run", "--extra", "dev", "python"]
    assert gate.choose_python(lambda _name: None) == [sys.executable]


# ----------------------------------------------------------------------------- git-pre-commit.py


def _recording_validator(calls: list[list[Path]], result: int = 0) -> Callable[[Sequence[Path]], int]:
    def validate(files: Sequence[Path]) -> int:
        calls.append(list(files))
        return result

    return validate


def test_private_handoff_is_refused_and_nothing_is_validated(
    precommit: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    validated: list[list[Path]] = []
    staged = [REPO / "README.md", REPO / "AI_HANDOFF.md"]
    code = precommit.main([], list_staged=lambda: staged, validate=_recording_validator(validated))
    assert code == 1 and validated == []
    err = capsys.readouterr().err
    assert "AI_HANDOFF.md is private" in err and "git rm --cached" in err


def test_handoff_is_refused_when_the_repository_is_rooted_in_a_parent_folder(precommit: ModuleType) -> None:
    """The old bash hook compared the repo-relative name, so `Oscalis/AI_HANDOFF.md` slipped through."""
    project = REPO.parent / "Oscalis-under-a-parent" / "Oscalis"
    assert precommit.private_files([project / "AI_HANDOFF.md"], root=project) == [project / "AI_HANDOFF.md"]
    assert precommit.private_files([project / "docs" / "AI_HANDOFF.md"], root=project) == []


def test_only_existing_data_files_are_validated(precommit: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "data" / "profiles").mkdir(parents=True)
    keep = tmp_path / "data" / "profiles" / "a b.json"  # a space in the name must survive
    keep.write_text("{}", encoding="utf-8")
    deleted = tmp_path / "data" / "profiles" / "gone.json"
    other = [tmp_path / "README.md", tmp_path / "data" / "notes.txt", tmp_path / "src" / "x.json"]
    assert precommit.data_files([keep, deleted, *other], root=tmp_path) == [keep]


def test_data_files_trigger_validation_and_a_failure_blocks(precommit: ModuleType) -> None:
    lock = REPO / "data" / "sources.lock.json"
    seen: list[list[Path]] = []
    staged = [lock, REPO / "README.md"]
    assert precommit.main([], list_staged=lambda: staged, validate=_recording_validator(seen, result=1)) == 1
    assert seen == [[lock]]


def test_commit_without_data_files_passes_without_validating(precommit: ModuleType) -> None:
    called: list[list[Path]] = []
    staged = [REPO / "README.md"]
    assert precommit.main([], list_staged=lambda: staged, validate=_recording_validator(called)) == 0
    assert called == []


def test_validators_run_for_real_on_the_shipped_data(precommit: ModuleType) -> None:
    assert precommit.run_validators([REPO / "data" / "sources.lock.json"]) == 0


@pytest.mark.parametrize("break_it", ["dangling", "bad-uuid"])
def test_validators_block_broken_data(precommit: ModuleType, tmp_path: Path, break_it: str) -> None:
    """A dangling crosswalk edge fails integrity; a malformed UUID fails validation. Either must block."""
    data = tmp_path / "data"
    shutil.copytree(REPO / "tests" / "fixtures" / "data", data)
    target = sorted((data / "crosswalks").glob("*.json"))[0]
    doc = json.loads(target.read_text(encoding="utf-8"))
    if break_it == "dangling":
        doc["mapping-collection"]["mappings"][0]["maps"][0]["targets"][0]["id-ref"] = "zz-999"
    else:
        doc["mapping-collection"]["uuid"] = "not-a-uuid"
    target.write_text(json.dumps(doc), encoding="utf-8")
    assert precommit.run_validators([target], data_dir=data) == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_staged_reads_real_git_output(precommit: ModuleType, tmp_path: Path) -> None:
    git_exe = shutil.which("git")
    assert git_exe is not None

    def git(*args: str) -> None:
        subprocess.run([git_exe, "-C", str(tmp_path), *args], check=True, capture_output=True)  # noqa: S603

    git("init", "-q")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "AI_HANDOFF.md").write_text("private", encoding="utf-8")
    git("add", "-f", "data/a b.json", "AI_HANDOFF.md")
    got = {p.name for p in precommit.staged(tmp_path)}
    assert got == {"a b.json", "AI_HANDOFF.md"}
    assert precommit.private_files(precommit.staged(tmp_path), root=tmp_path.resolve())


# ----------------------------------------------------------------------------- .githooks/pre-commit wrapper


def test_wrapper_is_plain_sh_that_hands_off_to_python() -> None:
    raw = (REPO / ".githooks" / "pre-commit").read_bytes()
    assert b"\r" not in raw, "a CRLF line ending breaks the shebang on Linux and macOS"
    text = raw.decode("utf-8")
    assert text.startswith("#!/bin/sh\n")
    exec_lines = [line for line in text.splitlines() if "exec " in line]
    assert len(exec_lines) == 3, exec_lines  # the venv loop, python3, python
    assert all("checks/git-pre-commit.py" in line for line in exec_lines), exec_lines
    for bashism in ("[[", "mapfile", "readarray", "declare ", "local ", "PY=("):
        assert bashism not in text, f"{bashism!r} is not POSIX sh"


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh is not installed")
def test_wrapper_parses_as_sh() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    subprocess.run([sh, "-n", str(REPO / ".githooks" / "pre-commit")], check=True)  # noqa: S603
