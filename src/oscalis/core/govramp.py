"""Parse GovRAMP baseline workbooks and map their parameter text onto OSCAL parameter ids.

GovRAMP publishes each baseline as a spreadsheet. A parameter cell looks like::

    AC-2 (h) (1) [twenty-four (24) hours]
    AC-2 (h) (2) [eight (8) hours]
    AC-1 (c) (2)-1 [at least annually] [significant changes]

Each entry is ``<control id> <locator...> [-n] [value]...``. The locator names a lettered or numbered
item in the control statement (``h`` then ``1`` is statement item ``h.1``). The NIST OSCAL catalog marks each
parameter inside the statement item that uses it, so a locator can be resolved to catalog parameter ids by
reading the statement. A mapping is accepted only when the values fit the parameters at that spot without
guessing. Everything else is reported with a reason and kept as text.

A *slot* is one blank in the statement. It is usually one organization-defined parameter (ODP). Some statements
still insert an older composite parameter (``at-2_prm_1``) that lists several ODPs in ``aggregates`` properties.
GovRAMP wrote one value for that blank, so a composite is one slot and its value applies to every ODP it
aggregates.

This module is pure: it reads no files. ``src/scripts/import_govramp.py`` does the spreadsheet and file I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from oscalis.core.oscal import Catalog, Control, Param, Part, iter_controls

# ----------------------------------------------------------------------------- id normalization

_ID_RE: Final = re.compile(r"^\s*([A-Za-z]{2})-0*([1-9]\d*)\s*(?:\(\s*0*([1-9]\d*)\s*\))?\s*$")


def normalize_control_id(raw: object) -> str | None:
    """'AC-02 (01)' or 'AC-2 (1)' -> 'ac-2.1'. Returns None for non-NIST ids such as 'IA-5 (j)' or 'IA-0'."""
    m = _ID_RE.match(str(raw))
    if not m:
        return None
    base = f"{m[1].lower()}-{int(m[2])}"
    return f"{base}.{int(m[3])}" if m[3] else base


def is_not_selected(text: object) -> bool:
    return bool(text) and "not selected by govramp" in str(text).lower()


# ----------------------------------------------------------------------------- cell parsing

_BRACKET: Final = re.compile(r"\[([^\[\]]*)\]")
_ENTRY_HEAD: Final = re.compile(
    r"""^\s*
        (?P<ctrl>[A-Za-z]{2}-\d+(?:\s*\(\s*\d+\s*\))?)          # control or enhancement id
        (?P<loc>(?:\s*\(\s*[A-Za-z0-9]{1,3}\s*\))*)              # locator path: (h) (1) (a)
        (?:\s*-(?P<ord>\d+))?                                    # optional ordinal: -1, -2
        \s*(?P<rest>.*)$""",
    re.X | re.S,
)
_LOC_ITEM: Final = re.compile(r"\(\s*([A-Za-z0-9]{1,3})\s*\)")


@dataclass(frozen=True)
class ParamEntry:
    """One parsed entry of a parameter cell."""

    control_id: str  # normalized, e.g. 'ac-2.3'
    locator: tuple[str, ...]  # ('h', '1'); () when the entry names the whole control
    ordinal: int | None  # '-2' suffix: the 2nd slot at this locator
    values: tuple[str, ...]  # bracket contents, in order
    raw: str


def _split_entries(cell: str) -> list[str]:
    """Entries start where a control id begins a line or follows '|'. Bare '[..]' lines continue the previous one."""
    entries: list[str] = []
    for line in re.split(r"[\r\n|]+", cell):
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^[A-Za-z]{2}-\d", stripped) or not entries:
            entries.append(stripped)
        else:
            entries[-1] += " " + stripped
    return entries


def parse_param_cell(cell: str | None, default_control: str | None = None) -> list[ParamEntry]:
    """Parse a parameter cell. Entries without any bracketed value are skipped."""
    if not cell or not str(cell).strip():
        return []
    out: list[ParamEntry] = []
    for entry in _split_entries(str(cell)):
        m = _ENTRY_HEAD.match(entry)
        if not m:
            continue
        cid = normalize_control_id(m["ctrl"]) or default_control
        if cid is None:
            continue
        locator = tuple(x.lower() for x in _LOC_ITEM.findall(m["loc"] or ""))
        values = tuple(v.strip() for v in _BRACKET.findall(m["rest"]) if v.strip())
        if not values:
            continue
        out.append(ParamEntry(cid, locator, int(m["ord"]) if m["ord"] else None, values, entry))
    return out


_PREFIX: Final = re.compile(r"^\s*(?:selection(?:\s*\([^)]*\))?|assignment)\s*:\s*", re.I)


def clean_value(value: str) -> str:
    """'Selection: disables' -> 'disables'. Leaves ordinary values untouched."""
    return _PREFIX.sub("", value).strip()


# ----------------------------------------------------------------------------- catalog index

_INSERT: Final = re.compile(r"\{\{\s*insert:\s*param,\s*([A-Za-z0-9._\-]+)\s*\}\}")


@dataclass(frozen=True)
class Slot:
    """One blank in a control statement."""

    ids: tuple[str, ...]  # the ODP ids it fills (several for a composite)
    label: str  # catalog label such as 'frequency' or 'organization-defined frequency'
    select: bool  # True when the blank is a selection from choices
    choices: tuple[str, ...] = ()  # the selectable choices, when select is True
    inner: tuple[str, ...] = ()  # ODPs that the choices insert (a nested blank inside a choice)


def _label(part: Part) -> str:
    for prop in part.props or []:
        if prop.get("name") == "label":
            return re.sub(r"[().\s]", "", str(prop.get("value", ""))).lower()
    return ""


def _all_tags(part: Part) -> list[str]:
    out = _INSERT.findall(part.prose or "")
    for sub in part.parts or []:
        out.extend(_all_tags(sub))
    return out


def _slots_for(tag: str, params: Mapping[str, Param], odps: set[str], seen: frozenset[str] = frozenset()) -> list[Slot]:
    """Turn one statement tag into slots."""
    param = params.get(tag)
    if tag in odps:
        label = (param.label or "").lower() if param else ""
        if param and param.select:
            choices = tuple(str(c) for c in param.select.get("choice", []))
            inner = tuple(dict.fromkeys(i for c in choices for i in _INSERT.findall(c) if i in odps))
            return [Slot((tag,), label, True, choices, inner)]
        return [Slot((tag,), label, False)]
    if param is None or tag in seen:
        return []
    aggregated = tuple(
        str(p["value"]) for p in param.props or [] if p.get("name") == "aggregates" and str(p.get("value")) in odps
    )
    if aggregated:
        return [Slot(aggregated, (param.label or "").lower(), bool(param.select))]
    out: list[Slot] = []  # a selection whose choices insert other parameters
    for choice in (param.select or {}).get("choice", []) if param.select else []:
        for inner in _INSERT.findall(str(choice)):
            out.extend(_slots_for(inner, params, odps, seen | {tag}))
    return out


def _dedupe_slots(slots: Iterable[Slot]) -> list[Slot]:
    seen: dict[tuple[str, ...], Slot] = {}
    for s in slots:
        seen.setdefault(s.ids, s)
    return list(seen.values())


@dataclass
class ControlIndex:
    """What GovRAMP locators can resolve to inside one control."""

    control_id: str
    slots: list[Slot]  # every blank in the statement, in reading order
    by_locator: dict[tuple[str, ...], list[Slot]] = field(default_factory=dict)  # item path -> blanks inside it


def _index_parts(
    parts: Sequence[Part],
    path: tuple[str, ...],
    out: dict[tuple[str, ...], list[Slot]],
    params: Mapping[str, Param],
    odps: set[str],
) -> None:
    for part in parts:
        label = _label(part)
        sub_path = (*path, label) if label else path
        if label:
            out[sub_path] = _dedupe_slots(s for t in _all_tags(part) for s in _slots_for(t, params, odps))
        _index_parts(part.parts or [], sub_path, out, params, odps)


def _index_control(control: Control) -> ControlIndex:
    params = {p.id: p for p in control.params or []}
    odps = {p.id for p in control.params or [] if "_odp" in p.id}
    by_loc: dict[tuple[str, ...], list[Slot]] = {}
    all_slots: list[Slot] = []
    for st in (p for p in control.parts or [] if p.name == "statement"):
        _index_parts(st.parts or [], (), by_loc, params, odps)
        all_slots.extend(s for t in _all_tags(st) for s in _slots_for(t, params, odps))
    return ControlIndex(control.id.lower(), _dedupe_slots(all_slots), by_loc)


def build_index(catalog: Catalog) -> dict[str, ControlIndex]:
    """Index every control: its blanks and the blanks inside each lettered/numbered statement item."""
    return {c.id.lower(): _index_control(c) for c, _g, _p, _d in iter_controls(catalog)}


# ----------------------------------------------------------------------------- mapping

REASONS: Final = {
    "no-locator-multi": "no locator, and the values do not fit the control's unassigned blanks one-to-one",
    "locator-not-found": "the locator does not match any labelled statement item",
    "count-mismatch": "number of values differs from number of blanks at that locator",
    "ordinal-out-of-range": "the -n suffix is larger than the number of blanks available",
    "no-parameters": "the control or locator has no organization-defined parameter to assign",
    "kind-mismatch": "a 'Selection:' value would land on a free assignment, or an 'Assignment:' on a selection",
    "conflict": "the target parameter was already assigned by an earlier entry",
    "refers-to-requirements": "the value only points to the additional requirements text",
    "unparsed": "no bracketed values could be read from the entry",
}
# How a value was placed, strongest first. Reported separately so a reviewer can see each tier.
METHODS: Final = (
    "locator",
    "sole-parameter",
    "ordinal",
    "choice-match",
    "control-order",
    "label-aware",
    "cjis-subitem",
)


@dataclass(frozen=True)
class MappedParam:
    param_id: str
    values: tuple[str, ...]
    entry: ParamEntry
    method: str
    fan_out: int = 1  # >1 when one value was applied to every parameter of a composite blank


@dataclass(frozen=True)
class Unmapped:
    entry: ParamEntry
    reason: str  # key of REASONS


@dataclass
class MappingResult:
    mapped: list[MappedParam] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)

    def extend(self, other: MappingResult) -> None:
        self.mapped.extend(other.mapped)
        self.unmapped.extend(other.unmapped)

    def counts(self) -> Counter[str]:
        c: Counter[str] = Counter()
        c["entries"] = len({id(m.entry) for m in self.mapped} | {id(u.entry) for u in self.unmapped})
        c["params"] = len(self.mapped)
        for m in self.mapped:
            c[f"method:{m.method}"] += 1
            if m.fan_out > 1:
                c["fan-out"] += 1
        for u in self.unmapped:
            c[f"reason:{u.reason}"] += 1
        return c


_POINTER: Final = re.compile(r"^\s*see\b.*\b(requirement|guidance)", re.I)
_TEMPORAL: Final = re.compile(
    r"annual|quarter|month|week|daily|day|hour|minute|year|continuous|every|within|periodic|immediate", re.I
)
_LABEL_TEMPORAL: Final = re.compile(r"frequenc|time period|time-period", re.I)
_LABEL_OTHER: Final = re.compile(r"event|personnel|role|official|conditions", re.I)


def _kind_ok(value: str, slot: Slot) -> bool:
    """A 'Selection:' / 'Assignment:' prefix must agree with the blank's declared kind."""
    head = value.lstrip().lower()
    if head.startswith("selection"):
        return slot.select
    if head.startswith("assignment"):
        return not slot.select
    return True


