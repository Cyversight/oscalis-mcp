"""Build OSCAL profile documents from parsed GovRAMP workbook rows.

Pure functions: no file or spreadsheet access. ``src/scripts/import_govramp.py`` reads the workbooks and writes the
documents returned here.

How GovRAMP data is expressed in OSCAL:

* membership: ``imports[].include-controls[].with-ids`` against the NIST catalog (or against a parent profile).
* parameter values: ``modify.set-parameters`` keyed by the catalog's parameter ids.
* additional requirements and guidance, and parameter text that could not be mapped: ``modify.alters[].adds[].parts``,
  one part per labelled item with a ``locator`` prop. The graph layer reads these as per-control notes.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from oscalis.core.govramp import (
    ControlIndex,
    MappedParam,
    MappingResult,
    Unmapped,
    map_cell,
    parse_requirements,
    repair_text,
)

NAMESPACE: Final = uuid.UUID("c7b1f6a0-3d4e-5b8a-9e21-0f4d6a7b8c93")
NOTE_NS: Final = "urn:oscalis:ns:govramp"
OSCAL_VERSION: Final = "1.1.3"


@dataclass(frozen=True)
class ControlRow:
    """One control row of a GovRAMP matrix or overlay workbook."""

    control_id: str  # normalized, e.g. 'ac-2.1'
    authorized: bool  # "Required for Authorized"
    ready: bool  # "Required for Ready"
    core: bool = False  # "Required for Core" (Moderate only)
    params: str | None = None  # GovRAMP-defined parameter cell
    requirements: str | None = None  # additional requirements and guidance cell


@dataclass
class BuildStats:
    """What happened while building one profile. Feeds the import report."""

    controls: int = 0
    mapping: Counter[str] = field(default_factory=Counter)
    unmapped: list[tuple[str, Unmapped]] = field(default_factory=list)
    mapped: list[tuple[str, MappedParam]] = field(default_factory=list)
    notes: int = 0
    repaired_characters: int = 0


def stable_uuid(key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"profile:{key}"))


def _meta(title: str, version: str, remarks: str, modified: str) -> dict[str, Any]:
    return {
        "title": title,
        "last-modified": modified,
        "version": version,
        "oscal-version": OSCAL_VERSION,
        "remarks": remarks,
    }


def _note_part(kind: str, locator: str, text: str) -> dict[str, Any]:
    part: dict[str, Any] = {"name": kind, "ns": NOTE_NS, "prose": text}
    if locator:
        part["props"] = [{"name": "locator", "value": locator}]
    return part


def _set_parameters(mapped: Sequence[MappedParam]) -> list[dict[str, Any]]:
    """One set-parameter per parameter id. A later entry for the same id replaces an earlier one."""
    by_id: dict[str, list[str]] = {}
    for m in mapped:
        by_id[m.param_id] = list(m.values)
    return [{"param-id": pid, "values": vals} for pid, vals in sorted(by_id.items())]


def collect(
    rows: Sequence[ControlRow],
    index: Mapping[str, ControlIndex],
    *,
    include: set[str] | None = None,
    prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BuildStats]:
    """Map parameters and gather requirement text for ``rows``. Returns (set-parameters, alters, stats).

    ``prefix`` is added to every note name (for example ``federal-``) so that a chained overlay does not overwrite
    the notes of the profile it builds on.
    """
    stats = BuildStats()
    mapped_all: list[MappedParam] = []
    alters: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r.control_id):
        if include is not None and row.control_id not in include:
            continue
        stats.controls += 1
        parts: list[dict[str, Any]] = []
        result: MappingResult = map_cell(index, row.params, row.control_id)
        stats.mapping.update(result.counts())
        stats.mapped.extend((row.control_id, m) for m in result.mapped)
        stats.unmapped.extend((row.control_id, u) for u in result.unmapped)
        mapped_all.extend(m for m in result.mapped if m.entry.control_id in index)
        for u in result.unmapped:
            if u.reason == "refers-to-requirements":
                continue  # carries no value: it only points at the requirements text, which is kept below
            loc = ".".join(u.entry.locator) + (f"-{u.entry.ordinal}" if u.entry.ordinal else "")
            text, fixed = repair_text(u.entry.raw)
            stats.repaired_characters += fixed
            parts.append(_note_part(f"{prefix}parameter-text", loc, text))
        for note in parse_requirements(row.requirements):
            text, fixed = repair_text(note.text)
            stats.repaired_characters += fixed
            parts.append(_note_part(f"{prefix}{note.kind}", note.locator, text))
        if parts:
            stats.notes += len(parts)
            alters.append({"control-id": row.control_id, "adds": [{"position": "ending", "parts": parts}]})
    return _set_parameters(mapped_all), alters, stats


def profile_document(
    key: str,
    title: str,
    version: str,
    remarks: str,
    modified: str,
    imports: list[dict[str, Any]],
    set_parameters: list[dict[str, Any]] | None = None,
    alters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "uuid": stable_uuid(key),
        "metadata": _meta(title, version, remarks, modified),
        "imports": imports,
        "merge": {"as-is": True},
    }
    modify: dict[str, Any] = {}
    if set_parameters:
        modify["set-parameters"] = set_parameters
    if alters:
        modify["alters"] = alters
    if modify:
        body["modify"] = modify
    return {"profile": body}


def catalog_import(href: str, ids: Sequence[str]) -> dict[str, Any]:
    return {"href": href, "include-controls": [{"with-ids": sorted(ids)}]}


def profile_import_all(href: str) -> dict[str, Any]:
    return {"href": href, "include-all": {}}


def profile_import_ids(href: str, ids: Sequence[str]) -> dict[str, Any]:
    return {"href": href, "include-controls": [{"with-ids": sorted(ids)}]}


def count_note_parts(alters: Sequence[Mapping[str, Any]] | None) -> int:
    """Number of text parts across all alters (a control may have several ``adds``)."""
    return sum(len(add.get("parts", [])) for a in alters or [] for add in a.get("adds", []))
