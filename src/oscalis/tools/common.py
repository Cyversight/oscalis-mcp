"""Shared projection, pagination, and token-budget helpers (see the README, Token cost)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal, get_args

from oscalis.core.config import Settings
from oscalis.core.govramp import normalize_control_id
from oscalis.core.graph import OscalGraph, UnknownKeyError, normalize_id

Projection = Literal["summary", "prose_only", "full_oscal"]
PROJECTIONS: tuple[str, ...] = get_args(Projection)

MAX_OFFSET: Final = 1_000_000  # far beyond any catalog (800-53 has 1,196 controls)
MAX_KEY_LIST: Final = 25  # distinct keys in a `profiles` or `target_catalogs` argument
MAX_QUERY_CHARS: Final = 512

_NATURAL_RE = re.compile(r"(\d+)")


def check_projection(projection: str) -> Projection:
    if projection not in PROJECTIONS:
        raise ValueError(f"projection must be one of {list(PROJECTIONS)}, got {projection!r}")
    return projection  # type: ignore[return-value]


def natural_key(value: str) -> list[Any]:
    """Sort 'ac-2' before 'ac-10'."""
    return [int(p) if p.isdigit() else p for p in _NATURAL_RE.split(value)]


def resolve_limit(settings: Settings, projection: Projection, limit: int | None) -> int:
    lim = settings.default_limit if limit is None else limit
    if lim < 1:
        raise ValueError("limit must be at least 1")
    lim = min(lim, settings.max_limit)
    if projection == "full_oscal":
        lim = min(lim, settings.max_full_items)
    return lim


def check_offset(offset: int) -> int:
    if offset < 0:
        raise ValueError("offset must be 0 or greater")
    if offset > MAX_OFFSET:  # also keeps the value inside SQLite's 64-bit integer range
        raise ValueError(f"offset must be at most {MAX_OFFSET:,}")
    return offset


def unique_keys(values: Sequence[str] | None, name: str) -> list[str]:
    """Catalog or profile keys from an argument: stripped, de-duplicated in order, and at most MAX_KEY_LIST of them.

    Every key costs queries per returned item, so an unbounded list is a cheap way to burn CPU.
    """
    keys = list(dict.fromkeys(v.strip() for v in values or ()))
    if len(keys) > MAX_KEY_LIST:
        raise ValueError(f"{name} accepts at most {MAX_KEY_LIST} distinct keys, got {len(keys)}")
    return keys


def check_query(query: str) -> str:
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters, got {len(query):,}")
    return query


def resolve_control_id(graph: OscalGraph, catalog: str, control_id: str) -> str:
    """The stored id for a control, also accepting the printed form of an enhancement ('AC-2(1)', 'AC-02 (01)')."""
    cid = normalize_id(control_id)
    if graph.node_exists(catalog, cid):
        return cid
    alt = normalize_control_id(control_id)
    if alt is not None and graph.node_exists(catalog, alt):
        return alt
    raise UnknownKeyError(
        f"control {cid!r} not found in catalog {catalog!r}. Use search_controls to find the identifier."
    )


def cap_hint(settings: Settings, projection: Projection, requested: int | None, limit: int) -> str | None:
    """The full_oscal cap sentence, when the cap made this page smaller than the caller asked for (or the default)."""
    wanted = settings.default_limit if requested is None else requested
    if projection == "full_oscal" and wanted > limit:
        return (
            f"full_oscal is capped at {settings.max_full_items} items per call; use projection='summary' to scan more."
        )
    return None


def page_meta(
    settings: Settings,
    *,
    projection: Projection,
    total: int,
    offset: int,
    limit: int,
    count: int,
    requested: int | None,
) -> dict[str, Any]:
    """Pagination block included in every list response."""
    truncated = offset + count < total
    meta: dict[str, Any] = {
        "projection": projection,
        "total": total,
        "offset": offset,
        "limit": limit,
        "count": count,
        "truncated": truncated,
    }
    hints: list[str] = []
    if truncated:
        meta["next_offset"] = offset + count
        hints.append(f"Pass offset={offset + count} for the next page, or narrow the request with filters.")
    cap = cap_hint(settings, projection, requested, limit)
    if cap:
        hints.append(cap)
    if hints:
        meta["hint"] = " ".join(hints)
    return meta


def project_control(control: dict[str, Any], projection: Projection) -> dict[str, Any]:
    """Apply a projection to a control dict produced by OscalGraph.get_control."""
    out: dict[str, Any] = {
        "catalog": control["catalog"],
        "control_id": control["control_id"],
        "title": control["title"],
        "class": control["class"],
        "parent_id": control["parent_id"],
        "enhancement_count": len(control["enhancement_ids"]),
    }
    # Only withdrawn (or otherwise non-live) controls carry these keys, so live controls cost nothing extra.
    if control.get("status"):
        out["status"] = control["status"]
    for rel, target in control.get("successors") or ():
        out.setdefault(rel.replace("-", "_"), []).append(target)  # incorporated_into / moved_to
    if projection == "summary":
        return out
    out["prose"] = control["prose"]
    if projection == "prose_only":
        return out
    out["enhancement_ids"] = control["enhancement_ids"]
    out["guidance"] = control.get("guidance", "")
    out["oscal"] = control.get("oscal", {})
    return out


# ----------------------------------------------------------------------------- wire format
# Tool results are the single biggest token cost an agent pays, so they are sent as compact JSON text only:
# no indentation, no duplicate structured copy, and no fields that carry no information.

_DROP_WHEN_NONE: frozenset[str] = frozenset({"class", "parent_id", "group_id"})
_DROP_WHEN_EMPTY: frozenset[str] = frozenset({"values", "constraints"})
_VERBATIM: frozenset[str] = frozenset({"oscal"})  # raw OSCAL is passed through untouched


def slim(value: Any) -> Any:
    """Remove noise from a response: null ``class``/``parent_id``/``group_id`` and empty ``values``/``constraints``.

    Only those keys are touched, so a meaningful ``null`` (for example "profile A has no value") survives.
    The contents of ``oscal`` (a control as published) are never altered. The input is not modified.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if v is None and k in _DROP_WHEN_NONE:
                continue
            if k in _DROP_WHEN_EMPTY and v == []:
                continue
            out[k] = v if k in _VERBATIM else slim(v)
        return out
    if isinstance(value, list):
        return [slim(v) for v in value]
    return value


def to_json(payload: Any) -> str:
    """Compact JSON for the wire. Non-ASCII text stays as written, not as ``\\uXXXX`` escapes."""
    return json.dumps(slim(payload), separators=(",", ":"), ensure_ascii=False)


def fit_page(settings: Settings, limit: int, build: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
    """Build a page of ``limit`` items, shortening it until the response fits ``settings.max_response_chars``.

    ``build(n)`` returns the response for a page of ``n`` items (and reports ``n`` as its ``limit``), so paging stays
    consistent: the next page starts at ``offset + n``. A single item larger than the budget is still returned, because
    there is nothing smaller to send. When the page was shortened, the response says so in ``hint``.
    """
    budget = settings.max_response_chars
    lim = limit
    result = build(lim)
    size = len(to_json(result))
    while size > budget and lim > 1:
        lim = max(1, min(lim - 1, lim * budget * 9 // (size * 10)))  # aim a little under the budget
        result = build(lim)
        size = len(to_json(result))
    if lim < limit:
        note = f"Page shortened to {lim} item(s) to keep the response under {budget:,} characters."
        result["hint"] = f"{result['hint']} {note}" if result.get("hint") else note
    return result
