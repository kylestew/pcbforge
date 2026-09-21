"""Create-only pcbforge project scaffolding."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.board_geometry import BOARD_FORMAT_VERSION
from pcbforge.policy import (
    POLICY_PROFILE_ID,
    POLICY_SCHEMA,
    PolicyError,
    PolicyInputError,
    check_policy,
    load_policy_profile,
)

from pcbforge.kicad_tools import VERSION as KICAD_VERSION
SPEC_SCHEMA = 1
PIN_SCHEMA = 2
AGENTS_SCHEMA = 2
ARCHITECT_GUIDE_SCHEMA = 2
ARCHITECTURE_DIAGRAM_SCHEMA = 2
MCU_GUIDE_SCHEMA = 2
CIRCUIT_GUIDE_SCHEMA = 2
ELECTRICAL_TEST_GUIDE_SCHEMA = 2
LAYOUT_HANDOFF_GUIDE_SCHEMA = 2
APPROVAL_GUIDE_SCHEMA = 2
CIRCUIT_REVIEW_SCHEMA = 4
POLICY_GUIDE_SCHEMA = POLICY_SCHEMA
STATUS_SCHEMA = 2
BOARD_ORIGIN_MM = 100.0

REQUIRED_KEYS = {
    "spec_schema",
    "name",
    "layers",
    "stm32_family",
    "power_in",
    "rails",
    "peripherals",
    "board_mm",
}
OPTIONAL_KEYS = {
    "connectors",
    "mounting",
    "qty",
    "bom_ceiling_usd",
    "modules_planned",
    "debug_uart",
    "special",
}
ALL_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS

STM32_FAMILIES = {"C0", "G0", "G4", "F0", "F1", "F4", "L0", "L4", "U5", "H7"}
POWER_INPUTS = {
    "usb-c",
    "battery-liion",
    "battery-aa",
    "barrel",
    "header",
    "other",
}
PERIPHERALS = {"usb-fs", "i2c", "spi", "uart", "adc", "dac", "pwm", "can", "other"}
SPECIALS = {
    "analog-precision",
    "rf",
    "high-current",
    "thermal",
    "low-power",
}

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RAIL_RE = re.compile(r"^\+(?=[A-Z0-9_]*[A-Z])[A-Z0-9_]+$")


class InitError(RuntimeError):
    """A runtime failure while initializing a project."""


class InitInputError(InitError):
    """A user-correctable project or spec input error."""


class DuplicateKeyError(yaml.YAMLError):
    """Raised when frontmatter contains a duplicate mapping key."""


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise DuplicateKeyError("mapping keys must be scalar values") from exc
        if duplicate:
            raise DuplicateKeyError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    layers: int
    stm32_family: str
    power_in: str
    rails: tuple[str, ...]
    peripherals: tuple[str, ...]
    board_mm: tuple[float, float]
    connectors: tuple[str, ...]
    mounting: str | None
    qty: int
    bom_ceiling_usd: float | None
    modules_planned: tuple[str, ...]
    debug_uart: bool
    special: tuple[str, ...]


@dataclass(frozen=True)
class InitResult:
    name: str
    project_dir: Path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _frontmatter(text: str) -> Mapping[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise InitInputError(
            "spec.md must begin with a YAML frontmatter delimiter ('---')"
        )

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise InitInputError(
            "spec.md is missing its closing frontmatter delimiter ('---')"
        ) from exc

    yaml_text = "\n".join(lines[1:end])
    try:
        loaded = yaml.load(yaml_text, Loader=_UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise InitInputError(f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(loaded, dict):
        raise InitInputError("spec.md frontmatter must be a YAML mapping")
    return loaded


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _string_list(
    data: Mapping[str, Any],
    key: str,
    errors: list[str],
    *,
    required: bool = False,
    choices: set[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        errors.append(f"{key}: expected a list of strings")
        return ()
    if required and not value:
        errors.append(f"{key}: must contain at least one value")

    values: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key}[{index}]: expected a non-empty string")
            continue
        if choices is not None and item not in choices:
            errors.append(
                f"{key}[{index}]: {item!r} is not one of {', '.join(sorted(choices))}"
            )
            continue
        if pattern is not None and pattern.fullmatch(item) is None:
            errors.append(f"{key}[{index}]: {item!r} is not a canonical power net name")
            continue
        values.append(item)

    if len(values) != len(set(values)):
        errors.append(f"{key}: values must be unique")
    return tuple(values)


def validate_spec(data: Mapping[str, Any]) -> ProjectSpec:
    """Validate and normalize the v1 spec frontmatter contract."""
    errors: list[str] = []

    missing = sorted(REQUIRED_KEYS - data.keys())
    if missing:
        errors.append(f"missing required keys: {', '.join(missing)}")
    unknown = sorted(set(data.keys()) - ALL_KEYS, key=str)
    if unknown:
        errors.append(f"unknown keys for spec_schema 1: {', '.join(map(str, unknown))}")

    schema = data.get("spec_schema")
    if type(schema) is not int or schema != SPEC_SCHEMA:
        errors.append("spec_schema: unsupported version — restart the project")

    name = data.get("name")
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        errors.append("name: expected kebab-case beginning with a letter")

    layers = data.get("layers")
    if type(layers) is not int or layers not in (2, 4):
        errors.append("layers: expected integer 2 or 4")

    stm32_family = data.get("stm32_family")
    if not isinstance(stm32_family, str) or stm32_family not in STM32_FAMILIES:
        errors.append(
            f"stm32_family: expected one of {', '.join(sorted(STM32_FAMILIES))}"
        )

    power_in = data.get("power_in")
    if not isinstance(power_in, str) or power_in not in POWER_INPUTS:
        errors.append(f"power_in: expected one of {', '.join(sorted(POWER_INPUTS))}")

    rails = _string_list(data, "rails", errors, required=True, pattern=RAIL_RE)
    peripherals = _string_list(data, "peripherals", errors, choices=PERIPHERALS)

    board_mm_raw = data.get("board_mm")
    board_mm = (0.0, 0.0)
    if (
        not isinstance(board_mm_raw, list)
        or len(board_mm_raw) != 2
        or not all(_is_number(item) for item in board_mm_raw)
    ):
        errors.append("board_mm: expected [positive width, positive height]")
    else:
        board_mm = (float(board_mm_raw[0]), float(board_mm_raw[1]))
        if board_mm[0] <= 0 or board_mm[1] <= 0:
            errors.append("board_mm: width and height must be positive")

    connectors = _string_list(data, "connectors", errors)
    modules_planned = _string_list(data, "modules_planned", errors)
    special = _string_list(data, "special", errors, choices=SPECIALS)

    mounting = data.get("mounting")
    if mounting is not None and (not isinstance(mounting, str) or not mounting.strip()):
        errors.append("mounting: expected a non-empty string")

    qty = data.get("qty", 5)
    if type(qty) is not int or qty <= 0:
        errors.append("qty: expected a positive integer")

    bom_ceiling = data.get("bom_ceiling_usd")
    if bom_ceiling is not None and (not _is_number(bom_ceiling) or bom_ceiling <= 0):
        errors.append("bom_ceiling_usd: expected a positive number")

    debug_uart = data.get("debug_uart", True)
    if type(debug_uart) is not bool:
        errors.append("debug_uart: expected a boolean")

    if errors:
        raise InitInputError(
            "invalid spec.md frontmatter:\n  - " + "\n  - ".join(errors)
        )

    assert isinstance(name, str)
    assert isinstance(layers, int)
    assert isinstance(stm32_family, str)
    assert isinstance(power_in, str)
    assert isinstance(qty, int)
    assert isinstance(debug_uart, bool)
    return ProjectSpec(
        name=name,
        layers=layers,
        stm32_family=stm32_family,
        power_in=power_in,
        rails=rails,
        peripherals=peripherals,
        board_mm=board_mm,
        connectors=connectors,
        mounting=mounting,
        qty=qty,
        bom_ceiling_usd=float(bom_ceiling) if bom_ceiling is not None else None,
        modules_planned=modules_planned,
        debug_uart=debug_uart,
        special=special,
    )


def read_spec(path: Path) -> ProjectSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InitInputError(f"missing spec.md at {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise InitInputError(f"cannot read spec.md at {path}: {exc}") from exc
    return validate_spec(_frontmatter(text))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: CommandRunner,
    purpose: str,
) -> str:
    try:
        completed = runner(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise InitError(f"{purpose} could not start: {exc}") from exc
    if completed.returncode != 0:
        details = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part.strip()
        )
        suffix = f"\n{details}" if details else ""
        raise InitError(f"{purpose} failed (exit {completed.returncode}){suffix}")
    return completed.stdout.strip()


def _tool_metadata(tool_root: Path, runner: CommandRunner) -> dict[str, Any]:
    kicad_version = _run_checked(
        [str(tool_root / "scripts" / "kicad-cli"), "version"],
        cwd=tool_root,
        runner=runner,
        purpose="KiCad version check",
    ).splitlines()[-1]
    if kicad_version != KICAD_VERSION:
        raise InitError(
            f"KiCad version mismatch: expected {KICAD_VERSION}, got {kicad_version}"
        )

    revision = _run_checked(
        ["git", "rev-parse", "HEAD"],
        cwd=tool_root,
        runner=runner,
        purpose="pcbforge revision check",
    ).splitlines()[-1]
    dirty = bool(
        _run_checked(
            ["git", "status", "--short"],
            cwd=tool_root,
            runner=runner,
            purpose="pcbforge worktree check",
        )
    )
    if dirty:
        raise InitError(
            "PCBForge checkout is dirty; commit or stash tool changes before "
            "initializing a reproducibly pinned project"
        )
    lockfile = tool_root / "toolchain" / "uv.lock"
    return {
        "revision": revision,
        "dirty": dirty,
        "kicad": kicad_version,
        "toolchain_lock_sha256": _sha256(lockfile),
    }


def _load_profile(tool_root: Path, layers: int) -> tuple[dict[str, Any], Path]:
    path = tool_root / "rules" / f"jlc-{layers}layer.json"
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InitError(f"cannot load rules profile {path}: {exc}") from exc
    if profile.get("schema") != 1 or profile.get("layers") != layers:
        raise InitError(f"invalid rules profile metadata in {path}")
    return profile, path


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")








def _layers(layers: int) -> str:
    copper = ['\t\t(0 "F.Cu" signal)']
    if layers == 4:
        copper.extend(
            [
                '\t\t(4 "In1.Cu" power)',
                '\t\t(6 "In2.Cu" power)',
            ]
        )
    copper.append('\t\t(2 "B.Cu" signal)')
    other = [
        '\t\t(9 "F.Adhes" user "F.Adhesive")',
        '\t\t(11 "B.Adhes" user "B.Adhesive")',
        '\t\t(13 "F.Paste" user)',
        '\t\t(15 "B.Paste" user)',
        '\t\t(5 "F.SilkS" user "F.Silkscreen")',
        '\t\t(7 "B.SilkS" user "B.Silkscreen")',
        '\t\t(1 "F.Mask" user)',
        '\t\t(3 "B.Mask" user)',
        '\t\t(17 "Dwgs.User" user "User.Drawings")',
        '\t\t(19 "Cmts.User" user "User.Comments")',
        '\t\t(21 "Eco1.User" user "User.Eco1")',
        '\t\t(23 "Eco2.User" user "User.Eco2")',
        '\t\t(25 "Edge.Cuts" user)',
        '\t\t(27 "Margin" user)',
        '\t\t(31 "F.CrtYd" user "F.Courtyard")',
        '\t\t(29 "B.CrtYd" user "B.Courtyard")',
        '\t\t(35 "F.Fab" user)',
        '\t\t(33 "B.Fab" user)',
        '\t\t(39 "User.1" user)',
        '\t\t(41 "User.2" user)',
        '\t\t(43 "User.3" user)',
        '\t\t(45 "User.4" user)',
        '\t\t(47 "User.5" user)',
        '\t\t(49 "User.6" user)',
        '\t\t(51 "User.7" user)',
        '\t\t(53 "User.8" user)',
        '\t\t(55 "User.9" user)',
    ]
    return "\n".join(copper + other)


def _edge_line(start: tuple[float, float], end: tuple[float, float]) -> str:
    return f"""\t(gr_line
\t\t(start {_number(start[0])} {_number(start[1])})
\t\t(end {_number(end[0])} {_number(end[1])})
\t\t(stroke
\t\t\t(width 0.05)
\t\t\t(type default)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "{uuid.uuid4()}")
\t)"""


def _render_board(spec: ProjectSpec) -> str:
    x0 = BOARD_ORIGIN_MM
    y0 = BOARD_ORIGIN_MM
    x1 = x0 + spec.board_mm[0]
    y1 = y0 + spec.board_mm[1]
    outline = "\n".join(
        [
            _edge_line((x0, y0), (x1, y0)),
            _edge_line((x1, y0), (x1, y1)),
            _edge_line((x1, y1), (x0, y1)),
            _edge_line((x0, y1), (x0, y0)),
        ]
    )
    return f"""(kicad_pcb
\t(version {BOARD_FORMAT_VERSION})
\t(generator "pcbforge")
\t(generator_version "0.1")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
{_layers(spec.layers)}
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t\t(tenting front back)
\t\t(aux_axis_origin {_number(x0)} {_number(y0)})
\t)
{outline}
)
"""


def _render_project(spec: ProjectSpec, profile: Mapping[str, Any]) -> str:
    rules = profile["rules"]
    defaults = profile["defaults"]
    project = {
        "board": {
            "3dviewports": [],
            "design_settings": {
                "defaults": {
                    "board_outline_line_width": 0.05,
                    "copper_line_width": defaults["track_width_mm"],
                    "courtyard_line_width": 0.05,
                    "fab_line_width": 0.1,
                    "other_line_width": 0.1,
                    "silk_line_width": defaults["silk_width_mm"],
                    "silk_text_size_h": defaults["silk_text_height_mm"],
                    "silk_text_size_v": defaults["silk_text_height_mm"],
                    "silk_text_thickness": defaults["silk_width_mm"],
                    "zones": {"min_clearance": rules["min_clearance_mm"]},
                },
                "diff_pair_dimensions": [
                    {"gap": 0.0, "via_gap": 0.0, "width": 0.0},
                    {
                        "gap": rules["min_clearance_mm"],
                        "via_gap": rules["min_clearance_mm"],
                        "width": rules["min_track_width_mm"],
                    },
                ],
                "drc_exclusions": [],
                "meta": {"version": 2},
                "rules": {
                    "min_clearance": rules["min_clearance_mm"],
                    "min_copper_edge_clearance": rules["min_copper_edge_clearance_mm"],
                    "min_groove_width": rules["min_groove_width_mm"],
                    "min_hole_clearance": rules["min_hole_copper_clearance_mm"],
                    "min_hole_to_hole": rules["min_hole_to_hole_mm"],
                    "min_silk_clearance": rules["min_silk_clearance_mm"],
                    "min_text_height": defaults["silk_text_height_mm"],
                    "min_text_thickness": defaults["silk_width_mm"],
                    "min_through_hole_diameter": rules["min_via_drill_mm"],
                    "min_track_width": rules["min_track_width_mm"],
                    "min_via_annular_width": rules["min_via_annular_width_mm"],
                    "min_via_diameter": rules["min_via_diameter_mm"],
                    "min_via_drill": rules["min_via_drill_mm"],
                    "solder_mask_clearance": rules["solder_mask_expansion_mm"],
                    "solder_mask_min_width": rules["min_solder_mask_web_mm"],
                    "solder_mask_to_copper_clearance": rules[
                        "min_solder_mask_to_copper_mm"
                    ],
                },
                "track_widths": [0.0, defaults["track_width_mm"]],
                "via_dimensions": [
                    {"diameter": 0.0, "drill": 0.0},
                    {
                        "diameter": defaults["via_diameter_mm"],
                        "drill": defaults["via_drill_mm"],
                    },
                ],
            },
            "layer_presets": [],
            "viewports": [],
        },
        "boards": [],
        "cvpcb": {},
        "erc": {},
        "libraries": {},
        "meta": {"filename": f"{spec.name}.kicad_pro", "version": 1},
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": rules["min_clearance_mm"],
                    "diff_pair_gap": rules["min_clearance_mm"],
                    "diff_pair_via_gap": rules["min_clearance_mm"],
                    "diff_pair_width": rules["min_track_width_mm"],
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": defaults["track_width_mm"],
                    "via_diameter": defaults["via_diameter_mm"],
                    "via_drill": defaults["via_drill_mm"],
                    "wire_width": 6,
                }
            ],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {},
        "schematic": {},
        "text_variables": {},
    }
    return json.dumps(project, indent=2, sort_keys=True) + "\n"


def _render_dru(profile: Mapping[str, Any]) -> str:
    rules = profile["rules"]
    return f"""(version 1)

