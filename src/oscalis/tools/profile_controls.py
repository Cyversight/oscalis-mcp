"""The controls a profile (baseline) selects, as a bounded, paginated list."""

from __future__ import annotations

from typing import Any

from oscalis.core.graph import OscalGraph
from oscalis.tools.common import (
    check_offset,
    check_projection,
    fit_page,
    natural_key,
    page_meta,
    project_control,
    resolve_limit,
)
from oscalis.tools.get_control import short_title


def list_profile_controls(
    graph: OscalGraph,
    profile: str,
    projection: str = "summary",
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Members of the resolved profile, sorted by catalog then natural control order (ac-2 before ac-10)."""
    proj = check_projection(projection)
    key = graph.require_profile(profile)
    lim = resolve_limit(graph.settings, proj, limit)
    off = check_offset(offset)
    ordered = sorted(graph.profile_members(key), key=lambda n: (n[0], natural_key(n[1])))
    title = next((short_title(p["title"]) for p in graph.list_profiles() if p["key"] == key), None)

    def build(page_limit: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for cat, cid in ordered[off : off + page_limit]:
            control = graph.get_control(cat, cid, full=proj == "full_oscal")
            items.append(project_control(control, proj) if control else {"catalog": cat, "control_id": cid})
        meta = page_meta(
            graph.settings,
            projection=proj,
            total=len(ordered),
            offset=off,
            limit=page_limit,
            count=len(items),
            requested=limit,
        )
        return {"profile": {"key": key, "title": title}, "controls": items, **meta}

    return fit_page(graph.settings, lim, build)
