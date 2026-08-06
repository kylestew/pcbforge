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

from pcbforge.policy import (
    POLICY_PROFILE_ID,
    POLICY_SCHEMA,
    PolicyError,
    PolicyInputError,
    check_policy,
    load_policy_profile,
)

ATO_VERSION = "0.15.7"
KICAD_VERSION = "9.0.9"
SPEC_SCHEMA = 1
PIN_SCHEMA = 1
AGENTS_SCHEMA = 1
ARCHITECT_GUIDE_SCHEMA = 1
ARCHITECTURE_DIAGRAM_SCHEMA = 1
MCU_GUIDE_SCHEMA = 1
CIRCUIT_GUIDE_SCHEMA = 1
BUILD_TEST_GUIDE_SCHEMA = 1
LAYOUT_HANDOFF_GUIDE_SCHEMA = 1
APPROVAL_GUIDE_SCHEMA = 1
CIRCUIT_REVIEW_SCHEMA = 1
POLICY_GUIDE_SCHEMA = POLICY_SCHEMA
STATUS_SCHEMA = 1
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
    ato_version = _run_checked(
        [str(tool_root / "scripts" / "ato"), "self-check"],
        cwd=tool_root,
        runner=runner,
        purpose="atopile version check",
    ).splitlines()[-1]
    if ato_version != ATO_VERSION:
        raise InitError(
            f"atopile version mismatch: expected {ATO_VERSION}, got {ato_version}"
        )

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
        "atopile": ato_version,
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


def _render_ato_yaml(spec: ProjectSpec) -> str:
    return f"""requires-atopile: "{ATO_VERSION}"

paths:
  src: ./src
  layout: ./

builds:
  default:
    entry: src/main.ato:App
    keep_designators: true
    paths:
      layout: ./{spec.name}.kicad_pcb
"""


def _render_main_ato(spec: ProjectSpec) -> str:
    return f'''"""Top-level circuit architecture for {spec.name}.

Populate this module during the ARCHITECT phase; the user approves the module
graph before component implementation begins.
"""

module App:
    pass
'''


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
\t(version 20241229)
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
                    "min_hole_clearance": rules["min_hole_copper_clearance_mm"],
                    "min_hole_to_hole": rules["min_hole_to_hole_mm"],
                    "min_text_height": defaults["silk_text_height_mm"],
                    "min_text_thickness": defaults["silk_width_mm"],
                    "min_track_width": rules["min_track_width_mm"],
                    "min_via_annular_width": rules["min_via_annular_width_mm"],
                    "min_via_diameter": rules["min_via_diameter_mm"],
                    "min_via_drill": rules["min_via_drill_mm"],
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
"""


def _render_agents(spec: ProjectSpec, tool_root: Path) -> str:
    return f"""<!-- pcbforge-agents-schema: {AGENTS_SCHEMA} -->
# pcbforge project: {spec.name}

This is a pcbforge circuit-as-code board project. Read `spec.md` and
`policy.yaml` first on every cold start, then refresh `STATUS.md` from source,
saved workflow gates, compiler output, and the KiCad board.

## Required reading

1. This file, `spec.md`, `policy.yaml`, and `STATUS.md`.
2. `{tool_root}/agent/operating-manual.md`.
3. `{tool_root}/agent/architect.md` before doing ARCHITECT work.
4. `{tool_root}/agent/mcu.md` for the MCU workstream inside ARCHITECT.
5. `{tool_root}/agent/circuit.md` before doing CIRCUIT work.
6. `{tool_root}/agent/layout-handoff.md` before preparing LAYOUT.

## Ownership

- The user owns the eight decision gates: SPEC, ARCHITECT proposal, CIRCUIT
  proposal and final, LAYOUT handoff and done-declaration, VERIFY, and ORDER;
  they also own intent, optional CubeMX review, layout, routing, and ordering.
- The agent owns circuit code, exact MCU and pin selection, part selection,
  checks, written layout audits, and preparation of review packets. Tool or
  agent ownership of work never grants completion authority.
- Never place, route, move, or “fix” copper unasked. Rewrite spatial board data
  only inside an open LAYOUT, only for work the user explicitly requested, and
  never in SPEC, ARCHITECT, CIRCUIT, or the handoff.
- Circuit source owns identity, footprints, fields, and connectivity.
- `{spec.name}.kicad_pcb` owns all spatial work.

## Decision authority

- Derive consequences of approved requirements, but never silently choose
  between materially different reasonable designs.
- A choice is material when alternatives affect topology, public interfaces,
  connectors, resource allocation, cost, risk, reversibility, or user
  experience. Present options, recommendation, tradeoffs, and consequences,
  then stop before changing the affected artifact.
- Silence, general permission to continue, and a broad implementation request
  are not approval.
