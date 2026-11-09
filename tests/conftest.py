"""Shared fixtures. Tests run against the bundled seed data and against small synthetic data sets."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

import pytest

from oscalis.core.config import Settings
from oscalis.core.graph import OscalGraph

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DATA = REPO / "tests" / "fixtures" / "data"  # small seed data set; tests never depend on data/
T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    import asyncio

    return asyncio.run(coro)


def meta(title: str = "t", version: str = "1.0", oscal: str = "1.1.3") -> dict[str, Any]:
    return {"title": title, "last-modified": "2026-01-01T00:00:00Z", "version": version, "oscal-version": oscal}


def catalog_doc(controls: list[dict[str, Any]], *, oscal: str = "1.1.3") -> dict[str, Any]:
    return {
        "catalog": {
            "uuid": str(uuid.uuid4()),
            "metadata": meta(oscal=oscal),
            "groups": [{"id": "g", "title": "Group", "controls": controls}],
        }
    }


def ctl(cid: str, title: str = "", controls: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"id": cid, "title": title or cid.upper()}
    if controls:
        c["controls"] = controls
    c.update(extra)
    return c


def profile_doc(imports: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"profile": {"uuid": str(uuid.uuid4()), "metadata": meta(), "imports": imports, **extra}}


def crosswalk_doc(src: str, dst: str, maps: list[tuple[str, str, str, str, float]]) -> dict[str, Any]:
    """maps: (source id, relation without prefix, target id, method, confidence)."""
    return {
        "mapping-collection": {
            "uuid": str(uuid.uuid4()),
            "metadata": meta(),
            "mappings": [
                {
                    "uuid": str(uuid.uuid4()),
                    "source-resource": {"catalog": src},
                    "target-resource": {"catalog": dst},
                    "provenance": {"document": "test", "version": "1", "href": "https://example.test/doc"},
                    "maps": [
                        {
                            "uuid": str(uuid.uuid4()),
                            "relationship": f"crosswalk:{rel}",
                            "method": method,
                            "confidence": conf,
                            "sources": [{"type": "control", "id-ref": s}],
                            "targets": [{"type": "control", "id-ref": t}],
                        }
                        for s, rel, t, method, conf in maps
                    ],
                }
            ],
        }
    }


def write_json(path: Path, doc: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def seed_settings() -> Settings:
    return Settings(data_dir=FIXTURE_DATA)


@pytest.fixture(scope="session")
def graph(seed_settings: Settings) -> OscalGraph:
    return OscalGraph.load(seed_settings)


@pytest.fixture(scope="session")
def shipped() -> OscalGraph:
    """The real data in data/ (NIST, FedRAMP, GovRAMP). Only tests that are about shipped data should use this."""
    return OscalGraph.load(Settings(data_dir=REPO / "data"))


@pytest.fixture
def make_graph(tmp_path: Path) -> Callable[..., OscalGraph]:
    """Build a graph from synthetic documents: make_graph(catalogs={key: doc}, profiles=..., crosswalks=...)."""

    def build(
        catalogs: dict[str, dict[str, Any]] | None = None,
        profiles: dict[str, dict[str, Any]] | None = None,
        crosswalks: dict[str, dict[str, Any]] | None = None,
        **settings: Any,
    ) -> OscalGraph:
        for folder, docs in (("catalogs", catalogs), ("profiles", profiles), ("crosswalks", crosswalks)):
            for key, doc in (docs or {}).items():
                write_json(tmp_path / folder / f"{key}.json", doc)
        return OscalGraph.load(replace(Settings(data_dir=tmp_path), **settings))

    return build
