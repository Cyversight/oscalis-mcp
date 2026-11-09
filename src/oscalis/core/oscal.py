"""OSCAL models, parsers, and validators.

Catalogs and profiles follow the NIST OSCAL JSON model (v1.x). Unknown keys are rejected on controls,
groups, parts, params, catalogs, and profiles so that non-OSCAL shapes (``control_id``, ``name`` and similar)
fail loudly instead of being silently ignored.

Crosswalks use a project-specific ``mapping-collection`` document modelled on the OSCAL mapping
collection (uuid, metadata, mappings with source/target resources and maps). It is NOT the official
OSCAL mapping-collection schema; see the README, Crosswalks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

UUID_PATTERN: Final = r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[45][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}$"
ID_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9._\-]*$"
_UUID_RE: Final = re.compile(UUID_PATTERN)
_VERSION_RE: Final = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")

RELATIONS: Final = (
    "crosswalk:equivalent",
    "crosswalk:satisfies",
    "crosswalk:related",
    "crosswalk:superset",
    "crosswalk:subset",
)
FILE_METHODS: Final = ("direct", "intersecting", "advisory")  # "transitive" is derived, never authored
# Allowed confidence range per method: direct 0.90-1.00, intersecting 0.50-0.70, advisory 0.20-0.40
CONFIDENCE_RANGES: Final[dict[str, tuple[float, float]]] = {
    "direct": (0.90, 1.00),
    "intersecting": (0.50, 0.70),
    "advisory": (0.20, 0.40),
}
MIN_OSCAL_VERSION: Final = (1, 1, 0)

DocKind = Literal["catalog", "profile", "mapping-collection"]

Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
Uuid = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]


class OscalValidationError(ValueError):
    """Raised when a document does not conform. ``errors`` holds one line per problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""))
        self.errors = errors


# --------------------------------------------------------------------------------------
# Pydantic models (strict about keys)
# --------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Metadata(_Open):
    title: str
    last_modified: str = Field(alias="last-modified")
    version: str
    oscal_version: str = Field(alias="oscal-version")


class Part(_Strict):
    id: Id | None = None
    name: str
    ns: str | None = None
    class_: str | None = Field(default=None, alias="class")
    title: str | None = None
    props: list[dict[str, Any]] | None = None
    prose: str | None = None
    parts: list[Part] | None = None
    links: list[dict[str, Any]] | None = None


class Param(_Strict):
    id: Id
    class_: str | None = Field(default=None, alias="class")
    depends_on: str | None = Field(default=None, alias="depends-on")
    props: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    label: str | None = None
    usage: str | None = None
    constraints: list[dict[str, Any]] | None = None
    guidelines: list[dict[str, Any]] | None = None
    values: list[str] | None = None
    select: dict[str, Any] | None = None
    remarks: str | None = None


class Control(_Strict):
    id: Id
    class_: str | None = Field(default=None, alias="class")
    title: str
    params: list[Param] | None = None
    props: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    parts: list[Part] | None = None
    controls: list[Control] | None = None


class Group(_Strict):
    id: Id | None = None
    class_: str | None = Field(default=None, alias="class")
    title: str
    params: list[Param] | None = None
    props: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    parts: list[Part] | None = None
    groups: list[Group] | None = None
    controls: list[Control] | None = None


class Catalog(_Strict):
    uuid: Uuid
    metadata: Metadata
    params: list[Param] | None = None
    controls: list[Control] | None = None
    groups: list[Group] | None = None
    back_matter: dict[str, Any] | None = Field(default=None, alias="back-matter")


class SelectControls(_Strict):
    with_child_controls: Literal["yes", "no"] | None = Field(default=None, alias="with-child-controls")
    with_ids: list[Id] | None = Field(default=None, alias="with-ids")
    matching: list[dict[str, Any]] | None = None


class ProfileImport(_Strict):
    href: str = Field(min_length=1)
    include_all: dict[str, Any] | None = Field(default=None, alias="include-all")
    include_controls: list[SelectControls] | None = Field(default=None, alias="include-controls")
    exclude_controls: list[SelectControls] | None = Field(default=None, alias="exclude-controls")


