"""Artifact validation and crosswalk referential-integrity checks used by the hooks.

``validate_files`` checks that catalogs, profiles, and crosswalks conform to OSCAL. ``verify_crosswalks``
checks that every crosswalk edge points to a control that exists in an active catalog.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from oscalis.core.graph import discover_files
from oscalis.core.oscal import (
    MIN_OSCAL_VERSION,
    OscalValidationError,
    check_crosswalk_semantics,
    crosswalk_edges,
    load_document,
    parse_crosswalk,
    validate_document,
)

Severity = Literal["error", "warning"]
LOCK_FILE = "sources.lock.json"
_DOC_SUFFIXES = {".json", ".yaml", ".yml"}
_KIND_BY_DIR = {"catalogs": "catalog", "profiles": "profile", "crosswalks": "mapping-collection"}
# Upstream OSCAL published before 1.1 (for example FedRAMP's 1.0.4 baselines) is accepted with a warning.
UPSTREAM_FLOOR = (1, 0, 0)


@dataclass(frozen=True)
class Finding:
    path: str
    message: str
    severity: Severity = "error"

    def __str__(self) -> str:
        return f"{self.severity.upper()}: {self.path}: {self.message}"


def has_errors(findings: Sequence[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)


def collect_files(targets: Sequence[Path]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        if t.is_dir():
            out.extend(
                p
                for p in sorted(t.rglob("*"))
                if p.is_file()
                and p.suffix.lower() in _DOC_SUFFIXES
                and p.name != LOCK_FILE
                and "schemas" not in p.parts
            )
        elif t.is_file() and t.suffix.lower() in _DOC_SUFFIXES and t.name != LOCK_FILE:
            out.append(t)
    return out


def upstream_files(data_dir: Path) -> set[str]:
    """File names recorded as ingested upstream content in sources.lock.json."""
    lock = data_dir / LOCK_FILE
    if not lock.is_file():
        return set()
    try:
        entries = json.loads(lock.read_text(encoding="utf-8")).get("files", [])
    except (OSError, json.JSONDecodeError):
        return set()
    return {Path(str(e.get("path", ""))).name for e in entries if isinstance(e, dict)}


def _load_official_validators(schema_dir: Path) -> dict[str, Any]:
    """Build jsonschema validators from NIST's published OSCAL schemas, if available."""
    try:
        import jsonschema
    except ImportError:
        return {}
    validators: dict[str, Any] = {}
    for kind in ("catalog", "profile"):
        path = schema_dir / f"oscal_{kind}_schema.json"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # Python's `re` has no Unicode property classes; approximate \p{L} and \p{N}.
        text = text.replace("\\\\p{L}", "[^\\\\W\\\\d_]").replace("\\\\p{N}", "\\\\d")
        schema = json.loads(text)
        validators[kind] = jsonschema.Draft7Validator(schema)
    return validators


def _official_errors(validator: Any, doc: dict[str, Any], limit: int = 10) -> list[str]:
    errs = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    out = [
        f"official schema: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message[:200]}"
        for e in errs[:limit]
    ]
    if len(errs) > limit:
        out.append(f"official schema: (+{len(errs) - limit} more)")
    return out


def validate_files(
    paths: Sequence[Path], data_dir: Path, *, schema_dir: Path | None = None, require_official: bool = False
) -> list[Finding]:
    findings: list[Finding] = []
    exempt = upstream_files(data_dir)
    validators = _load_official_validators(schema_dir) if schema_dir else {}
    if schema_dir and not validators:
        findings.append(
            Finding(
                str(schema_dir),
                "official OSCAL JSON schemas not found; structural checks only "
                "(run src/scripts/ingest_catalogs.py --schemas)",
                "error" if require_official else "warning",
            )
        )
    for path in paths:
        name = str(path)
        try:
            doc = load_document(path)
        except OscalValidationError as exc:
            findings.extend(Finding(name, e) for e in exc.errors)
            continue
        kind, errors = validate_document(doc, min_oscal_version=MIN_OSCAL_VERSION)
        if path.name in exempt:
            # Pinned upstream files may predate OSCAL 1.1 (for example FedRAMP's 1.0.4 baselines).
            old = [e for e in errors if e.startswith("metadata.oscal-version:")]
            errors = [e for e in errors if e not in old]
            if old:
                kind, floor_errors = validate_document(doc, min_oscal_version=UPSTREAM_FLOOR)
                errors = floor_errors
                findings.extend(
                    Finding(name, f"{e} (accepted: pinned upstream file in {LOCK_FILE})", "warning") for e in old
                )
        findings.extend(Finding(name, e) for e in errors)
        expected = _KIND_BY_DIR.get(path.parent.name)
        if expected and not errors and kind != expected:
            findings.append(Finding(name, f"{kind} document found in {path.parent.name}/ (expected {expected})"))
        official = validators.get(kind) if path.suffix.lower() == ".json" else None
        if official is not None:
            findings.extend(Finding(name, e) for e in _official_errors(official, doc))
    return findings


def _iter_control_ids(node: dict[str, Any]) -> Iterator[str]:
    for c in node.get("controls", []) or []:
        if isinstance(c, dict) and isinstance(c.get("id"), str):
            yield c["id"].lower()
            yield from _iter_control_ids(c)
    for g in node.get("groups", []) or []:
        if isinstance(g, dict):
            yield from _iter_control_ids(g)


def catalog_control_ids(data_dir: Path) -> tuple[dict[str, set[str]], list[Finding]]:
    ids: dict[str, set[str]] = {}
    findings: list[Finding] = []
    for key, path in discover_files(data_dir / "catalogs").items():
        try:
            doc = load_document(path)
        except OscalValidationError as exc:
            findings.extend(Finding(str(path), e) for e in exc.errors)
            continue
        body = doc.get("catalog")
        if not isinstance(body, dict):
            findings.append(Finding(str(path), "missing top-level 'catalog'"))
            continue
        ids[key] = set(_iter_control_ids(body))
    return ids, findings


def verify_crosswalks(data_dir: Path) -> list[Finding]:
    """Every edge must reference an existing control in an active catalog; relations must be consistent."""
    ids, findings = catalog_control_ids(data_dir)
    seen: dict[tuple[str, str, str], tuple[str, str]] = {}
    for key, path in discover_files(data_dir / "crosswalks").items():
        name = str(path)
        try:
            mc = parse_crosswalk(load_document(path))
        except OscalValidationError as exc:
            findings.extend(Finding(name, e) for e in exc.errors)
            continue
        findings.extend(Finding(name, e) for e in check_crosswalk_semantics(mc))
        for mapping in mc.mappings:
            for side, cat in (("source", mapping.source_resource.catalog), ("target", mapping.target_resource.catalog)):
                if cat not in ids:
                    findings.append(
                        Finding(name, f"{side}-resource catalog {cat!r} is not an active catalog (have {sorted(ids)})")
                    )
        for e in crosswalk_edges(mc):
            for cat, cid in ((e.src_catalog, e.src_id), (e.dst_catalog, e.dst_id)):
                if cat in ids and cid not in ids[cat]:
                    findings.append(
                        Finding(
                            name,
                            f"edge {e.src} -> {e.dst} ({e.relation}): control {cid!r} not found in catalog {cat!r}",
                        )
                    )
            ident = (e.src, e.dst, e.relation)
            prior = seen.get(ident)
            if prior and prior[0] != key:
                findings.append(
                    Finding(name, f"edge {e.src} -> {e.dst} ({e.relation}) also defined in {prior[1]}", "warning")
                )
            seen.setdefault(ident, (key, path.name))
    return findings
