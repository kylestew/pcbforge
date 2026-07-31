"""Structured CIRCUIT-to-LAYOUT handoff generation and validation."""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from pcbforge.build_test import (
    BuildTestError,
    BuildTestInputError,
    BoardEvidence,
    fingerprint_inputs,
    read_board_evidence,
    saved_report_status,
)
from pcbforge.initialize import InitInputError, read_spec

PLACEMENT_SCHEMA = 1
BRIEF_SCHEMA = 1
PROJECT_PIN_SCHEMA = 1
PLACEMENT_FILENAME = "placement.yaml"
BRIEF_FILENAME = "docs/placement-brief.md"
OWNED_CLASS_PREFIX = "pcbforge:"
CONTROLLED_CLASS_FIELDS = (
    "name",
    "clearance",
    "track_width",
    "via_diameter",
    "via_drill",
    "diff_pair_width",
    "diff_pair_gap",
    "diff_pair_via_gap",
    "priority",
)

ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
REFERENCE_RE = re.compile(r"^[A-Za-z]+[A-Za-z0-9_-]*$")
ENDPOINT_RE = re.compile(
    r"^(?P<reference>[A-Za-z]+[A-Za-z0-9_-]*)(?:\.(?P<pad>[^.\s]+))?$"
)
CONSTRAINT_TYPES = {
    "proximity",
    "separation",
    "board-edge",
    "keepout",
    "orientation",
    "accessibility",
    "airflow",
}
EDGES = {"any", "north", "east", "south", "west"}


class PlacementError(RuntimeError):
    """Layout-handoff generation or verification failed."""


class PlacementInputError(PlacementError):
    """The project or placement contract is malformed."""


@dataclass(frozen=True)
class PlacementGroup:
    identifier: str
    priority: int
    region: str
    rationale: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class PlacementConstraint:
    identifier: str
    kind: str
    subjects: tuple[str, ...]
    rationale: str
    min_mm: float | None
    max_mm: float | None
    edge: str | None
    direction: str | None
    keepout: str | None


@dataclass(frozen=True)
class DifferentialPair:
    width_mm: float
    gap_mm: float
    via_gap_mm: float


@dataclass(frozen=True)
class PlacementNetClass:
    name: str
    rationale: str
    nets: tuple[str, ...]
    clearance_mm: float
    track_width_mm: float
    via_diameter_mm: float
    via_drill_mm: float
    differential_pair: DifferentialPair | None

    @property
    def kicad_name(self) -> str:
        return f"{OWNED_CLASS_PREFIX}{self.name}"


@dataclass(frozen=True)
class PlacementContract:
    strategy: str
    board_rules: tuple[str, ...]
    groups: tuple[PlacementGroup, ...]
    placement_order: tuple[str, ...]
    constraints: tuple[PlacementConstraint, ...]
    net_classes: tuple[PlacementNetClass, ...]
    checklist: tuple[str, ...]


@dataclass(frozen=True)
class BriefResult:
    project_dir: Path
    fingerprint: str
    group_count: int
    constraint_count: int
    net_class_count: int
    reference_count: int
    brief_path: Path
    project_path: Path
    wrote_brief: bool
    wrote_project: bool

    @property
    def summary(self) -> str:
        return (
            f"{self.reference_count} references in {self.group_count} groups, "
            f"{self.constraint_count} constraints, and "
            f"{self.net_class_count} net classes"
        )


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError("mapping keys must be scalar values") from exc
        if duplicate:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlacementInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise PlacementInputError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise PlacementInputError(f"invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlacementInputError(f"{label} must be a YAML mapping")
    return data


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
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}: expected a non-empty string")
        return ""
    return value.strip()


