"""Vendor reference layout patterns: parse, bind to real references, describe.

A pattern is a transcription of a datasheet or EVM layout figure into a file
the tool can check a board against. It never contains absolute board
coordinates: every offset is expressed in the anchor footprint's local frame,
so the same pattern applies wherever the anchor lands.

Two fidelities exist and both are checked. `exact` carries millimetre offsets
and rotations taken from design files or a dimensioned drawing, and is the only
fidelity precise enough to stamp onto a board. `sketch` carries a side and a
maximum distance transcribed from a figure by eye; it can be measured but never
applied, because the numbers are not the vendor's.

This module reads and binds. It never measures a board and never writes one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from pcbforge.board_geometry import BoardGeometry

PATTERN_SCHEMA = 1
PATTERNS_DIRNAME = "patterns"
FIDELITIES = {"exact", "sketch"}
SIDES = {"same", "opposite"}
NEAR_SIDES = {"west", "east", "north", "south"}
RULE_TYPES = {"vias-under-pad", "note"}
DEFAULT_TOLERANCE_MM = 0.5

ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class PatternError(RuntimeError):
    """A reference layout pattern could not be used."""


class PatternInputError(PatternError):
    """The pattern file or its binding is malformed."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueLoader,
    node: yaml.MappingNode,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class PatternPart:
    partnumber_match: str
    footprint_match: str


@dataclass(frozen=True)
class PatternSource:
    document: str
    layers: int
    captured: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class PatternRole:
    """One satellite the pattern expects around the anchor."""

    identifier: str
    anchor_pads: tuple[str, ...]
    satellite_pads: int
    footprint_match: str | None
    side: str
    rationale: str
    offset_mm: tuple[float, float] | None
    rotation_deg: float | None
    tolerance_mm: float
    near_side: str | None
    max_mm: float | None


@dataclass(frozen=True)
class PatternRule:
    """A pattern statement about copper rather than about a placed part."""

    identifier: str
    kind: str
    rationale: str
    anchor_pad: str | None
    min_count: int | None
    text: str | None


@dataclass(frozen=True)
class Pattern:
    identifier: str
    part: PatternPart
    fidelity: str
    source: PatternSource
    frame: str
    roles: tuple[PatternRole, ...]
    rules: tuple[PatternRule, ...]
    path: Path

    def role(self, identifier: str) -> PatternRole:
        for item in self.roles:
            if item.identifier == identifier:
                return item
        raise KeyError(f"pattern {self.identifier} has no role {identifier!r}")