def _label_aware(slots: list[Slot], values: tuple[str, ...]) -> list[tuple[Slot, str]] | None:
    """Place fewer values than blanks by matching time-like values to time-labelled blanks in order.

    A temporal value (``at least annually``) may skip over a blank whose label says it is something else
    (``events``, ``personnel``). Any other pairing is rejected rather than guessed.
    """
    out: list[tuple[Slot, str]] = []
    pos = 0
    for value in values:
        temporal = bool(_TEMPORAL.search(value))
        while pos < len(slots):
            label = slots[pos].label
            if temporal and _LABEL_TEMPORAL.search(label):
                break
            if temporal and _LABEL_OTHER.search(label):
                pos += 1
                continue
            if not temporal and not _LABEL_TEMPORAL.search(label):
                break
            return None
        if pos >= len(slots):
            return None
        out.append((slots[pos], value))
        pos += 1
    return out


def _redirect(slot: Slot, values: tuple[str, ...]) -> tuple[str, ...]:
    """Choose the parameter ids that receive ``values``.

    A selection parameter should receive one of its choices. If its choices insert exactly one other parameter and the
    value is none of the choices, the value fills that nested blank instead (for example a time period inside
    "initiating a device lock after {time period} of inactivity").

    With several nested parameters (for example SI-5 item c, whose choices insert a personnel, an element and an
    organization blank) one value cannot be split among them, so it stays on the selection itself.
    """
    if slot.select and len(slot.inner) == 1:
        text = " ".join(values).lower()
        if not any(text and (text in c.lower() or c.lower() in text) for c in slot.choices):
            return slot.inner
    return slot.ids


