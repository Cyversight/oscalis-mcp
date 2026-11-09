"""Crosswalk queries: direct and transitive mappings with confidence scoring."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from oscalis.core.config import HARD_MAX_HOPS
from oscalis.core.graph import CAVEATS, OscalGraph, PathResult
from oscalis.tools.common import (
    Projection,
    check_offset,
    check_projection,
    fit_page,
    page_meta,
    project_control,
    resolve_control_id,
    resolve_limit,
    unique_keys,
)


class _Refs:
    """Provenance strings used by a response, each stored once and referred to by a short key (s1, s2, ...).

    The same source text (a document name, version and URL) would otherwise repeat on every hop of every mapping.
    """

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def key(self, text: str) -> str:
        return self._keys.setdefault(text, f"s{len(self._keys) + 1}")

    def table(self) -> dict[str, str]:
        return {key: text for text, key in self._keys.items()}


def _hop_view(path: PathResult, refs: _Refs) -> list[dict[str, Any]]:
    """Every hop of a multi-hop path, in order."""
    hops: list[dict[str, Any]] = []
    for h in path.hops:
        hop: dict[str, Any] = {
            "src": h.src,
            "relation": h.relation,
            "dst": h.dst,
            "confidence": h.confidence,
            "method": h.method,
            "ref": refs.key(h.source),
        }
        if h.derived:
            hop["derived_inverse"] = True
        hops.append(hop)
    return hops


def check_traversal_options(
    graph: OscalGraph,
    target_catalogs: Sequence[str] | None,
    max_hops: int | None,
    min_confidence: float | None,
) -> list[str] | None:
    """Validate the traversal arguments once, at the boundary. Returns the target catalog keys (None for all)."""
    targets = [graph.require_catalog(t) for t in unique_keys(target_catalogs, "target_catalogs")] or None
    if max_hops is not None and not 1 <= max_hops <= HARD_MAX_HOPS:
        raise ValueError(f"max_hops must be between 1 and {HARD_MAX_HOPS}")
    if min_confidence is not None and not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    return targets


def crosswalk_paths(
    graph: OscalGraph,
    catalog: str,
    control_id: str,
    *,
    target_catalogs: Sequence[str] | None,
    max_hops: int | None,
    min_confidence: float | None,
) -> tuple[str, list[PathResult]]:
    cat = graph.require_catalog(catalog)
    cid = resolve_control_id(graph, cat, control_id)
    targets = check_traversal_options(graph, target_catalogs, max_hops, min_confidence)
    node = f"{cat}:{cid}"
    return node, graph.traverse(node, max_hops=max_hops, min_confidence=min_confidence, target_catalogs=targets)


def crosswalk_control(
    graph: OscalGraph,
    catalog: str,
    control_id: str,
    target_catalogs: Sequence[str] | None = None,
    max_hops: int | None = None,
    min_confidence: float | None = None,
    profiles: Sequence[str] | None = None,
    projection: str = "summary",
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    proj: Projection = check_projection(projection)
    lim = resolve_limit(graph.settings, proj, limit)
    off = check_offset(offset)
    profile_keys = [graph.require_profile(p) for p in unique_keys(profiles, "profiles")]
    node, paths = crosswalk_paths(
        graph, catalog, control_id, target_catalogs=target_catalogs, max_hops=max_hops, min_confidence=min_confidence
    )
    src_cat, src_id = node.split(":", 1)
    source = graph.get_control(src_cat, src_id)

    def build(page_limit: int) -> dict[str, Any]:
        mappings: list[dict[str, Any]] = []
        refs = _Refs()
        for p in paths[off : off + page_limit]:
            t_cat, t_id = p.node.split(":", 1)
            target = graph.get_control(t_cat, t_id, full=proj == "full_oscal")
            entry: dict[str, Any] = {
                "target": project_control(target, proj) if target else {"catalog": t_cat, "control_id": t_id},
                "relation": p.relation,
                "confidence": p.confidence,
                "method": p.method,
                "hop_count": len(p.hops),
            }
            if len(p.hops) == 1:
                # a one-hop mapping IS the hop: src is the response `source`, dst is `target`, the rest is above
                entry["ref"] = refs.key(p.hops[0].source)
                if p.hops[0].derived:
                    entry["derived_inverse"] = True
            else:
                entry["path"] = _hop_view(p, refs)
            if p.method in CAVEATS:
                entry["caveat"] = CAVEATS[p.method]
            if profile_keys:
                entry["baselines"] = graph.profile_status(t_cat, t_id, profile_keys, include_notes=proj != "summary")
            mappings.append(entry)
        meta = page_meta(
            graph.settings,
            projection=proj,
            total=len(paths),
            offset=off,
            limit=page_limit,
            count=len(mappings),
            requested=limit,
        )
        return {
            "source": project_control(source, "summary") if source else {"catalog": src_cat, "control_id": src_id},
            "min_confidence": graph.settings.min_confidence if min_confidence is None else min_confidence,
            "max_hops": min(max_hops if max_hops is not None else graph.settings.max_hops, HARD_MAX_HOPS),
            "mappings": mappings,
            "refs": refs.table(),
            **meta,
        }

    return fit_page(graph.settings, lim, build)
