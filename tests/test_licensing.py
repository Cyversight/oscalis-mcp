"""Licensing files: the repository must carry its license and say which data is not covered by it."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLDER = "Cyversight LLC"


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_license_is_the_apache_2_text() -> None:
    text = read("LICENSE")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert len(text) > 11_000  # the whole text, not an excerpt


def test_notice_names_the_holder_and_the_third_party_data() -> None:
    notice = read("NOTICE")
    assert f"Copyright 2026 {HOLDER}" in notice
    for needle in ("NIST", "FedRAMP", "GovRAMP", "not affiliated with or endorsed by"):
        assert needle in notice


def test_every_data_path_pattern_in_notice_matches_a_real_file() -> None:
    """A NOTICE that lists files which no longer exist, or misses a group, is stale."""
    for pattern in (
        "data/catalogs/*.json",
        "data/profiles/800-53b-*.json",
        "data/crosswalks/csf-v2__800-53r5.2.json",
        "data/profiles/fedramp-*.json",
        "data/profiles/govramp-*.json",
        "reports/govramp-import-report.json",
        "data/crosswalks/ai-rmf__800-53r5.2.json",
    ):
        assert pattern in read("NOTICE"), pattern
        assert list(ROOT.glob(pattern)), f"{pattern} matches no file"


def test_every_shipped_profile_and_catalog_is_covered_by_the_notice() -> None:
    """Adding a data file with a new name pattern must be reflected in NOTICE."""
    covered = ("800-53b-", "fedramp-", "govramp-")
    for path in (ROOT / "data" / "profiles").glob("*.json"):
        assert path.name.startswith(covered), f"{path.name} is not described in NOTICE"
    crosswalks = sorted(p.name for p in (ROOT / "data" / "crosswalks").glob("*.json"))
    assert crosswalks == ["ai-rmf__800-53r5.2.json", "csf-v2__800-53r5.2.json"]


def test_project_metadata_matches_the_license() -> None:
    meta = tomllib.loads(read("pyproject.toml"))["project"]
    assert meta["license"] == {"file": "LICENSE"}
    assert {"name": HOLDER} in meta["authors"]
    assert {"name": "Al Lyle"} in meta["authors"]
    assert "License :: OSI Approved :: Apache Software License" in meta["classifiers"]
    assert meta["urls"]["Repository"] == "https://github.com/Cyversight/oscalis-mcp"


def test_readme_states_the_license_and_the_data_carve_out() -> None:
    readme = read("README.md")
    assert "Apache License, Version 2.0" in readme and f"Copyright 2026 {HOLDER}" in readme
    assert "NOTICE" in readme and "remains GovRAMP's" in readme


def test_private_handoff_is_kept_out_of_the_repository() -> None:
    """AI_HANDOFF.md is for the owner's assistant only. Three guards stop it reaching GitHub."""
    ignored = {line.strip() for line in read(".gitignore").splitlines()}
    assert "AI_HANDOFF.md" in ignored, ".gitignore must list AI_HANDOFF.md"
    assert "AI_HANDOFF.md" in read("checks/git-pre-commit.py"), "the pre-commit check must refuse to commit it"
    assert "git-pre-commit.py" in read(".githooks/pre-commit"), "the git hook must run that check"
    assert "AI_HANDOFF.md" in read(".github/workflows/ci.yml"), "CI must fail if it is tracked"
    # a published README or NOTICE must not point readers at a file they cannot see
    assert "AI_HANDOFF" not in read("README.md") and "AI_HANDOFF" not in read("NOTICE")