def _sibling_key(component: str) -> int | None:
    """Position of a sub-item label: '1' -> 1, 'a' -> 1, 'b' -> 2. None if it is neither."""
    if component.isdigit():
        return int(component)
    if len(component) == 1 and component.isalpha():
        return ord(component) - ord("a") + 1
    return None


def _compatible(value: str, slot: Slot) -> bool:
    """Does a value look like what the blank asks for? Time-like values need a frequency/time blank, and
    other values must not go into one."""
    temporal = bool(_TEMPORAL.search(value))
    wants_time = bool(_LABEL_TEMPORAL.search(slot.label))
    return temporal == wants_time


def _subitem_groups(ci: ControlIndex, entries: Sequence[ParamEntry]) -> dict[int, list[tuple[Slot, ParamEntry]]]:
    """Resolve CJIS-style sub-sub-item locators.

    CJIS Policy numbers one level deeper than the NIST statement: ``AT-2 (c) (1)`` and ``AT-2 (c) (2)`` name the
    first and second blank inside statement item ``c``. This is accepted only when all of these hold, so that it
    never guesses:
      * the locator itself does not exist but its parent does;
      * the entries sharing that parent number its blanks 1..n (or a..n) with no gaps or repeats;
      * there are exactly as many entries as blanks, each entry has a single value, and every value suits the
        blank it lands on (a time-like value on a frequency blank, anything else not).
    Returns {id(entry): ...} keyed by position of the entry in ``entries``.
    """
    candidates: dict[tuple[str, ...], list[tuple[int, ParamEntry, int]]] = {}
    for pos, e in enumerate(entries):
        if e.locator in ci.by_locator or len(e.locator) < 2 or e.ordinal is not None:
            continue
        key = _sibling_key(e.locator[-1])
        parent = e.locator[:-1]
        if key is None or parent not in ci.by_locator:
            continue
        candidates.setdefault(parent, []).append((pos, e, key))
    placed: dict[int, list[tuple[Slot, ParamEntry]]] = {}
    for parent, group in candidates.items():
        slots = ci.by_locator[parent]
        ordered = sorted(group, key=lambda g: g[2])
        if [g[2] for g in ordered] != list(range(1, len(ordered) + 1)) or len(ordered) != len(slots):
            continue
        if any(len(e.values) != 1 for _p, e, _k in ordered):
            continue
        if not all(_compatible(e.values[0], slot) for (_p, e, _k), slot in zip(ordered, slots, strict=True)):
            continue
        for (pos, e, _k), slot in zip(ordered, slots, strict=True):
            placed[pos] = [(slot, e)]
    return placed