- You may record approval already expressed by the user; never originate,
  infer, self-approve, or reuse it.
- Proposal approval precedes implementation. Required final approvals follow
  artifact presentation and validation.
- Initialization, the ARCHITECT source baseline, and FAB-OUT are checked tool
  transitions rather than user approvals. The LAYOUT handoff has its own
  explicit approval.
- Before requesting approval, run `pcbforge status review <phase>` and present
  its exact artifacts, check results, and fingerprint. After an unambiguous
  approval of that packet, record the saved review with
  `pcbforge status approve <phase> --last-reviewed --note "<approval>"`.
- Approvals are phase-specific and fingerprint-bound. A changed approved
  artifact requires renewed approval; rerunning checks cannot revive an old
  gate.
- ARCHITECT and CIRCUIT proposals use
  `status review <phase> --stage proposal` followed, only after explicit user
  approval, by `status approve <phase> --stage proposal --last-reviewed`.
- Only local, reversible details that do not alter an approved contract may be
  chosen autonomously, and their assumptions must be stated.

## Toolchain

- KiCad 9 only; KiCad 10 is incompatible with the pinned compiler.
- Use `{tool_root}/scripts/ato`, never a global `ato`.
- Use `{tool_root}/scripts/kicad-cli`, never a PATH `kicad-cli`.
- Use `{tool_root}/scripts/cubemx` for command-line CubeMX 6.18 validation.
- Ordering and spending remain human actions.

## Manufacturing and technology policy

- `{tool_root}/policies/{POLICY_PROFILE_ID}.yaml` is the tool-owned profile;
  `.pcbforge` pins its identity and hash. `policy.yaml` records this project's
  declarations, sourcing evidence, and requested exceptions.
- JLCPCB fabrication and assembly, STM32, 2/4 layers, SWD, pinned tools, exact
  part identity, official commodity libraries, spatial ownership, and human
  ordering authority are hard constraints. They cannot be excepted.
- FR4 1.6 mm / 1 oz, conventional vias, no controlled impedance, 0603 minimum
  commodity packages, and avoidance of BGA/WLCSP/sub-0.5-mm QFN are defaults.
  Deviations require a declared exception and explicit user approval.
- Record protection, ESD, test-point, polarity, and pin-1 applicability and
  evidence in `policy.yaml`. Record sourcing evidence for every selected LCSC
  item. Do not infer approval or current availability.
- Run `{tool_root}/scripts/pcbforge check-policy`. Approval commands persist
  decisions already made in conversation; they never constitute approval.

## Resume

1. Read this file, `spec.md`, `policy.yaml`, and `STATUS.md`.
2. Run `{tool_root}/scripts/pcbforge status --check --write` from this directory.
3. Use `{tool_root}/scripts/pcbforge status --next` for the compact handoff view.
4. Report the latest valid milestone, any previously performed transition that
   is now inactive, the current state, next owner, one primary action, and its
   command. If the phase is technically ready, present
   `status review <phase>` and stop for explicit user approval.

## Status dashboard

- `STATUS.md` is the tracked, user-facing workflow dashboard. Its YAML
  frontmatter contains append-only workflow events and check fingerprints; its
  Markdown body is generated. Never edit the body manually.
- Use `{tool_root}/scripts/pcbforge status --write` after meaningful project
  changes. Use `--check` when compiler, build-test, parts-policy,
  layout-handoff, IOC, or DRC evidence must be refreshed.
- `Complete` means a transition currently authorizes forward progress.
  `Performed, inactive` preserves a transition that ran before its upstream
  phase reopened; return to that phase instead of treating the transition as
  current completion. `Stale` means its upstream phase is current but its
  evidence must be refreshed.
- Never use `status mark <phase> complete`. After checks pass, run
  `{tool_root}/scripts/pcbforge status review <phase>`, present the packet, and
  stop. Only after explicit user approval, run
  `{tool_root}/scripts/pcbforge status approve <phase> --last-reviewed --note "<approval>"`.
  The command persists approval already expressed; it never constitutes it.
- Use `blocked` with a concrete reason, `reopened` when an approved phase
  changes, `skipped` only for optional publish, and `ai-assisted` only to log
  requested spatial layout work. Never infer user approval, layout completion,
  routing completion, or ordering.
- Record a declared exception with `pcbforge policy approve-exception <id>` and
  the final post-FAB review with `pcbforge policy confirm-sourcing`, always
  after the user explicitly approves or confirms it.

## ARCHITECT gate

After SPEC approval, immediately run `pcbforge init`. A successful atomic
initialization opens ARCHITECT directly without another approval. ARCHITECT
combines the functional graph, exact MCU plan, code skeleton, IOC, and MCU
audit:

