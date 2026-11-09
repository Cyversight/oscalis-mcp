"""Baseline comparison: which controls and parameter values differ between two profiles."""

from __future__ import annotations

import re
from typing import Any

from oscalis.core.graph import OscalGraph
from oscalis.tools.common import (
    Projection,
    cap_hint,
    check_offset,
    check_projection,
    fit_page,
    natural_key,
    project_control,
    resolve_limit,
)
from oscalis.tools.get_control import short_title


def _section(
    graph: OscalGraph,
    nodes: list[tuple[str, str]],
    projection: Projection,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    ordered = sorted(nodes, key=lambda n: (n[0], natural_key(n[1])))
    window = ordered[offset : offset + limit]
    items: list[dict[str, Any]] = []
    for cat, cid in window:
        control = graph.get_control(cat, cid, full=projection == "full_oscal")
        items.append(project_control(control, projection) if control else {"catalog": cat, "control_id": cid})
    truncated = offset + len(items) < len(ordered)
    out: dict[str, Any] = {"total": len(ordered), "count": len(items), "truncated": truncated, "items": items}
    if truncated:
        out["next_offset"] = offset + len(items)
    return out


def _profile_info(graph: OscalGraph, key: str) -> dict[str, Any]:
    for p in graph.list_profiles():
        if p["key"] == key:
            return {"key": key, "title": short_title(p["title"]), "control_count": p["control_count"]}
    return {"key": key}


_WS = re.compile(r"\s+")
EXCERPT_CHARS = 200
# Formatting that differs between programs' files without changing what is required.
# A leading label, optionally after the control id and item: "Requirement:", "Guidance 2:", "CM-6 (a) Requirement 1:".
_LEADING_LABEL = re.compile(
    r"^(?:[a-z]{2}-\d+(?:\s*\([0-9a-z]+\))*\s+)?"
    r"(?:(?:requirement|guidance)(?:\s*\d+)?|selection(?:\s*\(one or more\))?)\s*:\s*"
)
_ESCAPE = re.compile(r"\\(?=[^\w\s])")  # FedRAMP's Markdown escapes, as in "\*See Additional Requirements"
_TRAILING = re.compile(r"[\s.;:,]+$")
_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-"})
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
# A value that only sends the reader to the profile's notes ("see additional FedRAMP Requirements and Guidance").
# It states no value, so it compares as "not set". As a trailing remark after a real value it is dropped.
_POINTER = r"\*?\s*see (?:the )?additional (?:fedramp |govramp )?requirements?(?: and guidance)?"
_POINTER_ONLY = re.compile(rf"^{_POINTER}$")
_POINTER_TAIL = re.compile(rf"\s*\(\s*{_POINTER}\s*\.?\s*\)$")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.translate(_DASHES).translate(_QUOTES)).strip().lower()


def _canon(text: str) -> str:
    """Comparable form of a value or a note line: lowercased and whitespace-collapsed, typographic dashes and quotes
    made plain, without a leading "Requirement:"/"Guidance:"/"Selection:" label (optionally after a control id),
    Markdown escapes, or trailing punctuation."""
    return _TRAILING.sub("", _LEADING_LABEL.sub("", _ESCAPE.sub("", _norm(text))))


def _canon_value(text: str) -> str:
    """``_canon`` for a parameter value, which also drops a trailing "(See additional requirements and guidance.)"."""
    return _canon(_POINTER_TAIL.sub("", _canon(text)))


def _effective(entry: dict[str, Any] | None) -> str | None:
    """Comparable text of a parameter setting: its values, else its constraints. ``None`` when it sets nothing, which
    includes a value that only points at the notes."""
    if entry is None:
        return None
    parts = [_canon_value(p) for p in entry["values"] or entry["constraints"]]
    parts = [p for p in parts if p and not _POINTER_ONLY.match(p)]
    return "; ".join(parts) or None