def _string_list(
    value: Any,
    prefix: str,
    errors: list[str],
    *,
    nonempty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        errors.append(f"{prefix}: expected a {qualifier}list of strings")
        return ()
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{prefix}[{index}]: expected a non-empty string")
        else:
            result.append(item.strip())
    duplicates = sorted(item for item in set(result) if result.count(item) > 1)
    if duplicates:
        errors.append(f"{prefix}: duplicate values: {', '.join(duplicates)}")
    return tuple(result)


def _positive_number(
    value: Any,
    prefix: str,
    errors: list[str],
    *,
    required: bool,
) -> float | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        errors.append(f"{prefix}: expected a positive number")
        return None
    return float(value)


def _duplicates(values: Sequence[str]) -> list[str]:
    return sorted(item for item in set(values) if values.count(item) > 1)


def _read_rules(
    tool_root: Path,
    layers: int,
    pins: Mapping[str, Any],
) -> Mapping[str, float]:
    path = tool_root / "rules" / f"jlc-{layers}layer.json"
    try:
        contents = path.read_bytes()
        data = json.loads(contents)
        rules = data["rules"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PlacementError(f"cannot load rules profile {path}: {exc}") from exc
    if not isinstance(rules, dict):
        raise PlacementError(f"invalid rules profile {path}")
    pinned = pins.get("rules")
    if not isinstance(pinned, dict):
        raise PlacementInputError(".pcbforge rules: expected a mapping")
    expected_name = pinned.get("profile")
    expected_hash = pinned.get("profile_sha256")
    actual_hash = hashlib.sha256(contents).hexdigest()
    if expected_name != data.get("name") or expected_hash != actual_hash:
        raise PlacementInputError(
            "pinned JLC rules profile does not match the current tool profile: "
            f"expected {expected_name!r} / {expected_hash!r}, got "
            f"{data.get('name')!r} / {actual_hash!r}"
        )
    return rules


def _read_project_pins(project_dir: Path) -> Mapping[str, Any]:
    data = _load_yaml(project_dir / ".pcbforge", ".pcbforge")
    errors = []
    if type(data.get("schema")) is not int or data.get("schema") != PROJECT_PIN_SCHEMA:
        errors.append("schema: unsupported version — restart the project")
    guidance = data.get("guidance")
    if not isinstance(guidance, dict):
        errors.append("guidance: expected a mapping")
    else:
        for key in ("layout_handoff_schema", "approval_schema", "policy_schema"):
            if type(guidance.get(key)) is not int or guidance.get(key) != 1:
                errors.append(f"guidance.{key}: unsupported version — restart the project")
    if errors:
        raise PlacementInputError(
            "invalid project guidance for the layout handoff:\n  - "
            + "\n  - ".join(errors)
        )
    return data


def _parse_groups(raw: Any, errors: list[str]) -> tuple[PlacementGroup, ...]:
    if not isinstance(raw, list) or not raw:
        errors.append("groups: expected a non-empty list")
        return ()
    groups = []
    for index, item in enumerate(raw):
        prefix = f"groups[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        _unknown(
            item,
            {"id", "priority", "region", "rationale", "references"},
            prefix,
            errors,
        )
        identifier = _text(item, "id", prefix, errors)
        if identifier and ID_RE.fullmatch(identifier) is None:
            errors.append(f"{prefix}.id: expected a kebab-case ID")
        priority = item.get("priority")
        if type(priority) is not int or priority <= 0:
            errors.append(f"{prefix}.priority: expected a positive integer")
            priority = 0
        region = _text(item, "region", prefix, errors)
        rationale = _text(item, "rationale", prefix, errors)
        references = _string_list(
            item.get("references"),
            f"{prefix}.references",
            errors,
        )
        for reference in references:
            if REFERENCE_RE.fullmatch(reference) is None:
                errors.append(f"{prefix}.references: invalid reference {reference!r}")
        groups.append(
            PlacementGroup(
                identifier,
                priority,
                region,
                rationale,
                references,
            )
        )
    identifiers = [group.identifier for group in groups if group.identifier]
    duplicate_ids = _duplicates(identifiers)
    if duplicate_ids:
        errors.append(f"groups: duplicate IDs: {', '.join(duplicate_ids)}")
    priorities = [group.priority for group in groups if group.priority]
    duplicate_priorities = sorted(
        str(priority) for priority in set(priorities) if priorities.count(priority) > 1
    )
    if duplicate_priorities:
        errors.append(
            "groups: duplicate priorities: " + ", ".join(duplicate_priorities)
        )
    return tuple(groups)


def _parse_constraints(
    raw: Any,
    errors: list[str],
) -> tuple[PlacementConstraint, ...]:
    if not isinstance(raw, list):
        errors.append("constraints: expected a list")
        return ()
    constraints = []
    allowed = {
        "id",
        "type",
        "subjects",
        "rationale",
        "min_mm",
        "max_mm",
        "edge",
        "direction",
        "keepout",
    }
    for index, item in enumerate(raw):
        prefix = f"constraints[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        _unknown(item, allowed, prefix, errors)
        identifier = _text(item, "id", prefix, errors)
        if identifier and ID_RE.fullmatch(identifier) is None:
            errors.append(f"{prefix}.id: expected a kebab-case ID")
        kind = _text(item, "type", prefix, errors)
        if kind and kind not in CONSTRAINT_TYPES:
            errors.append(
                f"{prefix}.type: expected one of " + ", ".join(sorted(CONSTRAINT_TYPES))
            )
        subjects = _string_list(
            item.get("subjects"),
            f"{prefix}.subjects",
            errors,
        )
        rationale = _text(item, "rationale", prefix, errors)
        min_mm = _positive_number(
            item.get("min_mm"),
            f"{prefix}.min_mm",
            errors,
            required=kind in {"separation", "keepout"},
        )
        max_mm = _positive_number(
            item.get("max_mm"),
            f"{prefix}.max_mm",
            errors,
            required=kind in {"proximity", "board-edge"},
        )
        edge_raw = item.get("edge")
        edge = edge_raw.strip() if isinstance(edge_raw, str) else None
        direction_raw = item.get("direction")
        direction = direction_raw.strip() if isinstance(direction_raw, str) else None
        keepout_raw = item.get("keepout")
        keepout = keepout_raw.strip() if isinstance(keepout_raw, str) else None

        if kind in {"proximity", "separation"} and len(subjects) != 2:
            errors.append(f"{prefix}.subjects: {kind} requires exactly two endpoints")
        if kind == "board-edge" and len(subjects) != 1:
            errors.append(
                f"{prefix}.subjects: board-edge requires exactly one endpoint"
            )
        if kind in {"orientation", "accessibility"} and len(subjects) != 1:
            errors.append(f"{prefix}.subjects: {kind} requires exactly one reference")
        if kind == "airflow" and len(subjects) < 2:
            errors.append(
                f"{prefix}.subjects: airflow requires at least two references"
            )
        if kind == "keepout" and not keepout:
            errors.append(f"{prefix}.keepout: expected a non-empty description")
        if kind in {"board-edge", "accessibility"}:
            if edge not in EDGES:
                errors.append(
                    f"{prefix}.edge: expected one of {', '.join(sorted(EDGES))}"
                )
        elif edge is not None:
            errors.append(f"{prefix}.edge: not allowed for {kind or 'this type'}")
        if kind in {"orientation", "airflow"}:
            if not direction:
                errors.append(f"{prefix}.direction: expected a non-empty direction")
        elif direction is not None:
            errors.append(f"{prefix}.direction: not allowed for {kind or 'this type'}")
        if kind not in {"separation", "keepout"} and min_mm is not None:
            errors.append(f"{prefix}.min_mm: not allowed for {kind or 'this type'}")
        if kind not in {"proximity", "board-edge"} and max_mm is not None:
            errors.append(f"{prefix}.max_mm: not allowed for {kind or 'this type'}")
        if kind != "keepout" and keepout is not None:
            errors.append(f"{prefix}.keepout: not allowed for {kind or 'this type'}")
        if kind in {"orientation", "accessibility", "airflow"}:
            for subject in subjects:
                match = ENDPOINT_RE.fullmatch(subject)
                if match is not None and match.group("pad") is not None:
                    errors.append(
                        f"{prefix}.subjects: {kind} accepts references, not pads"
                    )
        constraints.append(
            PlacementConstraint(
                identifier,
                kind,
                subjects,
                rationale,
                min_mm,
                max_mm,
                edge,
                direction,
                keepout,
            )
        )
    identifiers = [
        constraint.identifier for constraint in constraints if constraint.identifier
    ]
    duplicate_ids = _duplicates(identifiers)
    if duplicate_ids:
        errors.append(f"constraints: duplicate IDs: {', '.join(duplicate_ids)}")
    return tuple(constraints)


def _parse_diff_pair(
    raw: Any,
    prefix: str,
    errors: list[str],
) -> DifferentialPair | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{prefix}: expected a mapping")
        return None
    _unknown(raw, {"width_mm", "gap_mm", "via_gap_mm"}, prefix, errors)
    width = _positive_number(
        raw.get("width_mm"), f"{prefix}.width_mm", errors, required=True
    )
    gap = _positive_number(raw.get("gap_mm"), f"{prefix}.gap_mm", errors, required=True)
    via_gap = _positive_number(
        raw.get("via_gap_mm"),
        f"{prefix}.via_gap_mm",
        errors,
        required=True,
    )
    if width is None or gap is None or via_gap is None:
        return None
    return DifferentialPair(width, gap, via_gap)


def _parse_net_classes(
    raw: Any,
    errors: list[str],
) -> tuple[PlacementNetClass, ...]:
    if not isinstance(raw, list) or not raw:
        errors.append("net_classes: expected a non-empty list")
        return ()
    classes = []
    allowed = {
        "name",
        "rationale",
        "nets",
        "clearance_mm",
        "track_width_mm",
        "via_diameter_mm",
        "via_drill_mm",
        "differential_pair",
    }
    for index, item in enumerate(raw):
        prefix = f"net_classes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        _unknown(item, allowed, prefix, errors)
        name = _text(item, "name", prefix, errors)
        if name and ID_RE.fullmatch(name) is None:
            errors.append(f"{prefix}.name: expected a kebab-case ID")
        rationale = _text(item, "rationale", prefix, errors)
        nets = _string_list(item.get("nets"), f"{prefix}.nets", errors)
        clearance = _positive_number(
            item.get("clearance_mm"),
            f"{prefix}.clearance_mm",
            errors,
            required=True,
        )
        width = _positive_number(
            item.get("track_width_mm"),
            f"{prefix}.track_width_mm",
            errors,
            required=True,
        )
        via_diameter = _positive_number(
            item.get("via_diameter_mm"),
            f"{prefix}.via_diameter_mm",
            errors,
            required=True,
        )
        via_drill = _positive_number(
            item.get("via_drill_mm"),
            f"{prefix}.via_drill_mm",
            errors,
            required=True,
        )
        differential_pair = _parse_diff_pair(
            item.get("differential_pair"),
            f"{prefix}.differential_pair",
            errors,
        )
        classes.append(
            PlacementNetClass(
                name,
                rationale,
                nets,
                clearance or 0,
                width or 0,
                via_diameter or 0,
                via_drill or 0,
                differential_pair,
            )
        )
    names = [item.name for item in classes if item.name]
    duplicate_names = _duplicates(names)
    if duplicate_names:
        errors.append(f"net_classes: duplicate names: {', '.join(duplicate_names)}")
    all_nets = [net for item in classes for net in item.nets]
    duplicate_nets = _duplicates(all_nets)
    if duplicate_nets:
        errors.append(
            "net_classes: nets assigned to multiple classes: "
            + ", ".join(duplicate_nets)
        )
    return tuple(classes)


def _validate_against_board(
    contract: PlacementContract,
    board: BoardEvidence,
    rules: Mapping[str, float],
    errors: list[str],
) -> None:
    duplicate_board_refs = _duplicates(board.references)
    if duplicate_board_refs:
        errors.append(
            "PCB contains duplicate references: " + ", ".join(duplicate_board_refs)
        )
    if "" in board.references:
        errors.append("PCB contains a footprint without a Reference property")
    board_references = set(board.references)
    grouped = [reference for group in contract.groups for reference in group.references]
    duplicate_refs = _duplicates(grouped)
    if duplicate_refs:
        errors.append(
            "groups: references assigned more than once: " + ", ".join(duplicate_refs)
        )
    missing_refs = sorted(board_references - set(grouped))
    unexpected_refs = sorted(set(grouped) - board_references)
    if missing_refs:
        errors.append(
            "groups: every footprint must be assigned; missing: "
            + ", ".join(missing_refs)
        )
    if unexpected_refs:
        errors.append(
            "groups: references not present on the PCB: " + ", ".join(unexpected_refs)
        )

    group_ids = [group.identifier for group in contract.groups]
    missing_groups = sorted(set(group_ids) - set(contract.placement_order))
    unexpected_groups = sorted(set(contract.placement_order) - set(group_ids))
    duplicate_order = _duplicates(contract.placement_order)
    if missing_groups:
        errors.append("placement_order: missing groups: " + ", ".join(missing_groups))
    if unexpected_groups:
        errors.append(
            "placement_order: unknown groups: " + ", ".join(unexpected_groups)
        )
    if duplicate_order:
        errors.append(
            "placement_order: duplicate groups: " + ", ".join(duplicate_order)
        )

    board_pads = set(board.pads)
    for constraint in contract.constraints:
        for endpoint in constraint.subjects:
            match = ENDPOINT_RE.fullmatch(endpoint)
            if match is None:
                errors.append(
                    f"constraints.{constraint.identifier}.subjects: "
                    f"invalid endpoint {endpoint!r}"
                )
                continue
            reference = match.group("reference")
            pad = match.group("pad")
            if reference not in board_references:
                errors.append(
                    f"constraints.{constraint.identifier}.subjects: "
                    f"unknown reference {reference!r}"
                )
            elif pad is not None and (reference, pad) not in board_pads:
                errors.append(
                    f"constraints.{constraint.identifier}.subjects: "
                    f"unknown pad {endpoint!r}"
                )

    board_nets = {net for _, _, net in board.pad_nets if net}
    for net_class in contract.net_classes:
        unknown_nets = sorted(set(net_class.nets) - board_nets)
        if unknown_nets:
            errors.append(
                f"net_classes.{net_class.name}.nets: unknown exact nets: "
                + ", ".join(unknown_nets)
            )
        checks = (
            ("clearance_mm", net_class.clearance_mm, "min_clearance_mm"),
            ("track_width_mm", net_class.track_width_mm, "min_track_width_mm"),
            ("via_diameter_mm", net_class.via_diameter_mm, "min_via_diameter_mm"),
            ("via_drill_mm", net_class.via_drill_mm, "min_via_drill_mm"),
        )
        for field, value, rule in checks:
            minimum = float(rules[rule])
            if value and value < minimum:
                errors.append(
                    f"net_classes.{net_class.name}.{field}: {value:g} mm is "
                    f"below profile minimum {minimum:g} mm"
                )
        if net_class.via_diameter_mm <= net_class.via_drill_mm:
            errors.append(
                f"net_classes.{net_class.name}: via diameter must exceed drill"
            )
        annular = (net_class.via_diameter_mm - net_class.via_drill_mm) / 2
        minimum_annular = float(rules["min_via_annular_width_mm"])
        if annular < minimum_annular:
            errors.append(
                f"net_classes.{net_class.name}: via annular width "
                f"{annular:g} mm is below profile minimum {minimum_annular:g} mm"
            )
        pair = net_class.differential_pair
        if pair is not None:
            if pair.width_mm < float(rules["min_track_width_mm"]):
                errors.append(
                    f"net_classes.{net_class.name}.differential_pair.width_mm: "
                    "below profile minimum"
                )
            if pair.gap_mm < float(rules["min_clearance_mm"]):
                errors.append(
                    f"net_classes.{net_class.name}.differential_pair.gap_mm: "
                    "below profile minimum"
                )
            if pair.via_gap_mm < float(rules["min_clearance_mm"]):
                errors.append(
                    f"net_classes.{net_class.name}.differential_pair.via_gap_mm: "
                    "below profile minimum"
                )


def read_placement_contract(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    board: BoardEvidence | None = None,
) -> PlacementContract:
    """Read and strictly validate placement.yaml schema 1."""
    project_dir = project_dir.expanduser().resolve()
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise PlacementInputError(str(exc)) from exc
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    pins = _read_project_pins(project_dir)
    data = _load_yaml(project_dir / PLACEMENT_FILENAME, PLACEMENT_FILENAME)
    errors: list[str] = []
    _unknown(
        data,
        {
            "placement_schema",
            "board",
            "groups",
            "placement_order",
            "constraints",
            "net_classes",
            "checklist",
        },
        PLACEMENT_FILENAME,
        errors,
    )
    if type(data.get("placement_schema")) is not int or data.get(
        "placement_schema"
    ) != PLACEMENT_SCHEMA:
        errors.append("placement_schema: unsupported version — restart the project")
    board_raw = data.get("board")
    strategy = ""
    board_rules: tuple[str, ...] = ()
    if not isinstance(board_raw, dict):
        errors.append("board: expected a mapping")
    else:
        _unknown(board_raw, {"strategy", "rules"}, "board", errors)
        strategy = _text(board_raw, "strategy", "board", errors)
        board_rules = _string_list(board_raw.get("rules"), "board.rules", errors)
    groups = _parse_groups(data.get("groups"), errors)
    placement_order = _string_list(
        data.get("placement_order"),
        "placement_order",
        errors,
    )
    constraints = _parse_constraints(data.get("constraints"), errors)
    net_classes = _parse_net_classes(data.get("net_classes"), errors)
    checklist = _string_list(data.get("checklist"), "checklist", errors)
    contract = PlacementContract(
        strategy,
        board_rules,
        groups,
        placement_order,
        constraints,
        net_classes,
        checklist,
    )
    if board is None:
        try:
            board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
        except (BuildTestInputError, BuildTestError) as exc:
            raise PlacementInputError(str(exc)) from exc
    rules = _read_rules(tool_root, spec.layers, pins)
    _validate_against_board(contract, board, rules, errors)
    if errors:
        raise PlacementInputError(
            f"invalid {PLACEMENT_FILENAME}:\n  - " + "\n  - ".join(errors)
        )
    return contract


def _read_project(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PlacementInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlacementInputError(f"invalid KiCad project {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlacementInputError(f"KiCad project {path} must be a JSON mapping")
    net_settings = data.get("net_settings")
    if not isinstance(net_settings, dict):
        raise PlacementInputError(f"{path.name}: missing net_settings mapping")
    classes = net_settings.get("classes")
    patterns = net_settings.get("netclass_patterns")
    if not isinstance(classes, list) or not all(
        isinstance(item, dict) for item in classes
    ):
        raise PlacementInputError(f"{path.name}: net_settings.classes must be a list")
    if not isinstance(patterns, list) or not all(
        isinstance(item, dict) for item in patterns
    ):
        raise PlacementInputError(
            f"{path.name}: net_settings.netclass_patterns must be a list"
        )
    if not any(item.get("name") == "Default" for item in classes):
        raise PlacementInputError(f"{path.name}: missing Default net class")
    return data


def _validate_user_class_conflicts(
    project: Mapping[str, Any],
    contract: PlacementContract,
) -> None:
    settings = project["net_settings"]
    contract_nets = {
        net for net_class in contract.net_classes for net in net_class.nets
    }
    conflicts = []
    for pattern in settings["netclass_patterns"]:
        netclass = str(pattern.get("netclass", ""))
        value = pattern.get("pattern")
        if not netclass.startswith(OWNED_CLASS_PREFIX) and isinstance(value, str):
            for net in contract_nets:
                if fnmatch.fnmatchcase(net, value):
                    conflicts.append(
                        f"{net} matches {value!r} ({netclass or 'unnamed class'})"
                    )
    assignments = settings.get("netclass_assignments")
    if isinstance(assignments, dict):
        for net, netclass in assignments.items():
            if (
                isinstance(net, str)
                and net in contract_nets
                and not str(netclass).startswith(OWNED_CLASS_PREFIX)
            ):
                conflicts.append(f"{net} ({netclass})")
    if conflicts:
        raise PlacementInputError(
            "KiCad user net-class assignments conflict with placement.yaml "
            "exact nets: " + ", ".join(sorted(set(conflicts)))
        )


def _class_payload(
    net_class: PlacementNetClass,
    template: Mapping[str, Any],
    priority: int | None,
) -> dict[str, Any]:
    payload = copy.deepcopy(dict(template))
    pair = net_class.differential_pair
    payload.update(
        {
            "name": net_class.kicad_name,
            "clearance": net_class.clearance_mm,
            "track_width": net_class.track_width_mm,
            "via_diameter": net_class.via_diameter_mm,
            "via_drill": net_class.via_drill_mm,
            "diff_pair_width": (
                pair.width_mm if pair is not None else net_class.track_width_mm
            ),
            "diff_pair_gap": (
                pair.gap_mm if pair is not None else net_class.clearance_mm
            ),
            "diff_pair_via_gap": (
                pair.via_gap_mm if pair is not None else net_class.clearance_mm
            ),
        }
    )
    if priority is not None:
        payload["priority"] = priority
    return payload


def _merged_project(
    project: Mapping[str, Any],
    contract: PlacementContract,
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(project))
    settings = merged["net_settings"]
    classes = settings["classes"]
    default_template = next(item for item in classes if item.get("name") == "Default")
    existing_owned = {
        str(item.get("name")): item
        for item in classes
        if str(item.get("name", "")).startswith(OWNED_CLASS_PREFIX)
    }
    user_classes = [
        item
        for item in classes
        if not str(item.get("name", "")).startswith(OWNED_CLASS_PREFIX)
    ]
    priority_enabled = "priority" in default_template or any(
        "priority" in item for item in existing_owned.values()
    )
    used_priorities = {
        item["priority"]
        for item in user_classes
        if item.get("name") != "Default"
        and type(item.get("priority")) is int
        and item["priority"] >= 0
    }
    owned_classes = []
    for net_class in sorted(contract.net_classes, key=lambda item: item.name):
        existing = existing_owned.get(net_class.kicad_name)
        existing_priority = existing.get("priority") if existing is not None else None
        if (
            priority_enabled
            and type(existing_priority) is int
            and existing_priority >= 0
            and existing_priority not in used_priorities
        ):
            priority = existing_priority
        elif priority_enabled:
            priority = next(
                candidate
                for candidate in range(
                    len(used_priorities) + len(contract.net_classes) + 1
                )
                if candidate not in used_priorities
            )
        else:
            priority = None
        if priority is not None:
            used_priorities.add(priority)
        owned_classes.append(
            _class_payload(
                net_class,
                existing or default_template,
                priority,
            )
        )
    settings["classes"] = user_classes + owned_classes
    user_patterns = [
        item
        for item in settings["netclass_patterns"]
        if not str(item.get("netclass", "")).startswith(OWNED_CLASS_PREFIX)
    ]
    owned_patterns = [
        {"netclass": net_class.kicad_name, "pattern": net}
        for net_class in sorted(contract.net_classes, key=lambda item: item.name)
        for net in sorted(net_class.nets)
    ]
    settings["netclass_patterns"] = user_patterns + owned_patterns
    return merged


def _owned_project_semantics(project: Mapping[str, Any]) -> Mapping[str, Any]:
    settings = project.get("net_settings")
    if not isinstance(settings, dict):
        return {"invalid": True}
    classes = settings.get("classes")
    patterns = settings.get("netclass_patterns")
    return {
        "classes": sorted(
            (
                {key: item.get(key) for key in CONTROLLED_CLASS_FIELDS if key in item}
                for item in classes
                if isinstance(item, dict)
                and str(item.get("name", "")).startswith(OWNED_CLASS_PREFIX)
            ),
            key=lambda item: str(item.get("name", "")),
        )
        if isinstance(classes, list)
        else [],
        "patterns": sorted(
            (
                item
                for item in patterns
                if isinstance(item, dict)
                and str(item.get("netclass", "")).startswith(OWNED_CLASS_PREFIX)
            ),
            key=lambda item: (
                str(item.get("netclass", "")),
                str(item.get("pattern", "")),
            ),
        )
        if isinstance(patterns, list)
        else [],
    }


def _topology_semantics(board: BoardEvidence) -> Mapping[str, Any]:
    return {
        "references": board.references,
        "footprints": board.footprints,
        "pads": board.pads,
        "pad_nets": board.pad_nets,
    }


def _contract_fingerprint(
    project_dir: Path,
    board: BoardEvidence,
    merged_project: Mapping[str, Any],
) -> str:
    digest = hashlib.sha256()
    digest.update((project_dir / PLACEMENT_FILENAME).read_bytes())
    digest.update(b"\0circuit-acceptance\0")
    digest.update(fingerprint_inputs(project_dir).encode())
    digest.update(b"\0pcb-topology\0")
    digest.update(
        json.dumps(
            _topology_semantics(board),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    digest.update(b"\0pcbforge-net-classes\0")
    digest.update(
        json.dumps(
            _owned_project_semantics(merged_project),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return digest.hexdigest()


def brief_status_fingerprint(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> str:
    """Fingerprint handoff inputs/outputs without board positions or user classes."""
    project_dir = project_dir.expanduser().resolve()
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    digest = hashlib.sha256()
    paths = (project_dir / PLACEMENT_FILENAME, brief_document_path(project_dir))
    for path in paths:
        relative = path.relative_to(project_dir).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"<missing>")
    digest.update(b"\0circuit-acceptance\0")
    try:
        digest.update(fingerprint_inputs(project_dir).encode())
    except (BuildTestError, OSError) as exc:
        digest.update(f"<invalid:{exc}>".encode())
    project_path: Path | None = None
    try:
        spec = read_spec(project_dir / "spec.md")
        board_path = project_dir / f"{spec.name}.kicad_pcb"
        project_path = project_dir / f"{spec.name}.kicad_pro"
        rules_path = tool_root / "rules" / f"jlc-{spec.layers}layer.json"
        digest.update(b"\0rules-profile\0")
        digest.update(hashlib.sha256(rules_path.read_bytes()).digest())
        board = read_board_evidence(board_path)
        topology: Any = _topology_semantics(board)
    except (InitInputError, BuildTestInputError, BuildTestError, OSError):
        board_paths = sorted(project_dir.glob("*.kicad_pcb"))
        topology = {
            "invalid_board": [
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in board_paths
                if path.is_file()
            ]
        }
    digest.update(json.dumps(topology, separators=(",", ":"), sort_keys=True).encode())
    project_paths = (
        [project_path]
        if project_path is not None and project_path.is_file()
        else sorted(project_dir.glob("*.kicad_pro"))
    )
    if project_paths:
        try:
            project = json.loads(project_paths[0].read_text(encoding="utf-8"))
            semantics: Any = _owned_project_semantics(project)
        except (OSError, UnicodeError, json.JSONDecodeError):
            semantics = {
                "invalid_project": hashlib.sha256(
                    project_paths[0].read_bytes()
                ).hexdigest()
            }
    else:
        semantics = {"missing_project": True}
    digest.update(json.dumps(semantics, separators=(",", ":"), sort_keys=True).encode())
    return digest.hexdigest()


def brief_inputs(project_dir: Path) -> tuple[Path, ...]:
    """Return visible handoff inputs and outputs for dashboard diagnostics."""
    project_dir = project_dir.expanduser().resolve()
    paths = [
        project_dir / PLACEMENT_FILENAME,
        brief_document_path(project_dir),
        *sorted(project_dir.glob("*.kicad_pcb")),
        *sorted(project_dir.glob("*.kicad_pro")),
    ]
    return tuple(path for path in paths if path.is_file())


def brief_document_path(project_dir: Path) -> Path:
    """Return the current generated placement-brief path."""
    return project_dir.expanduser().resolve() / BRIEF_FILENAME


def _distance(constraint: PlacementConstraint) -> str:
    if constraint.min_mm is not None:
        return f"minimum {constraint.min_mm:g} mm"
    if constraint.max_mm is not None:
        return f"maximum {constraint.max_mm:g} mm"
    return "n/a"


def _constraint_instruction(constraint: PlacementConstraint) -> str:
    detail = {
        "proximity": f"Keep endpoints within {_distance(constraint)}.",
        "separation": f"Separate endpoints by {_distance(constraint)}.",
        "board-edge": (
            f"Keep subject within {_distance(constraint)} of the "
            f"{constraint.edge} board edge."
        ),
        "keepout": (
            f"Maintain {_distance(constraint)} clearance from {constraint.keepout}."
        ),
        "orientation": f"Orient toward {constraint.direction}.",
        "accessibility": f"Keep accessible from the {constraint.edge} edge.",
        "airflow": f"Arrange for airflow toward {constraint.direction}.",
    }
    return detail[constraint.kind]


def _render_brief(
    project_name: str,
    contract: PlacementContract,
    fingerprint: str,
) -> str:
    metadata = yaml.safe_dump(
        {
            "pcbforge_brief_schema": BRIEF_SCHEMA,
            "placement_schema": PLACEMENT_SCHEMA,
            "fingerprint": fingerprint,
        },
        sort_keys=False,
    ).rstrip()
    groups_by_id = {group.identifier: group for group in contract.groups}
    group_sections = []
    for index, identifier in enumerate(contract.placement_order, start=1):
        group = groups_by_id[identifier]
        group_sections.append(
            f"### {index}. {group.identifier}\n\n"
            f"- Priority: {group.priority}\n"
            f"- Suggested region: {group.region}\n"
            f"- References: {', '.join(group.references)}\n"
            f"- Why: {group.rationale}"
        )
    constraint_rows = "\n".join(
        "| "
        + " | ".join(
            (
                item.identifier,
                item.kind,
                ", ".join(item.subjects),
                _constraint_instruction(item),
                item.rationale,
            )
        ).replace("\n", " ")
        + " |"
        for item in contract.constraints
    )
    if not constraint_rows:
        constraint_rows = "| None | — | — | — | — |"
    net_rows = "\n".join(
        "| "
        + " | ".join(
            (
                item.kicad_name,
                ", ".join(item.nets),
                f"{item.track_width_mm:g}",
                f"{item.clearance_mm:g}",
                f"{item.via_diameter_mm:g}/{item.via_drill_mm:g}",
                (
                    f"{item.differential_pair.width_mm:g}/"
                    f"{item.differential_pair.gap_mm:g}/"
                    f"{item.differential_pair.via_gap_mm:g}"
                    if item.differential_pair is not None
                    else "—"
                ),
                item.rationale,
            )
        ).replace("\n", " ")
        + " |"
        for item in contract.net_classes
    )
    return f"""---
{metadata}
---
# {project_name} placement brief

> Generated by PCBForge from `placement.yaml`. This is qualitative guidance,
> not placement geometry. The user owns every footprint position and all copper.

## Board strategy

{contract.strategy}

## Board-wide rules

{chr(10).join(f"- {rule}" for rule in contract.board_rules)}

## Placement order and groups

{chr(10).join(group_sections)}

## Typed constraints

| ID | Type | Subjects | Instruction | Rationale |
|---|---|---|---|---|
{constraint_rows}

## Seeded net classes

Only the exact listed nets are assigned. PCBForge owns classes whose names
begin with `pcbforge:`; user-created classes remain untouched.

| KiCad class | Exact nets | Track mm | Clearance mm | Via/drill mm | Diff width/gap/via-gap mm | Rationale |
|---|---|---:|---:|---:|---|---|
{net_rows}

## Layout review checklist

{chr(10).join(f"- [ ] {item}" for item in contract.checklist)}

## Human approval gate

Before LAYOUT begins, review this handoff beside the current approved CIRCUIT
overview. Run `pcbforge status review layout --stage handoff`, present its exact fingerprint,
and wait for explicit user approval. Record that approval with
`pcbforge status approve layout --stage handoff --fingerprint <sha256> --note "Approved
docs/placement-brief.md beside the current CIRCUIT overview"`. If the circuit evidence is
missing, stale, or inadequate for placement decisions, block the handoff.
"""


def _json_text(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _stage_file(path: Path, contents: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return Path(temporary_name)


def _atomic_restore(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    temporary = _stage_file(path, original)
    os.replace(temporary, path)


def _commit_outputs(outputs: Sequence[tuple[Path, bytes]]) -> tuple[bool, ...]:
    try:
        originals = {
            path: path.read_bytes() if path.exists() else None for path, _ in outputs
        }
    except OSError as exc:
        raise PlacementError(f"cannot stage layout-handoff outputs: {exc}") from exc
    changed = tuple(originals[path] != contents for path, contents in outputs)
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for (path, contents), is_changed in zip(outputs, changed, strict=True):
            if is_changed:
                staged[path] = _stage_file(path, contents)
        for path, _ in outputs:
            if path in staged:
                os.replace(staged.pop(path), path)
                replaced.append(path)
    except OSError as exc:
        rollback_errors = []
        for path in reversed(replaced):
            try:
                _atomic_restore(path, originals[path])
            except OSError as rollback:
                rollback_errors.append(f"{path.name}: {rollback}")
        detail = (
            "; rollback failed: " + "; ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise PlacementError(
            f"could not atomically write layout-handoff outputs: {exc}{detail}"
        ) from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return changed


def _project_context(
    project_dir: Path,
    tool_root: Path | None,
) -> tuple[Any, BoardEvidence, PlacementContract, Path, dict[str, Any]]:
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise PlacementInputError(f"project directory does not exist: {project_dir}")
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise PlacementInputError(str(exc)) from exc
    try:
        board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
    except (BuildTestInputError, BuildTestError) as exc:
        raise PlacementInputError(str(exc)) from exc
    contract = read_placement_contract(
        project_dir,
        tool_root=tool_root,
        board=board,
    )
    project_path = project_dir / f"{spec.name}.kicad_pro"
    project = _read_project(project_path)
    _validate_user_class_conflicts(project, contract)
    return spec, board, contract, project_path, project


def _require_circuit_acceptance(project_dir: Path) -> None:
    try:
        fingerprint = fingerprint_inputs(project_dir)
        ok, detail = saved_report_status(project_dir, fingerprint)
    except (BuildTestInputError, BuildTestError, OSError) as exc:
        raise PlacementInputError(
            f"CIRCUIT acceptance is not current: {exc}"
        ) from exc
    if not ok:
        raise PlacementInputError(f"CIRCUIT acceptance is not current: {detail}")


def generate_brief(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> BriefResult:
    """Generate docs/placement-brief.md and merge PCBForge-owned net classes."""
    project_dir = project_dir.expanduser().resolve()
    _require_circuit_acceptance(project_dir)
    spec, board, contract, project_path, project = _project_context(
        project_dir,
        tool_root,
    )
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    try:
        board_before = board_path.read_bytes()
    except OSError as exc:
        raise PlacementError(f"cannot read {board_path}: {exc}") from exc
    merged = _merged_project(project, contract)
    fingerprint = _contract_fingerprint(project_dir, board, merged)
    brief_text = _render_brief(spec.name, contract, fingerprint)
    project_text = _json_text(merged)
    brief_path = brief_document_path(project_dir)
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    wrote_brief, wrote_project = _commit_outputs(
        (
            (project_path, project_text.encode()),
            (brief_path, brief_text.encode()),
        )
    )
    try:
        board_after = board_path.read_bytes()
    except OSError as exc:
        raise PlacementError(f"cannot recheck {board_path}: {exc}") from exc
    if board_after != board_before:
        raise PlacementError(
            f"safety invariant failed: {board_path.name} changed during brief generation"
        )
    return BriefResult(
        project_dir,
        brief_status_fingerprint(project_dir, tool_root=tool_root),
        len(contract.groups),
        len(contract.constraints),
        len(contract.net_classes),
        len(board.references),
        brief_path.relative_to(project_dir),
        project_path.relative_to(project_dir),
        wrote_brief,
        wrote_project,
    )


def check_brief(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> BriefResult:
    """Validate current layout-handoff outputs without changing project files."""
    project_dir = project_dir.expanduser().resolve()
    _require_circuit_acceptance(project_dir)
    spec, board, contract, project_path, project = _project_context(
        project_dir,
        tool_root,
    )
    expected_project = _merged_project(project, contract)
    if _owned_project_semantics(project) != _owned_project_semantics(expected_project):
        raise PlacementError(
            f"{project_path.name}: PCBForge-owned net classes are missing or stale; "
            "run `pcbforge prepare-layout`"
        )
    fingerprint = _contract_fingerprint(project_dir, board, expected_project)
    expected_brief = _render_brief(spec.name, contract, fingerprint)
    brief_path = brief_document_path(project_dir)
    try:
        actual_brief = brief_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        relative = brief_path.relative_to(project_dir).as_posix()
        raise PlacementError(
            f"missing {relative}; run `pcbforge prepare-layout`"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise PlacementError(f"cannot read {brief_path}: {exc}") from exc
    if actual_brief != expected_brief:
        relative = brief_path.relative_to(project_dir).as_posix()
        raise PlacementError(
            f"{relative} is missing, modified, or stale; "
            "run `pcbforge prepare-layout`"
        )
    return BriefResult(
        project_dir,
        brief_status_fingerprint(project_dir, tool_root=tool_root),
        len(contract.groups),
        len(contract.constraints),
        len(contract.net_classes),
        len(board.references),
        brief_path.relative_to(project_dir),
        project_path.relative_to(project_dir),
        False,
        False,
    )
