"""Catalog discovery, single-control lookup, and full-text search."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from oscalis.core.graph import OscalGraph, UnknownKeyError
from oscalis.tools.common import (
    Projection,
    check_offset,
    check_projection,
    check_query,
    fit_page,
    page_meta,
    project_control,
    resolve_control_id,
    resolve_limit,
    unique_keys,
)

_TITLE_PREFIX = re.compile(r"^Electronic (?:\(OSCAL\) )?Version of ")
_TITLE_ABBREVIATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bSpecial Publication\b"), "SP"),
    (re.compile(r"\bRevision\b"), "Rev"),
)


def short_title(title: str) -> str:
    """Drop NIST's "Electronic (OSCAL) Version of" boilerplate and abbreviate "Special Publication"/"Revision".

    Applies to the listing (the tool and `oscal://catalogs`). The published title stays in the index and OSCAL files.
    """
    out = _TITLE_PREFIX.sub("", title)
    for pattern, abbreviation in _TITLE_ABBREVIATIONS:
        out = pattern.sub(abbreviation, out)
    return out or title


def clean_oscal_version(version: str) -> str:
    """Write an OSCAL version the same way everywhere: NIST's files say both "1.2.3" and "v1.2.3"."""
    return version[1:] if re.fullmatch(r"v\d[\w.\-+]*", version) else version


def list_catalogs(graph: OscalGraph) -> dict[str, Any]:
    """Catalogs and profiles currently loaded, with control counts.

    The source file name is left out: it is always ``<key>.json``, so it tells an agent nothing the key does not.
    Titles are shortened (see ``short_title``) and OSCAL versions are written without a leading "v".
    """

    def tidy(key: str, value: Any) -> Any:
        if key == "title":
            return short_title(value)
        if key == "oscal_version":
            return clean_oscal_version(value)
        return value

    def trim(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: tidy(k, v) for k, v in row.items() if k != "source"} for row in rows]

    return {"catalogs": trim(graph.list_catalogs()), "profiles": trim(graph.list_profiles())}


def get_control(
    graph: OscalGraph,
    catalog: str,
    control_id: str,
    projection: str = "summary",
    profiles: Sequence[str] | None = None,
) -> dict[str, Any]:
    proj = check_projection(projection)
    cat = graph.require_catalog(catalog)
    cid = resolve_control_id(graph, cat, control_id)
    control = graph.get_control(cat, cid, full=proj == "full_oscal")
    if control is None:  # resolve_control_id found it, so this is unreachable unless the index changes
        raise UnknownKeyError(f"control {cid!r} not found in catalog {cat!r}")
    out: dict[str, Any] = {**project_control(control, proj), "projection": proj, "truncated": False}
    keys = [graph.require_profile(p) for p in unique_keys(profiles, "profiles")]
    if keys:
        out["baselines"] = graph.profile_status(cat, control["control_id"], keys, include_notes=proj != "summary")
    return out


def search_controls(
    graph: OscalGraph,
    query: str,
    catalog: str | None = None,
    projection: str = "summary",
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    proj: Projection = check_projection(projection)
    check_query(query)
    cat = graph.require_catalog(catalog) if catalog else None
    lim = resolve_limit(graph.settings, proj, limit)
    off = check_offset(offset)

    def build(page_limit: int) -> dict[str, Any]:
        rows, total = graph.search(query, cat, page_limit, off)
        items: list[dict[str, Any]] = []
        for row in rows:
            if proj == "full_oscal":
                full = graph.get_control(row["catalog"], row["control_id"], full=True)
                row = full if full is not None else row
            items.append(project_control(row, proj))
        meta = page_meta(
            graph.settings,
            projection=proj,
            total=total,
            offset=off,
            limit=page_limit,
            count=len(items),
            requested=limit,
        )
        return {"query": query, "catalog": cat, "results": items, **meta}

    return fit_page(graph.settings, lim, build)