def map_control(ci: ControlIndex, entries: Sequence[ParamEntry]) -> MappingResult:
    """Map all parameter entries of one control. Entries with a locator or ordinal go first, then the rest."""
    result = MappingResult()
    claimed: set[tuple[str, ...]] = set()
    subitems = _subitem_groups(ci, entries)

    def assign(entry: ParamEntry, pairs: list[tuple[Slot, tuple[str, ...]]], method: str) -> None:
        if any(not _kind_ok(v, slot) for slot, vals in pairs for v in vals):
            result.unmapped.append(Unmapped(entry, "kind-mismatch"))
            return
        if any(slot.ids in claimed for slot, _ in pairs):
            result.unmapped.append(Unmapped(entry, "conflict"))
            return
        for slot, vals in pairs:
            claimed.add(slot.ids)
            cleaned = tuple(clean_value(v) for v in vals)
            target_ids = _redirect(slot, cleaned)
            result.mapped.extend(MappedParam(pid, cleaned, entry, method, len(target_ids)) for pid in target_ids)

    def fail(entry: ParamEntry, reason: str) -> None:
        result.unmapped.append(Unmapped(entry, reason))

    pending: list[ParamEntry] = []
    for pos, entry in enumerate(entries):
        if all(_POINTER.match(v) for v in entry.values):
            fail(entry, "refers-to-requirements")
            continue
        if pos in subitems:
            ((slot, _e),) = subitems[pos]
            assign(entry, [(slot, entry.values)], "cjis-subitem")
            continue
        if not entry.locator and entry.ordinal is None:
            pending.append(entry)
            continue
        if entry.locator:
            found = ci.by_locator.get(entry.locator)
            if found is None:
                fail(entry, "locator-not-found")
                continue
            targets = found
        else:
            targets = ci.slots
        if not targets:
            fail(entry, "no-parameters")
        elif entry.ordinal is not None:
            if entry.ordinal > len(targets):
                fail(entry, "ordinal-out-of-range")
            else:
                assign(entry, [(targets[entry.ordinal - 1], entry.values)], "ordinal")
        elif len(entry.values) == len(targets):
            assign(entry, [(t, (v,)) for t, v in zip(targets, entry.values, strict=True)], "locator")
        elif len(targets) == 1:
            assign(entry, [(targets[0], entry.values)], "locator")
        else:
            placed = _label_aware(targets, entry.values)
            if placed is None:
                fail(entry, "count-mismatch")
            else:
                assign(entry, [(s, (v,)) for s, v in placed], "label-aware")

    for entry in pending:
        free = [s for s in ci.slots if s.ids not in claimed]
        if not ci.slots:
            fail(entry, "no-parameters")
        elif free and len(entry.values) == len(free):
            method = "sole-parameter" if len(ci.slots) == 1 else "control-order"
            assign(entry, [(s, (v,)) for s, v in zip(free, entry.values, strict=True)], method)
        elif len(free) == 1:
            assign(entry, [(free[0], entry.values)], "sole-parameter")
        else:
            by_choice = _by_choice(free, entry.values)
            if by_choice is None:
                fail(entry, "no-locator-multi")
            else:
                assign(entry, [(by_choice, entry.values)], "choice-match")
    return result


