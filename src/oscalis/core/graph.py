"""SQLite-backed index of catalogs, resolved profiles, and the directed crosswalk graph.

The index is built once at startup and is read-only afterwards (``PRAGMA query_only``).
All SQL is parameterized; user text reaching FTS5 is reduced to quoted alphanumeric tokens.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from oscalis.core.config import Settings
from oscalis.core.oscal import (
    EdgeSpec,
    FlatCatalog,
    OscalValidationError,
    Param,
    Profile,
    SelectControls,
    crosswalk_edges,
    flatten_catalog,
    load_document,
    parse_catalog,
    parse_crosswalk,
    parse_profile,
    render_param,
    validate_document,
)

log = logging.getLogger("oscalis.graph")

_SCHEMA: Final = """
CREATE TABLE catalogs (
    key TEXT PRIMARY KEY, title TEXT NOT NULL, version TEXT NOT NULL,
    oscal_version TEXT NOT NULL, uuid TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE controls (
    catalog TEXT NOT NULL REFERENCES catalogs(key),
    control_id TEXT NOT NULL,
    title TEXT NOT NULL,
    control_class TEXT,
    parent_id TEXT,
    group_id TEXT,
    depth INTEGER NOT NULL,
    prose TEXT NOT NULL,
    guidance TEXT NOT NULL,
    enhancement_ids TEXT NOT NULL,
    oscal_json TEXT NOT NULL,
    status TEXT,
    successors_json TEXT NOT NULL,
    PRIMARY KEY (catalog, control_id)
);
CREATE INDEX idx_controls_parent ON controls(catalog, parent_id);
CREATE TABLE params (
    catalog TEXT NOT NULL, param_id TEXT NOT NULL, control_id TEXT, label TEXT, values_json TEXT NOT NULL,
    aggregates_json TEXT NOT NULL DEFAULT '[]',
    nested_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (catalog, param_id)
);
CREATE INDEX idx_params_id ON params(param_id);
CREATE TABLE profiles (
    key TEXT PRIMARY KEY, title TEXT NOT NULL, version TEXT NOT NULL, oscal_version TEXT NOT NULL,
    uuid TEXT NOT NULL, source TEXT NOT NULL, doc_json TEXT NOT NULL
);
CREATE TABLE profile_controls (
    profile TEXT NOT NULL REFERENCES profiles(key), catalog TEXT NOT NULL, control_id TEXT NOT NULL,
    PRIMARY KEY (profile, catalog, control_id)
);
CREATE INDEX idx_profile_controls_ctrl ON profile_controls(catalog, control_id);
CREATE TABLE profile_params (
    profile TEXT NOT NULL REFERENCES profiles(key), catalog TEXT NOT NULL, param_id TEXT NOT NULL,
    control_id TEXT, values_json TEXT NOT NULL, constraints_json TEXT NOT NULL,
    PRIMARY KEY (profile, catalog, param_id)
);
CREATE INDEX idx_profile_params_ctrl ON profile_params(catalog, control_id);
CREATE TABLE profile_notes (
    profile TEXT NOT NULL REFERENCES profiles(key), catalog TEXT NOT NULL, control_id TEXT NOT NULL,
    name TEXT NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY (profile, catalog, control_id, name, locator)
);
CREATE TABLE edges (
    src TEXT NOT NULL, dst TEXT NOT NULL, relation TEXT NOT NULL,
    confidence REAL NOT NULL, method TEXT NOT NULL, source TEXT NOT NULL, rationale TEXT NOT NULL,
    derived INTEGER NOT NULL DEFAULT 0, map_uuid TEXT NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
CREATE VIRTUAL TABLE controls_fts USING fts5(
    catalog UNINDEXED, control_id UNINDEXED, title, prose, guidance, tokenize = 'unicode61'
);
"""

# Inverse relation used when deriving reverse edges at load time. `satisfies` is deliberately absent.
_INVERSE: Final[dict[str, str]] = {
    "crosswalk:equivalent": "crosswalk:equivalent",
    "crosswalk:related": "crosswalk:related",
    "crosswalk:superset": "crosswalk:subset",
    "crosswalk:subset": "crosswalk:superset",
}

# Relation composition table: a composed path never claims more than its weakest hop
_COMPOSE: Final[dict[tuple[str, str], str]] = {
    ("equivalent", "equivalent"): "equivalent",
    ("equivalent", "satisfies"): "satisfies",
    ("equivalent", "superset"): "superset",
    ("equivalent", "subset"): "subset",
    ("equivalent", "related"): "related",
    ("satisfies", "equivalent"): "satisfies",
    ("satisfies", "satisfies"): "satisfies",
    ("satisfies", "superset"): "satisfies",
    ("satisfies", "subset"): "related",
    ("satisfies", "related"): "related",
    ("superset", "equivalent"): "superset",
    ("superset", "satisfies"): "related",
    ("superset", "superset"): "superset",
    ("superset", "subset"): "related",
    ("superset", "related"): "related",
    ("subset", "equivalent"): "subset",
    ("subset", "satisfies"): "related",
    ("subset", "superset"): "related",
    ("subset", "subset"): "subset",
    ("subset", "related"): "related",
    ("related", "equivalent"): "related",
    ("related", "satisfies"): "related",
    ("related", "superset"): "related",
    ("related", "subset"): "related",
    ("related", "related"): "related",
}
_METHOD_STRENGTH: Final = {"advisory": 0, "intersecting": 1, "direct": 2}
TRANSITIVE_CAP: Final = 0.80
HOP_DECAY: Final = 0.9

CAVEATS: Final[dict[str, str]] = {
    "intersecting": "Partial overlap only. Not evidence that the target requirement is met.",
    "advisory": "Analyst judgement or topical match. Not an authoritative mapping and not compliance evidence.",
}

_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9]+")


class UnknownKeyError(ValueError):
    """Unknown catalog/profile/control key. The message lists valid alternatives."""


@dataclass(frozen=True)
class Hop:
    src: str
    relation: str
    dst: str
    confidence: float
    method: str
    source: str
    derived: bool


@dataclass(frozen=True)
class PathResult:
    node: str
    hops: tuple[Hop, ...]
    relation: str  # composed relation, with "crosswalk:" prefix
    confidence: float
    method: str


def _short(relation: str) -> str:
    return relation.removeprefix("crosswalk:")


def score_path(hops: Sequence[Hop]) -> tuple[str, float, str]:
    """Return (relation, confidence, method) for a path, following the crosswalk rules."""
    n = len(hops)
    rel = _short(hops[0].relation)
    for hop in hops[1:]:
        rel = _COMPOSE[(rel, _short(hop.relation))]
    weakest = min((h.method for h in hops), key=lambda m: _METHOD_STRENGTH[m])
    if n == 1:
        method = weakest
    else:
        method = "transitive" if weakest == "direct" else weakest
        # rule 7: multi-hop equivalence needs every hop to be direct and equivalent
        if rel == "equivalent" and not all(h.method == "direct" and _short(h.relation) == "equivalent" for h in hops):
            rel = "related"
    conf = 1.0
    for h in hops:
        conf *= h.confidence
    conf *= HOP_DECAY ** (n - 1)
    if method == "transitive":
        conf = min(conf, TRANSITIVE_CAP)
    return f"crosswalk:{rel}", round(conf, 2), method


@dataclass(frozen=True)
class _ParamValue:
    control_id: str | None
    values: tuple[str, ...]
    constraints: tuple[str, ...]


@dataclass
class _Resolved:
    members: set[tuple[str, str]]
    params: dict[tuple[str, str], _ParamValue]
    # per-control text added by a profile's lters: (catalog, control id) -> {(name, locator): text}
    notes: dict[tuple[str, str], dict[tuple[str, str], str]]


def _part_text(part: dict[str, Any]) -> str:
    chunks = [str(part.get("prose", "")).strip()]
    chunks.extend(_part_text(sub) for sub in part.get("parts", []) or [])
    return "\n".join(c for c in chunks if c)


def normalize_id(value: str) -> str:
    return value.strip().lower()


def discover_files(directory: Path) -> dict[str, Path]:
    """Map key (file stem) -> file for every JSON/YAML document in ``directory``."""
    found: dict[str, Path] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".json", ".yaml", ".yml") or not path.is_file():
            continue
        if path.stem in found:
            raise OscalValidationError([f"{directory.name}/: duplicate key {path.stem!r} ({path.name})"])
        found[path.stem] = path
    return found


def fts_expression(query: str) -> str:
    tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
    if not tokens:
        raise ValueError("search query must contain at least one letter or digit")
    return " ".join(f'"{t}"*' for t in tokens[:12])


class SqliteFeatureError(RuntimeError):
    """The Python build's SQLite lacks a feature Oscalis needs. The message says how to check and what to do."""


def require_fts5(conn: sqlite3.Connection) -> None:
    """Fail early, with advice, when this Python's SQLite was built without FTS5 (full-text search)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.oscalis_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.oscalis_fts5_probe")
    except sqlite3.OperationalError as exc:
        raise SqliteFeatureError(
            f"this Python's SQLite ({sqlite3.sqlite_version}) was built without FTS5, which Oscalis needs for "
            "search. Use a standard Python build (python.org, uv-managed, Homebrew, or the Debian/Ubuntu/Fedora "
            "package) instead of a minimal or custom one."
        ) from exc


class OscalGraph:
    """Read-only query surface over the loaded OSCAL data."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        db_target = ":memory:"
        if settings.db_path is not None:
            settings.db_path.parent.mkdir(parents=True, exist_ok=True)
            settings.db_path.unlink(missing_ok=True)
            db_target = str(settings.db_path)
        self._conn = sqlite3.connect(db_target, check_same_thread=False)
        require_fts5(self._conn)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._children: dict[tuple[str, str], list[str]] | None = None
        self._profile_docs: dict[str, Profile] = {}
        self._profile_files: dict[str, Path] = {}
        self._resolved: dict[str, _Resolved] = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, settings: Settings | None = None) -> OscalGraph:
        graph = cls(settings or Settings.from_env())
        graph._build()
        return graph

    def _build(self) -> None:
        s = self.settings
        with self._lock:
            self._conn.executescript(_SCHEMA)
            catalog_files = discover_files(s.catalogs_dir)
            profile_files = discover_files(s.profiles_dir)
            overlap = set(catalog_files) & set(profile_files)
            if overlap:
                raise OscalValidationError([f"keys used by both a catalog and a profile: {sorted(overlap)}"])
            for key, path in catalog_files.items():
                self._load_catalog(key, path)
            self._profile_files = profile_files
            for key, path in profile_files.items():
                self._profile_docs[key] = parse_profile(load_document(path))
            for key in profile_files:
                self._materialize_profile(key)
            self._load_crosswalks(discover_files(s.crosswalks_dir))
            self._conn.execute("ANALYZE")
            self._conn.commit()
            self._conn.execute("PRAGMA query_only = ON")  # index is read-only from here on
        log.info(
            "loaded %d catalogs, %d profiles, %d edges",
            len(catalog_files),
            len(profile_files),
            self._scalar("SELECT count(*) FROM edges"),
        )

    def _load_catalog(self, key: str, path: Path) -> None:
        try:
            cat = parse_catalog(load_document(path))
            flat: FlatCatalog = flatten_catalog(cat)
        except OscalValidationError as exc:
            raise OscalValidationError([f"{path.name}: {e}" for e in exc.errors]) from exc
        c = self._conn
        c.execute(
            "INSERT INTO catalogs VALUES (?,?,?,?,?,?)",
            (key, cat.metadata.title, cat.metadata.version, cat.metadata.oscal_version, cat.uuid, path.name),
        )
        c.executemany(
            "INSERT INTO controls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    key,
                    fc.control_id,
                    fc.title,
                    fc.class_,
                    fc.parent_id,
                    fc.group_id,
                    fc.depth,
                    fc.prose,
                    fc.guidance,
                    json.dumps(list(fc.enhancement_ids)),
                    json.dumps(fc.oscal, separators=(",", ":")),
                    fc.status,
                    json.dumps([list(s) for s in fc.successors]),
                )
                for fc in flat.controls
            ],
        )
        c.executemany(
            "INSERT OR REPLACE INTO params VALUES (?,?,?,?,?,?,?)",
            [
                (
                    key,
                    p.param_id,
                    p.control_id,
                    p.label,
                    json.dumps(list(p.values)),
                    json.dumps(list(p.aggregates)),
                    json.dumps(list(p.nested)),
                )
                for p in flat.params
            ],
        )
        c.executemany(
            "INSERT INTO controls_fts(catalog, control_id, title, prose, guidance) VALUES (?,?,?,?,?)",
            [(key, fc.control_id, fc.title, fc.prose, fc.guidance) for fc in flat.controls],
        )

    # --------------------------------------------------------- profile resolution

    def _children_map(self) -> dict[tuple[str, str], list[str]]:
        if self._children is None:
            m: dict[tuple[str, str], list[str]] = {}
            for r in self._all("SELECT catalog, control_id, parent_id FROM controls WHERE parent_id IS NOT NULL"):
                m.setdefault((r["catalog"], r["parent_id"]), []).append(r["control_id"])
            self._children = m
        return self._children

    def _descendants(self, catalog: str, control_id: str) -> Iterable[str]:
        stack = list(self._children_map().get((catalog, control_id), ()))
        while stack:
            cid = stack.pop()
            yield cid
            stack.extend(self._children_map().get((catalog, cid), ()))

    def _classify_href(self, profile_key: str, href: str) -> tuple[str, str]:
        """Resolve an import href to ('catalog'|'profile', key)."""
        prof = self._profile_docs[profile_key]
        candidates: list[str] = []
        if href.startswith("#"):
            wanted = href[1:].lower()
            for res in (prof.back_matter or {}).get("resources", []):
                if str(res.get("uuid", "")).lower() == wanted:
                    candidates.extend(str(r.get("href", "")) for r in res.get("rlinks", []))
        else:
            candidates.append(href)
        catalogs = set(self._catalog_keys())
        for cand in candidates:
            name = PurePosixPath(cand.split("#")[0].split("?")[0]).name
            if not name:
                continue
            target = self.settings.href_aliases.get(name)
            if target is None:
                target = name.rsplit(".", 1)[0]
            if target in catalogs:
                return "catalog", target
            if target in self._profile_docs:
                return "profile", target
        raise OscalValidationError(
            [
                f"profile {profile_key}: import {href!r} does not resolve to a loaded catalog or profile "
                f"(candidates: {candidates or 'none'}; catalogs: {sorted(catalogs)}). "
                "Run src/scripts/ingest_catalogs.py or extend href_aliases."
            ]
        )

    def _select(self, universe: set[tuple[str, str]], sels: list[SelectControls]) -> set[tuple[str, str]]:
        """Controls chosen by ``with-ids`` and/or ``matching`` globs. Each selector is evaluated on its own.

        ``with-child-controls: yes`` adds the descendants of only the controls that selector chose. A control chosen by
        an earlier selector that said ``no`` keeps its enhancements out.
        """
        chosen: set[tuple[str, str]] = set()
        known = {m[1] for m in universe}
        for sel in sels:
            ids = {i.lower() for i in sel.with_ids or []}
            patterns = []
            for m in sel.matching or []:
                pattern = m.get("pattern")
                if not isinstance(pattern, str) or not pattern.strip():
                    raise OscalValidationError(["a 'matching' selector needs a non-empty 'pattern'"])
                patterns.append(pattern.strip().lower())
            picked = {
                member
                for member in universe
                if member[1] in ids or any(fnmatch.fnmatchcase(member[1], p) for p in patterns)
            }
            if sel.with_child_controls == "yes":
                for cat, cid in list(picked):
                    picked.update((cat, d) for d in self._descendants(cat, cid) if (cat, d) in universe)
            chosen |= picked
            missing = ids - known
            if missing:
                self.warnings.append(f"selector references {len(missing)} ids not in source: {sorted(missing)[:5]}")
            for p in patterns:
                if not any(fnmatch.fnmatchcase(k, p) for k in known):
                    self.warnings.append(f"selector pattern {p!r} matches no control in source")
        return chosen

    def _resolve_profile(self, key: str, stack: tuple[str, ...] = ()) -> _Resolved:
        if key in self._resolved:
            return self._resolved[key]
        if key in stack:
            raise OscalValidationError([f"profile import cycle: {' -> '.join((*stack, key))}"])
        prof = self._profile_docs[key]
        members: set[tuple[str, str]] = set()
        params: dict[tuple[str, str], _ParamValue] = {}
        notes: dict[tuple[str, str], dict[tuple[str, str], str]] = {}
        for imp in prof.imports:
            kind, target = self._classify_href(key, imp.href)
            inherited: dict[tuple[str, str], _ParamValue] = {}
            inherited_notes: dict[tuple[str, str], dict[tuple[str, str], str]] = {}
            if kind == "catalog":
                universe = {
                    (target, r["control_id"])
                    for r in self._all("SELECT control_id FROM controls WHERE catalog = ?", (target,))
                }
            else:
                sub = self._resolve_profile(target, (*stack, key))
                universe, inherited, inherited_notes = set(sub.members), sub.params, sub.notes
            selected = (
                set(universe) if imp.include_all is not None else self._select(universe, imp.include_controls or [])
            )
            if imp.exclude_controls:
                selected -= self._select(universe, imp.exclude_controls)
            members |= selected
            for pk, pv in inherited.items():
                owner = (pk[0], pv.control_id) if pv.control_id else None
                if owner is None or owner in selected:
                    params[pk] = pv
            for node, entries in inherited_notes.items():
                if node in selected:
                    notes.setdefault(node, {}).update(entries)
        for sp in (prof.modify.set_parameters if prof.modify else None) or []:
            constraints = tuple(str(c.get("description", "")) for c in sp.constraints or [])
            if sp.select:
                constraints += (render_param(Param(id=sp.param_id, select=sp.select), sp.param_id),)
            hits = self._all("SELECT catalog, control_id FROM params WHERE param_id = ?", (sp.param_id,))
            for hit in hits:
                if (hit["catalog"], hit["control_id"]) in members:
                    params[(hit["catalog"], sp.param_id)] = _ParamValue(
                        hit["control_id"], tuple(sp.values or ()), constraints
                    )
        self._apply_alters(prof, members, notes)
        resolved = _Resolved(members, params, notes)
        self._resolved[key] = resolved
        return resolved

    def _apply_alters(
        self,
        prof: Profile,
        members: set[tuple[str, str]],
        notes: dict[tuple[str, str], dict[tuple[str, str], str]],
    ) -> None:
        """Collect text parts added by ``modify.alters[].adds[].parts`` as per-control notes.

        Only added parts that carry prose are used. Anything else in an alter (removes, props, links,
        positioned inserts into the control text) is preserved in the stored document but not applied.
        """
        by_id: dict[str, list[tuple[str, str]]] = {}
        for cat, cid in members:
            by_id.setdefault(cid, []).append((cat, cid))
        for alter in (prof.modify.alters if prof.modify else None) or []:
            cid = str(alter.get("control-id", "")).lower()
            for node in by_id.get(cid, []):
                for add in alter.get("adds", []) or []:
                    for part in add.get("parts", []) or []:
                        text = _part_text(part)
                        if not text:
                            continue
                        locator = next(
                            (
                                str(p.get("value", ""))
                                for p in part.get("props", []) or []
                                if p.get("name") == "locator"
                            ),
                            "",
                        )
                        notes.setdefault(node, {})[(str(part.get("name", "note")), locator)] = text

    def _materialize_profile(self, key: str) -> None:
        prof = self._profile_docs[key]
        resolved = self._resolve_profile(key)
        c = self._conn
        c.execute(
            "INSERT INTO profiles VALUES (?,?,?,?,?,?,?)",
            (
                key,
                prof.metadata.title,
                prof.metadata.version,
                prof.metadata.oscal_version,
                prof.uuid,
                self._profile_files[key].name,
                json.dumps(prof.model_dump(mode="json", by_alias=True, exclude_none=True)),
            ),
        )
        c.executemany(
            "INSERT INTO profile_controls VALUES (?,?,?)", [(key, cat, cid) for cat, cid in sorted(resolved.members)]
        )
        c.executemany(
            "INSERT INTO profile_params VALUES (?,?,?,?,?,?)",
            [
                (key, cat, pid, pv.control_id, json.dumps(list(pv.values)), json.dumps(list(pv.constraints)))
                for (cat, pid), pv in sorted(resolved.params.items())
            ],
        )
        c.executemany(
            "INSERT INTO profile_notes VALUES (?,?,?,?,?,?)",
            [
                (key, cat, cid, name, locator, text)
                for (cat, cid), entries in sorted(resolved.notes.items())
                for (name, locator), text in sorted(entries.items())
            ],
        )

    # ------------------------------------------------------------------ crosswalks

    def _load_crosswalks(self, files: dict[str, Path]) -> None:
        explicit: list[EdgeSpec] = []
        problems: list[str] = []
        for _key, path in files.items():
            try:
                doc = load_document(path)
            except OscalValidationError as exc:
                problems.extend(f"{path.name}: {e}" for e in exc.errors)
                continue
            # The same checks as validate-oscal-schemas.py (UUIDs, version, confidence ranges, href, duplicates), so a
            # file the validator rejects can never be served.
            kind, errors = validate_document(doc)
            if kind != "mapping-collection" and not errors:
                errors = [f"{kind} document found in crosswalks/ (expected mapping-collection)"]
            if errors:
                problems.extend(f"{path.name}: {e}" for e in errors)
                continue
            explicit.extend(crosswalk_edges(parse_crosswalk(doc)))
        known = {f"{r['catalog']}:{r['control_id']}" for r in self._all("SELECT catalog, control_id FROM controls")}
        for e in explicit:
            for node in (e.src, e.dst):
                if node not in known:
                    problems.append(f"edge {e.src} -> {e.dst}: unknown node {node!r}")
        if problems:
            raise OscalValidationError(sorted(set(problems)))
        upsert = (
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(src,dst,relation) DO UPDATE SET confidence=excluded.confidence, method=excluded.method, "
            "source=excluded.source, rationale=excluded.rationale, map_uuid=excluded.map_uuid "
            "WHERE excluded.confidence > edges.confidence"
        )
        for e in explicit:
            self._conn.execute(
                upsert, (e.src, e.dst, e.relation, e.confidence, e.method, e.source, e.rationale, 0, e.map_uuid)
            )
        # Derive inverses from the stored edges, not from the input list: when a mapping appears in two files, the
        # stored edge is the higher-confidence copy, and its inverse must carry the same values.
        stored = self._all(
            "SELECT src, dst, relation, confidence, method, source, rationale, map_uuid FROM edges WHERE derived = 0"
        )
        for r in stored:
            inverse = _INVERSE.get(r["relation"])
            if inverse:
                self._conn.execute(
                    "INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        r["dst"],
                        r["src"],
                        inverse,
                        r["confidence"],
                        r["method"],
                        r["source"],
                        r["rationale"],
                        1,
                        r["map_uuid"],
                    ),
                )

    # --------------------------------------------------------------------- queries

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._one(sql, params)
        return row[0] if row else None

    def _catalog_keys(self) -> list[str]:
        return [r["key"] for r in self._all("SELECT key FROM catalogs ORDER BY key")]

    def catalog_keys(self) -> list[str]:
        return self._catalog_keys()

    def profile_keys(self) -> list[str]:
        return [r["key"] for r in self._all("SELECT key FROM profiles ORDER BY key")]

    def require_catalog(self, key: str) -> str:
        k = key.strip()
        if k not in self._catalog_keys():
            raise UnknownKeyError(f"unknown catalog {key!r}. Valid catalogs: {self._catalog_keys()}")
        return k

    def require_profile(self, key: str) -> str:
        k = key.strip()
        if k not in self.profile_keys():
            raise UnknownKeyError(f"unknown profile {key!r}. Valid profiles: {self.profile_keys()}")
        return k

    def list_catalogs(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT c.key, c.title, c.version, c.oscal_version, c.source, count(k.control_id) AS n, "
            "count(CASE WHEN k.status = 'withdrawn' THEN 1 END) AS withdrawn "
            "FROM catalogs c LEFT JOIN controls k ON k.catalog = c.key GROUP BY c.key ORDER BY c.key"
        )
        return [
            {
                "key": r["key"],
                "title": r["title"],
                "version": r["version"],
                "oscal_version": r["oscal_version"],
                "source": r["source"],
                "control_count": r["n"],
                "withdrawn_count": r["withdrawn"],
            }
            for r in rows
        ]

    def list_profiles(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT p.key, p.title, p.version, p.oscal_version, p.source, count(pc.control_id) AS n "
            "FROM profiles p LEFT JOIN profile_controls pc ON pc.profile = p.key GROUP BY p.key ORDER BY p.key"
        )
        return [
            {
                "key": r["key"],
                "title": r["title"],
                "version": r["version"],
                "oscal_version": r["oscal_version"],
                "source": r["source"],
                "control_count": r["n"],
            }
            for r in rows
        ]

    _LIGHT: Final = (
        "catalog, control_id, title, control_class, parent_id, group_id, depth, prose, enhancement_ids, status, "
        "successors_json"
    )

    def get_control(self, catalog: str, control_id: str, *, full: bool = False) -> dict[str, Any] | None:
        cols = self._LIGHT + (", guidance, oscal_json" if full else "")
        row = self._one(
            f"SELECT {cols} FROM controls WHERE catalog = ? AND control_id = ?",  # noqa: S608 (static columns)
            (catalog, normalize_id(control_id)),
        )
        return self._row_to_dict(row, full) if row else None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, full: bool) -> dict[str, Any]:
        d: dict[str, Any] = {
            "catalog": row["catalog"],
            "control_id": row["control_id"],
            "title": row["title"],
            "class": row["control_class"],
            "parent_id": row["parent_id"],
            "group_id": row["group_id"],
            "depth": row["depth"],
            "prose": row["prose"],
            "enhancement_ids": json.loads(row["enhancement_ids"]),
            "status": row["status"],
            "successors": [tuple(s) for s in json.loads(row["successors_json"])],
        }
        if full:
            d["guidance"] = row["guidance"]
            d["oscal"] = json.loads(row["oscal_json"])
        return d

    def get_controls(self, nodes: Iterable[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for cat, cid in nodes:
            d = self.get_control(cat, cid)
            if d:
                out[(cat, cid)] = d
        return out

    def search(self, query: str, catalog: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        expr = fts_expression(query)
        where = "controls_fts MATCH ?"
        args: list[Any] = [expr]
        if catalog:
            where += " AND controls_fts.catalog = ?"
            args.append(catalog)
        total = int(self._scalar(f"SELECT count(*) FROM controls_fts WHERE {where}", args))  # noqa: S608
        rows = self._all(
            "SELECT k.catalog, k.control_id, k.title, k.control_class, k.parent_id, k.group_id, k.depth, "  # noqa: S608
            "k.prose, k.enhancement_ids, k.status, k.successors_json FROM controls_fts "
            "JOIN controls k ON k.catalog = controls_fts.catalog AND k.control_id = controls_fts.control_id "
            f"WHERE {where} ORDER BY bm25(controls_fts, 0.0, 0.0, 5.0, 1.0, 0.5), k.catalog, k.control_id "
            "LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [self._row_to_dict(r, False) for r in rows], total

    def list_controls(
        self, catalog: str, limit: int, offset: int, top_level_only: bool = True
    ) -> tuple[list[dict[str, Any]], int]:
        cond = " AND parent_id IS NULL" if top_level_only else ""
        total = int(self._scalar(f"SELECT count(*) FROM controls WHERE catalog = ?{cond}", (catalog,)))  # noqa: S608
        rows = self._all(
            f"SELECT {self._LIGHT} FROM controls WHERE catalog = ?{cond} "  # noqa: S608
            "ORDER BY rowid LIMIT ? OFFSET ?",
            (catalog, limit, offset),
        )
        return [self._row_to_dict(r, False) for r in rows], total

    # -------------------------------------------------------------------- profiles

    def profile_members(self, profile: str) -> set[tuple[str, str]]:
        return {
            (r["catalog"], r["control_id"])
            for r in self._all("SELECT catalog, control_id FROM profile_controls WHERE profile = ?", (profile,))
        }

    def param_nested(self) -> dict[tuple[str, str], tuple[str, ...]]:
        """Selection parameters with parameters inserted in their choices: (catalog, selection id) -> nested ids."""
        return {
            (r["catalog"], r["param_id"]): tuple(json.loads(r["nested_json"]))
            for r in self._all("SELECT catalog, param_id, nested_json FROM params WHERE nested_json != '[]'")
        }

    def param_aggregates(self) -> dict[tuple[str, str], tuple[str, ...]]:
        """Legacy parameters that stand for several assessment-level ones: (catalog, legacy id) -> the ids it covers."""
        return {
            (r["catalog"], r["param_id"]): tuple(json.loads(r["aggregates_json"]))
            for r in self._all("SELECT catalog, param_id, aggregates_json FROM params WHERE aggregates_json != '[]'")
        }

    def profile_param_values(self, profile: str) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for r in self._all(
            "SELECT catalog, param_id, control_id, values_json, constraints_json FROM profile_params WHERE profile = ?",
            (profile,),
        ):
            out[(r["catalog"], r["param_id"])] = {
                "control_id": r["control_id"],
                "values": json.loads(r["values_json"]),
                "constraints": json.loads(r["constraints_json"]),
            }
        return out

    def profile_document(self, profile: str) -> dict[str, Any]:
        row = self._one("SELECT doc_json FROM profiles WHERE key = ?", (profile,))
        if row is None:
            raise UnknownKeyError(f"unknown profile {profile!r}. Valid profiles: {self.profile_keys()}")
        doc: dict[str, Any] = json.loads(row["doc_json"])
        return doc

    def profile_notes(self, profile: str) -> dict[tuple[str, str], dict[tuple[str, str], str]]:
        out: dict[tuple[str, str], dict[tuple[str, str], str]] = {}
        for r in self._all(
            "SELECT catalog, control_id, name, locator, text FROM profile_notes WHERE profile = ?", (profile,)
        ):
            out.setdefault((r["catalog"], r["control_id"]), {})[(r["name"], r["locator"])] = r["text"]
        return out

    def profile_status(
        self, catalog: str, control_id: str, profiles: Sequence[str], *, include_notes: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Baseline membership, parameter values, and notes for one control across profiles.

        ``note_count`` is always present. The note text is included only when ``include_notes`` is true,
        because requirement text can be long.
        """
        out: dict[str, dict[str, Any]] = {}
        for p in profiles:
            member = self._one(
                "SELECT 1 FROM profile_controls WHERE profile = ? AND catalog = ? AND control_id = ?",
                (p, catalog, control_id),
            )
            params: dict[str, Any] = {}
            notes: list[dict[str, str]] = []
            if member:
                for r in self._all(
                    "SELECT param_id, values_json, constraints_json FROM profile_params "
                    "WHERE profile = ? AND catalog = ? AND control_id = ? ORDER BY param_id",
                    (p, catalog, control_id),
                ):
                    params[r["param_id"]] = {
                        "values": json.loads(r["values_json"]),
                        "constraints": json.loads(r["constraints_json"]),
                    }
                notes = [
                    {"name": r["name"], "locator": r["locator"], "text": r["text"]}
                    for r in self._all(
                        "SELECT name, locator, text FROM profile_notes "
                        "WHERE profile = ? AND catalog = ? AND control_id = ? ORDER BY name, locator",
                        (p, catalog, control_id),
                    )
                ]
            entry: dict[str, Any] = {"in_baseline": member is not None, "parameters": params, "note_count": len(notes)}
            if include_notes:
                entry["notes"] = notes
            out[p] = entry
        return out

    # ------------------------------------------------------------------- crosswalk

    def node_exists(self, catalog: str, control_id: str) -> bool:
        return (
            self._one("SELECT 1 FROM controls WHERE catalog = ? AND control_id = ?", (catalog, control_id)) is not None
        )

    def edges_from(self, node: str) -> list[Hop]:
        return [
            Hop(r["src"], r["relation"], r["dst"], r["confidence"], r["method"], r["source"], bool(r["derived"]))
            for r in self._all(
                "SELECT src, dst, relation, confidence, method, source, derived FROM edges "
                "WHERE src = ? ORDER BY confidence DESC, dst, relation",
                (node,),
            )
        ]

    def edge_count(self) -> int:
        return int(self._scalar("SELECT count(*) FROM edges"))

    def traverse(
        self,
        start: str,
        *,
        max_hops: int | None = None,
        min_confidence: float | None = None,
        target_catalogs: Sequence[str] | None = None,
    ) -> list[PathResult]:
        """Breadth-first walk over outgoing edges. Shortest path wins, ties go to higher confidence.

        Paths below the confidence floor are pruned during the walk, so the shortest path is chosen among paths that
        meet the floor. Pruning is safe because extending a path never
        raises its confidence.
        """
        limit = min(max_hops if max_hops is not None else self.settings.max_hops, 4)
        floor = self.settings.min_confidence if min_confidence is None else min_confidence
        visited = {start}
        frontier: dict[str, tuple[Hop, ...]] = {start: ()}
        results: list[PathResult] = []
        for _depth in range(1, limit + 1):
            best: dict[str, PathResult] = {}
            for node, hops in frontier.items():
                for edge in self.edges_from(node):
                    if edge.dst in visited:
                        continue
                    path = (*hops, edge)
                    relation, conf, method = score_path(path)
                    if conf < floor:
                        continue
                    cur = best.get(edge.dst)
                    if cur is None or conf > cur.confidence:
                        best[edge.dst] = PathResult(edge.dst, path, relation, conf, method)
            if not best:
                break
            visited.update(best)
            frontier = {n: r.hops for n, r in best.items()}
            results.extend(best.values())
        if target_catalogs:
            wanted = set(target_catalogs)
            results = [r for r in results if r.node.split(":", 1)[0] in wanted]
        results.sort(key=lambda r: (-r.confidence, len(r.hops), r.node))
        return results
