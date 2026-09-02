"""Absolute board geometry read from a KiCad 9 ``.kicad_pcb``.

Read-only. Nothing here writes, and no caller may use it to modify a board.

Every coordinate returned is absolute board millimetres in KiCad's frame, where
y grows downward. Footprint children (pads, graphics) are stored in a local,
unrotated frame; :func:`_transform` maps them into board space.

Two conventions were verified against ``kicad-cli pcb export ipcd356``, which
emits absolute pad coordinates, rather than assumed. See
``pilots/kicad9-multichannel/scripts/check_board_geometry.py`` to re-derive
them:

* The rotation formula below is correct to 0.0017 mm, the IPC-D-356 rounding
  limit, across 327 pads.
* Back-side footprints need **no** x mirror. KiCad already stores their child
  offsets mirrored. Adding a mirror inflates the worst error to 5.4 mm.

Coordinate reads are deliberately lenient: :func:`pcbforge.sexpr.number`
returns its default for a non-numeric atom and this module does not second
guess it. KiCad never writes one, and guarding every point read would roughly
double the module. The structural cases that would silently produce a wrong
answer -- a footprint with no ``(at ...)``, no ``(layer ...)``, or no
Reference -- are checked and rejected.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from pcbforge import sexpr
from pcbforge.sexpr import Node

BOARD_FORMAT_VERSION = "20241229"
FALLBACK_MARGIN_MM = 0.25

Point = tuple[float, float]

_GRAPHIC_TAGS = frozenset({"fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly"})
_OUTLINE_TAGS = frozenset({"gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"})
_PRIMITIVE_TAGS = frozenset({"gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"})
_COPPER_TYPES = frozenset({"signal", "power", "mixed", "jumper"})
_THROUGH_HOLE_TYPES = frozenset({"thru_hole", "np_thru_hole"})
_SIDES = {"F.Cu": "front", "B.Cu": "back"}
_EDGE_LAYER = "Edge.Cuts"
_EPS = 1e-9
_ANGLE_EPS = 1e-6


class BoardGeometryError(RuntimeError):
    """The board cannot be read as KiCad 9 geometry."""


@dataclass(frozen=True)
class Box:
    """An axis-aligned box in board millimetres. y grows downward.

    ``contains`` is inclusive: a point exactly on an edge is inside, because a
    pad centre sitting on a courtyard edge is inside that courtyard.

    ``overlaps`` is strict: boxes that share exactly one edge do not overlap.

    ``distance_to`` is the true rectangle gap and returns ``0.0`` when the
    boxes touch or overlap. Note that ``overlaps`` and ``distance_to() == 0``
    are therefore **not** equivalent -- touching boxes give ``False`` and
    ``0.0``.
    """

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def centre(self) -> Point:
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def grow(self, margin: float) -> "Box":
        """A copy expanded by ``margin`` on every side."""
        return Box(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin,
        )

    def contains(self, point: Point, clearance: float = 0.0) -> bool:
        """True when ``point`` lies in this box grown by ``clearance``."""
        x, y = point
        return (
            self.min_x - clearance - _EPS <= x <= self.max_x + clearance + _EPS
            and self.min_y - clearance - _EPS <= y <= self.max_y + clearance + _EPS
        )

    def contains_box(self, other: "Box", clearance: float = 0.0) -> bool:
        """True when ``other`` lies wholly inside this box grown by ``clearance``."""
        return self.contains((other.min_x, other.min_y), clearance) and self.contains(
            (other.max_x, other.max_y), clearance
        )

    def overlaps(self, other: "Box", clearance: float = 0.0) -> bool:
        """True when this box grown by ``clearance`` shares area with ``other``."""
        return (
            self.min_x - clearance < other.max_x
            and other.min_x < self.max_x + clearance
            and self.min_y - clearance < other.max_y
            and other.min_y < self.max_y + clearance
        )

    def distance_to(self, other: "Box") -> float:
        """Nearest-edge distance in mm; ``0.0`` when overlapping or touching."""
        gap_x = max(self.min_x - other.max_x, other.min_x - self.max_x, 0.0)
        gap_y = max(self.min_y - other.max_y, other.min_y - self.max_y, 0.0)
        return math.hypot(gap_x, gap_y)


def union(boxes: Iterable[Box]) -> Box | None:
    """The smallest box containing every input, or ``None`` when there are none."""
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return None
    return Box(
        min(box.min_x for box in boxes),
        min(box.min_y for box in boxes),
        max(box.max_x for box in boxes),
        max(box.max_y for box in boxes),
    )


@dataclass(frozen=True)
class PadGeometry:
    """One pad, with an absolute centre in board millimetres.

    ``size_x``/``size_y`` are the stored dimensions, unrotated. ``box`` is the
    absolute extent with the pad's own rotation and any custom primitives
    already applied, so distance work should use ``box`` and never rebuild one
    from the sizes.
    """

    number: str
    x: float
    y: float
    size_x: float
    size_y: float
    net: str
    through_hole: bool
    box: Box

    @property
    def centre(self) -> Point:
        return (self.x, self.y)


@dataclass(frozen=True)
class FootprintGeometry:
    """One placed footprint and its absolute extent.

    Not hashable: ``frozen=True`` synthesizes ``__hash__`` over every field and
    ``properties`` is a ``dict``, so ``hash()`` raises at call time. Key on
    ``reference`` instead.
    """

    reference: str
    footprint: str
    x: float
    y: float
    rotation: float
    side: str
    box: Box
    box_source: str
    pads: tuple[PadGeometry, ...]
    properties: Mapping[str, str]

    @property
    def centre(self) -> Point:
        return (self.x, self.y)

    def pad(self, number: str) -> PadGeometry:
        for pad in self.pads:
            if pad.number == number:
                return pad
        raise KeyError(f"{self.reference} has no pad {number!r}")


@dataclass(frozen=True)
class ViaGeometry:
    x: float
    y: float
    diameter: float
    drill: float
    net: str


@dataclass(frozen=True)
class ZoneGeometry:
    layer: str
    net: str
    box: Box


@dataclass(frozen=True)
class BoardGeometry:
    """Absolute geometry for one KiCad 9 board."""

    version: int
    layer_count: int
    footprints: tuple[FootprintGeometry, ...]
    outline: Box | None
    outline_segments: int
    vias: tuple[ViaGeometry, ...]
    zones: tuple[ZoneGeometry, ...]
    _by_reference: dict[str, FootprintGeometry] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_by_reference",
            {item.reference: item for item in self.footprints},
        )

    def footprint(self, reference: str) -> FootprintGeometry:
        try:
            return self._by_reference[reference]
        except KeyError:
            raise KeyError(f"no footprint {reference!r} on the board") from None

    def pad(self, reference: str, number: str) -> PadGeometry:
        return self.footprint(reference).pad(number)


def _transform(fx: float, fy: float, rotation: float, point: Point) -> Point:
    """Map one footprint-local point into absolute board coordinates.

    Verified against an IPC-D-356 export to 0.0017 mm. The same transform
    applies on both sides; back-side offsets are already stored mirrored.
    """
    radians = math.radians(rotation)
    cos = math.cos(radians)
    sin = math.sin(radians)
    dx, dy = point
    return (fx + dx * cos + dy * sin, fy - dx * sin + dy * cos)


def _read_root(path: Path) -> Node:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BoardGeometryError(f"missing {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise BoardGeometryError(f"cannot read {path}: {exc}") from exc
    try:
        root = sexpr.parse(text)
    except sexpr.SExprError as exc:
        raise BoardGeometryError(f"{path.name}: not a KiCad board: {exc}") from exc
    if sexpr.head(root) != "kicad_pcb":
        raise BoardGeometryError(f"{path.name}: not a KiCad board")
    version = sexpr.atom(sexpr.child(root, "version"))
    if version != BOARD_FORMAT_VERSION:
        raise BoardGeometryError(
            f"{path.name}: unsupported board version {version!r}; "
            f"expected {BOARD_FORMAT_VERSION}"
        )
    return root


def _net_names(root: Node) -> dict[str, str]:
    """Index-to-name table from the top-level ``(net N "NAME")`` entries.

    Vias carry only the numeric index, so this table is the only way to name
    their net.
    """
    return {
        sexpr.atom(node, 1): sexpr.atom(node, 2)
        for node in sexpr.children(root, "net")
    }


def _layer_count(root: Node) -> int:
    layers = sexpr.child(root, "layers")
    if layers is None:
        return 0
    return sum(
        1
        for entry in layers
        if isinstance(entry, list) and sexpr.atom(entry, 2) in _COPPER_TYPES
    )


def _layer_of(node: Node) -> str:
    return sexpr.atom(sexpr.child(node, "layer"))


def _point(node: Node | None) -> Point:
    return (sexpr.number(node, 1), sexpr.number(node, 2))


def _graphic_points(node: Node) -> list[Point]:
    """Bounding points of one ``fp_*`` or ``gr_*`` graphic, in its own frame.

    A rect and a circle contribute all four corners of their local bounding
    square, not two opposite ones: under a rotation that is not a multiple of
    90 degrees, two opposite corners do not bound the shape.
    """
    tag = sexpr.head(node)
    _, _, suffix = tag.partition("_")
    if suffix == "line":
        return [_point(sexpr.child(node, "start")), _point(sexpr.child(node, "end"))]
    if suffix == "rect":
        (sx, sy) = _point(sexpr.child(node, "start"))
        (ex, ey) = _point(sexpr.child(node, "end"))
        return [(sx, sy), (ex, sy), (ex, ey), (sx, ey)]
    if suffix == "arc":
        return [
            _point(sexpr.child(node, "start")),
            _point(sexpr.child(node, "mid")),
            _point(sexpr.child(node, "end")),
        ]
    if suffix == "circle":
        (cx, cy) = _point(sexpr.child(node, "center"))
        (ex, ey) = _point(sexpr.child(node, "end"))
        radius = math.hypot(ex - cx, ey - cy)
        return [
            (cx - radius, cy - radius),
            (cx + radius, cy - radius),
            (cx + radius, cy + radius),
            (cx - radius, cy + radius),
        ]
    if suffix == "poly":
        pts = sexpr.child(node, "pts")
        return [_point(xy) for xy in sexpr.children(pts or [], "xy")]
    return []


def _collect_points(node: Node, tags: frozenset[str], layer: str) -> list[Point]:
    points: list[Point] = []
    for item in node:
        if not isinstance(item, list) or sexpr.head(item) not in tags:
            continue
        if _layer_of(item) != layer:
            continue
        points.extend(_graphic_points(item))
    return points


def _box_from_points(points: Iterable[Point]) -> Box | None:
    xs: list[float] = []
    ys: list[float] = []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return Box(min(xs), min(ys), max(xs), max(ys))


def _properties(node: Node) -> dict[str, str]:
    return {
        sexpr.atom(item, 1): sexpr.atom(item, 2)
        for item in sexpr.children(node, "property")
    }


def _pad_extent(pad: Node, centre: Point) -> Box | None:
    """Absolute extent of one pad around an already-transformed centre.

    The pad's own ``(at ... angle)`` is ALREADY in board space -- it is the
    footprint rotation plus the pad's relative rotation, normalised. It must
    never be combined with the footprint rotation again. Measured across 327
    pads of the multichannel fixture, the pad-minus-footprint angle is 0 for
    285 of them.
    """
    boxes: list[Box] = []
    angle = sexpr.number(sexpr.child(pad, "at"), 3)
    size = sexpr.child(pad, "size")
    if size is not None:
        size_x = sexpr.number(size, 1)
        size_y = sexpr.number(size, 2)
        remainder = angle % 90.0
        if remainder < _ANGLE_EPS or 90.0 - remainder < _ANGLE_EPS:
            if round(angle / 90.0) % 2:
                size_x, size_y = size_y, size_x
        else:
            size_x = size_y = max(size_x, size_y)
        boxes.append(
            Box(
                centre[0] - size_x / 2,
                centre[1] - size_y / 2,
                centre[0] + size_x / 2,
                centre[1] + size_y / 2,
            )
        )
    primitives = sexpr.child(pad, "primitives")
    if primitives is not None:
        # A custom pad's (size ...) describes only its anchor, so the real
        # copper comes from the primitives, drawn in the pad's own frame.
        points: list[Point] = []
        for item in primitives:
            if isinstance(item, list) and sexpr.head(item) in _PRIMITIVE_TAGS:
                points.extend(_graphic_points(item))
        primitive_box = _box_from_points(
            _transform(centre[0], centre[1], angle, point) for point in points
        )
        if primitive_box is not None:
            boxes.append(primitive_box)
    return union(boxes)


def _pad_geometry(
    pad: Node,
    fx: float,
    fy: float,
    rotation: float,
) -> tuple[PadGeometry, Box | None]:
    at = sexpr.child(pad, "at")
    centre = _transform(fx, fy, rotation, _point(at))
    size = sexpr.child(pad, "size")
    net = sexpr.child(pad, "net")
    extent = _pad_extent(pad, centre)
    geometry = PadGeometry(
        number=sexpr.atom(pad, 1),
        x=centre[0],
        y=centre[1],
        size_x=sexpr.number(size, 1),
        size_y=sexpr.number(size, 2),
        net=sexpr.atom(net, 2) if net is not None else "",
        through_hole=sexpr.atom(pad, 2) in _THROUGH_HOLE_TYPES,
        box=extent
        if extent is not None
        else Box(centre[0], centre[1], centre[0], centre[1]),
    )
    return geometry, extent


def _footprint_box(
    node: Node,
    fx: float,
    fy: float,
    rotation: float,
    side: str,
    pad_boxes: Sequence[Box],
) -> tuple[Box, str]:
    """Absolute extent of one footprint, and which source produced it.

    Courtyard graphics when the footprint has them, otherwise the union of fab,
    silkscreen and pad extents grown by ``FALLBACK_MARGIN_MM``. A plain
    courtyard-then-fab precedence is wrong: a part can carry a pin-1 dot on
    F.Fab and no body outline, which would report a 5x7 mm package as a 0.06 mm
    box.

    Local points are transformed individually and then bounded. Do NOT bound
    them first and transform the corners: for a rotation that is not a multiple
    of 90 degrees, rotating an axis-aligned box and re-bounding circumscribes
    it, which over-estimates a 2x4 courtyard at 45 degrees by 41 percent. Every
    footprint in the repo fixtures sits at a right angle, so no golden test
    catches a regression here.
    """
    prefix = "B" if side == "back" else "F"
    courtyard = _collect_points(node, _GRAPHIC_TAGS, f"{prefix}.CrtYd")
    if courtyard:
        box = _box_from_points(
            _transform(fx, fy, rotation, point) for point in courtyard
        )
        if box is not None:
            return box, "courtyard"
    local = _collect_points(node, _GRAPHIC_TAGS, f"{prefix}.Fab")
    local += _collect_points(node, _GRAPHIC_TAGS, f"{prefix}.SilkS")
    candidates = [
        _box_from_points(_transform(fx, fy, rotation, point) for point in local),
        *pad_boxes,
    ]
    box = union(candidate for candidate in candidates if candidate is not None)
    if box is None:
        return Box(fx, fy, fx, fy), "none"
    return box.grow(FALLBACK_MARGIN_MM), "fallback"


def _footprint_geometry(node: Node) -> FootprintGeometry:
    name = sexpr.atom(node, 1)
    properties = _properties(node)
    reference = properties.get("Reference", "")
    if not reference:
        raise BoardGeometryError(f"footprint {name} has no Reference property")
    at = sexpr.child(node, "at")
    if at is None:
        raise BoardGeometryError(f"footprint {reference} has no (at ...)")
    layer = _layer_of(node)
    side = _SIDES.get(layer)
    if side is None:
        raise BoardGeometryError(
            f"footprint {reference} is on unsupported layer {layer!r}"
        )
    fx = sexpr.number(at, 1)
    fy = sexpr.number(at, 2)
    rotation = sexpr.number(at, 3)
    if not (math.isfinite(fx) and math.isfinite(fy) and math.isfinite(rotation)):
        raise BoardGeometryError(f"footprint {reference} has a non-finite position")
    pads: list[PadGeometry] = []
    pad_boxes: list[Box] = []
    for pad in sexpr.children(node, "pad"):
        geometry, extent = _pad_geometry(pad, fx, fy, rotation)
        pads.append(geometry)
        if extent is not None:
            pad_boxes.append(extent)
    box, box_source = _footprint_box(node, fx, fy, rotation, side, pad_boxes)
    return FootprintGeometry(
        reference=reference,
        footprint=name,
        x=fx,
        y=fy,
        rotation=rotation,
        side=side,
        box=box,
        box_source=box_source,
        pads=tuple(pads),
        properties=properties,
    )


def _footprints(root: Node) -> tuple[FootprintGeometry, ...]:
    items = [_footprint_geometry(node) for node in sexpr.children(root, "footprint")]
    counts = Counter(item.reference for item in items)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise BoardGeometryError(
            "duplicate footprint references: " + ", ".join(duplicates)
        )
    return tuple(sorted(items, key=lambda item: item.reference))


def _outline(root: Node) -> tuple[Box | None, int]:
    points: list[Point] = []
    segments = 0
    for node in root:
        if not isinstance(node, list) or sexpr.head(node) not in _OUTLINE_TAGS:
            continue
        if _layer_of(node) != _EDGE_LAYER:
            continue
        segments += 1
        points.extend(_graphic_points(node))
    return _box_from_points(points), segments


def _vias(root: Node, nets: Mapping[str, str]) -> tuple[ViaGeometry, ...]:
    vias = []
    for node in sexpr.children(root, "via"):
        net = sexpr.child(node, "net")
        index = sexpr.atom(net, 1) if net is not None else ""
        x, y = _point(sexpr.child(node, "at"))
        vias.append(
            ViaGeometry(
                x=x,
                y=y,
                diameter=sexpr.number(sexpr.child(node, "size"), 1),
                drill=sexpr.number(sexpr.child(node, "drill"), 1),
                net=nets.get(index, ""),
            )
        )
    return tuple(vias)


def _zones(root: Node, nets: Mapping[str, str]) -> tuple[ZoneGeometry, ...]:
    zones = []
    for node in sexpr.children(root, "zone"):
        # (polygon ...) is the user-drawn outline. (filled_polygon ...) is
        # derived, repeats per layer, and can hold thousands of points.
        polygon = sexpr.child(node, "polygon")
        if polygon is None:
            continue
        box = _box_from_points(
            _point(xy) for xy in sexpr.children(sexpr.child(polygon, "pts") or [], "xy")
        )
        if box is None:
            continue
        layer = sexpr.child(node, "layer")
        if layer is not None:
            name = sexpr.atom(layer)
        else:
            name = sexpr.atom(sexpr.child(node, "layers"))
        net_name = sexpr.child(node, "net_name")
        if net_name is not None:
            net = sexpr.atom(net_name)
        else:
            net = nets.get(sexpr.atom(sexpr.child(node, "net"), 1), "")
        zones.append(ZoneGeometry(layer=name, net=net, box=box))
    return tuple(zones)


def read_board_geometry(path: Path) -> BoardGeometry:
    """Read absolute geometry from a KiCad 9 board. Never writes."""
    path = Path(path)
    root = _read_root(path)
    nets = _net_names(root)
    outline, segments = _outline(root)
    return BoardGeometry(
        version=int(BOARD_FORMAT_VERSION),
        layer_count=_layer_count(root),
        footprints=_footprints(root),
        outline=outline,
        outline_segments=segments,
        vias=_vias(root, nets),
        zones=_zones(root, nets),
    )


__all__ = [
    "BOARD_FORMAT_VERSION",
    "FALLBACK_MARGIN_MM",
    "BoardGeometry",
    "BoardGeometryError",
    "Box",
    "FootprintGeometry",
    "PadGeometry",
    "Point",
    "ViaGeometry",
    "ZoneGeometry",
    "read_board_geometry",
    "union",
]