class SetParameter(_Strict):
    param_id: Id = Field(alias="param-id")
    class_: str | None = Field(default=None, alias="class")
    depends_on: str | None = Field(default=None, alias="depends-on")
    props: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    label: str | None = None
    usage: str | None = None
    constraints: list[dict[str, Any]] | None = None
    guidelines: list[dict[str, Any]] | None = None
    values: list[str] | None = None
    select: dict[str, Any] | None = None


class ProfileModify(_Strict):
    set_parameters: list[SetParameter] | None = Field(default=None, alias="set-parameters")
    alters: list[dict[str, Any]] | None = None


class Profile(_Strict):
    uuid: Uuid
    metadata: Metadata
    imports: list[ProfileImport] = Field(min_length=1)
    merge: dict[str, Any] | None = None
    modify: ProfileModify | None = None
    back_matter: dict[str, Any] | None = Field(default=None, alias="back-matter")


class ControlRef(_Strict):
    type: Literal["control"]
    id_ref: Id = Field(alias="id-ref")


class ResourceRef(_Strict):
    catalog: str = Field(min_length=1)


class Provenance(_Strict):
    document: str = Field(min_length=1)
    version: str = Field(min_length=1)
    href: str | None = None


class CrosswalkMap(_Strict):
    uuid: Uuid
    relationship: Literal[
        "crosswalk:equivalent",
        "crosswalk:satisfies",
        "crosswalk:related",
        "crosswalk:superset",
        "crosswalk:subset",
    ]
    method: Literal["direct", "intersecting", "advisory"]
    confidence: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]  # strict: true or "0.95" is not a number
    sources: list[ControlRef] = Field(min_length=1)
    targets: list[ControlRef] = Field(min_length=1)
    remarks: str | None = None


class CrosswalkMapping(_Strict):
    uuid: Uuid
    source_resource: ResourceRef = Field(alias="source-resource")
    target_resource: ResourceRef = Field(alias="target-resource")
    provenance: Provenance
    maps: list[CrosswalkMap] = Field(min_length=1)


class MappingCollection(_Strict):
    uuid: Uuid
    metadata: Metadata
    mappings: list[CrosswalkMapping] = Field(min_length=1)


# --------------------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------------------