# Conservative pcbforge defaults for {profile["name"]}.
# Source: {profile["source"]["url"]} (checked {profile["source"]["checked"]}).
(rule "pcbforge: copper clearance"
    (constraint clearance (min {rules["min_clearance_mm"]}mm)))

(rule "pcbforge: track width"
    (constraint track_width (min {rules["min_track_width_mm"]}mm)))

(rule "pcbforge: via size"
    (constraint via_diameter (min {rules["min_via_diameter_mm"]}mm))
    (constraint hole_size (min {rules["min_via_drill_mm"]}mm)))

(rule "pcbforge: board edge"
    (constraint edge_clearance (min {rules["min_copper_edge_clearance_mm"]}mm)))

(rule "pcbforge: drilled holes"
    (constraint hole_clearance (min {rules["min_hole_copper_clearance_mm"]}mm))
    (constraint hole_to_hole (min {rules["min_hole_to_hole_mm"]}mm)))

(rule "pcbforge: plated through-hole annular ring"
    (condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole'")
    (constraint annular_width (min {rules["min_pth_annular_width_mm"]}mm)))

(rule "pcbforge: plated slot width"
    (condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole' && A.Hole_Size_X != A.Hole_Size_Y")
    (constraint hole_size (min {rules["min_plated_slot_width_mm"]}mm)))

(rule "pcbforge: non-plated hole size"
    (condition "A.Type == 'Pad' && A.Pad_Type == 'NPTH, mechanical' && A.Hole_Size_X == A.Hole_Size_Y")
    (constraint hole_size (min {rules["min_npth_diameter_mm"]}mm)))

(rule "pcbforge: non-plated slot width"
    (condition "A.Type == 'Pad' && A.Pad_Type == 'NPTH, mechanical' && A.Hole_Size_X != A.Hole_Size_Y")
    (constraint hole_size (min {rules["min_npth_slot_width_mm"]}mm)))

(rule "pcbforge: minimum SMD pad size"
    (condition "A.Type == 'Pad' && A.Pad_Type == 'SMD'")
    (constraint assertion "A.Size_X >= {rules["min_smd_pad_size_mm"]}mm && A.Size_Y >= {rules["min_smd_pad_size_mm"]}mm"))

(rule "pcbforge: ordinary same-net copper spacing"
    (condition "A.Net == B.Net && A.Net != 0 && A.Type != 'Pad' && B.Type != 'Pad'")
    (constraint clearance (min {rules["min_same_net_copper_spacing_mm"]}mm)))
"""


def _render_agents(spec: ProjectSpec, tool_root: Path) -> str:
    return f"""<!-- pcbforge-agents-schema: 2 -->
# {spec.name}

Read `{tool_root}/agent/operating-manual.md` before work.
This is a native KiCad 10.0.3 project. The saved schematic owns the circuit.
Read spec.md, policy.yaml and STATUS.md to resume. Run pcbforge status --check --write.

The agent authors and edits real schematics with pcbforge.schematic_edit.SchematicDocument.
Preserve existing symbol UUIDs and unrelated user drawing work. Validate saved files.
Use official KiCad libraries first. Keep exact MPN, LCSC, Datasheet and pcbforge_purpose fields.
Keep reviewed electrical facts and executable requirements separate from circuit implementation.

The user approves the architecture and exact MCU plan before implementation.
The user approves the checked circuit once before its native PCB update.
Run prepare-pcb-update, ask the user to update the PCB in KiCad, then run finish-circuit.
Never infer approvals. Changed electrical intent requires renewed circuit approval.

The user owns layout and routing. Agent spatial edits require a specific request,
a current layout handoff and an open LAYOUT phase. Back up before spatial edits.
Record each requested assist. Placement measurements remain advisory.
Ordering remains human-owned. Never send orders or messages without authorization.
"""



def _render_gitignore() -> str:
    return "build/\nfab/*\n!fab/.gitkeep\nbom/*\n!bom/.gitkeep\n*-backups/\n_autosave-*\n*.kicad_prl\nreview/circuit/preview/\n"



def _render_pins(
    spec: ProjectSpec,
    metadata: Mapping[str, Any],
    profile: Mapping[str, Any],
    profile_path: Path,
    policy_profile_hash: str,
) -> str:
    pins = {
        "schema": PIN_SCHEMA,
        "project": spec.name,
        "pcbforge": {
            "revision": metadata["revision"],
            "dirty": metadata["dirty"],
        },
        "toolchain": {
            "kicad": metadata["kicad"],
            "uv_lock_sha256": metadata["toolchain_lock_sha256"],
        },
        "rules": {
            "profile": profile["name"],
            "profile_sha256": _sha256(profile_path),
        },
        "policy": {
            "profile": POLICY_PROFILE_ID,
            "profile_sha256": policy_profile_hash,
            "baseline_approval": "spec",
        },
        "guidance": {
            "agents_schema": AGENTS_SCHEMA,
            "architect_schema": ARCHITECT_GUIDE_SCHEMA,
            "architecture_diagram_schema": ARCHITECTURE_DIAGRAM_SCHEMA,
            "mcu_schema": MCU_GUIDE_SCHEMA,
            "circuit_schema": CIRCUIT_GUIDE_SCHEMA,
            "electrical_test_schema": ELECTRICAL_TEST_GUIDE_SCHEMA,
            "layout_handoff_schema": LAYOUT_HANDOFF_GUIDE_SCHEMA,
            "approval_schema": APPROVAL_GUIDE_SCHEMA,
            "circuit_review_schema": CIRCUIT_REVIEW_SCHEMA,
            "policy_schema": POLICY_GUIDE_SCHEMA,
            "status_schema": STATUS_SCHEMA,
        },
    }
    return yaml.safe_dump(pins, sort_keys=False)


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _render_scaffold(
    stage: Path,
    spec: ProjectSpec,
    tool_root: Path,
    profile: Mapping[str, Any],
    metadata: Mapping[str, Any],
    profile_path: Path,
    policy_profile_hash: str,
) -> None:
    from pcbforge.schematic import empty_schematic
    root_uuid = str(uuid.uuid4())
    _write(stage / f"{spec.name}.kicad_sch", empty_schematic(spec.name, uid=root_uuid))
    _write(stage / "sym-lib-table", "(sym_lib_table\n  (version 7)\n)\n")
    _write(stage / "circuit-review.yaml", "circuit_review_schema: 4\nerc_exclusions: []\nreadability_exclusions: []\n")
    _write(stage / "circuit-tests.yaml", "circuit_tests_schema: 1\nrequirements: []\ntests: []\n")
    _write(stage / "electrical-facts.yaml", "electrical_facts_schema: 1\nvalues: {}\nparts: {}\nmcu: {}\n")
    _write(stage / f"{spec.name}.kicad_pcb", _render_board(spec))
    project = json.loads(_render_project(spec, profile))
    project["sheets"] = [[root_uuid, "Root"]]
    _write(stage / f"{spec.name}.kicad_pro", json.dumps(project, indent=2) + "\n")
    _write(stage / f"{spec.name}.kicad_dru", _render_dru(profile))
    _write(stage / "fp-lib-table", "(fp_lib_table\n  (version 7)\n)\n")
    _write(stage / "AGENTS.md", _render_agents(spec, tool_root))
    _write(stage / ".gitignore", _render_gitignore())
    _write(stage / "bom" / ".gitkeep", "")
    _write(stage / "fab" / ".gitkeep", "")
    _write(stage / "firmware" / ".gitkeep", "")
    _write(
        stage / ".pcbforge",
        _render_pins(
            spec,
            metadata,
            profile,
            profile_path,
            policy_profile_hash,
        ),
    )


def _generated_paths(spec: ProjectSpec) -> list[Path]:
    return [
        Path(f"{spec.name}.kicad_sch"),
        Path("sym-lib-table"),
        Path("circuit-review.yaml"),
        Path("circuit-tests.yaml"),
        Path("electrical-facts.yaml"),
        Path(f"{spec.name}.kicad_pcb"),
        Path(f"{spec.name}.kicad_pro"),
        Path(f"{spec.name}.kicad_dru"),
        Path("fp-lib-table"),
        Path("AGENTS.md"),
        Path(".gitignore"),
        Path("bom"),
        Path("fab"),
        Path("firmware"),
        Path(".pcbforge"),
    ]


def _preflight_destination(project_dir: Path, spec: ProjectSpec) -> None:
    if project_dir.name != spec.name:
        raise InitInputError(
            f"spec name {spec.name!r} must match project directory {project_dir.name!r}"
        )
    conflicts = [
        str(path) for path in _generated_paths(spec) if (project_dir / path).exists()
    ]
    if conflicts:
        if ".pcbforge" in conflicts:
            raise InitInputError("project is already initialized (.pcbforge exists)")
        raise InitInputError(
            "refusing to overwrite existing scaffold paths: " + ", ".join(conflicts)
        )


def _smoke_build(stage: Path, tool_root: Path, runner: CommandRunner) -> None:
    schematic = next(stage.glob("*.kicad_sch"))
    _run_checked([str(tool_root / "scripts/kicad-cli"), "sch", "export", "netlist",
                  "--output", str(stage / "smoke.net"), str(schematic)],
                 cwd=stage, runner=runner, purpose="native schematic smoke test")
    _run_checked([str(tool_root / "scripts/kicad-cli"), "pcb", "export", "svg", "--layers", "F.Cu,B.Cu,Edge.Cuts", "--mode-multi",
                  "--output", str(stage / "smoke-pcb"), str(schematic.with_suffix(".kicad_pcb"))],
                 cwd=stage, runner=runner, purpose="native PCB smoke test")



def _discard_build_outputs(stage: Path) -> None:
    for path in (stage / "smoke.net", stage / "smoke-pcb"):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _commit_scaffold(stage: Path, project_dir: Path, spec: ProjectSpec) -> None:
    paths = _generated_paths(spec)
    marker = Path(".pcbforge")
    paths.remove(marker)
    paths.append(marker)
    committed: list[Path] = []

    def install(source: Path, target: Path) -> None:
        if source.is_dir():
            target.mkdir()
            committed.append(target)
            for child in source.iterdir():
                install(child, target / child.name)
            return
        os.link(source, target)
        committed.append(target)

    try:
        for relative in paths:
            install(stage / relative, project_dir / relative)
    except OSError as exc:
        for target in reversed(committed):
            if target.is_dir():
                target.rmdir()
            elif target.exists():
                target.unlink()
        raise InitError(f"could not commit scaffold atomically: {exc}") from exc


def _rollback_scaffold(project_dir: Path, spec: ProjectSpec) -> tuple[str, ...]:
    """Remove only paths proven absent by preflight and created by this init."""
    failures: list[str] = []
    for relative in reversed(_generated_paths(spec)):
        target = project_dir / relative
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as exc:
            failures.append(f"{relative}: {exc}")
    return tuple(failures)


def initialize_project(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    smoke_build: bool = True,
) -> InitResult:
    """Validate, smoke-test, and create a pcbforge scaffold."""
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.exists():
        raise InitInputError(f"project directory does not exist: {project_dir}")
    if not project_dir.is_dir():
        raise InitInputError(f"project path is not a directory: {project_dir}")

    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    spec = read_spec(project_dir / "spec.md")
    _preflight_destination(project_dir, spec)
    from pcbforge.status import (
        StatusError,
        StatusInputError as DashboardInputError,
        TransitionEvent,
        _now,
        inspect_status,
        policy_approval_context,
        read_status_document,
        write_status,
    )

    try:
        status_document = read_status_document(project_dir)
        status_report = inspect_status(project_dir, document=status_document)
    except DashboardInputError as exc:
        raise InitInputError(str(exc)) from exc
    spec_phase = status_report.phases[0]
    spec_approval = next(
        (
            event
            for event in reversed(status_document.events)
            if event.phase == "spec"
        ),
        None,
    )
    if (
        not spec_phase.complete
        or spec_approval is None
        or spec_approval.action != "complete"
        or not spec_approval.approval_fingerprint
    ):
        raise InitInputError(
            "cannot initialize: SPEC does not have current artifact-bound explicit "
            "user approval "
            f"({spec_phase.detail})"
        )

    try:
        baseline_approval, exception_approvals, _ = policy_approval_context(
            status_document
        )
        policy_result = check_policy(
            project_dir,
            tool_root=tool_root,
            through_phase="spec",
            baseline_approval=baseline_approval,
            exception_approvals=exception_approvals,
        )
        _, _, policy_profile_hash = load_policy_profile(tool_root)
    except (PolicyInputError, PolicyError) as exc:
        raise InitInputError(f"cannot initialize: {exc}") from exc
    if not policy_result.ok:
        detail = "\n  - ".join(
            f"[{violation.rule}] {violation.message}"
            for violation in policy_result.violations
        )
        raise InitInputError(
            "cannot initialize: project policy failed:\n  - " + detail
        )

    profile, profile_path = _load_profile(tool_root, spec.layers)
    metadata = _tool_metadata(tool_root, runner)

    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{spec.name}.pcbforge-init-",
            dir=project_dir.parent,
        )
    )
    try:
        shutil.copy2(project_dir / "spec.md", stage / "spec.md")
        _render_scaffold(
            stage,
            spec,
            tool_root,
            profile,
            metadata,
            profile_path,
            policy_profile_hash,
        )
        if smoke_build:
            _smoke_build(stage, tool_root, runner)
            _discard_build_outputs(stage)
        _commit_scaffold(stage, project_dir, spec)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # STATUS.md may already exist from the conversational SPEC phase. Treat its
    # transition write as part of initialization: if it fails, remove only the
    # create-only scaffold paths that preflight proved were absent.
    try:
        status_document = replace(
            status_document,
            transition_events=(
                *status_document.transition_events,
                TransitionEvent(
                    _now(),
                    "initialize",
                    "complete",
                    "Validated create-only scaffold and native KiCad smoke test passed",
                ),
            ),
        )
        write_status(project_dir, document=status_document)
    except (StatusError, OSError) as exc:
        rollback_failures = _rollback_scaffold(project_dir, spec)
        detail = (
            "; rollback failures: " + "; ".join(rollback_failures)
            if rollback_failures
            else ""
        )
        raise InitError(
            f"initialization transition could not be recorded; scaffold rolled back: "
            f"{exc}{detail}"
        ) from exc

    return InitResult(name=spec.name, project_dir=project_dir)