1. Map every spec requirement to a functional block and typed interface:
   power input and every rail, MCU family, SWD, optional debug UART, every
   peripheral, every connector, and every special constraint.
2. Inspect `{tool_root}/modules/index.md`. The catalog is currently empty:
   say so, propose project-local modules from scratch, and never invent module
   imports or renders. Treat unmatched `modules_planned` entries as unverified.
3. Keep `src/main.ato` as a thin `App`. Put functional interface skeletons in
   `src/modules/*.ato`; reserve `src/mcu.ato` for the per-project MCU boundary.
4. Prefer `ElectricPower`, `I2C`, `SPI`, `UART`, `USB2_0_IF`, `CAN`, `SWD`,
   and `ElectricSignal`/`Electrical` over raw nets. Clarify `other` interfaces.
5. Draft `docs/architecture.md` with marker
   `pcbforge-architecture-diagram-schema: {ARCHITECTURE_DIAGRAM_SCHEMA}` and a
   Mermaid `flowchart LR`. Also draft `docs/mcu.md` with the exact
   STM32/package, peripheral allocation, provisional pin/resource plan,
   sourcing, and material tradeoffs. Do both before writing source or the IOC.
6. Run `pcbforge status review architect --stage proposal`, present the exact
   packet, and stop. After approval, record
   `{tool_root}/scripts/pcbforge status approve architect --stage proposal --last-reviewed --note "<approved choices>; diagram: docs/architecture.md"`.
   A spec, diagram, or MCU-plan change invalidates this approval.
7. Only after proposal approval, write the module skeleton, create
   `firmware/{spec.name}.ioc`, and derive `src/mcu.ato` from it.
8. Follow `{tool_root}/agent/mcu.md`; preserve SWD, check every pin/resource,
   offer optional CubeMX review, and run `pcbforge check-ioc`.
9. Do not choose non-MCU parts, footprints, LCSC numbers, passive values, or
   layout geometry. Build with the pinned compiler and preserve spatial data.
10. Audit every functional `App` instance, typed top-level connection, and
   external boundary against the approved diagram, including the one-to-one
   IOC-to-`src/mcu.ato` audit. Run `pcbforge status --check --write` and resolve
   every build or IOC failure without changing spatial board data.
11. Run `{tool_root}/scripts/pcbforge finish-architect`. It verifies the
   current proposal and checks, proves the board stayed spatially unchanged,
   captures the pre-CIRCUIT source baseline, and opens CIRCUIT without another
   user approval.

## CIRCUIT gate

Before adding physical parts, follow `{tool_root}/agent/circuit.md`:

1. Do not edit physical Atopile source yet. Create `circuit-review.yaml`, the
   exact `review/circuit/circuit.yaml` proposal model, the deliberately
   authored browser-readable `review/circuit/circuit.svg`, and
   `docs/circuit-proposal.md`. Do not generate a KiCad schematic.
2. Run `pcbforge check-circuit-review --stage proposal --write`, then
   `pcbforge status review circuit --stage proposal`. Present the explanatory
   SVG, narrative, exact model summary, and fingerprint, then stop. Record the
   proposal fingerprint only after explicit user approval.
3. After proposal approval, implement the circuit in Atopile. Reuse canonical
   KiCad symbols and footprints for commodity packages. A
   selected MPN or LCSC number is supplier metadata, not a reason to generate
   another 0603 resistor, capacitor, or LED library asset.
4. Generate project-local KiCad assets only when the exact required package or
   pin mapping is absent from the official libraries, then verify the generated
   geometry against the datasheet.
5. Give every resolved PCB net a concise human-readable name owned by Atopile
   source, and record the same exact `compiler_name` in the approved proposal
   model. Reject generic `hv`, `lv`, `line`, numeric-only, and
   hierarchy-generated routing labels; name intentional single-pad unused
   nets `NC_<REF>_<PIN>`. Never rename nets only in the KiCad PCB.
6. Run `{tool_root}/scripts/pcbforge check-parts` during part selection and
   before presenting CIRCUIT for completion.
7. Complete protection/testability evidence and sourcing entries in
   `policy.yaml`; run `{tool_root}/scripts/pcbforge check-policy` and stop for
   explicit user approval of every required exception.
8. Write `docs/circuit-review.md`, then run
   `pcbforge check-circuit-review --stage final --write`. Exact part identity,
   physical pins, and endpoint topology must match both the approved model and
   compiled Atopile design. Electrical differences return to proposal approval.
9. Create the exact, tracked `build-test.yaml` acceptance contract.
10. Give every required atopile assertion a unique `pcbforge-test` marker and
   list the same IDs in the contract.
11. Run
   `{tool_root}/scripts/pcbforge status --check --write`.