def _same_group(
    pa: dict[tuple[str, str], dict[str, Any]],
    pb: dict[tuple[str, str], dict[str, Any]],
    nested: dict[tuple[str, str], tuple[str, ...]],
) -> set[tuple[str, str]]:
    """Parameter keys whose difference is only which parameter of a selection carries the value.

    In PE-14 the selection "[temperature; humidity; ...; [Assignment: environmental control]]" contains its own
    assignment parameter. FedRAMP writes "ASHRAE ..." on the selection, GovRAMP on the nested assignment. When both
    profiles state the same set of values across the selection and its nested parameters, none of them is a change.
    """
    same: set[tuple[str, str]] = set()
    for sel, inner in nested.items():
        group = [sel, *((sel[0], n) for n in inner)]
        if all(_effective(pa.get(k)) == _effective(pb.get(k)) for k in group):
            continue  # nothing differs here; the per-parameter comparison already agrees
        va = {v for k in group if (v := _effective(pa.get(k)))}
        vb = {v for k in group if (v := _effective(pb.get(k)))}
        if va and va == vb:
            same.update(group)
    return same


def _param_view(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """A parameter setting as sent to the agent: ``values`` and/or ``constraints``, each once.

    An earlier shape also carried a joined ``text`` copy of whichever list was set, which doubled the payload.
    ``to_json`` drops the empty list, so a setting shows only the kind it has. ``None`` means "no setting".
    ``via`` names the legacy parameter the value was set on, when the profile did not set this id directly.
    """
    if entry is None:
        return None
    view = {"values": entry["values"], "constraints": entry["constraints"]}
    if entry.get("via"):
        view["via"] = entry["via"]
    return view


def _expand_aggregates(
    values: dict[tuple[str, str], dict[str, Any]], aggregates: dict[tuple[str, str], tuple[str, ...]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Rewrite settings made on legacy parameters (``at-2_prm_1``) onto the parameters they stand for.

    NIST's catalog says ``at-2_prm_1`` aggregates ``at-02_odp.01`` and ``at-02_odp.02``. FedRAMP's OSCAL 1.0.4 files set
    the legacy id; GovRAMP's set the newer ones. Comparing by id alone would report the same value twice, once removed
    and once added. A value the profile sets directly on a covered id wins over the legacy one.
    """
    out = {k: v for k, v in values.items() if k not in aggregates}
    for key, entry in values.items():
        for covered in aggregates.get(key, ()):
            out.setdefault((key[0], covered), {**entry, "via": key[1]})
    return out


def _window(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    window = items[offset : offset + limit]
    out: dict[str, Any] = {
        "total": len(items),
        "count": len(window),
        "truncated": offset + len(window) < len(items),
        "items": window,
    }
    if out["truncated"]:
        out["next_offset"] = offset + len(window)
    return out


def _identity(text: str) -> str:
    return text


def _excerpt(text: str) -> str:
    return text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else "")


def _lines(text: str) -> list[str]:
    """Comparable lines of a note (see ``_canon``)."""
    return [line for line in (_canon(raw) for raw in text.splitlines()) if line]


def _unmatched(text: str, other_side: set[str]) -> str:
    """The lines of ``text`` that appear nowhere in the other profile's notes for the same control."""
    return "\n".join(raw for raw in text.splitlines() if raw.strip() and _canon(raw) not in other_side)


def _requirement_changes(
    graph: OscalGraph, a: str, b: str, common: set[tuple[str, str]], *, full_text: bool
) -> list[dict[str, Any]]:
    """Notes that differ between the profiles, for controls both select.

    Notes are paired by (name, locator) first. A note left without a partner is then compared line by line with every
    note the other profile has on that control, whatever its name: FedRAMP files all its text for a control as one
    `item` note, GovRAMP splits it into `requirement` and `guidance`, so the same sentence would otherwise show as both
    removed and added. Lines found on the other side are dropped; an unpaired note is reported only with the lines that
    are really missing, and not at all when every line is found.
    """
    na, nb = graph.profile_notes(a), graph.profile_notes(b)
    show = _identity if full_text else _excerpt
    out: list[dict[str, Any]] = []
    for node in sorted(set(na) | set(nb), key=lambda n: (n[0], natural_key(n[1]))):
        if node not in common:
            continue
        ea, eb = na.get(node, {}), nb.get(node, {})
        lines_a = {line for text in ea.values() for line in _lines(text)}
        lines_b = {line for text in eb.values() for line in _lines(text)}
        for name_loc in sorted(set(ea) | set(eb)):
            ta, tb = ea.get(name_loc), eb.get(name_loc)
            if ta is not None and tb is not None:
                if _lines(ta) == _lines(tb):
                    continue
                change = "modified"
            elif ta is not None:
                ta = _unmatched(ta, lines_b)
                if not ta:
                    continue
                change = "removed"
            elif tb is not None:
                tb = _unmatched(tb, lines_a)
                if not tb:
                    continue
                change = "added"
            else:  # unreachable: name_loc comes from one side or the other
                continue
            out.append(
                {
                    "catalog": node[0],
                    "control_id": node[1],
                    "name": name_loc[0],
                    "locator": name_loc[1],
                    "change": change,
                    "a": None if ta is None else show(ta),
                    "b": None if tb is None else show(tb),
                }
            )
    return out


def baseline_delta(
    graph: OscalGraph,
    profile_a: str,
    profile_b: str,
    projection: str = "summary",
    limit: int | None = None,
    offset: int = 0,
    include_parameter_changes: bool = True,
) -> dict[str, Any]:
    """Compare profile A (the reference) with profile B. ``limit``/``offset`` apply to each section."""
    proj = check_projection(projection)
    a = graph.require_profile(profile_a)
    b = graph.require_profile(profile_b)
    lim = resolve_limit(graph.settings, proj, limit)
    off = check_offset(offset)

    members_a, members_b = graph.profile_members(a), graph.profile_members(b)
    added = sorted(members_b - members_a)
    removed = sorted(members_a - members_b)
    common = members_a & members_b

    changes: list[dict[str, Any]] = []
    if include_parameter_changes:
        aggregates = graph.param_aggregates()
        pa = _expand_aggregates(graph.profile_param_values(a), aggregates)
        pb = _expand_aggregates(graph.profile_param_values(b), aggregates)
        regrouped = _same_group(pa, pb, graph.param_nested())
        for key in sorted(set(pa) | set(pb), key=lambda k: (k[0], natural_key(k[1]))):
            if key in regrouped:
                continue
            va, vb = pa.get(key), pb.get(key)
            owner = (va or vb or {}).get("control_id")
            if owner is None or (key[0], owner) not in common:
                continue
            if _effective(va) != _effective(vb):
                changes.append(
                    {
                        "catalog": key[0],
                        "control_id": owner,
                        "param_id": key[1],
                        "a": _param_view(va),
                        "b": _param_view(vb),
                    }
                )
    changes.sort(key=lambda c: (c["catalog"], natural_key(c["control_id"]), natural_key(c["param_id"])))
    req_changes = _requirement_changes(graph, a, b, common, full_text=proj != "summary")
    sections = ("added_in_b", "removed_in_b", "parameter_changes", "requirement_changes")

    def build(page_limit: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "profile_a": _profile_info(graph, a),
            "profile_b": _profile_info(graph, b),
            "projection": proj,
            "offset": off,
            "limit": page_limit,
            "summary": {
                "added_in_b": len(added),
                "removed_in_b": len(removed),
                "common": len(common),
                "parameter_changes": len(changes),
                "requirement_changes": len(req_changes),
            },
            "added_in_b": _section(graph, added, proj, page_limit, off),
            "removed_in_b": _section(graph, removed, proj, page_limit, off),
            "parameter_changes": _window(changes, off, page_limit),
            "requirement_changes": _window(req_changes, off, page_limit),
            "note": (
                "Parameter comparison covers values set by the profiles (set-parameters) and compares their text, "
                "ignoring case and spacing, so 'values' in one profile and 'constraints' in another are comparable. "
                "A missing value means the profile leaves the catalog default. A value set on a legacy parameter id is "
                "compared on the ids it stands for ('via' names it). The same value on a selection or on the parameter "
                "nested in that selection is not a change, and a value that only says 'see additional requirements' "
                "counts as unset. Requirement changes compare the additional text "
                "a profile attaches to a control (alters); a note without a same-named partner shows only the lines "
                "no note on the other side contains."
            ),
        }
        truncated = any(bool(result[k]["truncated"]) for k in sections)
        result["truncated"] = truncated
        hints: list[str] = []
        if truncated:
            # limit and offset apply to every section, so one offset moves all of them to their next page
            hints.append(f"A section was truncated. Pass offset={off + page_limit} for the next page of every section.")
            if proj != "summary":
                hints.append("projection='summary' returns shorter items.")
        cap = cap_hint(graph.settings, proj, limit, page_limit)
        if cap:
            hints.append(cap)
        if hints:
            result["hint"] = " ".join(hints)
        return result

    return fit_page(graph.settings, lim, build)
