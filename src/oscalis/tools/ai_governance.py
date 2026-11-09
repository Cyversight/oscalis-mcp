"""AI governance mapping: NIST AI RMF subcategories to security controls."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from oscalis.core.graph import OscalGraph, UnknownKeyError
from oscalis.tools.common import (
    check_offset,
    check_projection,
    fit_page,
    page_meta,
    project_control,
    resolve_control_id,
    resolve_limit,
    unique_keys,
)
from oscalis.tools.crosswalk import check_traversal_options, crosswalk_control, crosswalk_paths

AI_FRAMEWORKS: Final = ("ai-rmf",)  # catalog keys treated as AI governance frameworks
DEFAULT_TARGET: Final = "800-53r5.2"


def _targets(graph: OscalGraph, target_catalogs: Sequence[str] | None) -> Sequence[str] | None:
    if target_catalogs:
        return target_catalogs
    return [DEFAULT_TARGET] if DEFAULT_TARGET in graph.catalog_keys() else None


def ai_governance_map(
    graph: OscalGraph,
    framework: str = "ai-rmf",
    control_id: str | None = None,
    target_catalogs: Sequence[str] | None = None,
    profiles: Sequence[str] | None = None,
    max_hops: int | None = None,
    min_confidence: float | None = None,
    projection: str = "summary",
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Map an AI governance control to security controls, or list the framework's controls.

    With ``control_id``: the crosswalk of that control into ``target_catalogs`` (default NIST SP 800-53),
    optionally with baseline membership for ``profiles``.
    Without ``control_id``: a page of the framework's controls, each with the number of mapped targets.
    """
    if framework not in AI_FRAMEWORKS:
        raise UnknownKeyError(f"framework must be one of {list(AI_FRAMEWORKS)}, got {framework!r}")
    graph.require_catalog(framework)
    # Validate every argument up front, so listing mode (which uses only some of them) rejects bad input too.
    targets = check_traversal_options(graph, _targets(graph, target_catalogs), max_hops, min_confidence)
    profile_keys = [graph.require_profile(p) for p in unique_keys(profiles, "profiles")]

    if control_id is not None:
        result = crosswalk_control(
            graph, framework, control_id, targets, max_hops, min_confidence, profile_keys, projection, limit, offset
        )
        control = graph.get_control(framework, resolve_control_id(graph, framework, control_id))
        result["framework"] = framework
        result["function_or_group"] = control["group_id"] if control else None
        result["guidance"] = (
            "Mappings are advisory unless method is 'direct'. Do not treat them as compliance evidence."
        )
        return result
    if profile_keys:
        raise ValueError("profiles applies only with control_id; listing mode reports mapped-target counts only")

    proj = check_projection(projection)
    lim = resolve_limit(graph.settings, proj, limit)
    off = check_offset(offset)

    def build(page_limit: int) -> dict[str, Any]:
        rows, total = graph.list_controls(framework, page_limit, off, top_level_only=False)
        items: list[dict[str, Any]] = []
        for row in rows:
            full = graph.get_control(framework, row["control_id"], full=True) if proj == "full_oscal" else row
            item = project_control(full if full else row, proj)
            item["group_id"] = row["group_id"]
            _node, paths = crosswalk_paths(
                graph,
                framework,
                row["control_id"],
                target_catalogs=targets,
                max_hops=max_hops,
                min_confidence=min_confidence,
            )
            item["mapped_target_count"] = len(paths)
            items.append(item)
        meta = page_meta(
            graph.settings,
            projection=proj,
            total=total,
            offset=off,
            limit=page_limit,
            count=len(items),
            requested=limit,
        )
        return {
            "framework": framework,
            "target_catalogs": list(targets) if targets else None,
            "controls": items,
            **meta,
        }

    return fit_page(graph.settings, lim, build)
