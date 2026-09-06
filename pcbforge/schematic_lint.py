"""Readability checks measured from the saved KiCad drawing."""

from dataclasses import dataclass
import math
from pathlib import Path

from pcbforge import sexpr as sx
from pcbforge.kicad_sym import LibSymbol, _pins, _units
from pcbforge.sch_lint import Box, SheetGeometry, TextBox, lint
from pcbforge.schematic import digest, hierarchy, properties


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    sheet: str
    severity: str = "error"

    @property
    def identifier(self) -> str:
        return digest([self.code, self.message, self.sheet])[:20]


def _point(node):
    return sx.number(node), sx.number(node, 2)


def _transform(x, y, node):
    y = -y
    mirror = sx.atom(sx.child(node, "mirror"))
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    at = sx.child(node, "at")
    angle = math.radians(-sx.number(at, 3))
    return (round(sx.number(at) + x * math.cos(angle) - y * math.sin(angle), 6),
            round(sx.number(at, 2) + x * math.sin(angle) + y * math.cos(angle), 6))


def _text(node, text, owner=None):
    effects = sx.child(node, "effects") or []
    def hidden(parent):
        flag = sx.child(parent, "hide")
        return "hide" in sx.atoms(parent) or flag is not None and sx.atom(flag, default="yes") == "yes"
    if hidden(node) or hidden(effects) or not text:
        return None
    font = sx.child(effects, "font") or []
    size = sx.child(font, "size")
    height, width = sx.number(size, 1, 1.27), sx.number(size, 2, 1.27)
    lines = text.splitlines() or [""]
    w = max(len(line) for line in lines) * width * 0.65
    h = height * len(lines)
    at = sx.child(node, "at")
    x, y = _point(at)
    if int(sx.number(at, 3)) % 180 == 90:
        w, h = h, w
    justify = sx.atoms(sx.child(effects, "justify") or [])
    left = x if "left" in justify else x - w if "right" in justify else x - w / 2
    top = y if "top" in justify else y-h if "bottom" in justify else y-h/2
    return TextBox(text, Box(left, top, left+w, top+h), owner)


def lint_saved(path: Path) -> tuple[Finding, ...]:
    findings = []
    seen = set()
    for file, instance, root in hierarchy(path):
        # Repeated instances share drawing geometry and need one visual inspection.
        if file in seen:
            continue
        seen.add(file)
        sheet = file.relative_to(path.resolve().parent).as_posix()
        libraries = {sx.atom(n): n for n in sx.children(sx.child(root, "lib_symbols") or [], "symbol")}
        symbols, tips_by_symbol, groups = {}, {}, {}
        texts, tips, wires, labels = [], set(), [], []
        junctions = {_point(sx.child(n, "at")) for n in sx.children(root, "junction")}
        for node in sx.children(root, "symbol"):
            props = properties(node)
            ref = props.get("Reference", "?")
            key = ref + ":" + sx.atom(sx.child(node, "unit"), default="1")
            name = sx.atom(sx.child(node, "lib_id"))
            lib = libraries.get(name)
            if lib is None:
                findings.append(Finding("missing-symbol", f"{ref}: missing embedded definition {name}", sheet))
                continue
            definition = LibSymbol(name, lib, _pins(lib, name.split(":")[-1]), _units(lib))
            unit = int(sx.atom(sx.child(node, "unit"), default="1"))
            pin_tips = {_transform(p.x, p.y, node) for p in definition.pins if p.unit in (0, unit)}
            tips.update(pin_tips)
            tips_by_symbol[key] = frozenset(pin_tips)
            x1, y1, x2, y2 = definition.bbox(unit, include_pins=False)
            corners = [_transform(x, y, node) for x, y in ((x1,y1),(x1,y2),(x2,y1),(x2,y2))]
            if not ref.startswith("#"):
                symbols[key] = Box(min(p[0] for p in corners), min(p[1] for p in corners),
                                   max(p[0] for p in corners), max(p[1] for p in corners))
                if not props.get("pcbforge_purpose"):
                    findings.append(Finding("missing-purpose", f"{ref}: explain the component purpose", sheet, "warning"))
            for prop in sx.children(node, "property"):
                text = _text(prop, sx.atom(prop, 2), key)
                if text:
                    texts.append(text)
        for node in sx.children(root, "wire"):
            points = [_point(p) for p in sx.children(sx.child(node, "pts") or [], "xy")]
            if len(points) != 2:
                findings.append(Finding("invalid-wire", "wire must have two points", sheet))
                continue
            wires.append(tuple(points))
        # Native sheet pins and bus entries are valid wire terminations.
        tips.update(_point(sx.child(pin, "at")) for sheet_node in sx.children(root, "sheet") for pin in sx.children(sheet_node, "pin"))
        for entry in sx.children(root, "bus_entry"):
            start = _point(sx.child(entry, "at"))
            size = _point(sx.child(entry, "size"))
            tips.update((start, (round(start[0]+size[0], 6), round(start[1]+size[1], 6))))
        bus_segments = [tuple(_point(p) for p in sx.children(sx.child(bus, "pts") or [], "xy")) for bus in sx.children(root, "bus")]
        for tag in ("label", "global_label", "hierarchical_label"):
            for node in sx.children(root, tag):
                point = _point(sx.child(node, "at"))
                labels.append((point, sx.atom(node)))
                if point not in tips and not any(_on_wire(point, a, b) for a, b in [*wires, *(segment for segment in bus_segments if len(segment) == 2)]):
                    findings.append(Finding("dangling-label", f"label {sx.atom(node)!r} at {point} connects to nothing", sheet))
                text = _text(node, sx.atom(node))
                if text:
                    texts.append(text)
        for node in sx.children(root, "text"):
            text = _text(node, sx.atom(node))
            if text:
                texts.append(text)
        for node in sx.children(root, "sheet"):
            at, size = _point(sx.child(node,"at")), _point(sx.child(node,"size"))
            key = "sheet:" + sx.atom(sx.child(node, "uuid"))
            symbols[key] = Box(at[0], at[1], at[0]+size[0], at[1]+size[1])
            pins = {_point(sx.child(n,"at")) for n in sx.children(node,"pin")}
            tips.update(pins)
            tips_by_symbol[key] = frozenset(pins)
        geometry = SheetGeometry(tuple(texts), symbols, tips_by_symbol, tuple(wires), frozenset(junctions),
                                 frozenset(tips), frozenset(p for p,_ in labels), {}, groups, tuple(labels))
        for finding in lint(geometry):
            severity = "warning" if finding.code in {"wire-crossing", "wire-passes-pin"} else "error"
            findings.append(Finding(finding.code, finding.message, sheet, severity))
        if len(labels) > len(wires) and len(symbols) > 3:
            findings.append(Finding("fragmented-paths", "labels outnumber wires; review continuous functional paths", sheet, "warning"))
    return tuple(findings)


def _on_wire(p, a, b):
    if a[0] == b[0]:
        return p[0] == a[0] and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    if a[1] == b[1]:
        return p[1] == a[1] and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
    return False