def _choice_parts(value: str) -> list[str]:
    """Split a combined selection such as 'confidentiality AND integrity' into its candidate choices."""
    parts = re.split(r"\s+(?:and|or)\s+|\s*[;,]\s*", clean_value(value), flags=re.I)
    return [p.strip().lower() for p in parts if p.strip()]


def _by_choice(free: list[Slot], values: tuple[str, ...]) -> Slot | None:
    """The one free selection slot whose choices contain every part of every value, if exactly one does.

    Matching a value to the literal choices is evidence, not a guess. Zero or several candidates means no placement.
    """
    wanted = [p for v in values for p in _choice_parts(v)]
    if not wanted:
        return None
    hits = [
        s
        for s in free
        if s.select and s.choices and all(any(w == c.lower() or w in c.lower() for c in s.choices) for w in wanted)
    ]
    return hits[0] if len(hits) == 1 else None


def map_cell(index: Mapping[str, ControlIndex], cell: str | None, control_id: str) -> MappingResult:
    """Parse one parameter cell and map its entries onto ``index``."""
    entries = parse_param_cell(cell, default_control=control_id)
    if cell and str(cell).strip() and not entries:
        stub = ParamEntry(control_id, (), None, (), str(cell).strip())
        return MappingResult(unmapped=[Unmapped(stub, "unparsed")])
    result = MappingResult()
    by_control: dict[str, list[ParamEntry]] = {}
    for e in entries:
        by_control.setdefault(e.control_id, []).append(e)
    for cid, group in by_control.items():
        ci = index.get(cid)
        if ci is None:
            result.unmapped.extend(Unmapped(e, "locator-not-found") for e in group)
        else:
            result.extend(map_control(ci, group))
    return result


