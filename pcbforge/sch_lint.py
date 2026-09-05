"""Readability lint for the generated KiCad review schematic.

Works on the writer's in-memory geometry (sheet coordinates, mm, y down),
never on the serialized file. All wires are axis aligned, which keeps the
checks exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

Point = tuple[float, float]
_EPS = 1e-6
_TEXT_CLEARANCE = 0.25


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float

    def overlaps(self, other: "Box", clearance: float = 0.0) -> bool:
        return (
            self.x1 - clearance < other.x2
            and other.x1 - clearance < self.x2
            and self.y1 - clearance < other.y2
            and other.y1 - clearance < self.y2
        )

    def contains(self, point: Point, inset: float = 0.0) -> bool:
        return (
            self.x1 + inset < point[0] < self.x2 - inset
            and self.y1 + inset < point[1] < self.y2 - inset
        )

    def contains_box(self, other: "Box") -> bool:
        return (
            self.x1 - _EPS <= other.x1
            and self.y1 - _EPS <= other.y1
            and other.x2 <= self.x2 + _EPS
            and other.y2 <= self.y2 + _EPS
        )

    @property
    def center(self) -> Point:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)


@dataclass(frozen=True)
class TextBox:
    label: str
    box: Box
    owner: str | None = None


@dataclass(frozen=True)
class SheetGeometry:
    texts: tuple[TextBox, ...]
    symbols: Mapping[str, Box]
    symbol_pins: Mapping[str, frozenset[Point]]
    wires: tuple[tuple[Point, Point], ...]
    junctions: frozenset[Point]
    pin_tips: frozenset[Point]
    label_points: frozenset[Point]
    group_boxes: Mapping[str, Box]
    group_of: Mapping[str, str]
    labels: tuple[tuple[Point, str], ...] = ()
    power_points: Mapping[Point, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LintWarning:
    code: str
    message: str

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _fmt_point(point: Point) -> str:
    return f"({point[0]:.2f}, {point[1]:.2f})"


def _on_segment(point: Point, a: Point, b: Point) -> bool:
    if abs(a[0] - b[0]) < _EPS:
        return abs(point[0] - a[0]) < _EPS and min(a[1], b[1]) - _EPS <= point[1] <= max(a[1], b[1]) + _EPS
    if abs(a[1] - b[1]) < _EPS:
        return abs(point[1] - a[1]) < _EPS and min(a[0], b[0]) - _EPS <= point[0] <= max(a[0], b[0]) + _EPS
    return False


def _strictly_on_segment(point: Point, a: Point, b: Point) -> bool:
    if _near(point, a) or _near(point, b):
        return False
    return _on_segment(point, a, b)


def _near(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS


def _segment_box(a: Point, b: Point) -> Box:
    return Box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _crossing(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    """Proper crossing point of two axis-aligned segments, or None."""
    a_vertical = abs(a[0] - b[0]) < _EPS
    c_vertical = abs(c[0] - d[0]) < _EPS
    if a_vertical == c_vertical:
        return None
    if a_vertical:
        vx, vy1, vy2 = a[0], min(a[1], b[1]), max(a[1], b[1])
        hy, hx1, hx2 = c[1], min(c[0], d[0]), max(c[0], d[0])
    else:
        vx, vy1, vy2 = c[0], min(c[1], d[1]), max(c[1], d[1])
        hy, hx1, hx2 = a[1], min(a[0], b[0]), max(a[0], b[0])
    if hx1 - _EPS <= vx <= hx2 + _EPS and vy1 - _EPS <= hy <= vy2 + _EPS:
        return (vx, hy)
    return None


def _collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    a_vertical = abs(a[0] - b[0]) < _EPS
    c_vertical = abs(c[0] - d[0]) < _EPS
    if a_vertical != c_vertical:
        return None
    if a_vertical:
        if abs(a[0] - c[0]) > _EPS:
            return None
        low = max(min(a[1], b[1]), min(c[1], d[1]))
        high = min(max(a[1], b[1]), max(c[1], d[1]))
        if high - low > _EPS:
            return (a[0], (low + high) / 2)
        return None
    if abs(a[1] - c[1]) > _EPS:
        return None
    low = max(min(a[0], b[0]), min(c[0], d[0]))
    high = min(max(a[0], b[0]), max(c[0], d[0]))
    if high - low > _EPS:
        return ((low + high) / 2, a[1])
    return None


def _segment_hits_box(a: Point, b: Point, box: Box, inset: float = 0.0) -> bool:
    """True when the segment enters the (inset) box interior."""
    if box.x2 - box.x1 <= 2 * inset or box.y2 - box.y1 <= 2 * inset:
        inset = 0.0
    inner = Box(box.x1 + inset, box.y1 + inset, box.x2 - inset, box.y2 - inset)
    if inner.contains(a) or inner.contains(b):
        return True
    seg = _segment_box(a, b)
    if not inner.overlaps(seg):
        return False
    if abs(a[0] - b[0]) < _EPS:  # vertical
        return inner.x1 < a[0] < inner.x2 and min(a[1], b[1]) < inner.y2 and max(a[1], b[1]) > inner.y1
    return inner.y1 < a[1] < inner.y2 and min(a[0], b[0]) < inner.x2 and max(a[0], b[0]) > inner.x1


def lint(geometry: SheetGeometry) -> list[LintWarning]:
    warnings: list[LintWarning] = []
    wires = geometry.wires
    wire_points: set[Point] = set()
    for a, b in wires:
        wire_points.add(a)
        wire_points.add(b)

    # text vs text
    texts = list(geometry.texts)
    for index, first in enumerate(texts):
        for second in texts[index + 1 :]:
            if first.box.overlaps(second.box, -_TEXT_CLEARANCE):
                warnings.append(
                    LintWarning(
                        "text-text-overlap",
                        f"text {first.label!r} overlaps text {second.label!r}",
                    )
                )

    # text vs wire / symbol
    for text in texts:
        for a, b in wires:
            if _segment_hits_box(a, b, text.box, inset=_TEXT_CLEARANCE):
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                warnings.append(
                    LintWarning(
                        "text-wire-overlap",
                        f"text {text.label!r} overlaps a wire near {_fmt_point(mid)}",
                    )
                )
        for reference, box in geometry.symbols.items():
            if text.owner == reference:
                continue
            if text.box.overlaps(box, -_TEXT_CLEARANCE):
                warnings.append(
                    LintWarning(
                        "text-symbol-overlap",
                        f"text {text.label!r} overlaps component {reference}",
                    )
                )

    # symbol vs symbol
    refs = sorted(geometry.symbols)
    for index, first in enumerate(refs):
        for second in refs[index + 1 :]:
            if geometry.symbols[first].overlaps(geometry.symbols[second]):
                warnings.append(
                    LintWarning("symbol-overlap", f"components {first} and {second} overlap")
                )

    # wire vs symbol body: a wire may touch a symbol only at one of its pin tips
    for a, b in wires:
        for reference, box in geometry.symbols.items():
            if not _segment_hits_box(a, b, box, inset=0.05):
                continue
            own_tips = geometry.symbol_pins.get(reference, frozenset())
            if any(_near(p, tip) for p in (a, b) for tip in own_tips):
                continue  # a stub leaving one of this symbol's own pins
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            warnings.append(
                LintWarning(
                    "wire-symbol-overlap",
                    f"wire near {_fmt_point(mid)} crosses component {reference}",
                )
            )

    # wire vs wire
    for index, (a, b) in enumerate(wires):
        for c, d in wires[index + 1 :]:
            overlap = _collinear_overlap(a, b, c, d)
            if overlap is not None:
                warnings.append(
                    LintWarning(
                        "overlapping-wire-runs",
                        f"wire runs overlap near {_fmt_point(overlap)}",
                    )
                )
                continue
            point = _crossing(a, b, c, d)
            if point is None:
                continue
            if any(_near(point, p) for p in (a, b, c, d)):
                continue  # T or corner, junction handled by the writer
            if any(_near(point, j) for j in geometry.junctions):
                continue
            warnings.append(
                LintWarning(
                    "wire-crossing",
                    f"wires cross without connecting at {_fmt_point(point)}",
                )
            )

    # wire passing over a pin tip it does not end on: KiCad connects it
    for a, b in wires:
        for tip in geometry.pin_tips:
            if _strictly_on_segment(tip, a, b):
                warnings.append(
                    LintWarning(
                        "wire-passes-pin",
                        f"wire runs through pin tip at {_fmt_point(tip)} without ending there",
                    )
                )

    # dangling wire ends
    for a, b in wires:
        for point in (a, b):
            connected = (
                point in geometry.pin_tips
                or point in geometry.label_points
                or point in geometry.junctions
                or sum(1 for c, d in wires if _on_segment(point, c, d)) > 1
            )
            if not connected:
                warnings.append(
                    LintWarning("dangling-wire-end", f"wire end at {_fmt_point(point)} connects to nothing")
                )

    # a label sharing a point with a differently named power pin merges the nets
    for point, text in geometry.labels:
        power_name = geometry.power_points.get(point)
        if power_name is not None and power_name != text:
            warnings.append(
                LintWarning(
                    "label-meets-power-symbol",
                    f"label {text!r} and power symbol {power_name!r} share {_fmt_point(point)}; KiCad joins them",
                )
            )
    seen_labels: dict[Point, str] = {}
    for point, text in geometry.labels:
        other = seen_labels.setdefault(point, text)
        if other != text:
            warnings.append(
                LintWarning(
                    "label-meets-power-symbol",
                    f"labels {other!r} and {text!r} share {_fmt_point(point)}; KiCad joins them",
                )
            )

    # groups
    group_ids = sorted(geometry.group_boxes)
    for index, first in enumerate(group_ids):
        for second in group_ids[index + 1 :]:
            if geometry.group_boxes[first].overlaps(geometry.group_boxes[second]):
                warnings.append(
                    LintWarning("group-boxes-overlap", f"group boxes {first} and {second} overlap")
                )
    for reference, box in geometry.symbols.items():
        group_id = geometry.group_of.get(reference)
        group_box = geometry.group_boxes.get(group_id or "")
        if group_box is None:
            continue
        if not group_box.contains_box(box):
            warnings.append(
                LintWarning("symbol-outside-group", f"{reference} lies outside group box {group_id}")
            )
        for other_id, other_box in geometry.group_boxes.items():
            if other_id != group_id and other_box.overlaps(box):
                warnings.append(
                    LintWarning("symbol-outside-group", f"{reference} intrudes into group box {other_id}")
                )
    for a, b in wires:
        for group_id, box in geometry.group_boxes.items():
            a_in, b_in = box.contains(a), box.contains(b)
            if a_in != b_in and not (_point_on_box_edge(a, box) or _point_on_box_edge(b, box)):
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                warnings.append(
                    LintWarning(
                        "wire-crosses-group-box",
                        f"wire near {_fmt_point(mid)} crosses the {group_id} group box; "
                        "move the groups apart or draw without boxes (group_boxes=False)",
                    )
                )
            elif not a_in and not b_in and _segment_hits_box(a, b, box):
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                warnings.append(
                    LintWarning(
                        "wire-crosses-group-box",
                        f"wire near {_fmt_point(mid)} passes through the {group_id} group box",
                    )
                )
    return warnings


def _point_on_box_edge(point: Point, box: Box) -> bool:
    on_x = abs(point[0] - box.x1) < _EPS or abs(point[0] - box.x2) < _EPS
    on_y = abs(point[1] - box.y1) < _EPS or abs(point[1] - box.y2) < _EPS
    return (on_x and box.y1 - _EPS <= point[1] <= box.y2 + _EPS) or (
        on_y and box.x1 - _EPS <= point[0] <= box.x2 + _EPS
    )


__all__ = ["Box", "LintWarning", "SheetGeometry", "TextBox", "lint"]