@dataclass(frozen=True)
class BoardFacts:
    """The board as binding sees it: identity and connectivity, no geometry.

    Binding is a netlist question, so it takes this rather than a whole board.
    Tests build one directly; `board_facts` builds one from PA1 geometry.
    """

    footprints: Mapping[str, str]
    partnumbers: Mapping[str, str]
    pad_nets: Mapping[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class PatternBinding:
    pattern: Pattern
    anchor: str
    roles: tuple[tuple[str, str | None], ...]

    @property
    def bound(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (role, reference) for role, reference in self.roles if reference is not None
        )

    @property
    def unbound(self) -> tuple[str, ...]:
        return tuple(role for role, reference in self.roles if reference is None)

    def payload(self) -> dict[str, Any]:
        """The stable JSON view that joins the handoff fingerprint."""
        return {
            "pattern": self.pattern.identifier,
            "anchor": self.anchor,
            "roles": {role: reference for role, reference in self.roles},
        }


def board_facts(geometry: BoardGeometry) -> BoardFacts:
    """Reduce PA1 geometry to the identity and connectivity binding needs."""
    footprints: dict[str, str] = {}
    partnumbers: dict[str, str] = {}
    pad_nets: dict[str, tuple[tuple[str, str], ...]] = {}
    for item in geometry.footprints:
        footprints[item.reference] = item.footprint
        partnumbers[item.reference] = item.properties.get("Partnumber", "")
        pad_nets[item.reference] = tuple(
            (pad.number, pad.net) for pad in item.pads if pad.net
        )
    return BoardFacts(footprints, partnumbers, pad_nets)


def _load_yaml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PatternInputError(f"missing {label}") from exc
    except (OSError, UnicodeError) as exc:
        raise PatternInputError(f"cannot read {label}: {exc}") from exc
    try:
        loaded = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise PatternInputError(f"invalid {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PatternInputError(f"invalid {label}: expected a mapping")
    return loaded


def _unknown(
    raw: Mapping[str, Any],
    allowed: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    keys = sorted(set(raw) - allowed, key=str)
    if keys:
        errors.append(f"{prefix}: unknown keys: {', '.join(map(str, keys))}")


def _text(
    raw: Mapping[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
    *,
    required: bool = True,
) -> str | None:
    value = raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}: expected a non-empty string")
        return None
    return value.strip()


def _regex(value: str | None, prefix: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    try:
        re.compile(value)
    except re.error as exc:
        errors.append(f"{prefix}: invalid regular expression: {exc}")
        return None
    return value


def _positive_int(
    value: Any,
    prefix: str,
    errors: list[str],
    *,
    default: int | None = None,
) -> int | None:
    if value is None and default is not None:
        return default
    if type(value) is not int or value <= 0:
        errors.append(f"{prefix}: expected a positive integer")
        return None
    return value


def _number(value: Any, prefix: str, errors: list[str]) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{prefix}: expected a number")
        return None
    return float(value)


def _string_list(value: Any, prefix: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}: expected a non-empty list of strings")
        return ()
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{prefix}[{index}]: expected a non-empty string")
        else:
            result.append(item.strip())
    return tuple(result)


def _pad_list(value: Any, prefix: str, errors: list[str]) -> tuple[str, ...]:
    """Pad numbers are strings, but YAML turns an unquoted `9` into an int."""
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}: expected a non-empty list of pad numbers")
        return ()
    result = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif type(item) is int:
            result.append(str(item))
        else:
            errors.append(f"{prefix}[{index}]: expected a pad number")
    return tuple(result)


def _pad_number(value: Any, prefix: str, errors: list[str]) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if type(value) is int:
        return str(value)
    errors.append(f"{prefix}: expected a pad number")
    return None


def _parse_role(
    raw: Any,
    prefix: str,
    fidelity: str,
    errors: list[str],
) -> PatternRole | None:
    if not isinstance(raw, dict):
        errors.append(f"{prefix}: expected a mapping")
        return None
    _unknown(
        raw,
        {
            "id",
            "anchor_pads",
            "satellite_pads",
            "footprint_match",
            "side",
            "rationale",
            "offset_mm",
            "rotation_deg",
            "tolerance_mm",
            "near_side",
            "max_mm",
        },
        prefix,
        errors,
    )
    identifier = _text(raw, "id", prefix, errors)
    if identifier and ID_RE.fullmatch(identifier) is None:
        errors.append(f"{prefix}.id: expected a kebab-case ID")
    anchor_pads = _pad_list(raw.get("anchor_pads"), f"{prefix}.anchor_pads", errors)
    satellite_pads = _positive_int(
        raw.get("satellite_pads"),
        f"{prefix}.satellite_pads",
        errors,
        default=1,
    )
    footprint_match = _regex(
        _text(raw, "footprint_match", prefix, errors, required=False),
        f"{prefix}.footprint_match",
        errors,
    )
    side = raw.get("side", "same")
    if side not in SIDES:
        errors.append(f"{prefix}.side: expected one of {', '.join(sorted(SIDES))}")
        side = "same"
    rationale = _text(raw, "rationale", prefix, errors)

    exact_keys = {"offset_mm", "rotation_deg", "tolerance_mm"}
    sketch_keys = {"near_side", "max_mm"}
    wrong = sorted(
        (sketch_keys if fidelity == "exact" else exact_keys) & set(raw)
    )
    if fidelity in FIDELITIES and wrong:
        errors.append(
            f"{prefix}: {', '.join(wrong)} not allowed in a {fidelity} pattern"
        )

    offset_mm: tuple[float, float] | None = None
    rotation_deg: float | None = None
    tolerance_mm = DEFAULT_TOLERANCE_MM
    near_side: str | None = None
    max_mm: float | None = None

    if fidelity == "exact":
        raw_offset = raw.get("offset_mm")
        if (
            not isinstance(raw_offset, list)
            or len(raw_offset) != 2
            or any(
                not isinstance(item, (int, float)) or isinstance(item, bool)
                for item in raw_offset
            )
        ):
            errors.append(f"{prefix}.offset_mm: expected [x, y] in millimetres")
        else:
            offset_mm = (float(raw_offset[0]), float(raw_offset[1]))
        rotation_deg = _number(raw.get("rotation_deg"), f"{prefix}.rotation_deg", errors)
        if "tolerance_mm" in raw:
            value = _number(raw.get("tolerance_mm"), f"{prefix}.tolerance_mm", errors)
            if value is not None and value <= 0:
                errors.append(f"{prefix}.tolerance_mm: expected a positive number")
            elif value is not None:
                tolerance_mm = value
    elif fidelity == "sketch":
        near_side = raw.get("near_side")
        if near_side not in NEAR_SIDES:
            errors.append(
                f"{prefix}.near_side: expected one of {', '.join(sorted(NEAR_SIDES))}"
            )
            near_side = None
        max_mm = _number(raw.get("max_mm"), f"{prefix}.max_mm", errors)
        if max_mm is not None and max_mm <= 0:
            errors.append(f"{prefix}.max_mm: expected a positive number")
            max_mm = None

    if identifier is None or rationale is None:
        return None
    return PatternRole(
        identifier,
        anchor_pads,
        satellite_pads or 1,
        footprint_match,
        side,
        rationale,
        offset_mm,
        rotation_deg,
        tolerance_mm,
        near_side,
        max_mm,
    )


def _parse_rule(raw: Any, prefix: str, errors: list[str]) -> PatternRule | None:
    if not isinstance(raw, dict):
        errors.append(f"{prefix}: expected a mapping")
        return None
    _unknown(
        raw,
        {"id", "type", "rationale", "anchor_pad", "min_count", "text"},
        prefix,
        errors,
    )
    identifier = _text(raw, "id", prefix, errors)
    if identifier and ID_RE.fullmatch(identifier) is None:
        errors.append(f"{prefix}.id: expected a kebab-case ID")
    kind = _text(raw, "type", prefix, errors)
    if kind is not None and kind not in RULE_TYPES:
        errors.append(f"{prefix}.type: expected one of {', '.join(sorted(RULE_TYPES))}")
        kind = None
    rationale = _text(raw, "rationale", prefix, errors, required=kind != "note")

    anchor_pad: str | None = None
    min_count: int | None = None
    text: str | None = None
    if kind == "vias-under-pad":
        anchor_pad = _pad_number(raw.get("anchor_pad"), f"{prefix}.anchor_pad", errors)
        min_count = _positive_int(raw.get("min_count"), f"{prefix}.min_count", errors)
        if "text" in raw:
            errors.append(f"{prefix}.text: not allowed for vias-under-pad")
    elif kind == "note":
        text = _text(raw, "text", prefix, errors)
        for key in ("anchor_pad", "min_count"):
            if key in raw:
                errors.append(f"{prefix}.{key}: not allowed for note")

    if identifier is None or kind is None:
        return None
    return PatternRule(identifier, kind, rationale or "", anchor_pad, min_count, text)


def _duplicates(values: Sequence[str]) -> list[str]:
    return sorted(item for item in set(values) if values.count(item) > 1)


def read_pattern(path: Path) -> Pattern:
    """Read and strictly validate one pattern file."""
    path = Path(path)
    label = path.name
    data = _load_yaml(path, label)
    errors: list[str] = []
    _unknown(
        data,
        {
            "pattern_schema",
            "id",
            "part",
            "fidelity",
            "source",
            "frame",
            "roles",
            "rules",
        },
        label,
        errors,
    )
    if data.get("pattern_schema") != PATTERN_SCHEMA:
        errors.append("pattern_schema: unsupported version")

    identifier = _text(data, "id", label, errors)
    if identifier and ID_RE.fullmatch(identifier) is None:
        errors.append("id: expected a kebab-case ID")
    if identifier and identifier != path.stem:
        errors.append(f"id: expected {path.stem!r} to match the file name")

    fidelity = _text(data, "fidelity", label, errors)
    if fidelity is not None and fidelity not in FIDELITIES:
        errors.append(f"fidelity: expected one of {', '.join(sorted(FIDELITIES))}")
        fidelity = None

    part_raw = data.get("part")
    part = PatternPart("", "")
    if not isinstance(part_raw, dict):
        errors.append("part: expected a mapping")
    else:
        _unknown(part_raw, {"partnumber_match", "footprint_match"}, "part", errors)
        partnumber_match = _regex(
            _text(part_raw, "partnumber_match", "part", errors),
            "part.partnumber_match",
            errors,
        )
        footprint_match = _regex(
            _text(part_raw, "footprint_match", "part", errors),
            "part.footprint_match",
            errors,
        )
        part = PatternPart(partnumber_match or "", footprint_match or "")

    source_raw = data.get("source")
    source = PatternSource("", 0, "", ())
    if not isinstance(source_raw, dict):
        errors.append("source: expected a mapping")
    else:
        _unknown(
            source_raw,
            {"document", "layers", "captured", "notes"},
            "source",
            errors,
        )
        document = _text(source_raw, "document", "source", errors)
        layers = _positive_int(source_raw.get("layers"), "source.layers", errors)
        captured_raw = source_raw.get("captured")
        captured = ""
        if isinstance(captured_raw, date):
            captured = captured_raw.isoformat()
        elif isinstance(captured_raw, str) and captured_raw.strip():
            captured = captured_raw.strip()
        else:
            errors.append("source.captured: expected a date")
        notes: tuple[str, ...] = ()
        if "notes" in source_raw:
            notes = _string_list(source_raw.get("notes"), "source.notes", errors)
        source = PatternSource(document or "", layers or 0, captured, notes)

    frame = _text(data, "frame", label, errors)

    roles: list[PatternRole] = []
    roles_raw = data.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        errors.append("roles: expected a non-empty list")
    else:
        for index, item in enumerate(roles_raw):
            role = _parse_role(item, f"roles[{index}]", fidelity or "", errors)
            if role is not None:
                roles.append(role)

    rules: list[PatternRule] = []
    if "rules" in data:
        rules_raw = data.get("rules")
        if not isinstance(rules_raw, list) or not rules_raw:
            errors.append("rules: expected a non-empty list")
        else:
            for index, item in enumerate(rules_raw):
                rule = _parse_rule(item, f"rules[{index}]", errors)
                if rule is not None:
                    rules.append(rule)

    duplicate_roles = _duplicates([role.identifier for role in roles])
    if duplicate_roles:
        errors.append(f"roles: duplicate IDs: {', '.join(duplicate_roles)}")
    duplicate_rules = _duplicates([rule.identifier for rule in rules])
    if duplicate_rules:
        errors.append(f"rules: duplicate IDs: {', '.join(duplicate_rules)}")

    if errors:
        raise PatternInputError(f"invalid {label}:\n  - " + "\n  - ".join(errors))

    return Pattern(
        identifier or "",
        part,
        fidelity or "",
        source,
        frame or "",
        tuple(roles),
        tuple(rules),
        path,
    )


def resolve_pattern_path(
    pattern_id: str,
    project_dir: Path,
    tool_root: Path,
) -> Path:
    """A project copy of a pattern wins over the tool catalog, by id."""
    candidates = (
        project_dir / PATTERNS_DIRNAME / f"{pattern_id}.yaml",
        tool_root / PATTERNS_DIRNAME / f"{pattern_id}.yaml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise PatternInputError(f"unknown pattern {pattern_id!r}: looked in {searched}")


def _satellite_matches(
    reference: str,
    role: PatternRole,
    nets: frozenset[str],
    facts: BoardFacts,
) -> bool:
    shared = sum(
        1 for _, net in facts.pad_nets.get(reference, ()) if net in nets
    )
    if shared < role.satellite_pads:
        return False
    if role.footprint_match is None:
        return True
    return re.search(role.footprint_match, facts.footprints.get(reference, "")) is not None


def bind(
    pattern: Pattern,
    anchor: str,
    group_references: Iterable[str],
    facts: BoardFacts,
    overrides: Mapping[str, str] | None = None,
) -> PatternBinding:
    """Resolve each pattern role to a reference in the anchor's group.

    Deterministic: explicit overrides bind first, then roles in file order, so
    an override for one bypass capacitor leaves exactly one candidate for its
    twin. Ambiguity is an error rather than a guess -- picking one of two
    identical capacitors silently would report a pass for the wrong part.
    """
    references = tuple(group_references)
    if anchor not in references:
        raise PatternInputError(
            f"pattern anchor {anchor} is not a reference in its group"
        )
    partnumber = facts.partnumbers.get(anchor, "")
    if re.fullmatch(pattern.part.partnumber_match, partnumber) is None:
        raise PatternInputError(
            f"pattern {pattern.identifier} expects a part matching "
            f"{pattern.part.partnumber_match!r}; {anchor} is {partnumber or 'unknown'}"
        )
    footprint = facts.footprints.get(anchor, "")
    if re.search(pattern.part.footprint_match, footprint) is None:
        raise PatternInputError(
            f"pattern {pattern.identifier} expects a footprint matching "
            f"{pattern.part.footprint_match!r}; {anchor} is {footprint or 'unknown'}"
        )

    anchor_nets = dict(facts.pad_nets.get(anchor, ()))
    resolved: dict[str, str | None] = {}
    used: set[str] = set()

    for role_id, reference in sorted((overrides or {}).items()):
        try:
            role = pattern.role(role_id)
        except KeyError as exc:
            raise PatternInputError(
                f"bind names unknown role {role_id!r} of pattern {pattern.identifier}"
            ) from exc
        if reference not in references or reference == anchor:
            raise PatternInputError(
                f"bind maps role {role_id} to {reference}, which is not another "
                "reference in the group"
            )
        nets = frozenset(
            anchor_nets[pad] for pad in role.anchor_pads if pad in anchor_nets
        )
        if not _satellite_matches(reference, role, nets, facts):
            raise PatternInputError(
                f"bind maps role {role_id} to {reference}, which shares no net with "
                f"{anchor} pads {', '.join(role.anchor_pads)}"
            )
        resolved[role_id] = reference
        used.add(reference)

    for role in pattern.roles:
        if role.identifier in resolved:
            continue
        missing = [pad for pad in role.anchor_pads if pad not in anchor_nets]
        if missing:
            raise PatternInputError(
                f"pattern {pattern.identifier} role {role.identifier} names "
                f"{anchor} pad(s) {', '.join(missing)}, which carry no net"
            )
        nets = frozenset(anchor_nets[pad] for pad in role.anchor_pads)
        candidates = [
            reference
            for reference in references
            if reference != anchor
            and reference not in used
            and _satellite_matches(reference, role, nets, facts)
        ]
        if len(candidates) > 1:
            raise PatternInputError(
                f"pattern {pattern.identifier} role {role.identifier} matches "
                f"{', '.join(candidates)}; add an explicit bind: entry"
            )
        if candidates:
            resolved[role.identifier] = candidates[0]
            used.add(candidates[0])
        else:
            resolved[role.identifier] = None

    return PatternBinding(
        pattern,
        anchor,
        tuple((role.identifier, resolved[role.identifier]) for role in pattern.roles),
    )


__all__ = [
    "DEFAULT_TOLERANCE_MM",
    "PATTERNS_DIRNAME",
    "PATTERN_SCHEMA",
    "BoardFacts",
    "Pattern",
    "PatternBinding",
    "PatternError",
    "PatternInputError",
    "PatternPart",
    "PatternRole",
    "PatternRule",
    "PatternSource",
    "bind",
    "board_facts",
    "read_pattern",
    "resolve_pattern_path",
]
