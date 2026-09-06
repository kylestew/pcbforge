"""Transactional edits to saved schematics; untouched top-level records survive."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from pcbforge import sexpr as sx
from pcbforge.kicad_sym import lib_symbol
from pcbforge.kicad_tools import library_dir
from pcbforge.schematic import SchematicError, empty_schematic, extract, properties, read_root, source_files


def record_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    depth = 0
    quoted = escaped = False
    start = 0
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            if depth == 1:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 1:
                spans.append((start, index + 1))
            if depth < 0:
                raise SchematicError("unbalanced schematic")
    if depth or quoted:
        raise SchematicError("unbalanced schematic")
    return spans


def _uuid() -> sx.Node:
    return ["uuid", sx.Quoted(str(uuid.uuid4()))]


def _set(node: sx.Node, key: str, *values: str) -> None:
    prior = sx.child(node, key)
    replacement = [key, *values]
    if prior is None:
        node.append(replacement)
    else:
        node[node.index(prior)] = replacement


def _effects(size: float = 1.27) -> sx.Node:
    return ["effects", ["font", ["size", str(size), str(size)]]]


class SchematicDocument:
    def __init__(self, path: Path, text: str, *, exists: bool = True):
        self.path = Path(path).resolve()
        self.original = text if exists else None
        self.text = text
        self.root = sx.parse(text)
        self.original_nodes = copy.deepcopy(self.root)

    @classmethod
    def load(cls, path: Path) -> SchematicDocument:
        read_root(path)
        return cls(path, path.read_text())

    @classmethod
    def create(cls, path: Path, *, title: str | None = None) -> SchematicDocument:
        path = Path(path)
        if path.exists():
            raise SchematicError(f"refusing to overwrite {path}")
        return cls(path, empty_schematic(title or path.stem), exists=False)

    @contextmanager
    def transaction(self):
        before = copy.deepcopy(self.root)
        try:
            yield self
        except BaseException:
            self.root = before
            raise

    def _symbol(self, reference: str, unit: int = 1) -> sx.Node:
        matches = [n for n in sx.children(self.root, "symbol")
                   if properties(n).get("Reference") == reference and int(sx.atom(sx.child(n, "unit"), default="1")) == unit]
        if len(matches) != 1:
            raise SchematicError(f"expected one symbol {reference} unit {unit}; found {len(matches)}")
        return matches[0]

    def add_symbol(self, reference: str, symbol_id: str, at: tuple[float, float], *,
                   value: str | None = None, footprint: str = "", fields: dict[str, str] | None = None,
                   unit: int = 1, angle: float = 0, symbols_dir: Path | None = None) -> str:
        if any(properties(n).get("Reference") == reference and int(sx.atom(sx.child(n, "unit"), default="1")) == unit
               for n in sx.children(self.root, "symbol")):
            raise SchematicError(f"duplicate symbol {reference} unit {unit}")
        definition = lib_symbol(symbol_id, symbols_dir or library_dir("symbols"))
        if unit < 1 or unit > definition.units:
            raise SchematicError(f"{symbol_id} has no unit {unit}")
        libs = sx.child(self.root, "lib_symbols")
        if libs is None:
            libs = ["lib_symbols"]
            self.root.append(libs)
        if not any(sx.atom(n) == symbol_id for n in sx.children(libs, "symbol")):
            libs.append(copy.deepcopy(definition.node))
        uid = str(uuid.uuid4())
        x, y = at
        root_uuid = sx.atom(sx.child(self.root, "uuid"))
        node = ["symbol", ["lib_id", sx.Quoted(symbol_id)], ["at", str(x), str(y), str(angle)],
                ["unit", str(unit)], ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"],
                ["uuid", sx.Quoted(uid)]]
        data = {"Reference": reference, "Value": value or symbol_id.split(":")[-1], "Footprint": footprint,
                "Datasheet": "", **(fields or {})}
        for index, (key, val) in enumerate(data.items()):
            effects = _effects()
            if key not in {"Reference", "Value"}:
                effects.append(["hide", "yes"])
            node.append(["property", sx.Quoted(key), sx.Quoted(val), ["at", str(x + 3), str(y + index * 2), "0"], effects])
        for pin in definition.pins:
            if pin.unit in (0, unit):
                node.append(["pin", sx.Quoted(pin.number), _uuid()])
        node.append(["instances", ["project", sx.Quoted(self.path.stem),
                     ["path", sx.Quoted("/" + root_uuid), ["reference", sx.Quoted(reference)], ["unit", str(unit)]]]])
        self.root.append(node)
        return uid

    def _component_units(self, reference: str, unit: int | None):
        if unit is not None:
            return [self._symbol(reference, unit)]
        nodes = [n for n in sx.children(self.root, "symbol") if properties(n).get("Reference") == reference]
        if not nodes:
            raise SchematicError(f"unknown component: {reference}")
        return nodes

    def set_field(self, reference: str, name: str, value: str, *, unit: int | None = None) -> None:
        """Update a component field across its units unless one unit is specified."""
        nodes = self._component_units(reference, unit)
        if name == "Reference":
            if value != reference and any(properties(n).get("Reference") == value for n in sx.children(self.root, "symbol")):
                raise SchematicError(f"reference already exists: {value}")
            if any(len(sx.children(p, "path")) > 1 for n in nodes for p in sx.children(sx.child(n, "instances") or [], "project")):
                raise SchematicError("annotate repeated sheet instances in KiCad; references differ by instance")
        for node in nodes:
            prop = next((p for p in sx.children(node, "property") if sx.atom(p) == name), None)
            if prop is None:
                at = copy.deepcopy(sx.child(node, "at"))
                node.append(["property", sx.Quoted(name), sx.Quoted(value), at, ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]]])
            else:
                prop[2] = sx.Quoted(value)
            if name == "Reference":
                for item in sx.walk(sx.child(node, "instances") or []):
                    if sx.head(item) == "reference":
                        item[1] = sx.Quoted(value)

    def set_flag(self, reference: str, flag: str, enabled: bool, *, unit: int | None = None) -> None:
        if flag not in {"dnp", "in_bom", "on_board"} or type(enabled) is not bool:
            raise SchematicError(f"unsupported symbol flag or value: {flag}")
        for node in self._component_units(reference, unit):
            _set(node, flag, "yes" if enabled else "no")

    def move_symbol(self, reference: str, at: tuple[float, float], *, unit: int = 1) -> None:
        node = self._symbol(reference, unit)
        old = sx.child(node, "at")
        dx, dy = at[0] - sx.number(old), at[1] - sx.number(old, 2)
        _set(node, "at", str(at[0]), str(at[1]), sx.atom(old, 3, "0"))
        for prop in sx.children(node, "property"):
            pos = sx.child(prop, "at")
            if pos:
                pos[1], pos[2] = str(sx.number(pos) + dx), str(sx.number(pos, 2) + dy)

    def pin_position(self, reference: str, number: str, *, unit: int = 1) -> tuple[float, float]:
        node = self._symbol(reference, unit)
        lib_id = sx.atom(sx.child(node, "lib_id"))
        lib = next((n for n in sx.children(sx.child(self.root, "lib_symbols") or [], "symbol") if sx.atom(n) == lib_id), None)
        if lib is None:
            raise SchematicError(f"missing embedded symbol {lib_id}")
        from pcbforge.kicad_sym import _pins
        pins = [p for p in _pins(lib, lib_id.split(":")[-1]) if p.number == str(number) and p.unit in (0, unit)]
        if not pins:
            raise SchematicError(f"unknown {reference}.{number}")
        pin = pins[0]
        x, y = pin.x, -pin.y
        mirror = sx.atom(sx.child(node, "mirror"))
        if mirror == "x":
            y = -y
        elif mirror == "y":
            x = -x
        at = sx.child(node, "at")
        radians = math.radians(-sx.number(at, 3))
        return (round(sx.number(at) + x * math.cos(radians) - y * math.sin(radians), 6),
                round(sx.number(at, 2) + x * math.sin(radians) + y * math.cos(radians), 6))

    def wire(self, *points: tuple[float, float]) -> None:
        if len(points) < 2:
            raise SchematicError("a wire needs two points")
        for a, b in zip(points, points[1:]):
            if a == b or (a[0] != b[0] and a[1] != b[1]):
                raise SchematicError("wire segments must be nonzero and axis aligned")
            self.root.append(["wire", ["pts", ["xy", *map(str, a)], ["xy", *map(str, b)]],
                              ["stroke", ["width", "0"], ["type", "default"]], _uuid()])

    def connect(self, first: str, second: str, *, via: tuple[tuple[float, float], ...] = ()) -> None:
        a, ap = first.rsplit(".", 1)
        b, bp = second.rsplit(".", 1)
        self.wire(self.pin_position(a, ap), *via, self.pin_position(b, bp))

    def label(self, name: str, at: tuple[float, float], *, angle: float = 0, kind: str = "label") -> None:
        if kind not in {"label", "global_label", "hierarchical_label"}:
            raise SchematicError(f"unsupported label kind: {kind}")
        self.root.append([kind, sx.Quoted(name), *([["shape", "input"]] if kind != "label" else []), ["at", *map(str, at), str(angle)], _effects(), _uuid()])

    def no_connect(self, reference: str, number: str, *, unit: int = 1) -> None:
        self.root.append(["no_connect", ["at", *map(str, self.pin_position(reference, number, unit=unit))], _uuid()])

    def junction(self, at: tuple[float, float]) -> None:
        self.root.append(["junction", ["at", *map(str, at)], ["diameter", "0"], ["color", "0", "0", "0", "0"], _uuid()])

    def text_note(self, text: str, at: tuple[float, float], *, size: float = 1.27) -> None:
        self.root.append(["text", sx.Quoted(text), ["at", *map(str, at), "0"], _effects(size), _uuid()])

    def add_sheet(self, name: str, filename: str, at: tuple[float, float], size=(40, 25)) -> str:
        if Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise SchematicError("sheet filename must be project relative")
        uid = str(uuid.uuid4())
        self.root.append(["sheet", ["at", *map(str, at)], ["size", *map(str, size)],
                          ["stroke", ["width", "0"], ["type", "default"]],
                          ["fill", ["color", "0", "0", "0", "0"]], ["uuid", sx.Quoted(uid)],
                          ["property", sx.Quoted("Sheetname"), sx.Quoted(name), ["at", str(at[0]), str(at[1]-2), "0"], _effects()],
                          ["property", sx.Quoted("Sheetfile"), sx.Quoted(filename), ["at", str(at[0]), str(at[1]+size[1]+2), "0"], _effects()]])
        return uid

    def sheet_pin(self, sheet_uuid: str, name: str, at: tuple[float, float], *, direction="input", angle=180) -> None:
        """Add an explicit native sheet interface; connect its matching child label."""
        if direction not in {"input", "output", "bidirectional", "tri_state", "passive"} or angle not in {0, 90, 180, 270}:
            raise SchematicError("invalid sheet pin direction or angle")
        sheet = next((n for n in sx.children(self.root, "sheet") if sx.atom(sx.child(n, "uuid")) == sheet_uuid), None)
        if sheet is None or not name:
            raise SchematicError("sheet pin needs an existing sheet and a name")
        if any(sx.atom(p) == name for p in sx.children(sheet, "pin")):
            raise SchematicError("duplicate sheet pin name")
        origin, size = sx.child(sheet, "at"), sx.child(sheet, "size")
        x1, y1 = sx.number(origin), sx.number(origin, 2)
        x2, y2 = x1 + sx.number(size), y1 + sx.number(size, 2)
        x, y = at
        if not ((x in (x1, x2) and y1 <= y <= y2) or (y in (y1, y2) and x1 <= x <= x2)):
            raise SchematicError("sheet pin must lie on the sheet boundary")
        sheet.append(["pin", sx.Quoted(name), direction, ["at", str(x), str(y), str(angle)], _effects(), _uuid()])

    def bind_instance(self, project_name: str, sheet_path: str, references: dict[str, str]) -> None:
        """Bind a reusable sheet's symbols to one native hierarchical instance."""
        if not sheet_path.startswith("/") or not references:
            raise SchematicError("instance needs an absolute UUID path and reference mapping")
        for node in sx.children(self.root, "symbol"):
            source = properties(node).get("Reference", "")
            if source not in references:
                raise SchematicError(f"instance reference mapping omits {source}")
            instances = sx.child(node, "instances")
            if instances is None:
                instances = ["instances"]; node.append(instances)
            project = next((p for p in sx.children(instances, "project") if sx.atom(p) == project_name), None)
            if project is None:
                project = ["project", sx.Quoted(project_name)]; instances.append(project)
            if any(sx.atom(p) == sheet_path for p in sx.children(project, "path")):
                raise SchematicError("instance path already exists")
            project.append(["path", sx.Quoted(sheet_path), ["reference", sx.Quoted(references[source])],
                            ["unit", sx.atom(sx.child(node, "unit"), default="1")]])

    def remove_object(self, uid: str) -> None:
        matches = [n for n in self.root if isinstance(n, list) and sx.atom(sx.child(n, "uuid")) == uid]
        if len(matches) != 1:
            raise SchematicError(f"expected one object UUID: {uid}")
        self.root.remove(matches[0])

    def serialize(self) -> str:
        spans = record_spans(self.text)
        originals = [n for n in self.original_nodes if isinstance(n, list)]
        current = [n for n in self.root if isinstance(n, list)]
        # UUID identifies instances. Singleton document records use their tag.
        def key(n):
            return (sx.head(n), sx.atom(sx.child(n, "uuid")) or sx.head(n))
        indexed = {key(n): n for n in current}
        if len(indexed) != len(current):
            raise SchematicError("duplicate top-level object identities")
        pieces, cursor = [], 0
        for node, (start, end) in zip(originals, spans):
            pieces.append(self.text[cursor:start])
            updated = indexed.pop(key(node), None)
            if updated is not None:
                pieces.append(self.text[start:end] if updated == node else sx.dumps(updated))
            cursor = end
        tail = self.text[cursor:]
        close = tail.rfind(")")
        pieces.append(tail[:close])
        pieces.extend("\n  " + sx.dumps(n) for n in indexed.values())
        pieces.append(tail[close:])
        return "".join(pieces)

    def save(self, *, tool_root: Path | None = None, runner=None) -> Path | None:
        contents = self.serialize()
        if contents == self.original:
            return None
        expected = self.path.read_text() if self.path.exists() else None
        if expected != self.original:
            raise SchematicError("schematic changed since load; reload before saving")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Validate a complete staged project so no failed export can damage source.
        with tempfile.TemporaryDirectory(prefix="pcbforge-edit-") as tmp:
            stage = Path(tmp)
            source_snapshot = {}
            for file in self.path.parent.rglob("*"):
                if file.is_file() and file.suffix in {".kicad_sch", ".kicad_pro", ".kicad_sym"} and not any(p.endswith("backups") for p in file.parts):
                    target = stage / file.relative_to(self.path.parent)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = file.read_bytes()
                    source_snapshot[file] = data
                    target.write_bytes(data)
            target = stage / self.path.name
            target.write_text(contents)
            extract(target, tool_root=tool_root, **({"runner": runner} if runner else {}))
        if any(not file.is_file() or file.read_bytes() != data for file, data in source_snapshot.items()):
            raise SchematicError("project sources changed during validation; reload before saving")
        if (self.path.read_text() if self.path.exists() else None) != expected:
            raise SchematicError("schematic changed during validation; reload before saving")
        backup = None
        if self.original is not None:
            directory = self.path.parent / "schematic-backups"
            directory.mkdir(exist_ok=True)
            backup = directory / f"{self.path.stem}-{uuid.uuid4().hex}.kicad_sch"
            backup.write_text(self.original)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".schematic-")
        try:
            with os.fdopen(fd, "w") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.original = self.text = contents
        self.original_nodes = copy.deepcopy(self.root)
        return backup