def load_document(path: Path) -> dict[str, Any]:
    """Load a JSON or YAML document. Raises ``OscalValidationError`` on parse failure."""
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate a BOM written by Windows editors
        if suffix == ".json":
            data = json.loads(text)
        elif suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            raise OscalValidationError([f"{path.name}: unsupported extension {suffix!r} (use .json/.yaml/.yml)"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise OscalValidationError([f"{path.name}: cannot parse: {exc}"]) from exc
    if not isinstance(data, dict):
        raise OscalValidationError([f"{path.name}: top level must be an object"])
    return data


def detect_kind(doc: dict[str, Any]) -> DocKind:
    kinds = [k for k in ("catalog", "profile", "mapping-collection") if k in doc]
    if len(kinds) != 1 or len(doc) != 1:
        raise OscalValidationError(
            [f"top level must contain exactly one of catalog|profile|mapping-collection, found {sorted(doc)}"]
        )
    kind: DocKind = kinds[0]
    return kind


def _fmt(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        out.append(f"{loc}: {err['msg']}")
    return out


def parse_catalog(doc: dict[str, Any]) -> Catalog:
    try:
        return Catalog.model_validate(doc["catalog"])
    except KeyError as exc:
        raise OscalValidationError(["missing top-level 'catalog'"]) from exc
    except ValidationError as exc:
        raise OscalValidationError(_fmt(exc)) from exc


def parse_profile(doc: dict[str, Any]) -> Profile:
    try:
        return Profile.model_validate(doc["profile"])
    except KeyError as exc:
        raise OscalValidationError(["missing top-level 'profile'"]) from exc
    except ValidationError as exc:
        raise OscalValidationError(_fmt(exc)) from exc


def parse_crosswalk(doc: dict[str, Any]) -> MappingCollection:
    try:
        return MappingCollection.model_validate(doc["mapping-collection"])
    except KeyError as exc:
        raise OscalValidationError(["missing top-level 'mapping-collection'"]) from exc
    except ValidationError as exc:
        raise OscalValidationError(_fmt(exc)) from exc


def parse_version(value: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match(value.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _walk_uuids(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "uuid":
                yield f"{path}.uuid" if path else "uuid", v
            else:
                yield from _walk_uuids(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_uuids(v, f"{path}[{i}]")


def _check_uuids(body: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for where, value in _walk_uuids(body):
        if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
            errors.append(f"{where}: invalid UUID {value!r} (must be RFC 4122 v4/v5)")
            continue
        key = value.lower()
        if key in seen:
            errors.append(f"{where}: duplicate UUID {value} (first seen at {seen[key]})")
        else:
            seen[key] = where
    return errors


def _check_back_matter(body: dict[str, Any]) -> list[str]:
    bm = body.get("back-matter")
    if bm is None:
        return []
    errors: list[str] = []
    resources = bm.get("resources", []) if isinstance(bm, dict) else []
    for i, res in enumerate(resources):
        if not isinstance(res, dict) or "uuid" not in res:
            errors.append(f"back-matter.resources[{i}]: missing required uuid")
    return errors


def _check_version(meta: Metadata, floor: tuple[int, int, int] | None) -> list[str]:
    if floor is None:
        return []
    parsed = parse_version(meta.oscal_version)
    if parsed is None:
        return [f"metadata.oscal-version: cannot parse {meta.oscal_version!r}"]
    if parsed < floor:
        want = ".".join(map(str, floor))
        return [f"metadata.oscal-version: {meta.oscal_version} is below required minimum {want}"]
    return []


@dataclass(frozen=True)
class FlatControl:
    control_id: str  # lowercased OSCAL id
    title: str
    class_: str | None
    parent_id: str | None
    group_id: str | None
    depth: int
    prose: str
    guidance: str
    oscal: dict[str, Any]  # control JSON without nested controls
    enhancement_ids: tuple[str, ...]
    status: str | None = None  # the OSCAL "status" prop, for example "withdrawn"
    # where a withdrawn control went: (rel, control id), rel is "incorporated-into" or "moved-to"
    successors: tuple[tuple[str, str], ...] = ()


_SUCCESSOR_RELS: Final = ("incorporated-into", "moved-to")


def control_status(c: Control) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """A control's status prop (lowercased) and the controls a withdrawn control was incorporated into or moved to."""
    status = next((str(p.get("value", "")).strip().lower() for p in c.props or [] if p.get("name") == "status"), None)
    successors = tuple(
        (str(link["rel"]), str(link.get("href", ""))[1:].lower())
        for link in c.links or []
        if link.get("rel") in _SUCCESSOR_RELS and str(link.get("href", "")).startswith("#")
    )
    return status or None, successors


@dataclass(frozen=True)
class FlatParam:
    param_id: str
    control_id: str | None
    label: str | None
    values: tuple[str, ...]
    # A legacy parameter (for example `at-2_prm_1`) stands for several assessment-level ones (`at-02_odp.01`, `.02`).
    # NIST records this as `aggregates` props. Older profiles such as FedRAMP's OSCAL 1.0.4 files set the legacy id.
    aggregates: tuple[str, ...] = ()
    # Parameters inserted inside this parameter's selection choices, for example PE-14's "[Assignment: environmental
    # control]" inside its selection. Profiles put the same value on either one.
    nested: tuple[str, ...] = ()


def _aggregates(param: Param) -> tuple[str, ...]:
    return tuple(str(p.get("value", "")) for p in param.props or [] if p.get("name") == "aggregates" and p.get("value"))


def _nested(param: Param) -> tuple[str, ...]:
    choices = (param.select or {}).get("choice") or []
    return tuple(dict.fromkeys(m for choice in choices for m in _INSERT_RE.findall(str(choice))))


@dataclass
class FlatCatalog:
    controls: list[FlatControl] = field(default_factory=list)
    params: list[FlatParam] = field(default_factory=list)


def _iter_controls(
    controls: list[Control] | None, group_id: str | None, parent: Control | None, depth: int
) -> Iterator[tuple[Control, str | None, Control | None, int]]:
    for c in controls or []:
        yield c, group_id, parent, depth
        yield from _iter_controls(c.controls, group_id, c, depth + 1)


def _iter_groups(groups: list[Group] | None, inherited: str | None) -> Iterator[tuple[Group, str | None]]:
    for g in groups or []:
        gid = g.id if g.id else inherited
        yield g, gid
        yield from _iter_groups(g.groups, gid)


def _collect_params(cat: Catalog) -> dict[str, Param]:
    out: dict[str, Param] = {p.id: p for p in cat.params or []}
    for g, _ in _iter_groups(cat.groups, None):
        out.update({p.id: p for p in g.params or []})
    for c, *_ in _iter_all_controls(cat):
        out.update({p.id: p for p in c.params or []})
    return out


def _iter_all_controls(cat: Catalog) -> Iterator[tuple[Control, str | None, Control | None, int]]:
    yield from _iter_controls(cat.controls, None, None, 0)
    for g, gid in _iter_groups(cat.groups, None):
        yield from _iter_controls(g.controls, gid, None, 0)


def iter_controls(cat: Catalog) -> Iterator[tuple[Control, str | None, Control | None, int]]:
    """Every control in document order as (control, group id, parent control, depth)."""
    yield from _iter_all_controls(cat)


def collect_params(cat: Catalog) -> dict[str, Param]:
    """All parameters declared anywhere in the catalog, keyed by id."""
    return _collect_params(cat)


_INSERT_RE: Final = re.compile(r"\{\{\s*insert:\s*param,\s*([A-Za-z0-9._\-]+)\s*\}\}")


def render_param(
    param: Param | None, param_id: str, params: dict[str, Param] | None = None, _seen: frozenset[str] = frozenset()
) -> str:
    """Human-readable placeholder for a parameter used inside prose.

    With `params`, parameters inserted inside selection choices (for example AC-11's "after
    {{ insert: param, ac-11_odp.02 }} of inactivity") are rendered too. Without it, choices are left as written.
    """
    if param is None:
        return f"[parameter {param_id}]"
    if param.values:
        return ", ".join(param.values)
    if param.select:
        choices = [str(c) for c in param.select.get("choice", [])]
        if params is not None:
            choices = [_render(c, params, _seen | {param_id}) for c in choices]
        how = param.select.get("how-many", "one")
        return f"[Selection ({how.replace('-', ' ')}): " + "; ".join(choices) + "]"
    if param.label:
        return f"[Assignment: {param.label}]"
    return f"[parameter {param_id}]"


def _render(text: str, params: dict[str, Param], seen: frozenset[str]) -> str:
    def one(m: re.Match[str]) -> str:
        if m[1] in seen:  # a parameter inserted inside itself: stop instead of recursing
            return f"[parameter {m[1]}]"
        return render_param(params.get(m[1]), m[1], params, seen)

    return _INSERT_RE.sub(one, text)


def render_text(text: str, params: dict[str, Param]) -> str:
    return _render(text, params, frozenset())


def _part_label(part: Part) -> str:
    for prop in part.props or []:
        if prop.get("name") == "label":
            return str(prop.get("value", ""))
    return ""


def _flatten_part(part: Part, params: dict[str, Param], depth: int, out: list[str]) -> None:
    if part.prose:
        label = _part_label(part)
        line = (f"{label} " if label else "") + render_text(part.prose.strip(), params)
        out.append("  " * depth + line)
    for sub in part.parts or []:
        _flatten_part(sub, params, depth + 1 if part.prose else depth, out)


def _flatten_named(
    parts: list[Part] | None, name: str, params: dict[str, Param], *, exclude_class: str | None = None
) -> str:
    lines: list[str] = []
    for part in parts or []:
        if part.name == name and (exclude_class is None or part.class_ != exclude_class):
            _flatten_part(part, params, 0, lines)
    return "\n".join(lines)


# NIST's AI RMF catalog puts each subcategory's official outcome in an `overview` part, and files the AI RMF Playbook's
# suggested actions as a `statement` part with class `suggested_action`. Those actions are advice, not the requirement.
SUGGESTED_ACTION: Final = "suggested_action"


def control_prose(parts: list[Part] | None, params: dict[str, Param]) -> str:
    """The control's own requirement text.

    The `statement` part, except one classed as suggested actions. A control with no other statement (the AI RMF
    subcategories) uses its `overview` part, which is where NIST put the outcome.
    """
    prose = _flatten_named(parts, "statement", params, exclude_class=SUGGESTED_ACTION)
    return prose or _flatten_named(parts, "overview", params)


def control_guidance(parts: list[Part] | None, params: dict[str, Param]) -> str:
    """Guidance parts, followed by any suggested actions under a heading that says what they are."""
    guidance = _flatten_named(parts, "guidance", params)
    actions = "\n".join(
        _flatten_named([p], "statement", params)
        for p in parts or []
        if p.name == "statement" and p.class_ == SUGGESTED_ACTION
    )
    if not actions:
        return guidance
    block = "Suggested actions:\n" + "\n".join(f"- {line}" for line in actions.splitlines() if line.strip())
    return f"{guidance}\n{block}" if guidance else block


def flatten_catalog(cat: Catalog) -> FlatCatalog:
    """Flatten nested groups/controls into rows. Control ids are lowercased."""
    params = _collect_params(cat)
    flat = FlatCatalog()
    seen: set[str] = set()
    flat.params.extend(
        FlatParam(p.id, None, p.label, tuple(p.values or []), _aggregates(p), _nested(p)) for p in cat.params or []
    )
    children: dict[str, list[str]] = {}
    ordered = list(_iter_all_controls(cat))
    for c, _gid, parent, _depth in ordered:
        if parent is not None:
            children.setdefault(parent.id.lower(), []).append(c.id.lower())
    for c, gid, parent, depth in ordered:
        cid = c.id.lower()
        if cid in seen:
            raise OscalValidationError([f"controls: duplicate control id {c.id!r}"])
        seen.add(cid)
        status, successors = control_status(c)
        flat.controls.append(
            FlatControl(
                control_id=cid,
                title=c.title,
                class_=c.class_,
                parent_id=parent.id.lower() if parent else None,
                group_id=gid,
                depth=depth,
                prose=control_prose(c.parts, params),
                guidance=control_guidance(c.parts, params),
                oscal=c.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"controls"}),
                enhancement_ids=tuple(children.get(cid, ())),
                status=status,
                successors=successors,
            )
        )
        flat.params.extend(
            FlatParam(p.id, cid, p.label, tuple(p.values or []), _aggregates(p), _nested(p)) for p in c.params or []
        )
    return flat


def _part_ids(parts: list[Part] | None) -> Iterator[str]:
    for part in parts or []:
        if part.id:
            yield part.id.lower()
        yield from _part_ids(part.parts)


def _check_catalog_semantics(cat: Catalog) -> list[str]:
    errors: list[str] = []
    if not cat.controls and not cat.groups:
        errors.append("catalog: must contain at least one of 'groups' or 'controls'")
    seen: dict[str, str] = {}
    for c, *_ in _iter_all_controls(cat):
        key = c.id.lower()
        if key in seen:
            errors.append(f"controls: duplicate control id {c.id!r}")
        seen[key] = c.id
    known: set[str] = set(seen)
    for g, _ in _iter_groups(cat.groups, None):
        if g.id:
            known.add(g.id.lower())
        known.update(_part_ids(g.parts))
    for c, *_ in _iter_all_controls(cat):
        known.update(_part_ids(c.parts))
    known.update(p.lower() for p in _collect_params(cat))
    res = (cat.back_matter or {}).get("resources", [])
    known.update(str(r.get("uuid", "")).lower() for r in res if isinstance(r, dict))
    for c, *_ in _iter_all_controls(cat):
        for link in c.links or []:
            href = str(link.get("href", ""))
            if href.startswith("#") and href[1:].lower() not in known:
                errors.append(f"control {c.id}: link references unknown id {href!r}")
    return errors


def _check_profile_semantics(prof: Profile) -> list[str]:
    errors: list[str] = []
    resources = {
        str(r.get("uuid", "")).lower() for r in (prof.back_matter or {}).get("resources", []) if isinstance(r, dict)
    }
    for i, imp in enumerate(prof.imports):
        if imp.href.startswith("#") and imp.href[1:].lower() not in resources:
            errors.append(f"imports[{i}].href: {imp.href!r} does not match a back-matter resource uuid")
        if imp.include_all is None and not imp.include_controls:
            errors.append(f"imports[{i}]: needs include-all or include-controls")
    return errors


def _check_crosswalk_semantics(mc: MappingCollection) -> list[str]:
    errors: list[str] = []
    for i, mapping in enumerate(mc.mappings):
        if mapping.source_resource.catalog == mapping.target_resource.catalog:
            errors.append(f"mappings[{i}]: source and target catalog are identical")
        for j, m in enumerate(mapping.maps):
            lo, hi = CONFIDENCE_RANGES[m.method]
            if not lo <= m.confidence <= hi:
                errors.append(
                    f"mappings[{i}].maps[{j}]: confidence {m.confidence} outside {m.method} range {lo:.2f}-{hi:.2f}"
                )
            if m.method == "direct" and not mapping.provenance.href:
                errors.append(f"mappings[{i}].maps[{j}]: 'direct' mappings require provenance.href")
    seen: set[tuple[str, str, str]] = set()
    for e in crosswalk_edges(mc):
        ident = (e.src, e.dst, e.relation)
        if ident in seen:
            errors.append(f"edge {e.src} -> {e.dst} ({e.relation}) is defined more than once in this file")
        seen.add(ident)
    return errors


def check_crosswalk_semantics(mc: MappingCollection) -> list[str]:
    """Rules a parsed crosswalk must meet beyond its structure."""
    return _check_crosswalk_semantics(mc)


def validate_document(
    doc: dict[str, Any], *, min_oscal_version: tuple[int, int, int] | None = MIN_OSCAL_VERSION
) -> tuple[DocKind, list[str]]:
    """Validate a document. Returns (kind, errors). An empty list means conformant."""
    try:
        kind = detect_kind(doc)
    except OscalValidationError as exc:
        return "catalog", exc.errors
    body = doc[kind]
    errors: list[str] = []
    if isinstance(body, dict):
        errors.extend(_check_uuids(body))
        errors.extend(_check_back_matter(body))
    try:
        if kind == "catalog":
            cat = parse_catalog(doc)
            errors.extend(_check_version(cat.metadata, min_oscal_version))
            errors.extend(_check_catalog_semantics(cat))
        elif kind == "profile":
            prof = parse_profile(doc)
            errors.extend(_check_version(prof.metadata, min_oscal_version))
            errors.extend(_check_profile_semantics(prof))
        else:
            mc = parse_crosswalk(doc)
            errors.extend(_check_version(mc.metadata, min_oscal_version))
            errors.extend(_check_crosswalk_semantics(mc))
    except OscalValidationError as exc:
        errors.extend(exc.errors)
    return kind, errors


# --------------------------------------------------------------------------------------
# Crosswalk edge extraction
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeSpec:
    src_catalog: str
    src_id: str
    dst_catalog: str
    dst_id: str
    relation: str
    confidence: float
    method: str
    source: str
    rationale: str
    map_uuid: str

    @property
    def src(self) -> str:
        return f"{self.src_catalog}:{self.src_id}"

    @property
    def dst(self) -> str:
        return f"{self.dst_catalog}:{self.dst_id}"


def crosswalk_edges(mc: MappingCollection) -> list[EdgeSpec]:
    """Expand each map's sources x targets into directed edges (ids lowercased)."""
    edges: list[EdgeSpec] = []
    for mapping in mc.mappings:
        prov = mapping.provenance
        source = f"{prov.document} {prov.version}" + (f" <{prov.href}>" if prov.href else "")
        for m in mapping.maps:
            for s in m.sources:
                for t in m.targets:
                    edges.append(
                        EdgeSpec(
                            src_catalog=mapping.source_resource.catalog,
                            src_id=s.id_ref.lower(),
                            dst_catalog=mapping.target_resource.catalog,
                            dst_id=t.id_ref.lower(),
                            relation=m.relationship,
                            confidence=m.confidence,
                            method=m.method,
                            source=source,
                            rationale=m.remarks or "",
                            map_uuid=m.uuid,
                        )
                    )
    return edges
