"""Schemdraw-based authoring support for the CIRCUIT review diagram.

The CIRCUIT proposal SVG is authored as a small Python script built on
schemdraw, not as raw SVG. The script draws the schematic with semantic
elements and relative placement; this module supplies the review-specific
scaffolding around it:

- model-bound helpers (sections, net flags, test points, NC flags) whose
  labels are recorded for later ``data-*`` tagging;
- a post-processing save step that stamps the model fingerprint, accessible
  title/desc, the review-only marker, and appends generated register and
  path-legend furniture so ``validate_circuit_svg`` coverage holds by
  construction;
- a text-collision lint that approximates label bounding boxes so authoring
  mistakes surface as machine output instead of requiring visual review.

The drawing itself remains the author's work; everything that used to be
hand-maintained table furniture is generated from ``circuit.yaml``.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from pcbforge.circuit_review import (
    CircuitModel,
    circuit_model_fingerprint,
    read_circuit_model,
    read_circuit_review_contract,
    validate_circuit_svg,
)

SVG_NS = "http://www.w3.org/2000/svg"

_NET_COLOR = "#1a4d8f"
_TP_COLOR = "#7a4a9e"
_NC_COLOR = "#888888"
_NOTE_COLOR = "#555555"
_SECTION_COLOR = "#8d2331"
_FURNITURE_COLOR = "#333333"
_CHAR_WIDTH = 0.58
_GEOMETRY_EPSILON = 0.05
_TEXT_CLEARANCE = 0.08
_CURVE_SAMPLES = 16
_CURVE_STEP = 2 * math.pi / _CURVE_SAMPLES
_AUDIT_ID = "pcbforge-diagram-audit"
_AUDIT_SCHEMA = 1


class DiagramError(RuntimeError):
    """Raised when the authored diagram cannot satisfy the review contract."""


@dataclass
class RenderResult:
    path: Path
    fingerprint: str
    collision_warnings: list[str]
    missing_component_labels: list[str]
    missing_component_symbols: list[str]
    warnings: list["DiagramWarning"]


@dataclass(frozen=True)
class DiagramWarning:
    """One non-blocking readability warning from the diagram audit."""

    code: str
    message: str

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class _Registry:
    """Exact rendered label text mapped to the data-* attribute it proves."""

    entries: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, text: str, attribute: str, value: str) -> None:
        self.entries.append((_normalize(text), attribute, value))


def _normalize(text: str) -> str:
    return " ".join(part.strip() for part in text.split() if part.strip())


def _direction_loc(direction: str) -> str:
    try:
        return {
            "up": "top",
            "down": "bottom",
            "left": "left",
            "right": "right",
        }[direction]
    except KeyError as exc:
        raise DiagramError(f"unknown flag direction {direction!r}") from exc


def _boxes_overlap(a, b) -> bool:
    return (
        a.xmin < b.xmax
        and b.xmin < a.xmax
        and a.ymin < b.ymax
        and b.ymin < a.ymax
    )


def _point_near(a, b, tolerance: float = _GEOMETRY_EPSILON) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def _point_in_box(point, box, inset: float = 0.0) -> bool:
    return (
        box.xmin + inset < point[0] < box.xmax - inset
        and box.ymin + inset < point[1] < box.ymax - inset
    )


def _cross(a, b) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _subtract(a, b) -> tuple[float, float]:
    return (a[0] - b[0], a[1] - b[1])


def _segment_intersection(a, b, c, d):
    """Return (kind, point) for a point or collinear segment intersection.

    Every tolerance scales with segment length. A raw epsilon on a cross
    product reads as "parallel" for the short half-segments Schemdraw emits
    for ordinary stubs, which invents intersections that are not there.
    """
    r = _subtract(b, a)
    s = _subtract(d, c)
    r_length = math.hypot(r[0], r[1])
    s_length = math.hypot(s[0], s[1])
    # NaN-safe: gap sentinels in Schemdraw paths fail this test and drop out
    if not (r_length > _GEOMETRY_EPSILON and s_length > _GEOMETRY_EPSILON):
        return None
    denominator = _cross(r, s)
    offset = _subtract(c, a)
    if abs(denominator) <= _GEOMETRY_EPSILON * r_length * s_length:
        if abs(_cross(offset, r)) > _GEOMETRY_EPSILON * r_length:
            return None
        length_squared = r_length * r_length
        span_epsilon = _GEOMETRY_EPSILON / r_length
        t0 = (offset[0] * r[0] + offset[1] * r[1]) / length_squared
        t1 = t0 + (s[0] * r[0] + s[1] * r[1]) / length_squared
        low, high = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
        if high < low - span_epsilon:
            return None
        if high - low <= span_epsilon:
            return ("point", (a[0] + low * r[0], a[1] + low * r[1]))
        return ("overlap", None)
    t = _cross(offset, s) / denominator
    u = _cross(offset, r) / denominator
    t_epsilon = _GEOMETRY_EPSILON / r_length
    u_epsilon = _GEOMETRY_EPSILON / s_length
    if (
        -t_epsilon <= t <= 1.0 + t_epsilon
        and -u_epsilon <= u <= 1.0 + u_epsilon
    ):
        return ("point", (a[0] + t * r[0], a[1] + t * r[1]))
    return None


def _segment_polyline(segment, transform) -> list:
    """Flatten one Schemdraw segment into absolute (start, end) line pairs.

    ``Segment.path`` only exists on plain path segments, so polygon, circle,
    arc, and bezier bodies are invisible without this. ``xform`` bakes in
    position, rotation, and zoom, so the flattened points are already global.
    """
    from schemdraw.segments import (
        Segment,
        SegmentArc,
        SegmentBezier,
        SegmentCircle,
        SegmentPoly,
    )

    placed = segment.xform(transform)
    if isinstance(placed, SegmentPoly):
        points = list(placed.verts)
        if getattr(placed, "closed", True) and len(points) > 2:
            points.append(points[0])
    elif isinstance(placed, SegmentCircle):
        cx, cy = placed.center
        points = [
            (
                cx + placed.radius * math.cos(_CURVE_STEP * index),
                cy + placed.radius * math.sin(_CURVE_STEP * index),
            )
            for index in range(_CURVE_SAMPLES + 1)
        ]
    elif isinstance(placed, SegmentArc):
        points = _arc_points(placed)
    elif isinstance(placed, SegmentBezier):
        points = _bezier_points(placed.p)
    elif isinstance(placed, Segment):
        points = list(placed.path)
    else:
        # SegmentText and anything else carries no outline to cross
        return []
    return [
        (start, end)
        for start, end in zip(points, points[1:])
        if not _point_near(start, end)
    ]


def _arc_points(arc) -> list:
    cx, cy = arc.center
    rx, ry = arc.width / 2, arc.height / 2
    theta = math.radians(arc.angle)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    start = math.radians(arc.theta1)
    sweep = math.radians(arc.theta2) - start
    points = []
    for index in range(_CURVE_SAMPLES + 1):
        phi = start + sweep * index / _CURVE_SAMPLES
        x, y = rx * math.cos(phi), ry * math.sin(phi)
        points.append((cx + x * cos_t - y * sin_t, cy + x * sin_t + y * cos_t))
    return points


def _bezier_points(control) -> list:
    points = []
    order = len(control) - 1
    if order < 1:
        return []
    for index in range(_CURVE_SAMPLES + 1):
        t = index / _CURVE_SAMPLES
        x = y = 0.0
        for power, (px, py) in enumerate(control):
            weight = (
                math.comb(order, power)
                * (t**power)
                * ((1 - t) ** (order - power))
            )
            x += weight * px
            y += weight * py
        points.append((x, y))
    return points


def _segment_intersects_box(a, b, box, inset: float = 0.0) -> bool:
    if box.xmax - box.xmin <= 2 * inset or box.ymax - box.ymin <= 2 * inset:
        inset = 0.0
    xmin, ymin = box.xmin + inset, box.ymin + inset
    xmax, ymax = box.xmax - inset, box.ymax - inset
    if _point_in_box(a, box, inset) or _point_in_box(b, box, inset):
        return True
    edges = [
        ((xmin, ymin), (xmax, ymin)),
        ((xmax, ymin), (xmax, ymax)),
        ((xmax, ymax), (xmin, ymax)),
        ((xmin, ymax), (xmin, ymin)),
    ]
    return any(_segment_intersection(a, b, start, end) for start, end in edges)


class ReviewDiagram:
    """One authored review schematic bound to one exact circuit model."""

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        model_path: Path | None = None,
        output_path: Path | None = None,
        title: str,
        desc: str,
    ) -> None:
        import schemdraw
        import schemdraw.elements as elements

        if project_dir is not None:
            project_dir = Path(project_dir).expanduser().resolve()
            contract = read_circuit_review_contract(project_dir)
            model_path = project_dir / contract.model
            output_path = project_dir / contract.diagram
        if model_path is None or output_path is None:
            raise DiagramError(
                "pass project_dir, or both model_path and output_path"
            )
        self.model: CircuitModel = read_circuit_model(Path(model_path))
        self.output_path = Path(output_path)
        self.title = title
        self.desc = desc

        schemdraw.use("svg")
        self.elm = elements
        self.drawing = schemdraw.Drawing(show=False)
        self.drawing.config(unit=2.0, fontsize=10)

        self._registry = _Registry()
        self._drawn_groups: set[str] = set()
        self._nets_by_id = {net.identifier: net for net in self.model.nets}
        self._component_refs = {item.reference for item in self.model.components}
        self._component_elements: dict[str, list[object]] = {}
        self._bound_component_refs: set[str] = set()

    def component(self, reference: str, element):
        """Add one real schematic symbol and bind it to a model component."""
        if reference not in self._component_refs:
            raise DiagramError(f"unknown component reference {reference!r}")
        self.drawing += element
        self._component_elements.setdefault(reference, []).append(element)
        self._bound_component_refs.add(reference)
        return element

    # ------------------------------------------------------------------
    # model-bound drawing helpers
    # ------------------------------------------------------------------

    def section(self, group_id: str, xy: tuple[float, float]) -> None:
        """Draw a group's title as a section header; every group needs one."""
        group = next(
            (item for item in self.model.groups if item.identifier == group_id),
            None,
        )
        if group is None:
            raise DiagramError(f"unknown group id {group_id!r}")
        self.drawing += (
            self.elm.Label()
            .at(xy)
            .label(group.title, fontsize=13, color=_SECTION_COLOR, halign="left")
        )
        self._registry.add(group.title, "data-group-id", group_id)
        self._drawn_groups.add(group_id)

    def netflag(
        self,
        at,
        net_id: str,
        direction: str = "up",
        length: float = 0.6,
        fontsize: float = 9,
        note: str = "",
    ):
        """Net flag: stub + open dot + the net's display name."""
        net = self._nets_by_id.get(net_id)
        if net is None:
            raise DiagramError(f"unknown net id {net_id!r}")
        text = f"{net.display_name} · {note}" if note else net.display_name
        line = self.elm.Line().at(at)
        line = getattr(line, direction)().length(length)
        self.drawing += line
        self.drawing += (
            self.elm.Dot(open=True)
            .at(line.end)
            .label(
                text,
                _direction_loc(direction),
                fontsize=fontsize,
                color=_NET_COLOR,
            )
        )
        self._registry.add(text, "data-net-id", net_id)
        return line

    def testpoint(
        self,
        at,
        reference: str,
        direction: str = "right",
        length: float = 0.8,
        note: str = "",
    ):
        """Test-point flag; the reference must exist in the model."""
        if reference not in self._component_refs:
            raise DiagramError(f"unknown test point reference {reference!r}")
        text = f"{reference} {note}".strip()
        line = self.elm.Line().at(at)
        line = getattr(line, direction)().length(length)
        self.drawing += line
        self.drawing += (
            self.elm.Dot(open=True)
            .at(line.end)
            .label(text, _direction_loc(direction), fontsize=8, color=_TP_COLOR)
        )
        self._registry.add(text, "data-component-ref", reference)
        self._bound_component_refs.add(reference)
        return line

    def nc(
        self,
        at,
        net_id: str,
        direction: str = "right",
        length: float = 0.4,
    ):
        """Intentionally-unconnected flag labeled with the NC net name."""
        net = self._nets_by_id.get(net_id)
        if net is None:
            raise DiagramError(f"unknown net id {net_id!r}")
        line = self.elm.Line().at(at)
        line = getattr(line, direction)().length(length)
        self.drawing += line
        self.drawing += (
            self.elm.Dot(open=True)
            .at(line.end)
            .label(
                net.display_name,
                _direction_loc(direction),
                fontsize=7,
                color=_NC_COLOR,
            )
        )
        self._registry.add(net.display_name, "data-net-id", net_id)
        return line

    def note(self, xy: tuple[float, float], text: str, fontsize: float = 8) -> None:
        self.drawing += (
            self.elm.Label()
            .at(xy)
            .label(text, fontsize=fontsize, color=_NOTE_COLOR, halign="left")
        )

    # ------------------------------------------------------------------
    # save and prove
    # ------------------------------------------------------------------

    def save(self) -> RenderResult:
        missing_groups = sorted(
            {item.identifier for item in self.model.groups} - self._drawn_groups
        )
        if missing_groups:
            raise DiagramError(
                "every group needs a section header; missing: "
                + ", ".join(missing_groups)
            )

        raw = self.drawing.get_imagedata("svg")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        ET.register_namespace("", SVG_NS)
        ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
        root = ET.fromstring(raw)

        root.set(
            "data-pcbforge-model-sha256", circuit_model_fingerprint(self.model)
        )
        title = ET.Element(f"{{{SVG_NS}}}title")
        title.text = self.title
        desc = ET.Element(f"{{{SVG_NS}}}desc")
        desc.text = self.desc
        root.insert(0, desc)
        root.insert(0, title)

        self._tag_registered_labels(root)
        missing_refs = self._missing_component_labels(root)
        missing_symbols = self._missing_component_symbols()
        audit_warnings = self._layout_warnings(root)
        audit_warnings.extend(
            DiagramWarning(
                "missing-component-symbol",
                f"{reference} is connected but has no bound schematic symbol",
            )
            for reference in missing_symbols
        )
        audit_warnings = sorted(
            set(audit_warnings), key=lambda item: (item.code, item.message)
        )
        self._append_furniture(root)
        self._append_audit(root, audit_warnings)

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(ET.tostring(root))

        validate_circuit_svg(self.output_path, self.model)
        collision_warnings = [
            warning.message
            for warning in audit_warnings
            if warning.code == "text-text-overlap"
        ]
        return RenderResult(
            path=self.output_path,
            fingerprint=root.get("data-pcbforge-model-sha256", ""),
            collision_warnings=collision_warnings,
            missing_component_labels=missing_refs,
            missing_component_symbols=missing_symbols,
            warnings=audit_warnings,
        )

    # ------------------------------------------------------------------
    # post-processing internals
    # ------------------------------------------------------------------

    def _text_elements(self, root: ET.Element) -> list[ET.Element]:
        return [
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "text"
        ]

    def _tag_registered_labels(self, root: ET.Element) -> None:
        wanted: dict[str, list[tuple[str, str]]] = {}
        for text, attribute, value in self._registry.entries:
            wanted.setdefault(text, []).append((attribute, value))
        for element in self._text_elements(root):
            content = _normalize(" ".join(element.itertext()))
            for attribute, value in wanted.get(content, []):
                element.set(attribute, value)

    def _missing_component_labels(self, root: ET.Element) -> list[str]:
        """References never visible in the drawing itself (advisory only)."""
        seen: set[str] = set()
        for element in self._text_elements(root):
            for token in _normalize(" ".join(element.itertext())).split():
                seen.add(token)
        return sorted(self._component_refs - seen)

    def _missing_component_symbols(self) -> list[str]:
        connected = {
            node.split(".", 1)[0] for net in self.model.nets for node in net.nodes
        }
        return sorted(connected - self._bound_component_refs)

    def _append_audit(self, root: ET.Element, warnings: list[DiagramWarning]) -> None:
        metadata = ET.SubElement(
            root,
            f"{{{SVG_NS}}}metadata",
            {"id": _AUDIT_ID},
        )
        metadata.text = json.dumps(
            {
                "schema": _AUDIT_SCHEMA,
                "bound_component_refs": sorted(self._bound_component_refs),
                "warnings": [warning.payload() for warning in warnings],
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _append_furniture(self, root: ET.Element) -> None:
        minx, miny, width, height = (
            float(part) for part in root.get("viewBox", "0 0 0 0").split()
        )
        x0 = minx + 8
        y = miny + height + 20
        max_x = minx + width - 8

        def add_text(
            x: float,
            ty: float,
            content: str,
            fontsize: float,
            color: str = _FURNITURE_COLOR,
            bold: bool = False,
            attrs: dict[str, str] | None = None,
            parent: ET.Element | None = None,
        ) -> ET.Element:
            element = ET.SubElement(
                root if parent is None else parent,
                f"{{{SVG_NS}}}text",
                {
                    "x": f"{x:.1f}",
                    "y": f"{ty:.1f}",
                    "font-size": str(fontsize),
                    "font-family": "sans",
                    "fill": color,
                    "data-pcbforge-furniture": "1",
                },
            )
            if bold:
                element.set("font-weight", "bold")
            for key, value in (attrs or {}).items():
                element.set(key, value)
            element.text = content
            return element

        add_text(
            x0, y, "PCBForge review-only — not PCB input", 14, _SECTION_COLOR,
            bold=True,
        )
        y += 26

        # Component and purpose register --------------------------------
        add_text(x0, y, "Component register", 12, bold=True)
        y += 6
        rows = [
            (
                item.reference,
                f"{item.reference}  {item.value} — {item.purpose}",
            )
            for item in self.model.components
        ]
        col_chars = max(len(text) for _, text in rows)
        col_width = col_chars * _CHAR_WIDTH * 8 + 24
        columns = max(1, int((width - 16) // col_width))
        per_column = -(-len(rows) // columns)
        row_height = 12.5
        for index, (reference, text) in enumerate(rows):
            cx = x0 + (index // per_column) * col_width
            cy = y + 14 + (index % per_column) * row_height
            add_text(
                cx, cy, text, 8,
                attrs={
                    "data-component-ref": reference,
                    "data-purpose-for": reference,
                },
            )
        y += 14 + per_column * row_height + 12

        # Net register --------------------------------------------------
        add_text(x0, y, "Net register", 12, bold=True)
        y += 6
        net_rows = [
            (net.identifier, net.display_name) for net in self.model.nets
        ]
        net_chars = max(len(text) for _, text in net_rows)
        net_width = net_chars * _CHAR_WIDTH * 8 + 24
        net_columns = max(1, int((width - 16) // net_width))
        net_per_column = -(-len(net_rows) // net_columns)
        for index, (identifier, text) in enumerate(net_rows):
            cx = x0 + (index // net_per_column) * net_width
            cy = y + 14 + (index % net_per_column) * row_height
            add_text(cx, cy, text, 8, attrs={"data-net-id": identifier})
        y += 14 + net_per_column * row_height + 12

        # Reviewed path legend ------------------------------------------
        add_text(x0, y, "Reviewed electrical paths", 12, bold=True)
        y += 8
        for path_item in self.model.paths:
            group = ET.SubElement(
                root,
                f"{{{SVG_NS}}}g",
                {
                    "data-path-id": path_item.identifier,
                    "data-pcbforge-furniture": "1",
                },
            )
            ET.SubElement(
                group,
                f"{{{SVG_NS}}}line",
                {
                    "x1": f"{x0:.1f}",
                    "y1": f"{y + 10:.1f}",
                    "x2": f"{x0 + 24:.1f}",
                    "y2": f"{y + 10:.1f}",
                    "stroke": _FURNITURE_COLOR,
                    "stroke-width": "2",
                },
            )
            chain = " → ".join(path_item.nodes)
            line_chars = max(
                40, int((max_x - x0 - 34) / (_CHAR_WIDTH * 8))
            )
            add_text(
                x0 + 32, y + 13, f"{path_item.title}:", 9, bold=True,
                parent=group,
            )
            offset = y + 13
            while chain:
                piece, chain = chain[:line_chars], chain[line_chars:]
                offset += 11.5
                add_text(x0 + 32, offset, piece, 8, parent=group)
            y = offset + 10

        new_height = (y + 16) - miny
        root.set("viewBox", f"{minx} {miny} {width} {new_height}")
        root.set("height", f"{new_height}pt")

    def _layout_warnings(self, root: ET.Element) -> list[DiagramWarning]:
        """Find readability problems in authored Schemdraw geometry."""
        from schemdraw.elements.lines import Dot, Line, Wire
        from schemdraw.segments import SegmentText

        warnings = [
            DiagramWarning("text-text-overlap", message)
            for message in self._collision_lint(root)
        ]

        texts = []
        wires = []
        wire_endpoints = {}
        dots = []
        for owner in self.drawing.elements:
            if isinstance(owner, Dot) and not owner.params.get("open", False):
                dots.append(owner.absanchors["center"])
            # fluent junctions: Line.dot()/Wire.dot() draw the dot on the line
            # itself, so they never appear as a standalone Dot element
            for param, anchor in (("dot", "end"), ("idot", "start")):
                marker = owner.params.get(param, False)
                if marker and marker != "open" and anchor in owner.absanchors:
                    dots.append(owner.absanchors[anchor])
            if isinstance(owner, (Line, Wire)):
                wire_endpoints[id(owner)] = (
                    owner.absanchors["start"],
                    owner.absanchors["end"],
                )
                for segment in owner.segments:
                    wires.extend(
                        (owner, start, end)
                        for start, end in _segment_polyline(
                            segment, owner.transform
                        )
                    )
            for segment in owner.segments:
                if not isinstance(segment, SegmentText):
                    continue
                text = _normalize(segment.text)
                if not text or (len(text) < 3 and "\n" not in text):
                    continue
                transformed = segment.xform(owner.transform)
                texts.append((owner, text, transformed.get_bbox()))

        symbols = []
        for reference, elements in self._component_elements.items():
            for element in elements:
                box = element.get_bbox(transform=True, includetext=False)
                values = (box.xmin, box.ymin, box.xmax, box.ymax)
                if all(math.isfinite(value) for value in values):
                    geometry = []
                    for segment in element.segments:
                        geometry.extend(
                            _segment_polyline(segment, element.transform)
                        )
                    symbols.append(
                        (
                            reference,
                            element,
                            box,
                            geometry,
                            tuple(element.absanchors.values()),
                        )
                    )

        for owner, text, box in texts:
            for wire_owner, start, end in wires:
                # a label belongs to its own wire; Schemdraw anchors the text
                # box on the baseline, so it always grazes the line it names
                if owner is wire_owner:
                    continue
                if _segment_intersects_box(start, end, box, inset=-_TEXT_CLEARANCE):
                    midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
                    warnings.append(
                        DiagramWarning(
                            "text-wire-overlap",
                            f"text {text!r} overlaps a wire near "
                            f"({midpoint[0]:.2f}, {midpoint[1]:.2f})",
                        )
                    )
            for reference, symbol_owner, symbol_box, _, _ in symbols:
                if owner is symbol_owner:
                    continue
                if _boxes_overlap(box, symbol_box):
                    warnings.append(
                        DiagramWarning(
                            "text-symbol-overlap",
                            f"text {text!r} overlaps component {reference}",
                        )
                    )

        for _, start, end in wires:
            for reference, _, _, geometry, anchors in symbols:
                crosses = False
                for symbol_start, symbol_end in geometry:
                    intersection = _segment_intersection(
                        start, end, symbol_start, symbol_end
                    )
                    if intersection is None:
                        continue
                    kind, point = intersection
                    if kind == "overlap":
                        crosses = True
                        break
                    assert point is not None
                    wire_endpoint = any(
                        _point_near(point, endpoint) for endpoint in (start, end)
                    )
                    component_anchor = any(
                        _point_near(point, anchor) for anchor in anchors
                    )
                    junction_dot = any(_point_near(point, dot) for dot in dots)
                    if not (component_anchor and (wire_endpoint or junction_dot)):
                        crosses = True
                        break
                if crosses:
                    midpoint = (
                        (start[0] + end[0]) / 2,
                        (start[1] + end[1]) / 2,
                    )
                    warnings.append(
                        DiagramWarning(
                            "wire-symbol-overlap",
                            f"wire near ({midpoint[0]:.2f}, {midpoint[1]:.2f}) "
                            f"crosses component {reference}",
                        )
                    )

        for index, (owner_a, a, b) in enumerate(wires):
            for owner_b, c, d in wires[index + 1 :]:
                if owner_a is owner_b:
                    continue
                intersection = _segment_intersection(a, b, c, d)
                if intersection is None:
                    continue
                kind, point = intersection
                if kind == "overlap":
                    midpoint = (
                        (
                            max(min(a[0], b[0]), min(c[0], d[0]))
                            + min(max(a[0], b[0]), max(c[0], d[0]))
                        )
                        / 2,
                        (
                            max(min(a[1], b[1]), min(c[1], d[1]))
                            + min(max(a[1], b[1]), max(c[1], d[1]))
                        )
                        / 2,
                    )
                    warnings.append(
                        DiagramWarning(
                            "overlapping-wire-runs",
                            f"wire runs overlap near "
                            f"({midpoint[0]:.2f}, {midpoint[1]:.2f})",
                        )
                    )
                    continue
                assert point is not None
                shared_endpoint = any(
                    _point_near(point, endpoint)
                    for endpoint in wire_endpoints[id(owner_a)]
                ) and any(
                    _point_near(point, endpoint)
                    for endpoint in wire_endpoints[id(owner_b)]
                )
                if shared_endpoint or any(_point_near(point, dot) for dot in dots):
                    continue
                warnings.append(
                    DiagramWarning(
                        "ambiguous-wire-crossing",
                        f"wires cross without a junction dot at "
                        f"({point[0]:.2f}, {point[1]:.2f})",
                    )
                )
        return warnings

    def _collision_lint(self, root: ET.Element) -> list[str]:
        boxes = []
        for element in self._text_elements(root):
            if element.get("data-pcbforge-furniture"):
                continue
            try:
                x = float(element.get("x", ""))
                ty = float(element.get("y", ""))
            except ValueError:
                continue
            fontsize = float(element.get("font-size", "10"))
            lines = [
                _normalize(part.text or "")
                for part in element.iter()
                if part.tag.rsplit("}", 1)[-1] == "tspan"
            ] or [_normalize("".join(element.itertext()))]
            longest = max((len(line) for line in lines), default=0)
            if not longest:
                continue
            if longest < 3 and len(lines) == 1:
                # pin numbers and other short adjuncts sit deliberately close
                continue
            text_width = longest * _CHAR_WIDTH * fontsize
            anchor = element.get("text-anchor", "start")
            if anchor == "middle":
                left = x - text_width / 2
            elif anchor == "end":
                left = x - text_width
            else:
                left = x
            box = (
                left,
                ty,
                left + text_width,
                ty + fontsize * 1.05 * len(lines),
            )
            # shrink to reduce false positives from the width estimate
            dx = (box[2] - box[0]) * 0.12
            dy = (box[3] - box[1]) * 0.12
            boxes.append(
                (
                    (box[0] + dx, box[1] + dy, box[2] - dx, box[3] - dy),
                    _normalize(" ".join(element.itertext())),
                )
            )
        warnings = []
        for i, (a, a_text) in enumerate(boxes):
            for b, b_text in boxes[i + 1:]:
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    warnings.append(
                        f"possible label collision: {a_text!r} and {b_text!r}"
                    )
        return warnings


__all__ = [
    "DiagramError",
    "DiagramWarning",
    "RenderResult",
    "ReviewDiagram",
]
