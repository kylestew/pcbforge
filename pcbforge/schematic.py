"""Saved KiCad schematics and KiCad's resolved circuit graph.

The netlist is an extraction, never another authored source. Connectivity is
resolved by KiCad, including hierarchical instances, buses and hidden pins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping

from pcbforge import sexpr as sx
from pcbforge.kicad_tools import command, SCHEMATIC_FORMAT


class SchematicError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read_root(path: Path) -> sx.Node:
    try:
        root = sx.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, sx.SExprError) as exc:
        raise SchematicError(f"cannot read {path}: {exc}") from exc
    if sx.head(root) != "kicad_sch":
        raise SchematicError(f"{path}: expected a KiCad schematic")
    if sx.atom(sx.child(root, "version")) != SCHEMATIC_FORMAT:
        raise SchematicError(f"{path}: save the schematic with KiCad 10.0.3")
    if not sx.atom(sx.child(root, "uuid")):
        raise SchematicError(f"{path}: missing sheet UUID")
    return root


def properties(node: sx.Node) -> dict[str, str]:
    return {sx.atom(p): sx.atom(p, 2) for p in sx.children(node, "property")}


def hierarchy(path: Path) -> tuple[tuple[Path, str, sx.Node], ...]:
    """All sheet instances, including repeated uses of the same sheet file."""
    path = path.resolve()
    base = path.parent
    result = []

    def visit(file: Path, instance: str, ancestors: tuple[Path, ...]) -> None:
        file = file.resolve()
        if not file.is_relative_to(base):
            raise SchematicError(f"sheet outside the self-contained project: {file}")
        if file in ancestors:
            raise SchematicError(f"cyclic sheet hierarchy: {file}")
        root = read_root(file)
        if not instance:
            instance = "/" + sx.atom(sx.child(root, "uuid"))
        result.append((file, instance, root))
        for sheet in sx.children(root, "sheet"):
            filename = properties(sheet).get("Sheetfile", "")
            uid = sx.atom(sx.child(sheet, "uuid"))
            if not filename or not uid:
                raise SchematicError(f"{file}: sheet has no filename or UUID")
            visit(file.parent / filename, instance + "/" + uid, ancestors + (file,))

    visit(path, "", ())
    return tuple(result)


def source_files(path: Path) -> tuple[Path, ...]:
    return tuple(sorted({p for p, _, _ in hierarchy(path)}))


@dataclass(frozen=True)
class CircuitPin:
    number: str
    name: str
    electrical: str
    net: str = ""
    no_connect: bool = False


@dataclass(frozen=True)
class CircuitComponent:
    reference: str
    identity: str
    symbol: str
    value: str
    footprint: str
    fields: Mapping[str, str]
    pins: tuple[CircuitPin, ...]
    dnp: bool = False
    exclude_bom: bool = False
    exclude_board: bool = False
    uuids: tuple[str, ...] = ()

    @property
    def mpn(self) -> str:
        return self.fields.get("MPN", "")

    @property
    def lcsc(self) -> str:
        return self.fields.get("LCSC", "")

    @property
    def fitted(self) -> bool:
        return not (self.dnp or self.exclude_bom or self.exclude_board)

    @property
    def purpose(self) -> str:
        return self.fields.get("pcbforge_purpose", "")


@dataclass(frozen=True)
class CircuitGraph:
    components: tuple[CircuitComponent, ...]
    nets: Mapping[str, tuple[str, ...]]
    root_uuid: str

    def component(self, reference: str) -> CircuitComponent:
        for component in self.components:
            if reference in (component.reference, component.identity):
                return component
        raise SchematicError(f"unknown component: {reference}")

    def pin(self, endpoint: str) -> CircuitPin:
        try:
            ref, number = endpoint.rsplit(".", 1)
        except ValueError as exc:
            raise SchematicError(f"expected REF.PIN: {endpoint}") from exc
        for pin in self.component(ref).pins:
            if pin.number == number:
                return pin
        raise SchematicError(f"unknown pin: {endpoint}")

    def connected(self, *endpoints: str) -> bool:
        if len(endpoints) < 2:
            raise SchematicError("a connection check needs at least two pins")
        pins = [self.pin(endpoint) for endpoint in endpoints]
        return bool(pins[0].net) and not any(p.no_connect for p in pins) and len({p.net for p in pins}) == 1

    def payload(self) -> dict[str, Any]:
        return {"schema": 1, "root_uuid": self.root_uuid,
                "components": [asdict(c) for c in sorted(self.components, key=lambda c: c.identity)],
                "nets": {k: sorted(v) for k, v in sorted(self.nets.items())}}

    @property
    def fingerprint(self) -> str:
        return digest(self.payload())

    def bom(self) -> tuple[dict[str, Any], ...]:
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for component in self.components:
            if component.fitted:
                key = (component.lcsc, component.mpn, component.footprint)
                grouped.setdefault(key, []).append(component.reference)
        return tuple({"lcsc": key[0], "mpn": key[1], "footprint": key[2],
                      "quantity": len(refs), "designators": sorted(refs)}
                     for key, refs in sorted(grouped.items()))


def from_payload(data: Mapping[str, Any]) -> CircuitGraph:
    if data.get("schema") != 1:
        raise SchematicError("unsupported extracted circuit graph")
    components = []
    for raw in data["components"]:
        item = dict(raw)
        item["pins"] = tuple(CircuitPin(**pin) for pin in item["pins"])
        item["uuids"] = tuple(item.get("uuids", ()))
        components.append(CircuitComponent(**item))
    return CircuitGraph(tuple(components), {k: tuple(v) for k, v in data["nets"].items()}, data["root_uuid"])


def parse_netlist(text: str, root_uuid: str) -> CircuitGraph:
    root = sx.parse(text)
    if sx.head(root) != "export" or sx.child(root, "components") is None:
        raise SchematicError("KiCad produced an incomplete netlist")
    libraries = {}
    for lib in sx.children(sx.child(root, "libparts") or [], "libpart"):
        key = sx.atom(sx.child(lib, "lib")) + ":" + sx.atom(sx.child(lib, "part"))
        libraries[key] = tuple(CircuitPin(sx.atom(sx.child(p, "num")), sx.atom(sx.child(p, "name")),
                                        sx.atom(sx.child(p, "type")))
                               for p in sx.children(sx.child(lib, "pins") or [], "pin"))
    nets: dict[str, tuple[str, ...]] = {}
    connected: dict[str, tuple[str, bool]] = {}
    for net in sx.children(sx.child(root, "nets") or [], "net"):
        name = sx.atom(sx.child(net, "name"))
        if not name or name in nets:
            raise SchematicError("net names must be nonempty and unique")
        nodes = []
        for node in sx.children(net, "node"):
            endpoint = sx.atom(sx.child(node, "ref")) + "." + sx.atom(sx.child(node, "pin"))
            if endpoint in connected:
                raise SchematicError(f"pin appears in multiple nets: {endpoint}")
            nodes.append(endpoint)
            connected[endpoint] = (name, "no_connect" in sx.atom(sx.child(node, "pintype")))
        nets[name] = tuple(sorted(nodes))
    components = []
    refs: set[str] = set()
    identities: set[str] = set()
    for raw in sx.children(sx.child(root, "components") or [], "comp"):
        ref = sx.atom(sx.child(raw, "ref"))
        props = {sx.atom(sx.child(p, "name")): sx.atom(sx.child(p, "value")) for p in sx.children(raw, "property")}
        fields = {sx.atom(sx.child(p, "name")): (sx.atoms(p)[0] if sx.atoms(p) else "")
                  for p in sx.children(sx.child(raw, "fields") or [], "field")}
        fields.update({k: v for k, v in props.items() if k not in {"Sheetname", "Sheetfile"} and not k.startswith("ki_")})
        fields.setdefault("Datasheet", sx.atom(sx.child(raw, "datasheet")))
        lib = sx.child(raw, "libsource") or []
        symbol = sx.atom(sx.child(lib, "lib")) + ":" + sx.atom(sx.child(lib, "part"))
        stamps = tuple(str(x) for x in sx.atoms(sx.child(raw, "tstamps") or []))
        sheet = sx.atom(sx.child(sx.child(raw, "sheetpath") or [], "tstamps"), default="/")
        identity = "/" + root_uuid + "/" + sheet.strip("/") + "/" + (stamps[0] if stamps else "")
        identity = identity.replace("//", "/")
        if not ref or ref in refs or not stamps or identity in identities:
            raise SchematicError(f"duplicate or missing reference/identity: {ref}")
        refs.add(ref)
        identities.add(identity)
        if symbol not in libraries:
            raise SchematicError(f"{ref}: netlist has no pin definition for {symbol}")
        pins = []
        for pin in libraries[symbol]:
            net, nc = connected.get(ref + "." + pin.number, ("", pin.electrical == "no_connect"))
            pins.append(CircuitPin(pin.number, pin.name, pin.electrical, net, nc))
        components.append(CircuitComponent(
            ref, identity, symbol, sx.atom(sx.child(raw, "value")), sx.atom(sx.child(raw, "footprint")),
            fields, tuple(pins), "dnp" in props, "exclude_from_bom" in props, "exclude_from_board" in props, stamps))
    graph = CircuitGraph(tuple(components), nets, root_uuid)
    for endpoint in connected:
        graph.pin(endpoint)
    return graph


def run_kicad(args: list[str], *, tool_root: Path | None = None, runner=subprocess.run) -> None:
    result = runner([command(tool_root), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise SchematicError(f"KiCad {' '.join(args[:3])} failed ({result.returncode}): {(result.stderr or result.stdout)[-1800:]}")


def extract(path: Path, *, tool_root: Path | None = None, runner=subprocess.run) -> CircuitGraph:
    path = path.resolve()
    sheets = hierarchy(path)
    sources = {*source_files(path), *path.parent.glob("*.kicad_pro")}
    before = {p: p.read_bytes() for p in sources}
    root_uuid = sx.atom(sx.child(sheets[0][2], "uuid"))
    excluded = set()
    for file, instance, root in sheets:
        for symbol in sx.children(root, "symbol"):
            if sx.atom(sx.child(symbol, "on_board")) == "no":
                excluded.add(instance + "/" + sx.atom(sx.child(symbol, "uuid")))
    with tempfile.TemporaryDirectory(prefix="pcbforge-netlist-") as temp:
        staging = Path(temp)
        export_path = path
        if excluded:
            # The PCB exporter drops off-board symbols. Include them in a
            # temporary extraction only; their saved flags are restored below.
            # KiCad still resolves every connection, including sheet instances.
            for file, raw in before.items():
                target = staging / file.relative_to(path.parent)
                target.parent.mkdir(parents=True, exist_ok=True)
                if file.suffix == ".kicad_sch":
                    root = sx.parse(raw.decode())
                    for symbol in sx.children(root, "symbol"):
                        flag = sx.child(symbol, "on_board")
                        if flag is not None and sx.atom(flag) == "no":
                            flag[1] = "yes"
                    target.write_text(sx.dumps(root))
                else:
                    target.write_bytes(raw)
            export_path = staging / path.name
        output = staging / "circuit.net"
        run_kicad(["sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(output), str(export_path)],
                  tool_root=tool_root, runner=runner)
        graph = parse_netlist(output.read_text(), root_uuid)
        graph = replace(graph, components=tuple(replace(c, exclude_board=c.identity in excluded) for c in graph.components))
    if any(p.read_bytes() != data for p, data in before.items()):
        raise SchematicError("schematic changed during extraction; retry the check")
    return graph


def semantic_diff(before: CircuitGraph, after: CircuitGraph) -> tuple[str, ...]:
    old = {c.identity: c for c in before.components}
    new = {c.identity: c for c in after.components}
    changes = [f"Removed {old[k].reference}" for k in sorted(old.keys() - new.keys())]
    changes += [f"Added {new[k].reference}" for k in sorted(new.keys() - old.keys())]
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        for attr in ("reference", "value", "footprint", "symbol", "fields", "dnp", "exclude_bom", "exclude_board"):
            if getattr(a, attr) != getattr(b, attr):
                changes.append(f"{b.reference}: {attr} changed from {getattr(a, attr)} to {getattr(b, attr)}")
        ap = {p.number: p for p in a.pins}
        bp = {p.number: p for p in b.pins}
        for pin in sorted(ap.keys() | bp.keys()):
            if ap.get(pin) != bp.get(pin):
                changes.append(f"{b.reference}.{pin}: {ap.get(pin)} → {bp.get(pin)}")
    return tuple(changes)


def empty_schematic(name: str, *, uid: str | None = None) -> str:
    import uuid
    uid = uid or str(uuid.uuid4())
    return f'''(kicad_sch (version {SCHEMATIC_FORMAT}) (generator "pcbforge")
  (uuid "{uid}") (paper "A4")
  (title_block (title {sx.quote(name)}))
  (lib_symbols)
  (sheet_instances (path "/" (page "1")))
)\n'''


def project_schematic(project_dir: Path) -> Path:
    from pcbforge.initialize import read_spec
    return project_dir / (read_spec(project_dir / "spec.md").name + ".kicad_sch")