12. Inspect the generated `docs/build-test.md` evidence report. CIRCUIT cannot
    become ready while build, IOC, parts, policy, circuit parity, assertions,
    exact BOM/PCB, or spatial-preservation evidence is failed or stale.
13. Present one final `pcbforge status review circuit` packet and stop. Record
    `status approve circuit` only after the user explicitly accepts that exact
    implementation-and-test fingerprint.

## CIRCUIT-to-LAYOUT handoff

After CIRCUIT completes, follow `{tool_root}/agent/layout-handoff.md`:

1. Write the exact qualitative placement contract in `placement.yaml`.
2. Assign every PCB footprint to exactly one group and reference only current
   PCB references, pads, and exact net names.
3. Run `{tool_root}/scripts/pcbforge prepare-layout`; it generates
   `docs/placement-brief.md` and merges only `pcbforge:` net classes into the
   KiCad project. It never edits the PCB.
4. Run `{tool_root}/scripts/pcbforge check-layout-handoff` and present
   `docs/placement-brief.md` beside the already-approved CIRCUIT explanatory
   SVG and final parity evidence.
5. Run `pcbforge status review layout --stage handoff` and present its packet.
   Record `status approve layout --stage handoff` only after the user approves
   `docs/placement-brief.md` beside the current CIRCUIT overview. If that
   evidence is missing, stale, or
   inadequate for placement decisions, block the handoff and do not begin
   LAYOUT.

## LAYOUT gate

- Placement and routing remain distinct user tasks, both performed in KiCad 9.
  The agent spots and audits by default and never edits spatial board data on
  its own initiative.
- When the user explicitly asks the agent to attempt placement or routing, that
  request authorizes spatial edits for that task only and expires with it.
  Before editing, copy `{spec.name}.kicad_pcb` into `layout-backups/`, state
  the exact intended edits, and stop for the user on any material choice.
  Afterwards report the delta and run
  `{tool_root}/scripts/pcbforge status mark layout ai-assisted --note "<request; changes>"`.
  That event is history, never approval. See
  `{tool_root}/agent/operating-manual.md` for the full assist rules.
- After the user declares both tasks done, run `pcbforge status review layout`
  and present the lightweight packet. Record `status approve layout` only
  after explicit confirmation of that exact board fingerprint.
- The single LAYOUT fingerprint binds placements, board geometry, tracks,
  vias, and zones. Later spatial edits reopen LAYOUT; VERIFY carries the
  detailed DRC, audit, and render scrutiny.

## FAB-OUT and order policy

- After VERIFY is approved, run
  `{tool_root}/scripts/pcbforge fab-out`. It plots Gerbers and drills for the
  pinned stackup, exports placements and DRC evidence, derives the JLC BOM and
  CPL, writes `fab/manifest.json` and `fab/{spec.name}-fab.zip`, and records
  the checked transition. It never edits the PCB and never orders.
- A refusal is a real defect: missing layer, absent drill file, an assembly
  part with no placement, a BOM that disagrees with `build-test.yaml`, or a
  board that no longer passes DRC. Fix the cause; never hand-edit `fab/`.
- Use `{tool_root}/scripts/pcbforge check-fab-out` to re-prove an existing
  packet. It also runs as the ORDER-stage `fab` check.
- After the checked FAB-OUT transition is complete, refresh live JLC
  availability and lifecycle
  evidence for the exact BOM.
- Present the result to the user and, only after explicit confirmation, record
  `{tool_root}/scripts/pcbforge policy confirm-sourcing --note "<review>"`.
- ORDER cannot complete unless that confirmation fingerprints the current
  policy sourcing records, exact build-test BOM, and fabrication outputs.
- The generator records its own transition after validating the packet. ORDER
  retains the explicit user review and approval.
"""


def _render_gitignore() -> str:
    return """.ato/
build/
fab/*
!fab/.gitkeep
bom/*
!bom/.gitkeep
*-backups/
_autosave-*
*.kicad_prl
"""


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
            "atopile": metadata["atopile"],
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
            "build_test_schema": BUILD_TEST_GUIDE_SCHEMA,
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
    _write(stage / "ato.yaml", _render_ato_yaml(spec))
    _write(stage / "src" / "main.ato", _render_main_ato(spec))
    _write(stage / f"{spec.name}.kicad_pcb", _render_board(spec))
    _write(stage / f"{spec.name}.kicad_pro", _render_project(spec, profile))
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
        Path("ato.yaml"),
        Path("src"),
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
    _run_checked(
        [str(tool_root / "scripts" / "ato"), "build", "--verbose"],
        cwd=stage,
        runner=runner,
        purpose="atopile scaffold smoke test",
    )


def _discard_build_outputs(stage: Path) -> None:
    for path in (stage / "build", stage / ".ato"):
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
                    "Validated create-only scaffold and compiler smoke test passed",
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
