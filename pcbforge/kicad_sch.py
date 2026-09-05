"""Authored KiCad review schematic bound to one exact circuit model.

The agent writes ``review/circuit/circuit_schematic.py`` against
:class:`ReviewSchematic`; this module does the geometry, symbol embedding,
serialisation, readability lint, audit record, and finally runs the same
``validate_circuit_schematic`` gate that ``check-circuit-review`` uses.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pcbforge import kicad_fp, kicad_sym, sexpr
from pcbforge.circuit_review import (
    SCHEMATIC_AUDIT_SCHEMA,
    CircuitModel,
    CircuitReviewError,
    audit_path_for,
    circuit_model_fingerprint,
    read_circuit_model,
    read_circuit_review_contract,
    validate_circuit_schematic,
)
from pcbforge.kicad_project import KicadProjectError, register_root_sheet
from pcbforge.kicad_sym import GRID, LibSymbol, SymbolChoice, SymbolError
from pcbforge.sch_lint import Box, LintWarning, SheetGeometry, TextBox, lint
from pcbforge.sexpr import Quoted

SCH_VERSION = "20250114"
GENERATOR = "pcbforge"
REVIEW_MARKER = "PCBForge review-only schematic — generated from the approved circuit model"
REVIEW_MARKER_DETAIL = (
    "Read and cross-probe it with pcbnew. Do not edit or save it in KiCad and never "
    "run Update PCB from Schematic: Atopile owns the board; pcbforge render-circuit owns this sheet."
)
FONT = 1.27
_PATH_PALETTE = (
    (200, 30, 30),
    (30, 110, 200),
    (20, 140, 60),
    (190, 110, 0),
    (130, 40, 170),
    (0, 140, 140),
)
_GROUND_RE = re.compile(r"^(GND\w*|[ADP]GND|VSS\w*|0V|GROUND)$", re.I)
CommandRunner = Callable[..., subprocess.CompletedProcess]
Point = tuple[float, float]


class SchematicError(RuntimeError):
    """The authored schematic script violated the contract."""


SchematicWarning = LintWarning


@dataclass(frozen=True)
class RenderResult:
    path: Path
    audit_path: Path
    fingerprint: str
    warnings: tuple[SchematicWarning, ...]
    symbol_choices: Mapping[str, SymbolChoice]
    missing_component_symbols: tuple[str, ...]

    @property
    def collision_warnings(self) -> list[str]:
        return [w.message for w in self.warnings if w.code != "missing-component-symbol"]

    @property
    def summary(self) -> str:
        generic = sum(1 for c in self.symbol_choices.values() if c.generic)
        text = (
            f"{self.path.name}: {len(self.symbol_choices)} symbols "
            f"({generic} generic), {len(self.warnings)} warning(s)"
        )
        return text


def snap(value: float) -> float:
    return round(round(value / GRID) * GRID, 4)


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _p(point: Point) -> Point:
    return (snap(point[0]), snap(point[1]))


# ----------------------------------------------------------------------
# placed symbol
# ----------------------------------------------------------------------


@dataclass
class Placed:
    reference: str
    symbol: LibSymbol
    at: Point
    rotation: int
    mirror: str | None
    unit: int
    ref_pos: Point | None = None
    value_pos: Point | None = None
    hide_value: bool = False
    hide_reference: bool = False

    def _transform(self, point: Point) -> Point:
        px, py = point
        if self.mirror == "x":
            py = -py
        elif self.mirror == "y":
            px = -px
        angle = math.radians(self.rotation)
        rx = px * math.cos(angle) - py * math.sin(angle)
        ry = px * math.sin(angle) + py * math.cos(angle)
        return (snap(self.at[0] + rx), snap(self.at[1] - ry))

    def _direction(self, vector: Point) -> Point:
        """Transform a library direction vector to sheet space."""
        vx, vy = vector
        if self.mirror == "x":
            vy = -vy
        elif self.mirror == "y":
            vx = -vx
        angle = math.radians(self.rotation)
        rx = vx * math.cos(angle) - vy * math.sin(angle)
        ry = vx * math.sin(angle) + vy * math.cos(angle)
        return (round(rx, 6), round(-ry, 6))

    def pin(self, number: str | int) -> Point:
        pin = self.symbol.pin(str(number), self.unit)
        return self._transform((pin.x, pin.y))

    def pin_outward(self, number: str | int) -> Point:
        """Unit vector pointing away from the body at the pin tip, sheet space."""
        pin = self.symbol.pin(str(number), self.unit)
        angle = math.radians(pin.rotation)
        return self._direction((-math.cos(angle), -math.sin(angle)))

    @property
    def pins(self) -> dict[str, Point]:
        return {
            pin.number: self._transform((pin.x, pin.y))
            for pin in self.symbol.pins
            if pin.unit in (0, self.unit)
        }

    def pin_side(self, number: str | int) -> str:
        """Which side of the body the pin leaves from, in sheet terms."""
        vx, vy = self.pin_outward(number)
        if abs(vx) >= abs(vy):
            return "right" if vx > 0 else "left"
        return "down" if vy > 0 else "up"

    @property
    def bbox(self) -> Box:
        x1, y1, x2, y2 = self.symbol.bbox(self.unit)
        corners = [self._transform(c) for c in ((x1, y1), (x2, y1), (x1, y2), (x2, y2))]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return Box(min(xs), min(ys), max(xs), max(ys))

    @property
    def left(self) -> float:
        return self.bbox.x1

    @property
    def right(self) -> float:
        return self.bbox.x2

    @property
    def top(self) -> float:
        return self.bbox.y1

    @property
    def bottom(self) -> float:
        return self.bbox.y2

    @property
    def center(self) -> Point:
        box = self.bbox
        return ((box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2)


@dataclass
class _Wire:
    start: Point
    end: Point
    path_id: str | None = None


@dataclass
class _Label:
    text: str
    at: Point
    angle: int
    net_id: str


@dataclass
class _Text:
    text: str
    at: Point
    size: float
    color: tuple[int, int, int] | None = None
    bold: bool = False
    justify: str = "left bottom"
    furniture: bool = False


@dataclass
class _Rect:
    box: Box
    group_id: str


# ----------------------------------------------------------------------
# the authoring surface
# ----------------------------------------------------------------------


class ReviewSchematic:
    """One authored review schematic bound to one exact circuit model."""

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        model_path: Path | None = None,
        output_path: Path | None = None,
        title: str,
        desc: str,
        tool_root: Path | None = None,
        symbols_dir: Path | None = None,
        board_pads: Mapping[str, set[str]] | None = None,
        runner: CommandRunner | None = None,
        project_name: str | None = None,
        audit_path: Path | None = None,
        footprints_dir: Path | None = None,
        group_boxes: bool = False,
    ) -> None:
        """Bind to a project (or a model and output path).

        ``board_pads`` overrides pad discovery per reference; otherwise pads
        come from the compiled board when it exists, else from the footprint
        the model names (pinned official library, then ``src/parts``), else
        the model's connected pins. ``group_boxes=True`` draws the dashed
        region per model group; the default draws only the group titles so
        wires can follow the circuit across groups.
        """
        self.project_dir = None
        self.project_path: Path | None = None
        self.board_path: Path | None = None
        if project_dir is not None:
            project_dir = Path(project_dir).expanduser().resolve()
            contract = read_circuit_review_contract(project_dir)
            model_path = project_dir / contract.model
            output_path = project_dir / contract.schematic
            audit_path = audit_path_for(project_dir)
            # the contract already proved the sheet is <spec.name>.kicad_sch
            project_name = project_name or Path(contract.schematic).stem
            self.project_path = project_dir / f"{project_name}.kicad_pro"
            self.board_path = project_dir / f"{project_name}.kicad_pcb"
            self.project_dir = project_dir
        if model_path is None or output_path is None:
            raise SchematicError("pass project_dir, or both model_path and output_path")
        self.model: CircuitModel = read_circuit_model(Path(model_path))
        self.output_path = Path(output_path)
        self.audit_path = Path(audit_path) if audit_path else self.output_path.with_name(
            self.output_path.stem + ".audit.json"
        )
        self.title = title
        self.desc = desc
        self.tool_root = (tool_root or Path(__file__).resolve().parents[1]).resolve()
        self.symbols_dir = Path(symbols_dir) if symbols_dir else kicad_sym.symbols_dir(self.tool_root)
        self.runner = runner
        self.project_name = project_name or "pcbforge-review"
        self.fingerprint = circuit_model_fingerprint(self.model)
        self.draw_group_boxes = bool(group_boxes)
        self._footprints_dir: Path | None = Path(footprints_dir) if footprints_dir else None
        if self._footprints_dir is None:
            try:
                self._footprints_dir = kicad_fp.footprints_dir(self.tool_root)
            except kicad_fp.FootprintError:
                self._footprints_dir = None
        self._board_pads = dict(board_pads or {})
        if not self._board_pads and self.project_dir is not None:
            self._board_pads = _board_pads(self.project_dir)
        self._pads: dict[str, tuple[set[str], str]] = {}
        self.pad_sources: dict[str, str] = {}
        for component in self.model.components:
            self._pads[component.reference] = self._resolve_pads(component.reference, component.footprint)
            self.pad_sources[component.reference] = self._pads[component.reference][1]

        self._components = {c.reference: c for c in self.model.components}
        self._nets = {n.identifier: n for n in self.model.nets}
        self._groups = {g.identifier: g for g in self.model.groups}
        self._group_of = {
            ref: g.identifier for g in self.model.groups for ref in g.references
        }
        self._paths = {p.identifier: p for p in self.model.paths}
        self._pin_names: dict[str, dict[str, str]] = {}
        self._pin_sides: dict[str, dict[str, str]] = {}
        self._pin_pitch: dict[str, float] = {}
        self._net_of_node = {
            node: net.identifier for net in self.model.nets for node in net.nodes
        }

        self.placed: dict[str, Placed] = {}
        self.choices: dict[str, SymbolChoice] = {}
        self._wires: list[_Wire] = []
        self._labels: list[_Label] = []
        self._powers: list[Placed] = []
        self._no_connects: list[Point] = []
        self._texts: list[_Text] = []
        self._rects: list[_Rect] = []
        self._manual_boxes: dict[str, Box] = {}
        self._title_at: dict[str, Point] = {}
        self._lib_symbols: dict[str, LibSymbol] = {}
        self._flagged: set[str] = set()
        self._power_count = 0
        self._flag_count = 0

    # ------------------------------------------------------------------
    # symbols
    # ------------------------------------------------------------------

    def _resolve_pads(self, reference: str, footprint: str) -> tuple[set[str], str]:
        """Every physical pad of a component and where the list came from."""
        pads = {pad for pad in self._board_pads.get(reference, set()) if pad}
        if pads:
            return pads, "board pads"
        try:
            found = kicad_fp.footprint_pads(footprint, self._footprints_dir, self.project_dir)
        except kicad_fp.FootprintError as exc:
            raise SchematicError(f"{reference}: {exc}") from exc
        if found is not None:
            pads, path = found
            official = self._footprints_dir is not None and path.is_relative_to(self._footprints_dir)
            return pads, f"{'official' if official else 'project'} footprint {footprint}"
        return set(), "model pins"

    def symbol_for(self, reference: str, override: str | None = None) -> SymbolChoice:
        """Resolve (and cache) the symbol policy decision for one component."""
        component = self._components.get(reference)
        if component is None:
            raise SchematicError(f"unknown component reference {reference!r}")
        if reference in self.choices and override is None:
            return self.choices[reference]
        model_pins = {
            node.split(".", 1)[1]
            for net in self.model.nets
            for node in net.nodes
            if node.split(".", 1)[0] == reference
        }
        pads, pads_source = self._pads.get(reference, (set(), "model pins"))
        try:
            choice = kicad_sym.choose_symbol(
                kind=component.kind,
                mpn=component.mpn,
                value=component.value,
                reference=reference,
                model_pins=model_pins,
                board_pads=pads,
                directory=self.symbols_dir,
                override=override,
                footprint=component.footprint,
                pads_source=pads_source,
                pin_names=self._pin_names.get(reference),
                pin_sides=self._pin_sides.get(reference),
                pin_pitch=self._pin_pitch.get(reference, 2.54),
            )
        except SymbolError as exc:
            raise SchematicError(str(exc)) from exc
        self.choices[reference] = choice
        return choice

    def pin_names(
        self,
        reference: str,
        names: Mapping[str, str],
        *,
        sides: Mapping[str, str] | None = None,
        pitch: float = 2.54,
    ) -> None:
        """Name (and optionally side) pins of a generated box symbol; call before ``place``.

        ``sides`` maps pin number to ``left``/``right``/``top``/``bottom`` in
        the order they should appear; ``pitch`` is the pin spacing (5.08 gives
        hanging parts room on dense boxes).
        """
        if reference not in self._components:
            raise SchematicError(f"unknown component reference {reference!r}")
        self._pin_names[reference] = {str(k): v for k, v in names.items()}
        self._pin_sides[reference] = {str(k): v for k, v in (sides or {}).items()}
        self._pin_pitch[reference] = pitch
        self.choices.pop(reference, None)

    def place(
        self,
        reference: str,
        at: Point | None = None,
        *,
        rotation: int = 0,
        mirror: str | None = None,
        symbol: str | None = None,
        unit: int = 1,
        right_of: Placed | None = None,
        left_of: Placed | None = None,
        below: Placed | None = None,
        above: Placed | None = None,
        gap: float = 7.62,
        align: Point | None = None,
        ref_pos: Point | None = None,
        value_pos: Point | None = None,
        hide_value: bool = False,
        hide_reference: bool = False,
    ) -> Placed:
        """Place one model component. Position by ``at`` or relative to another part."""
        if reference in self.placed and unit == self.placed[reference].unit:
            raise SchematicError(f"{reference} is already placed")
        if rotation % 90 or mirror not in (None, "x", "y"):
            raise SchematicError("rotation must be a multiple of 90; mirror x, y or None")
        choice = self.symbol_for(reference, symbol)
        lib = choice.symbol
        if unit < 1 or unit > lib.units:
            raise SchematicError(f"{reference}: {lib.lib_id} has {lib.units} unit(s)")
        probe = Placed(reference, lib, (0.0, 0.0), rotation % 360, mirror, unit)
        anchor = right_of or left_of or below or above
        if at is None:
            if anchor is None:
                raise SchematicError(f"{reference}: pass at=(x, y) or a relative anchor")
            # cross-axis alignment uses symbol origins: two-pin symbols keep
            # their pin axis through the origin, so stacked parts line up
            box = probe.bbox
            abox = anchor.bbox
            if right_of is not None:
                at = (abox.x2 + gap - box.x1, anchor.at[1])
            elif left_of is not None:
                at = (abox.x1 - gap - box.x2, anchor.at[1])
            elif below is not None:
                at = (anchor.at[0], abox.y2 + gap - box.y1)
            else:
                at = (anchor.at[0], abox.y1 - gap - box.y2)
        if align is not None:
            at = (at[0] + align[0], at[1] + align[1])
        placed = Placed(
            reference,
            lib,
            _p(at),
            rotation % 360,
            mirror,
            unit,
            ref_pos,
            value_pos,
            hide_value,
            hide_reference,
        )
        if len(lib.pins) > 1:
            for number, tip in placed.pins.items():
                for wire in self._wires:
                    if _strictly_inside(tip, wire.start, wire.end):
                        raise SchematicError(
                            f"pin {reference}.{number} lands on the wire {wire.start}->{wire.end} "
                            f"at {tip}; KiCad would connect it — move the part or end the wire there"
                        )
        self.placed[reference] = placed
        self._lib_symbols[lib.lib_id] = lib
        return placed

    def pin(self, reference: str, number: str | int) -> Point:
        return self._placed(reference).pin(number)

    def _placed(self, reference: str) -> Placed:
        placed = self.placed.get(reference)
        if placed is None:
            raise SchematicError(f"{reference} is not placed yet")
        return placed

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------

    def wire(self, *points: Point, path: str | None = None) -> list[Point]:
        """Draw orthogonal wire segments through ``points``."""
        if len(points) < 2:
            raise SchematicError("wire needs at least two points")
        if path is not None and path not in self._paths:
            raise SchematicError(f"unknown path id {path!r}")
        pts = [_p(pt) for pt in points]
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            if a[0] != b[0] and a[1] != b[1]:
                raise SchematicError(f"wire segment {a}->{b} is not orthogonal")
            self._refuse_pass_through(a, b)
            self._wires.append(_Wire(a, b, path))
        return pts

    def _refuse_pass_through(self, a: Point, b: Point) -> None:
        """A wire that runs over a multi-pin part's pin tip connects to it in KiCad."""
        for ref, placed in self.placed.items():
            if len(placed.symbol.pins) < 2:
                continue
            for number, tip in placed.pins.items():
                if _strictly_inside(tip, a, b):
                    raise SchematicError(
                        f"wire {a}->{b} runs through pin {ref}.{number} at {tip}; KiCad would "
                        "connect it — end the wire at the pin, or route around it "
                        "(stubs through a connector's neighbouring pins are the usual cause: "
                        "use a label along the pin row instead)"
                    )

    def connect(
        self,
        a: Point,
        b: Point,
        *,
        route: str = "auto",
        path: str | None = None,
    ) -> list[Point]:
        """Manhattan wire between two points with at most one bend."""
        a, b = _p(a), _p(b)
        if a[0] == b[0] or a[1] == b[1]:
            return self.wire(a, b, path=path)
        if route == "auto":
            route = "hv"
        if route == "hv":
            return self.wire(a, (b[0], a[1]), b, path=path)
        if route == "vh":
            return self.wire(a, (a[0], b[1]), b, path=path)
        raise SchematicError("route must be auto, hv or vh")

    def stub(self, point: Point, direction: Point | str, length: float = 2.54, *, path: str | None = None) -> Point:
        """Short wire from ``point`` in ``direction``; returns the far end."""
        vector = _vector(direction)
        end = _p((point[0] + vector[0] * length, point[1] + vector[1] * length))
        self.wire(point, end, path=path)
        return end

    def rail(self, y: float, x1: float, x2: float, *, path: str | None = None) -> tuple[Point, Point]:
        start, end = _p((min(x1, x2), y)), _p((max(x1, x2), y))
        self.wire(start, end, path=path)
        return start, end

    def drop(self, reference: str, number: str | int, y: float, *, path: str | None = None) -> Point:
        """Vertical wire from a pin tip to ``y`` (a rail). Returns the rail point."""
        tip = self.pin(reference, number)
        end = _p((tip[0], y))
        self.wire(tip, end, path=path)
        return end

    # ------------------------------------------------------------------
    # nets: labels, power, no-connects
    # ------------------------------------------------------------------

    def _net(self, net_id: str):
        net = self._nets.get(net_id)
        if net is None:
            raise SchematicError(f"unknown net id {net_id!r}")
        return net

    def label(
        self,
        reference: str,
        number: str | int,
        net_id: str,
        *,
        direction: Point | str | None = None,
        length: float = 2.54,
        path: str | None = None,
    ) -> Point:
        """Net label on a short stub from a pin tip (or directly at it when ``length`` is 0)."""
        net = self._net(net_id)
        placed = self._placed(reference)
        tip = placed.pin(number)
        vector = _vector(direction) if direction is not None else placed.pin_outward(number)
        end = tip
        if length > 0:
            end = self.stub(tip, vector, length, path=path)
        self._labels.append(_Label(_net_name(net), end, _label_angle(vector), net_id))
        return end

    def label_at(self, point: Point, net_id: str, *, direction: Point | str = "right") -> Point:
        """Net label on an existing wire end / point."""
        net = self._net(net_id)
        point = _p(point)
        self._labels.append(_Label(_net_name(net), point, _label_angle(_vector(direction)), net_id))
        return point

    def power(
        self,
        reference: str,
        number: str | int,
        net_id: str,
        *,
        direction: Point | str | None = None,
        length: float = 2.54,
        flag: bool = False,
        style: str | None = None,
        path: str | None = None,
    ) -> Point:
        """Power symbol (rail arrow or ground) on a stub from a pin tip."""
        placed = self._placed(reference)
        tip = placed.pin(number)
        vector = _vector(direction) if direction is not None else placed.pin_outward(number)
        end = self.stub(tip, vector, length, path=path) if length > 0 else tip
        self.power_at(end, net_id, direction=vector, flag=flag, style=style)
        return end

    def power_at(
        self,
        point: Point,
        net_id: str,
        *,
        direction: Point | str = "up",
        flag: bool = False,
        style: str | None = None,
    ) -> Placed:
        """Power symbol at a point that is already on a wire (e.g. a rail)."""
        net = self._net(net_id)
        vector = _vector(direction)
        style = style or ("ground" if _GROUND_RE.match(_net_name(net)) else "rail")
        symbol = kicad_sym.power_symbol(_net_name(net), style)
        natural = (0.0, -1.0) if style == "rail" else (0.0, 1.0)
        rotation = _rotation_between(natural, vector)
        point = _p(point)
        if flag:
            # lift the symbol off the wire so the sideways flag has room
            point = self.stub(point, vector, 2.54)
        self._power_count += 1
        placed = Placed(f"#PWR{self._power_count:02d}", symbol, point, rotation, None, 1)
        self._powers.append(placed)
        self._lib_symbols[symbol.lib_id] = symbol
        if flag:
            self.flag(point, direction=vector)
        return placed

    def flag(self, point: Point, *, direction: Point | str = "up") -> Placed:
        """PWR_FLAG sharing the connection point, turned 90° from ``direction``."""
        vector = _vector(direction)
        side = (-vector[1], vector[0]) if vector[0] == 0 else (vector[1], -vector[0])
        symbol = kicad_sym.power_symbol("PWR_FLAG", "flag")
        rotation = _rotation_between((0.0, -1.0), side)
        self._flag_count += 1
        placed = Placed(f"#FLG{self._flag_count:02d}", symbol, _p(point), rotation, None, 1, hide_value=True)
        self._powers.append(placed)
        self._lib_symbols[symbol.lib_id] = symbol
        return placed

    def no_connect(self, reference: str, number: str | int) -> Point:
        tip = self.pin(reference, number)
        if tip not in self._no_connects:
            self._no_connects.append(tip)
        return tip

    # ------------------------------------------------------------------
    # annotation
    # ------------------------------------------------------------------

    def note(self, at: Point, text: str, *, size: float = 1.27, color: tuple[int, int, int] | None = None) -> None:
        self._texts.append(_Text(text, _p(at), size, color))

    def group_box(self, group_id: str, box: tuple[float, float, float, float]) -> None:
        """Draw this group's rectangle at an explicit place (also when boxes are off)."""
        if group_id not in self._groups:
            raise SchematicError(f"unknown group id {group_id!r}")
        x1, y1, x2, y2 = box
        self._manual_boxes[group_id] = Box(snap(min(x1, x2)), snap(min(y1, y2)), snap(max(x1, x2)), snap(max(y1, y2)))

    def group_title(self, group_id: str, at: Point) -> None:
        """Put the group's title at ``at`` instead of above its placed members."""
        if group_id not in self._groups:
            raise SchematicError(f"unknown group id {group_id!r}")
        self._title_at[group_id] = _p(at)

    # ------------------------------------------------------------------
    # save and prove
    # ------------------------------------------------------------------

    def save(self) -> RenderResult:
        self._check_complete()
        self._auto_no_connects()
        junctions = self._junctions()
        self._split_wires(junctions)
        shorts, opens = self._preflight_connectivity()
        if shorts:
            raise SchematicError(
                "drawn connectivity joins model nets that must stay separate:\n  - "
                + "\n  - ".join(shorts)
            )
        group_boxes = self._group_boxes()
        furniture = self._furniture(group_boxes)
        geometry = self._geometry(junctions, group_boxes, furniture)
        warnings = list(lint(geometry))
        warnings += [SchematicWarning("open-net", message) for message in opens]
        tips_by_ref = {ref: set(p.pins.values()) for ref, p in self.placed.items()}
        for point in getattr(self, "_pass_pin_points", ()):
            owners = sorted(ref for ref, tips in tips_by_ref.items() if point in tips)
            warnings.append(
                SchematicWarning(
                    "wire-passes-pin",
                    f"wire runs through pin tip of {', '.join(owners)} at ({point[0]:.2f}, {point[1]:.2f}) "
                    "without ending there; KiCad connects it",
                )
            )
        warnings = sorted(set(warnings), key=lambda w: (w.code, w.message))
        missing_symbols = tuple(
            sorted(ref for ref in self._connected_refs() if ref not in self.placed)
        )
        warnings += [
            SchematicWarning("missing-component-symbol", f"{ref} is connected but has no placed symbol")
            for ref in missing_symbols
        ]
        text = self._serialize(junctions, group_boxes, furniture, geometry)
        self.root_uuid = _root_uuid(self.fingerprint)
        audit = {
            "schema": SCHEMATIC_AUDIT_SCHEMA,
            "model_sha256": self.fingerprint,
            "schematic_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "bound_component_refs": sorted(self.placed),
            "symbol_choices": {
                ref: {"lib_id": c.symbol.lib_id, "generic": c.generic, "reason": c.reason}
                for ref, c in sorted(self.choices.items())
                if ref in self.placed
            },
            "nets": {
                net.identifier: {
                    "display_name": net.display_name,
                    "compiler_name": net.compiler_name,
                }
                for net in self.model.nets
            },
            "warnings": [w.payload() for w in warnings],
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(text, encoding="utf-8")
        self.audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            verified = validate_circuit_schematic(
                self.output_path,
                self.model,
                audit_path=self.audit_path,
                project_name=self.project_name if self.project_dir else None,
                tool_root=self.tool_root,
                runner=self.runner,
            )
        except CircuitReviewError as exc:
            detail = str(exc)
            if warnings:
                detail += "\nreadability warnings (often the cause):\n  - " + "\n  - ".join(
                    f"[{w.code}] {w.message}" for w in warnings
                )
            raise SchematicError(detail) from exc
        # the gate can add net-naming warnings; keep the audit record authoritative
        merged = {(w.code, w.message) for w in warnings}
        for item in verified["warnings"]:
            merged.add((item["code"], item["message"]))
        if len(merged) != len(warnings):
            warnings = sorted((SchematicWarning(code, message) for code, message in merged), key=lambda w: (w.code, w.message))
            audit["warnings"] = [w.payload() for w in warnings]
            self.audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if self.project_path is not None and self.project_path.is_file():
            try:
                register_root_sheet(self.project_path, self.root_uuid, board_path=self.board_path)
            except KicadProjectError as exc:
                raise SchematicError(str(exc)) from exc
        return RenderResult(
            path=self.output_path,
            audit_path=self.audit_path,
            fingerprint=self.fingerprint,
            warnings=tuple(warnings),
            symbol_choices={ref: self.choices[ref] for ref in sorted(self.placed)},
            missing_component_symbols=missing_symbols,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _connected_refs(self) -> set[str]:
        return {node.split(".", 1)[0] for net in self.model.nets for node in net.nodes}

    def _check_complete(self) -> None:
        missing = sorted(set(self._components) - set(self.placed))
        if missing:
            raise SchematicError("every model component must be placed; missing: " + ", ".join(missing))
        drawn_paths = {w.path_id for w in self._wires if w.path_id}
        undrawn = sorted(set(self._paths) - drawn_paths)
        if undrawn:
            raise SchematicError(
                "every model path needs at least one wire drawn with path=...; missing: "
                + ", ".join(undrawn)
            )
        empty_groups = sorted(
            g.identifier for g in self.model.groups if not any(r in self.placed for r in g.references)
        )
        if empty_groups:
            raise SchematicError("groups without placed parts: " + ", ".join(empty_groups))

    def _auto_no_connects(self) -> None:
        """Mark pins nothing connects to: single-node nets and pins with no model net.

        Official symbols carry every physical pin, so an MCU's unused pins
        get a no-connect marker automatically unless the script drew
        something at the tip.
        """
        occupied = {w.start for w in self._wires} | {w.end for w in self._wires}
        occupied |= {label.at for label in self._labels}
        occupied |= {power.at for power in self._powers}
        for net in self.model.nets:
            if len(net.nodes) != 1:
                continue
            ref, number = net.nodes[0].split(".", 1)
            placed = self.placed.get(ref)
            if placed is None:
                continue
            try:
                tip = placed.pin(number)
            except SymbolError:
                continue
            if tip in occupied or tip in self._no_connects:
                continue
            self._no_connects.append(tip)
        for ref, placed in sorted(self.placed.items()):
            for pin in placed.symbol.pins:
                if pin.unit not in (0, placed.unit) or pin.hidden:
                    continue
                if f"{ref}.{pin.number}" in self._net_of_node:
                    continue
                tip = placed.pin(pin.number)
                if tip in occupied or tip in self._no_connects:
                    continue
                self._no_connects.append(tip)

    def _preflight_connectivity(self) -> tuple[list[str], list[str]]:
        """Connectivity from our own geometry, checked against the model.

        Returns (shorts, opens). A short names the element that first joined
        two model nets; an open is a model net drawn in disconnected pieces.
        KiCad's netlist is still the proof — this exists to point at the cause.
        """
        parent: dict[object, object] = {}
        nets_of: dict[object, set[str]] = {}
        joined_by: dict[object, str] = {}

        def find(key):
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(a, b, element: str) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            na, nb = nets_of.get(ra, set()), nets_of.get(rb, set())
            parent[ra] = rb
            merged = na | nb
            nets_of[rb] = merged
            if na and nb and na != nb and len(merged) > 1:
                # remember the first element that joined two model nets
                joined_by.setdefault(frozenset(merged), element)

        def fmt(point: Point) -> str:
            return f"({point[0]:.2f}, {point[1]:.2f})"

        # model pins seed the islands with their nets
        for ref, placed in self.placed.items():
            for number, tip in placed.pins.items():
                net = self._net_of_node.get(f"{ref}.{number}")
                if net is not None:
                    nets_of.setdefault(find(tip), set()).add(net)
        for wire in self._wires:
            union(wire.start, wire.end, f"wire {fmt(wire.start)}->{fmt(wire.end)}")
        for label in self._labels:
            union(label.at, ("name", label.text), f"label {label.text!r} at {fmt(label.at)}")
        for power in self._powers:
            if power.hide_value:
                continue  # PWR_FLAG carries no net name
            union(power.at, ("name", _power_value(power)), f"power symbol {_power_value(power)} at {fmt(power.at)}")

        shorts: list[str] = []
        seen: set[frozenset[str]] = set()
        for key in list(parent):
            root = find(key)
            nets = nets_of.get(root, set())
            if len(nets) > 1 and frozenset(nets) not in seen:
                seen.add(frozenset(nets))
                cause = next(
                    (element for group, element in joined_by.items() if group <= frozenset(nets)),
                    "(joined through several elements)",
                )
                shorts.append(
                    f"short: model nets {', '.join(sorted(nets))} are joined — first joined by {cause}"
                )

        opens: list[str] = []
        by_net: dict[str, dict[object, list[str]]] = {}
        for ref, placed in self.placed.items():
            for number, tip in placed.pins.items():
                net = self._net_of_node.get(f"{ref}.{number}")
                if net is None:
                    continue
                by_net.setdefault(net, {}).setdefault(find(tip), []).append(f"{ref}.{number}")
        for net, islands in sorted(by_net.items()):
            if len(islands) > 1:
                pieces = "; ".join("{" + ", ".join(sorted(p)) + "}" for p in islands.values())
                opens.append(f"net {net} is drawn as {len(islands)} disconnected pieces: {pieces}")
        return shorts, opens

    def _junctions(self) -> list[Point]:
        ends: dict[Point, int] = {}
        for wire in self._wires:
            for point in (wire.start, wire.end):
                ends[point] = ends.get(point, 0) + 1
        pin_tips = {tip for placed in self.placed.values() for tip in placed.pins.values()}
        pin_tips |= {placed.at for placed in self._powers}
        junctions: set[Point] = set()
        for point, count in ends.items():
            if count >= 3 or (count == 2 and point in pin_tips):
                junctions.add(point)
        for point in ends:
            for wire in self._wires:
                if _strictly_inside(point, wire.start, wire.end):
                    junctions.add(point)
                    break
        return sorted(junctions)

    def _split_wires(self, junctions: list[Point]) -> None:
        """Split segments at junctions, pin tips and wire ends lying on them.

        KiCad 9 only connects wires at segment ends; a junction or pin in
        the middle of a long run is not a connection until the run is split
        (KiCad does this itself on save, so split files stay stable).
        """
        cut_points: set[Point] = set(junctions)
        pin_tips = {tip for placed in self.placed.values() for tip in placed.pins.values()}
        wire_ends = {w.start for w in self._wires} | {w.end for w in self._wires}
        # a multi-pin part's tip inside a run that no wire ends on is a
        # connection nobody drew (single-pin parts such as test pads sit on
        # wires by design)
        multi_tips = {
            tip for placed in self.placed.values() if len(placed.pins) > 1 for tip in placed.pins.values()
        }
        self._pass_pin_points = sorted(
            tip
            for tip in multi_tips
            if tip not in wire_ends and any(_strictly_inside(tip, w.start, w.end) for w in self._wires)
        )
        cut_points |= pin_tips
        cut_points |= {placed.at for placed in self._powers}
        cut_points |= {label.at for label in self._labels}
        for wire in self._wires:
            cut_points.add(wire.start)
            cut_points.add(wire.end)
        result: list[_Wire] = []
        for wire in self._wires:
            inside = sorted(
                (pt for pt in cut_points if _strictly_inside(pt, wire.start, wire.end)),
                key=lambda pt: (abs(pt[0] - wire.start[0]) + abs(pt[1] - wire.start[1])),
            )
            previous = wire.start
            for point in inside:
                result.append(_Wire(previous, point, wire.path_id))
                previous = point
            result.append(_Wire(previous, wire.end, wire.path_id))
        self._wires = result

    def _group_boxes(self) -> dict[str, Box]:
        boxes: dict[str, Box] = {}
        attached = self._attachments()
        for group in self.model.groups:
            if group.identifier in self._manual_boxes:
                boxes[group.identifier] = self._manual_boxes[group.identifier]
                continue
            if not self.draw_group_boxes:
                continue
            members = [self.placed[r] for r in group.references if r in self.placed]
            if not members:
                continue
            xs: list[float] = []
            ys: list[float] = []
            for placed in members:
                box = placed.bbox
                xs += [box.x1, box.x2]
                ys += [box.y1, box.y2]
                for _, text, pos in self._property_positions(placed):
                    box = _text_box(text, pos, FONT, "left")
                    xs += [box.x1, box.x2]
                    ys += [box.y1, box.y2]
                for box in attached.get(placed.reference, ()):
                    xs += [box.x1, box.x2]
                    ys += [box.y1, box.y2]
            margin = 3.81
            boxes[group.identifier] = Box(
                snap(min(xs) - margin),
                snap(min(ys) - margin - 5.08),
                snap(max(xs) + margin),
                snap(max(ys) + margin),
            )
        return boxes

    def _attachments(self) -> dict[str, list[Box]]:
        """Label and power-symbol boxes owned by each part, via wire connectivity.

        A label or power symbol belongs to a part when every model pin on its
        wire island lies in one group; it then stretches that group's box.
        """
        parent: dict[Point, Point] = {}

        def find(point: Point) -> Point:
            parent.setdefault(point, point)
            while parent[point] != point:
                parent[point] = parent[parent[point]]
                point = parent[point]
            return point

        def union(a: Point, b: Point) -> None:
            parent[find(a)] = find(b)

        for wire in self._wires:
            union(wire.start, wire.end)
        island_refs: dict[Point, set[str]] = {}
        for ref, placed in self.placed.items():
            for tip in placed.pins.values():
                if tip in parent:
                    island_refs.setdefault(find(tip), set()).add(ref)
        out: dict[str, list[Box]] = {}

        def owner(point: Point) -> str | None:
            if point not in parent:
                return None
            refs = island_refs.get(find(point), set())
            groups = {self._group_of.get(ref) for ref in refs}
            if len(groups) != 1 or not refs:
                return None
            return sorted(refs)[0]

        for label in self._labels:
            ref = owner(label.at)
            if ref:
                out.setdefault(ref, []).append(_label_box(label))
        for power in self._powers:
            ref = owner(power.at)
            if ref:
                out.setdefault(ref, []).append(_power_box(power))
        for wire in self._wires:
            ref = owner(wire.start)
            if ref:
                out.setdefault(ref, []).append(
                    Box(min(wire.start[0], wire.end[0]), min(wire.start[1], wire.end[1]),
                        max(wire.start[0], wire.end[0]), max(wire.start[1], wire.end[1]))
                )
        return out

    def _group_anchors(self, group_boxes: dict[str, Box]) -> dict[str, Point]:
        """Where each group's title goes: inside its box, or above its members."""
        anchors: dict[str, Point] = {}
        for group in self.model.groups:
            gid = group.identifier
            if gid in self._title_at:
                anchors[gid] = self._title_at[gid]
                continue
            box = group_boxes.get(gid)
            if box is not None:
                anchors[gid] = (box.x1 + 1.27, box.y1 + 2.794)
                continue
            members = [self.placed[r] for r in group.references if r in self.placed]
            if not members:
                continue
            xs: list[float] = []
            ys: list[float] = []
            for placed in members:
                bbox = placed.bbox
                xs += [bbox.x1, bbox.x2]
                ys += [bbox.y1, bbox.y2]
                for _, text, pos in self._property_positions(placed):
                    tbox = _text_box(text, pos, FONT, "left")
                    xs += [tbox.x1, tbox.x2]
                    ys += [tbox.y1, tbox.y2]
            anchors[gid] = (snap(min(xs)), snap(min(ys) - 2.54))
        return anchors

    def _furniture(self, group_boxes: dict[str, Box]) -> list[_Text]:
        texts: list[_Text] = []
        anchors = self._group_anchors(group_boxes)
        extra = [
            _text_box(self._groups[gid].title, at, 1.5, "left bottom")
            for gid, at in anchors.items()
            if gid not in group_boxes
        ]
        content = self._content_box(group_boxes, extra)
        x0 = content.x1
        texts.append(_Text(self.title, (x0, content.y1 - 16.51), 2.0, None, bold=True, furniture=True))
        texts.append(_Text(REVIEW_MARKER, (x0, content.y1 - 11.43), 2.54, (200, 0, 0), bold=True, furniture=True))
        texts.append(_Text(REVIEW_MARKER_DETAIL, (x0, content.y1 - 8.255), 1.27, (200, 0, 0), furniture=True))
        texts.append(_Text(f"model sha256 {self.fingerprint}", (x0, content.y1 - 5.08), 1.0, (90, 90, 90), furniture=True))
        for group_id, at in anchors.items():
            group = self._groups[group_id]
            texts.append(_Text(group.title, at, 1.5, (60, 60, 120), bold=True))
            if group_id in group_boxes:
                texts.append(_Text(group.purpose, (at[0], at[1] + 1.905), 1.0, (90, 90, 90)))
        # registers to the right of the content, clear of long group purposes
        right = content.x2
        for text in texts:
            if not text.furniture:
                right = max(right, _text_box(text.text, text.at, text.size, text.justify).x2)
        rx = snap(right + 12.7)
        y = content.y1
        lines = ["Component register"]
        for group in self.model.groups:
            for ref in group.references:
                component = self._components[ref]
                lines.append(f"{ref}  {component.value}  — {component.purpose}")
        texts.append(_Text("\n".join(lines), (rx, y + len(lines) * 1.27 * 1.35), 1.0, None, furniture=True))
        y = snap(y + len(lines) * 1.27 * 1.35 + 7.62)
        unboxed = [g for g in self.model.groups if g.identifier not in group_boxes]
        if unboxed:
            lines = ["Group register (title · purpose)"]
            for group in unboxed:
                lines.append(f"{group.title} · {group.purpose}")
            texts.append(_Text("\n".join(lines), (rx, y + len(lines) * 1.27 * 1.35), 1.0, None, furniture=True))
            y = snap(y + len(lines) * 1.27 * 1.35 + 7.62)
        lines = ["Net register (sheet/board name · model name)"]
        for net in self.model.nets:
            if len(net.nodes) < 2:
                continue
            lines.append(f"{_net_name(net)} · {net.display_name}  ({len(net.nodes)} pins)")
        texts.append(_Text("\n".join(lines), (rx, y + len(lines) * 1.27 * 1.35), 1.0, None, furniture=True))
        y = snap(y + len(lines) * 1.27 * 1.35 + 7.62)
        if self.model.paths:
            lines = ["Reviewed paths (coloured wires)"]
            for index, path in enumerate(self.model.paths):
                lines.append(f"■ {path.title} — {path.purpose}")
            for index, (line, path) in enumerate(zip(lines[1:], self.model.paths)):
                texts.append(_Text(line, (rx, y + (index + 2) * 1.27 * 1.35), 1.0, _PATH_PALETTE[index % len(_PATH_PALETTE)], furniture=True))
            texts.append(_Text(lines[0], (rx, y + 1.27 * 1.35), 1.0, None, furniture=True))
        texts.append(_Text(self.desc, (x0, content.y2 + 7.62), 1.0, (90, 90, 90), furniture=True))
        return texts

    def _content_box(self, group_boxes: dict[str, Box], extra: Sequence[Box] = ()) -> Box:
        xs: list[float] = []
        ys: list[float] = []
        for box in list(group_boxes.values()) + list(extra) + [p.bbox for p in self.placed.values()] + [p.bbox for p in self._powers]:
            xs += [box.x1, box.x2]
            ys += [box.y1, box.y2]
        for wire in self._wires:
            xs += [wire.start[0], wire.end[0]]
            ys += [wire.start[1], wire.end[1]]
        for label in self._labels:
            box = _label_box(label)
            xs += [box.x1, box.x2]
            ys += [box.y1, box.y2]
        if not xs:
            return Box(25.4, 25.4, 50.8, 50.8)
        return Box(min(xs), min(ys), max(xs), max(ys))

    def _geometry(self, junctions: list[Point], group_boxes: dict[str, Box], furniture: list[_Text]) -> SheetGeometry:
        texts: list[TextBox] = []
        for placed in self.placed.values():
            for key, text, pos in self._property_positions(placed):
                if text:
                    texts.append(TextBox(f"{placed.reference} {key}", _text_box(text, pos, FONT, "left"), placed.reference))
        for label in self._labels:
            texts.append(TextBox(f"label {label.text}", _label_box(label)))
        for power in self._powers:
            if power.hide_value:
                continue  # PWR_FLAG shares a pin with its power symbol by design
            texts.append(TextBox(f"{power.symbol.lib_id}", _power_box(power), power.reference))
        for text in self._texts + furniture:
            if text.furniture:
                continue
            texts.append(TextBox(text.text.splitlines()[0][:40], _text_box(text.text, text.at, text.size, text.justify)))
        symbols = {ref: placed.bbox for ref, placed in self.placed.items()}
        symbol_pins = {ref: frozenset(placed.pins.values()) for ref, placed in self.placed.items()}
        pin_tips = {tip for placed in self.placed.values() for tip in placed.pins.values()}
        pin_tips |= {placed.at for placed in self._powers}
        power_points = {}
        for power in self._powers:
            if not power.hide_value:
                power_points[power.at] = _power_value(power)
        return SheetGeometry(
            texts=tuple(texts),
            symbols=symbols,
            symbol_pins=symbol_pins,
            labels=tuple((l.at, l.text) for l in self._labels),
            power_points=power_points,
            wires=tuple((w.start, w.end) for w in self._wires),
            junctions=frozenset(junctions),
            pin_tips=frozenset(pin_tips),
            label_points=frozenset(l.at for l in self._labels) | frozenset(self._no_connects),
            group_boxes=group_boxes,
            group_of=dict(self._group_of),
        )

    def _property_positions(self, placed: Placed) -> list[tuple[str, str, Point]]:
        component = self._components.get(placed.reference)
        value = component.value if component else placed.symbol.lib_id.split(":")[1]
        box = placed.bbox
        # text anchors need not sit on the wire grid; snapping them collides
        # reference and value on short symbols
        default_ref = (round(box.x2 + 1.27, 3), round((box.y1 + box.y2) / 2 - 0.635, 3))
        default_val = (round(box.x2 + 1.27, 3), round((box.y1 + box.y2) / 2 + 1.905, 3))
        out = []
        if not placed.hide_reference:
            out.append(("Reference", placed.reference, placed.ref_pos or default_ref))
        if not placed.hide_value:
            out.append(("Value", value, placed.value_pos or default_val))
        return out

    def _serialize(
        self,
        junctions: list[Point],
        group_boxes: dict[str, Box],
        furniture: list[_Text],
        geometry: SheetGeometry,
    ) -> str:
        ns = _namespace(self.fingerprint)

        def uid(key: str) -> str:
            return str(uuid.uuid5(ns, key))

        root_uuid = _root_uuid(self.fingerprint)
        content = self._content_box(group_boxes)
        width = snap(max(content.x2 + 120.0, 210.0))
        height = snap(max(content.y2 + 25.4, 148.0))
        out: list[str] = [
            f"(kicad_sch (version {SCH_VERSION}) (generator \"{GENERATOR}\") (generator_version \"1\")",
            f"\t(uuid \"{root_uuid}\")",
            f"\t(paper \"User\" {_fmt(width)} {_fmt(height)})",
            "\t(title_block",
            f"\t\t(title {sexpr.quote(self.title)})",
            f"\t\t(rev {sexpr.quote(self.fingerprint[:12])})",
            f"\t\t(comment 1 {sexpr.quote(REVIEW_MARKER)})",
            f"\t\t(comment 3 {sexpr.quote(REVIEW_MARKER_DETAIL)})",
            f"\t\t(comment 2 {sexpr.quote('pcbforge_model_sha256=' + self.fingerprint)})",
            "\t)",
            "\t(lib_symbols",
        ]
        for lib_id in sorted(self._lib_symbols):
            out.append(sexpr.dumps(self._lib_symbols[lib_id].node, 2))
        out.append("\t)")

        for index, wire in enumerate(self._wires):
            color = ""
            width_text = "0"
            if wire.path_id:
                path_index = list(self._paths).index(wire.path_id)
                r, g, b = _PATH_PALETTE[path_index % len(_PATH_PALETTE)]
                color = f" (color {r} {g} {b} 1)"
                width_text = "0.5"
            out.append(
                f"\t(wire (pts (xy {_fmt(wire.start[0])} {_fmt(wire.start[1])}) "
                f"(xy {_fmt(wire.end[0])} {_fmt(wire.end[1])})) "
                f"(stroke (width {width_text}) (type default){color}) (uuid \"{uid(f'wire:{index}')}\"))"
            )
        for point in junctions:
            out.append(
                f"\t(junction (at {_fmt(point[0])} {_fmt(point[1])}) (diameter 0) (color 0 0 0 0) "
                f"(uuid \"{uid(f'junction:{point}')}\"))"
            )
        for index, point in enumerate(sorted(self._no_connects)):
            out.append(f"\t(no_connect (at {_fmt(point[0])} {_fmt(point[1])}) (uuid \"{uid(f'nc:{point}')}\"))")
        for index, label in enumerate(self._labels):
            justify = "left bottom" if label.angle in (0, 90) else "right bottom"
            out.append(
                f"\t(label {sexpr.quote(label.text)} (at {_fmt(label.at[0])} {_fmt(label.at[1])} {label.angle}) "
                f"(effects (font (size {_fmt(FONT)} {_fmt(FONT)})) (justify {justify})) (uuid \"{uid(f'label:{index}')}\"))"
            )
        for group_id, box in group_boxes.items():
            out.append(
                f"\t(rectangle (start {_fmt(box.x1)} {_fmt(box.y1)}) (end {_fmt(box.x2)} {_fmt(box.y2)}) "
                f"(stroke (width 0.2) (type dash) (color 96 96 160 1)) (fill (type none)) (uuid \"{uid(f'group:{group_id}')}\"))"
            )
        for index, text in enumerate(self._texts + furniture):
            out.append(_text_sexpr(text, uid(f"text:{index}")))
        for placed in list(self.placed.values()) + self._powers:
            out.append(self._symbol_sexpr(placed, root_uuid, uid))
        out.append("\t(sheet_instances (path \"/\" (page \"1\")))")
        out.append(")")
        return "\n".join(out) + "\n"

    def _symbol_sexpr(self, placed: Placed, root_uuid: str, uid) -> str:
        component = self._components.get(placed.reference)
        x, y = placed.at
        mirror = f" (mirror {placed.mirror})" if placed.mirror else ""
        lines = [
            f"\t(symbol (lib_id {sexpr.quote(placed.symbol.lib_id)}) (at {_fmt(x)} {_fmt(y)} {placed.rotation}){mirror} "
            f"(unit {placed.unit}) (exclude_from_sim no) (in_bom {'no' if component is None else 'yes'}) "
            f"(on_board {'no' if component is None else 'yes'}) (dnp no) (uuid \"{uid(f'symbol:{placed.reference}:{placed.unit}')}\")"
        ]
        # KiCad applies the symbol transform to field orientation and flips
        # justification for 90/180; this table keeps text horizontal and
        # left-justified at the stored absolute position (verified on 9.0.9).
        text_angle, text_justify = {
            0: (0, "left"),
            90: (90, "right"),
            180: (0, "right"),
            270: (90, "left"),
        }[placed.rotation]
        if component is None:
            value = placed.symbol.lib_id.split(":", 1)[1] if placed.symbol.lib_id.endswith("PWR_FLAG") else _power_value(placed)
            vx, vy = _power_value_pos(placed)
            hide_value = placed.symbol.lib_id.endswith("PWR_FLAG")
            lines.append(
                f"\t\t(property \"Reference\" {sexpr.quote(placed.reference)} (at {_fmt(x)} {_fmt(y)} 0) "
                f"(effects (font (size 1.27 1.27)) (hide yes)))"
            )
            lines.append(
                f"\t\t(property \"Value\" {sexpr.quote(value)} (at {_fmt(vx)} {_fmt(vy)} 0) "
                f"(effects (font (size 1.27 1.27)){' (hide yes)' if hide_value else ''}))"
            )
            lines.append(f"\t\t(property \"Footprint\" \"\" (at {_fmt(x)} {_fmt(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))")
            lines.append(f"\t\t(property \"Datasheet\" \"\" (at {_fmt(x)} {_fmt(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))")
        else:
            positions = dict((k, v) for k, _, v in self._property_positions(placed))
            ref_at = positions.get("Reference", placed.at)
            val_at = positions.get("Value", placed.at)
            lines.append(
                f"\t\t(property \"Reference\" {sexpr.quote(placed.reference)} (at {_fmt(ref_at[0])} {_fmt(ref_at[1])} {text_angle}) "
                f"(effects (font (size 1.27 1.27)) (justify {text_justify}){' (hide yes)' if placed.hide_reference else ''}))"
            )
            lines.append(
                f"\t\t(property \"Value\" {sexpr.quote(component.value)} (at {_fmt(val_at[0])} {_fmt(val_at[1])} {text_angle}) "
                f"(effects (font (size 1.27 1.27)) (justify {text_justify}){' (hide yes)' if placed.hide_value else ''}))"
            )
            lines.append(
                f"\t\t(property \"Footprint\" {sexpr.quote(component.footprint)} (at {_fmt(x)} {_fmt(y)} 0) "
                f"(effects (font (size 1.27 1.27)) (hide yes)))"
            )
            lines.append(f"\t\t(property \"Datasheet\" \"\" (at {_fmt(x)} {_fmt(y)} 0) (effects (font (size 1.27 1.27)) (hide yes)))")
            hidden = {
                "pcbforge_group": self._group_of.get(component.reference, ""),
                "pcbforge_purpose": component.purpose,
                "pcbforge_kind": component.kind,
                "pcbforge_mpn": component.mpn,
                "pcbforge_lcsc": component.lcsc,
            }
            for key, value in hidden.items():
                lines.append(
                    f"\t\t(property {sexpr.quote(key)} {sexpr.quote(value)} (at {_fmt(x)} {_fmt(y)} 0) "
                    f"(effects (font (size 1.27 1.27)) (hide yes)))"
                )
        for pin in placed.symbol.pins:
            if pin.unit in (0, placed.unit):
                lines.append(f"\t\t(pin {sexpr.quote(pin.number)} (uuid \"{uid(f'pin:{placed.reference}:{placed.unit}:{pin.number}')}\"))")
        lines.append(
            f"\t\t(instances (project {sexpr.quote(self.project_name)} (path \"/{root_uuid}\" "
            f"(reference {sexpr.quote(placed.reference)}) (unit {placed.unit}))))"
        )
        lines.append("\t)")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _namespace(fingerprint: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"pcbforge:{fingerprint}")


def _root_uuid(fingerprint: str) -> str:
    return str(uuid.uuid5(_namespace(fingerprint), "root"))


def _net_name(net) -> str:
    """The name KiCad (and the board) use for a net: compiler name, else display."""
    return net.compiler_name or net.display_name


def _board_pads(project_dir: Path) -> dict[str, set[str]]:
    """Every pad name per reference from the compiled board, when it exists.

    Unused pads count too: an official symbol is accepted only when its pin
    numbers equal the whole physical pad list.
    """
    from pcbforge.build_test import BuildTestError, read_board_evidence
    from pcbforge.initialize import InitInputError, read_spec

    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError:
        return {}
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    if not board_path.is_file():
        return {}
    try:
        board = read_board_evidence(board_path)
    except (BuildTestError, OSError, UnicodeError) as exc:
        raise SchematicError(f"cannot read board pads from {board_path.name}: {exc}") from exc
    pads: dict[str, set[str]] = {}
    for ref, pad in board.pads:
        if pad:
            pads.setdefault(ref, set()).add(pad)
    return pads


def _vector(direction: Point | str) -> Point:
    if isinstance(direction, str):
        table = {
            "up": (0.0, -1.0),
            "down": (0.0, 1.0),
            "left": (-1.0, 0.0),
            "right": (1.0, 0.0),
        }
        if direction not in table:
            raise SchematicError(f"direction must be up/down/left/right, got {direction!r}")
        return table[direction]
    vx, vy = direction
    if abs(vx) >= abs(vy):
        return (1.0 if vx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if vy > 0 else -1.0)


def _label_angle(vector: Point) -> int:
    vx, vy = vector
    if vx > 0:
        return 0
    if vx < 0:
        return 180
    return 90 if vy < 0 else 270


def _rotation_between(natural: Point, wanted: Point) -> int:
    """Symbol rotation (0/90/180/270) turning ``natural`` (sheet space) into ``wanted``."""
    table = {(0.0, -1.0): 0, (-1.0, 0.0): 90, (0.0, 1.0): 180, (1.0, 0.0): 270}
    return (table[wanted] - table[natural]) % 360


def _strictly_inside(point: Point, a: Point, b: Point) -> bool:
    if point in (a, b):
        return False
    if a[0] == b[0] == point[0]:
        return min(a[1], b[1]) < point[1] < max(a[1], b[1])
    if a[1] == b[1] == point[1]:
        return min(a[0], b[0]) < point[0] < max(a[0], b[0])
    return False


# KiCad stroke font: measured ~0.88 x size per character at 1.27 mm
_CHAR_W = 0.85


def _text_box(text: str, at: Point, size: float, justify: str) -> Box:
    lines = text.splitlines() or [""]
    width = max(len(line) for line in lines) * _CHAR_W * size
    height = len(lines) * size * 1.45
    x, y = at
    h = "left"
    v = "bottom"
    for token in justify.split():
        if token in ("left", "right", "center"):
            h = token
        if token in ("top", "bottom", "middle"):
            v = token
    x1 = x if h == "left" else (x - width if h == "right" else x - width / 2)
    y1 = y - height if v == "bottom" else (y if v == "top" else y - height / 2)
    return Box(x1, y1, x1 + width, y1 + height)


def _label_box(label: _Label) -> Box:
    width = len(label.text) * _CHAR_W * FONT + 1.27
    x, y = label.at
    if label.angle == 0:
        return Box(x, y - FONT * 1.3, x + width, y)
    if label.angle == 180:
        return Box(x - width, y - FONT * 1.3, x, y)
    if label.angle == 90:
        return Box(x - FONT * 1.3, y - width, x, y)
    return Box(x - FONT * 1.3, y, x, y + width)


def _power_box(placed: Placed) -> Box:
    box = placed.bbox
    if placed.hide_value:
        return box
    vx, vy = _power_value_pos(placed)
    text = _text_box(_power_value(placed), (vx, vy), FONT, "center middle")
    return Box(min(box.x1, text.x1), min(box.y1, text.y1), max(box.x2, text.x2), max(box.y2, text.y2))


def _power_value(placed: Placed) -> str:
    prop = next(
        (p for p in sexpr.children(placed.symbol.node, "property") if sexpr.atom(p) == "Value"),
        None,
    )
    return sexpr.atom(prop, 2) if prop else placed.symbol.lib_id.split(":")[1]


def _power_value_pos(placed: Placed) -> Point:
    box = placed.bbox
    lib_id = placed.symbol.lib_id
    is_ground = _GROUND_RE.match(_power_value(placed)) is not None
    # value sits past the symbol graphics, away from the connection point
    if placed.rotation == 0:
        return (placed.at[0], box.y1 - 1.27) if not is_ground else (placed.at[0], box.y2 + 1.905)
    if placed.rotation == 180:
        return (placed.at[0], box.y2 + 1.905) if not is_ground else (placed.at[0], box.y1 - 1.27)
    if placed.rotation == 90:
        return (box.x1 - 1.27, placed.at[1]) if not is_ground else (box.x2 + 1.27, placed.at[1])
    return (box.x2 + 1.27, placed.at[1]) if not is_ground else (box.x1 - 1.27, placed.at[1])


def _text_sexpr(text: _Text, uid: str) -> str:
    color = f" (color {text.color[0]} {text.color[1]} {text.color[2]} 1)" if text.color else ""
    bold = " (bold yes)" if text.bold else ""
    return (
        f"\t(text {sexpr.quote(text.text)} (exclude_from_sim no) (at {_fmt(text.at[0])} {_fmt(text.at[1])} 0) "
        f"(effects (font (size {_fmt(text.size)} {_fmt(text.size)}){bold}{color}) (justify {text.justify})) (uuid \"{uid}\"))"
    )


def probe_text(sch: "ReviewSchematic", references: Sequence[str] | None = None) -> str:
    """Describe resolved symbols: pins, model nets, and tip offsets per rotation.

    Read this before choosing rotations; it replaces guessing which end pin 1 is.
    """
    refs = list(references) if references else [c.reference for c in sch.model.components]
    lines: list[str] = []
    for ref in refs:
        if ref not in sch._components:
            raise SchematicError(f"unknown component reference {ref!r}")
        choice = sch.symbol_for(ref)
        lib = choice.symbol
        kind = "generic box" if choice.generic else "stock"
        lines.append(f"{ref}: {lib.lib_id} ({kind}; {choice.reason})")
        variants = [(0, None), (90, None), (180, None), (270, None)]
        if sch._components[ref].kind == "connector":
            variants.append((0, "y"))
        header = "  pin  name        type          model net            " + "  ".join(
            f"rot{rot}{'+mir' + mirror if mirror else '':>6}".ljust(22) for rot, mirror in variants
        )
        lines.append(header)
        pins = sorted(lib.pins, key=lambda p: (len(p.number), p.number))
        for pin in pins:
            if pin.unit not in (0, 1):
                continue
            net = sch._net_of_node.get(f"{ref}.{pin.number}", "-")
            cells = []
            for rot, mirror in variants:
                placed = Placed(ref, lib, (0.0, 0.0), rot, mirror, 1)
                x, y = placed.pin(pin.number)
                cells.append(f"{placed.pin_side(pin.number):<5} ({x:+.2f},{y:+.2f})".ljust(22))
            lines.append(
                f"  {pin.number:<4} {pin.name[:11]:<11} {pin.electrical[:13]:<13} {net[:20]:<20} "
                + "  ".join(cells)
            )
        lines.append("  offsets are the pin tip relative to place(at=...); 'up' means the pin leaves the body upward")
        lines.append("")
    return "\n".join(lines)


def export_preview(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> list[Path]:
    """Export ``review/circuit/preview/circuit.svg`` (+ ``.png`` when possible).

    The SVG is produced by ``kicad-cli sch export svg``; KiCad's searchable
    ``<text>`` fallbacks are stripped so rasterizers do not draw them twice
    over the stroke-font glyphs. The preview directory is a review aid, not
    evidence, and is ignored by git.
    """
    import shutil
    import tempfile

    project_dir = Path(project_dir).expanduser().resolve()
    contract = read_circuit_review_contract(project_dir)
    schematic = project_dir / contract.schematic
    root = (tool_root or Path(__file__).resolve().parents[1]).resolve()
    kicad = str(root / "scripts" / "kicad-cli")
    preview_dir = project_dir / "review" / "circuit" / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="pcbforge-sch-preview-") as temporary:
        staging = Path(temporary)
        completed = runner(
            [
                kicad, "sch", "export", "svg",
                "--exclude-drawing-sheet",
                "--no-background-color",
                "--output", str(staging),
                str(schematic),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        produced = sorted(staging.glob("*.svg"))
        if not produced:
            raise SchematicError(
                f"kicad-cli could not export an SVG preview (exit {completed.returncode})"
            )
        text = produced[0].read_text(encoding="utf-8")
        text = re.sub(r"<text\b.*?</text>", "", text, flags=re.S)
        svg_path = preview_dir / "circuit.svg"
        svg_path.write_text(text, encoding="utf-8")
        outputs.append(svg_path)
    png_path = preview_dir / "circuit.png"
    rsvg = shutil.which("rsvg-convert")
    magick = shutil.which("magick")
    command: list[str] | None = None
    if rsvg:
        command = [rsvg, "-w", "3000", "--background-color=white", str(svg_path), "-o", str(png_path)]
    elif magick:
        command = [magick, "-density", "200", "-background", "white", str(svg_path), "-flatten", "-trim", "+repage", str(png_path)]
    if command:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode == 0 and png_path.is_file():
            outputs.append(png_path)
    return outputs


__all__ = [
    "Placed",
    "RenderResult",
    "ReviewSchematic",
    "SchematicError",
    "SchematicWarning",
    "REVIEW_MARKER",
    "export_preview",
    "probe_text",
    "snap",
]
