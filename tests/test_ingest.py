"""ingest_catalogs.py without the network: downloads are replaced by an in-memory fake."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import REPO, catalog_doc, ctl, profile_doc

Fetcher = Callable[[str], bytes]


def load_script() -> ModuleType:
    """Import the script as a real module, so patching ``fetch`` on it affects the functions that call it."""
    spec = importlib.util.spec_from_file_location("ingest_under_test", REPO / "src" / "scripts" / "ingest_catalogs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ingest() -> ModuleType:
    mod = load_script()

    def no_network(url: str) -> bytes:  # a test that forgets to serve its URLs must fail, never go online
        raise AssertionError(f"unexpected network fetch in test: {url}")

    # the allow-list tests exercise the genuine function; it refuses before any I/O
    mod.real_fetch = mod.fetch  # type: ignore[attr-defined]
    mod.fetch = no_network  # type: ignore[attr-defined]
    return mod


def as_bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode("utf-8")


def source(mod: ModuleType, key: str, kind: str = "catalog") -> Any:
    return mod.Source(key, kind, f"https://raw.githubusercontent.com/example/{key}.json")


def serve(mod: ModuleType, monkeypatch: pytest.MonkeyPatch, responses: dict[str, bytes | Exception]) -> list[str]:
    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mod, "fetch", fake_fetch)
    return calls


def test_valid_download_is_written_and_recorded(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = as_bytes(catalog_doc([ctl("x-1")]))
    src = source(ingest, "demo")
    serve(ingest, monkeypatch, {src.url: raw})
    assert ingest.ingest([src], tmp_path) == 0
    assert (tmp_path / "catalogs" / "demo.json").read_bytes() == raw
    lock = json.loads((tmp_path / "sources.lock.json").read_text(encoding="utf-8"))
    entry = lock["files"][0]
    assert entry["path"] == "catalogs/demo.json" and entry["url"] == src.url
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest() and entry["bytes"] == len(raw)
    assert entry["retrieved"] and entry["version"] == "1.0"
    # LF on every platform: on Windows a plain write_text would turn each "\n" into "\r\n", so the same run would
    # produce different bytes on Windows and Linux.
    assert b"\r" not in (tmp_path / "sources.lock.json").read_bytes()


def test_invalid_download_keeps_the_existing_file(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = as_bytes(catalog_doc([ctl("x-1")]))
    target = tmp_path / "catalogs" / "demo.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(good)
    broken = catalog_doc([ctl("x-1")])
    broken["catalog"]["uuid"] = "not-a-uuid"
    src = source(ingest, "demo")
    serve(ingest, monkeypatch, {src.url: as_bytes(broken)})
    assert ingest.ingest([src], tmp_path) == 1
    assert target.read_bytes() == good  # unchanged
    lock = tmp_path / "sources.lock.json"
    assert json.loads(lock.read_text(encoding="utf-8"))["files"] == []  # nothing recorded for the failure


def test_non_json_and_network_errors_are_failures_not_crashes(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, c = source(ingest, "garbage"), source(ingest, "offline"), source(ingest, "ok")
    serve(
        ingest,
        monkeypatch,
        {
            a.url: b"<html>not json</html>",
            b.url: urllib.error.URLError("no route"),
            c.url: as_bytes(catalog_doc([ctl("x-1")])),
        },
    )
    assert ingest.ingest([a, b, c], tmp_path) == 2  # two failures, and the third source still succeeds
    assert (tmp_path / "catalogs" / "ok.json").is_file()
    assert not (tmp_path / "catalogs" / "garbage.json").exists()


def test_old_oscal_version_is_accepted_for_upstream_files(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = profile_doc([{"href": "x.json", "include-all": {}}])
    doc["profile"]["metadata"]["oscal-version"] = "1.0.4"
    src = source(ingest, "old", "profile")
    serve(ingest, monkeypatch, {src.url: as_bytes(doc)})
    assert ingest.ingest([src], tmp_path) == 0


def test_rerun_updates_the_lock_entry_instead_of_duplicating(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = source(ingest, "demo")
    serve(ingest, monkeypatch, {src.url: as_bytes(catalog_doc([ctl("x-1")]))})
    ingest.ingest([src], tmp_path)
    serve(ingest, monkeypatch, {src.url: as_bytes(catalog_doc([ctl("x-1"), ctl("x-2")]))})
    ingest.ingest([src], tmp_path)
    files = json.loads((tmp_path / "sources.lock.json").read_text(encoding="utf-8"))["files"]
    assert len(files) == 1


def test_rerun_keeps_entries_for_other_sources_and_generated_section(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sources.lock.json").write_text(
        json.dumps(
            {"files": [{"path": "catalogs/other.json", "key": "other"}], "generated": [{"path": "profiles/g.json"}]}
        ),
        encoding="utf-8",
    )
    src = source(ingest, "demo")
    serve(ingest, monkeypatch, {src.url: as_bytes(catalog_doc([ctl("x-1")]))})
    ingest.ingest([src], tmp_path)
    lock = json.loads((tmp_path / "sources.lock.json").read_text(encoding="utf-8"))
    assert {f["path"] for f in lock["files"]} == {"catalogs/other.json", "catalogs/demo.json"}
    assert lock["generated"] == [{"path": "profiles/g.json"}]


def test_schema_downloads_only_need_to_be_json_schemas(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = source(ingest, "s_good", "schema"), source(ingest, "s_bad", "schema")
    serve(
        ingest,
        monkeypatch,
        {good.url: json.dumps({"definitions": {}}).encode(), bad.url: json.dumps({"x": 1}).encode()},
    )
    assert ingest.ingest([good, bad], tmp_path) == 1
    assert (tmp_path / "schemas" / "s_good.json").is_file() and not (tmp_path / "schemas" / "s_bad.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://raw.githubusercontent.com/x.json",  # not https
        "https://evil.example.com/x.json",  # host not allowed
        "file:///etc/passwd",
        "https://raw.githubusercontent.com.evil.example/x.json",  # look-alike host
    ],
)
def test_fetch_refuses_unlisted_hosts_and_schemes(ingest: ModuleType, url: str) -> None:
    with pytest.raises(ValueError, match="refusing to fetch"):
        ingest.real_fetch(url)


def test_every_configured_source_is_https_on_an_allowed_host(ingest: ModuleType) -> None:
    for s in (*ingest.SOURCES, *ingest.SCHEMAS):
        assert s.url.startswith("https://") and s.url.split("/")[2] in ingest.ALLOWED_HOSTS
        assert s.rel_path.endswith(".json")
    keys = [s.key for s in ingest.SOURCES]
    assert len(keys) == len(set(keys))


def test_cli_list_and_unknown_key(ingest: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert ingest.main(["--list"]) == 0
    assert "800-53r5.2" in capsys.readouterr().out
    assert ingest.main(["--only", "nope", "--data-dir", str(tmp_path)]) == 2


def test_only_flag_downloads_just_the_requested_keys(
    ingest: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wanted = next(s for s in ingest.SOURCES if s.key == "csf-v2")
    calls = serve(ingest, monkeypatch, {wanted.url: as_bytes(catalog_doc([ctl("x-1")]))})
    assert ingest.main(["--only", "csf-v2", "--data-dir", str(tmp_path)]) == 0
    assert calls == [wanted.url]