# ----------------------------------------------------------------------------- requirement text

_REQ_HEAD: Final = re.compile(
    r"""^\s*
        (?P<ctrl>[A-Za-z]{2}-\d+(?:\s*\(\s*\d+\s*\))?)
        (?P<loc>(?:\s*\(\s*[A-Za-z0-9]{1,3}\s*\))*)
        \s*(?P<kind>Requirements?|Guidance|Note)\s*:\s*(?P<text>.*)$""",
    re.X | re.I | re.S,
)
_BROKEN_APOSTROPHE: Final = re.compile("(?<=\\w)\ufffd(?=\\w)")


def repair_text(text: str) -> tuple[str, int]:
    """Replace the apostrophe the source spreadsheets lost to an encoding error. Returns (text, replacements)."""
    fixed, n = _BROKEN_APOSTROPHE.subn("'", text)
    return fixed.replace("\u00a0", " ").strip(), n


@dataclass(frozen=True)
class RequirementNote:
    """A labelled piece of additional text GovRAMP attaches to a control."""

    kind: str  # 'requirement' | 'guidance' | 'note'
    locator: str  # '' for the whole control, else 'd' or 'h.1'
    text: str


def parse_requirements(cell: str | None) -> list[RequirementNote]:
    """Split an 'Additional Requirements and Guidance' cell into labelled notes.

    Text that does not start with a recognised ``<id> [locator] Requirement|Guidance:`` label is kept as a single
    unlabelled ``requirement`` note so that nothing is dropped.
    """
    if not cell or not str(cell).strip():
        return []
    items: list[tuple[str, str, list[str]]] = []
    for line in re.split(r"\r?\n", str(cell)):
        m = _REQ_HEAD.match(line)
        if m:
            kind = m["kind"].lower()
            kind = "requirement" if kind.startswith("requirement") else kind
            loc = ".".join(x.lower() for x in _LOC_ITEM.findall(m["loc"] or ""))
            items.append((kind, loc, [m["text"]]))
        elif items:
            items[-1][2].append(line)
        elif line.strip():
            items.append(("requirement", "", [line]))
    merged: dict[tuple[str, str], list[str]] = {}
    for kind, loc, lines in items:
        text = "\n".join(lines).strip()
        if text:
            merged.setdefault((kind, loc), []).append(text)
    return [RequirementNote(k, loc, "\n\n".join(texts)) for (k, loc), texts in merged.items()]
