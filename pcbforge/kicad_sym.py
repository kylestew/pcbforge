"""KiCad symbol resolution for the generated review schematic.

Symbols come from the pinned KiCad 9 bundle's stock libraries, are flattened
(`extends` resolved) and renamed to `Lib:Name` so they can be embedded in a
`.kicad_sch` `lib_symbols` block. Parts whose stock symbol does not match the
compiled footprint pads get a generated box symbol instead.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from pcbforge import sexpr
from pcbforge.sexpr import Node, Quoted

GRID = 1.27
GENERIC_LIB = "pcbforge"
POWER_LIB = "pcbforge_power"
_SHIM_RE = re.compile(r'^KICAD9_CLI="(?P<path>[^"]+)"', re.MULTILINE)
_KICAD10_ONLY = {
    "duplicate_pin_numbers_are_jumpers",
    "in_pos_files",
    "show_name",
    "do_not_autoplace",
}


class SymbolError(RuntimeError):
    """A symbol could not be resolved or generated."""


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    electrical: str
    x: float
    y: float
    rotation: float
    length: float
    unit: int
    hidden: bool = False


@dataclass(frozen=True)
class LibSymbol:
    lib_id: str
    node: Node
    pins: tuple[Pin, ...]
    units: int
    power: bool = False

    @property
    def pin_numbers(self) -> frozenset[str]:
        return frozenset(pin.number for pin in self.pins)

    def pin(self, number: str, unit: int = 1) -> Pin:
        for pin in self.pins:
            if pin.number == number and pin.unit in (0, unit):
                return pin
        raise SymbolError(f"{self.lib_id} has no pin {number!r} in unit {unit}")

    def bbox(self, unit: int = 1) -> tuple[float, float, float, float]:
        """Graphics extent in library coordinates (y up), pins included."""
        xs: list[float] = []
        ys: list[float] = []
        for sub in sexpr.children(self.node, "symbol"):
            sub_unit = _unit_of(sexpr.atom(sub))
            if sub_unit not in (0, unit):
                continue
            for item in sub:
                if not isinstance(item, list):
                    continue
                tag = sexpr.head(item)
                if tag == "rectangle":
                    for key in ("start", "end"):
                        point = sexpr.child(item, key)
                        xs.append(sexpr.number(point, 1))
                        ys.append(sexpr.number(point, 2))
                elif tag in ("polyline", "bezier"):
                    pts = sexpr.child(item, "pts")
                    for xy in sexpr.children(pts or [], "xy"):
                        xs.append(sexpr.number(xy, 1))
                        ys.append(sexpr.number(xy, 2))
                elif tag == "circle":
                    center = sexpr.child(item, "center")
                    radius = sexpr.number(sexpr.child(item, "radius"), 1)
                    cx, cy = sexpr.number(center, 1), sexpr.number(center, 2)
                    xs += [cx - radius, cx + radius]
                    ys += [cy - radius, cy + radius]
                elif tag == "arc":
                    for key in ("start", "mid", "end"):
                        point = sexpr.child(item, key)
                        xs.append(sexpr.number(point, 1))
                        ys.append(sexpr.number(point, 2))
        for pin in self.pins:
            if pin.unit not in (0, unit):
                continue
            xs.append(pin.x)
            ys.append(pin.y)
            angle = math.radians(pin.rotation)
            xs.append(pin.x + math.cos(angle) * pin.length)
            ys.append(pin.y + math.sin(angle) * pin.length)
        if not xs:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class SymbolChoice:
    """One resolved symbol and the search that led to it.

    ``candidates`` lists every official id tried in order; ``rejected``
    pairs each rejected id with why its pins do not fit the pads.
    """

    symbol: LibSymbol
    reason: str
    generic: bool
    candidates: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    pads_source: str = ""


# ----------------------------------------------------------------------
# library location and parsing
# ----------------------------------------------------------------------


def symbols_dir(tool_root: Path) -> Path:
    """Locate the pinned KiCad 9 stock symbol directory via the CLI shim."""
    shim = Path(tool_root) / "scripts" / "kicad-cli"
    try:
        text = shim.read_text(encoding="utf-8")
    except OSError as exc:
        raise SymbolError(f"cannot read {shim}: {exc}") from exc
    match = _SHIM_RE.search(text)
    if match is None:
        raise SymbolError(f"{shim} does not pin KICAD9_CLI")
    cli = Path(match.group("path"))
    # .../KiCad.app/Contents/MacOS/kicad-cli -> .../Contents/SharedSupport/symbols
    candidate = cli.parents[1] / "SharedSupport" / "symbols"
    if not candidate.is_dir():
        raise SymbolError(f"KiCad 9 symbol library directory not found: {candidate}")
    return candidate


@lru_cache(maxsize=None)
def _library(path: str) -> Mapping[str, Node]:
    try:
        root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, sexpr.SExprError) as exc:
        raise SymbolError(f"cannot read symbol library {path}: {exc}") from exc
    return {sexpr.atom(node): node for node in sexpr.children(root, "symbol")}


def library_names(lib: str, directory: Path) -> tuple[str, ...]:
    path = Path(directory) / f"{lib}.kicad_sym"
    if not path.is_file():
        raise SymbolError(f"unknown symbol library {lib!r}")
    return tuple(_library(str(path)))


def _unit_of(sub_name: str) -> int:
    match = re.search(r"_(\d+)_(\d+)$", sub_name)
    return int(match.group(1)) if match else 0


def _pins(node: Node, name: str) -> tuple[Pin, ...]:
    pins: list[Pin] = []
    for sub in sexpr.children(node, "symbol"):
        unit = _unit_of(sexpr.atom(sub))
        for pin in sexpr.children(sub, "pin"):
            at = sexpr.child(pin, "at")
            hidden = any(
                isinstance(item, str) and item == "hide" for item in pin
            ) or sexpr.atom(sexpr.child(pin, "hide")) == "yes"
            pins.append(
                Pin(
                    number=sexpr.atom(sexpr.child(pin, "number")),
                    name=sexpr.atom(sexpr.child(pin, "name")),
                    electrical=sexpr.atom(pin, 1),
                    x=sexpr.number(at, 1),
                    y=sexpr.number(at, 2),
                    rotation=sexpr.number(at, 3),
                    length=sexpr.number(sexpr.child(pin, "length"), 1),
                    unit=unit,
                    hidden=hidden,
                )
            )
    return tuple(pins)


def _units(node: Node) -> int:
    return max((_unit_of(sexpr.atom(sub)) for sub in sexpr.children(node, "symbol")), default=1) or 1


def _rename(node: Node, old: str, new: str, lib_id: str) -> Node:
    node = copy.deepcopy(node)
    node[1] = Quoted(lib_id)
    for sub in sexpr.children(node, "symbol"):
        sub_name = sexpr.atom(sub)
        if sub_name.startswith(old + "_"):
            sub[1] = Quoted(new + sub_name[len(old):])
    return node


def _flatten(name: str, library: Mapping[str, Node]) -> Node:
    node = library[name]
    extends = sexpr.child(node, "extends")
    if extends is None:
        return copy.deepcopy(node)
    parent_name = sexpr.atom(extends)
    if parent_name not in library:
        raise SymbolError(f"{name} extends unknown symbol {parent_name!r}")
    parent = _flatten(parent_name, library)
    child_props = {sexpr.atom(p): p for p in sexpr.children(node, "property")}
    merged: Node = []
    for item in parent:
        if isinstance(item, list) and sexpr.head(item) == "property":
            merged.append(copy.deepcopy(child_props.pop(sexpr.atom(item), item)))
        elif isinstance(item, list) and sexpr.head(item) == "symbol":
            sub = copy.deepcopy(item)
            sub_name = sexpr.atom(sub)
            sub[1] = Quoted(name + sub_name[len(parent_name):])
            merged.append(sub)
        else:
            merged.append(copy.deepcopy(item))
    merged[1] = Quoted(name)
    for prop in child_props.values():
        merged.append(copy.deepcopy(prop))
    return merged


def load_stock(lib: str, name: str, directory: Path) -> LibSymbol:
    """Load one stock symbol, flattened and renamed to ``lib:name``."""
    path = Path(directory) / f"{lib}.kicad_sym"
    if not path.is_file():
        raise SymbolError(f"unknown symbol library {lib!r}")
    library = _library(str(path))
    if name not in library:
        raise SymbolError(f"{lib}.kicad_sym has no symbol {name!r}")
    flat = _flatten(name, library)
    for node in sexpr.walk(flat):
        if sexpr.head(node) in _KICAD10_ONLY:
            raise SymbolError(
                f"{lib}:{name} uses KiCad 10 syntax ({sexpr.head(node)}); "
                "the pinned KiCad 9 libraries are required"
            )
    lib_id = f"{lib}:{name}"
    flat[1] = Quoted(lib_id)
    return LibSymbol(
        lib_id=lib_id,
        node=flat,
        pins=_pins(flat, name),
        units=_units(flat),
        power=sexpr.child(flat, "power") is not None,
    )


def lib_symbol(lib_id: str, directory: Path) -> LibSymbol:
    if ":" not in lib_id:
        raise SymbolError(f"symbol id must be Lib:Name, got {lib_id!r}")
    lib, name = lib_id.split(":", 1)
    return load_stock(lib, name, directory)


# ----------------------------------------------------------------------
# generated symbols
# ----------------------------------------------------------------------


def _effects(size: float = 1.27, hide: bool = False, justify: str = "") -> Node:
    node: Node = ["effects", ["font", ["size", f"{size:g}", f"{size:g}"]]]
    if justify:
        node.append(["justify", *justify.split()])
    if hide:
        node.append(["hide", "yes"])
    return node


def _property(key: str, value: str, x: float, y: float, hide: bool = False, justify: str = "") -> Node:
    return [
        "property",
        Quoted(key),
        Quoted(value),
        ["at", _fmt(x), _fmt(y), "0"],
        _effects(hide=hide, justify=justify),
    ]


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _pin_node(number: str, name: str, electrical: str, x: float, y: float, rotation: float, length: float) -> Node:
    return [
        "pin",
        electrical,
        "line",
        ["at", _fmt(x), _fmt(y), _fmt(rotation)],
        ["length", _fmt(length)],
        ["name", Quoted(name or "~"), _effects()],
        ["number", Quoted(number), _effects()],
    ]


def _symbol_header(lib_id: str, reference_prefix: str, value: str, power: bool = False) -> Node:
    node: Node = ["symbol", Quoted(lib_id)]
    if power:
        node.append(["power"])
        node.append(["pin_numbers", ["hide", "yes"]])
        node.append(["pin_names", ["offset", "0"], ["hide", "yes"]])
    else:
        node.append(["pin_names", ["offset", "1.016"]])
    node += [
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
    ]
    return node


_POWER_NAME_RE = re.compile(r"^(GND|VSS|AGND|DGND|PGND|V[A-Z0-9+-]*|\+?\d+V\d*|VCC|VDD|VBAT|VBUS|AVDD|AVCC)$", re.I)


def generic_symbol(
    name: str,
    pins: Sequence[tuple[str, str]],
    *,
    reference_prefix: str = "U",
    sides: Mapping[str, str] | None = None,
    pitch: float = 2.54,
) -> LibSymbol:
    """Generate a rectangular box symbol with numbered pins on a 2.54 mm pitch.

    ``pins`` is a sequence of ``(number, name)``. ``sides`` may force a pin
    number to ``left``/``right``/``top``/``bottom`` and its insertion order is
    the order along that side (top to bottom, left to right); otherwise
    supply-like names go top (positive) / bottom (ground) and the rest
    alternate left/right in pin-number order.
    """
    if not pins:
        raise SymbolError(f"generic symbol {name!r} needs at least one pin")
    sides = dict(sides or {})
    buckets: dict[str, list[tuple[str, str]]] = {"left": [], "right": [], "top": [], "bottom": []}
    rest: list[tuple[str, str]] = []
    # the caller's ``sides`` order is the pin order along each side
    by_number = {number: pin_name for number, pin_name in pins}
    ordered = [(n, by_number[n]) for n in sides if n in by_number]
    ordered += [(n, name) for n, name in pins if n not in sides]
    for number, pin_name in ordered:
        side = sides.get(number)
        if side is None and _POWER_NAME_RE.match(pin_name or ""):
            side = "bottom" if re.match(r"^(GND|VSS|[ADP]GND)$", pin_name, re.I) else "top"
        if side:
            buckets[side].append((number, pin_name))
        else:
            rest.append((number, pin_name))
    half = (len(rest) + 1) // 2
    buckets["left"] += rest[:half]
    buckets["right"] += rest[half:]
    pitch = round(round(pitch / 1.27) * 1.27, 4) or 2.54
    rows = max(len(buckets["left"]), len(buckets["right"]), 1)
    cols = max(len(buckets["top"]), len(buckets["bottom"]), 1)
    longest = max((len(pin_name) for _, pin_name in pins), default=1)
    half_w = max(cols * pitch / 2 + 1.27, round((longest * 1.3 + 2.54) / 1.27) * 1.27 / 2 + 2.54)
    half_w = math.ceil(half_w / 1.27) * 1.27
    half_h = math.ceil((rows * pitch / 2 + 1.27) / 1.27) * 1.27
    length = 2.54
    lib_id = f"{GENERIC_LIB}:{name}"
    node = _symbol_header(lib_id, reference_prefix, name)
    node += [
        _property("Reference", reference_prefix, -half_w, half_h + 1.27, justify="left"),
        _property("Value", name, -half_w, -half_h - 1.27, justify="left"),
        _property("Footprint", "", 0, 0, hide=True),
        _property("Datasheet", "", 0, 0, hide=True),
        _property("Description", f"PCBForge generated box symbol for {name}", 0, 0, hide=True),
    ]
    graphics: Node = [
        "symbol",
        Quoted(f"{name}_0_1"),
        [
            "rectangle",
            ["start", _fmt(-half_w), _fmt(half_h)],
            ["end", _fmt(half_w), _fmt(-half_h)],
            ["stroke", ["width", "0.254"], ["type", "default"]],
            ["fill", ["type", "background"]],
        ],
    ]
    unit_node: Node = ["symbol", Quoted(f"{name}_1_1")]
    pin_list: list[Pin] = []

    def add(number: str, pin_name: str, x: float, y: float, rotation: float) -> None:
        unit_node.append(_pin_node(number, pin_name, "passive", x, y, rotation, length))
        pin_list.append(Pin(number, pin_name, "passive", x, y, rotation, length, 1))

    def spread(count: int, step: float = pitch) -> list[float]:
        start = (count - 1) * step / 2
        return [round((start - index * step) / 1.27) * 1.27 for index in range(count)]

    for (number, pin_name), y in zip(buckets["left"], spread(len(buckets["left"]))):
        add(number, pin_name, -half_w - length, y, 0)
    for (number, pin_name), y in zip(buckets["right"], spread(len(buckets["right"]))):
        add(number, pin_name, half_w + length, y, 180)
    for (number, pin_name), x in zip(buckets["top"], [-v for v in spread(len(buckets["top"]))]):
        add(number, pin_name, x, half_h + length, 270)
    for (number, pin_name), x in zip(buckets["bottom"], [-v for v in spread(len(buckets["bottom"]))]):
        add(number, pin_name, x, -half_h - length, 90)
    node += [graphics, unit_node, ["embedded_fonts", "no"]]
    return LibSymbol(lib_id=lib_id, node=node, pins=tuple(pin_list), units=1)


def power_symbol(net_name: str, style: str) -> LibSymbol:
    """Generate a power symbol whose value (and therefore net) is ``net_name``.

    ``style`` is ``rail`` (arrow up), ``ground`` (ground bar down) or ``flag``
    (PWR_FLAG, a power output that satisfies ERC on externally driven nets).
    """
    safe = re.sub(r"[^A-Za-z0-9_+\-.]", "_", net_name)
    if style == "flag":
        lib_id = f"{POWER_LIB}:PWR_FLAG"
        node = _symbol_header(lib_id, "#FLG", "PWR_FLAG", power=True)
        node += [
            _property("Reference", "#FLG", 0, 1.905, hide=True),
            _property("Value", "PWR_FLAG", 0, 3.81),
            _property("Footprint", "", 0, 0, hide=True),
            _property("Datasheet", "~", 0, 0, hide=True),
            _property("Description", "Power flag: tells ERC where power comes from", 0, 0, hide=True),
            ["symbol", Quoted("PWR_FLAG_0_0"), _pin_node("1", "~", "power_out", 0, 0, 90, 0)],
            [
                "symbol",
                Quoted("PWR_FLAG_0_1"),
                _polyline([(0, 0), (0, 1.27), (-1.016, 1.905), (0, 2.54), (1.016, 1.905), (0, 1.27)]),
            ],
            ["embedded_fonts", "no"],
        ]
        pins = (Pin("1", "~", "power_out", 0, 0, 90, 0, 0),)
        return LibSymbol(lib_id=lib_id, node=node, pins=pins, units=1, power=True)
    lib_id = f"{POWER_LIB}:{safe}"
    node = _symbol_header(lib_id, "#PWR", net_name, power=True)
    if style == "ground":
        node += [
            _property("Reference", "#PWR", 0, -6.35, hide=True),
            _property("Value", net_name, 0, -3.81),
            _property("Footprint", "", 0, 0, hide=True),
            _property("Datasheet", "", 0, 0, hide=True),
            _property("Description", f"Power symbol creates a global label with name {net_name}", 0, 0, hide=True),
            [
                "symbol",
                Quoted(f"{safe}_0_1"),
                _polyline([(0, 0), (0, -1.27), (1.27, -1.27), (0, -2.54), (-1.27, -1.27), (0, -1.27)]),
            ],
            ["symbol", Quoted(f"{safe}_1_1"), _pin_node("1", net_name, "power_in", 0, 0, 270, 0)],
            ["embedded_fonts", "no"],
        ]
        pins = (Pin("1", net_name, "power_in", 0, 0, 270, 0, 1),)
    elif style == "rail":
        node += [
            _property("Reference", "#PWR", 0, -3.81, hide=True),
            _property("Value", net_name, 0, 3.556),
            _property("Footprint", "", 0, 0, hide=True),
            _property("Datasheet", "", 0, 0, hide=True),
            _property("Description", f"Power symbol creates a global label with name {net_name}", 0, 0, hide=True),
            [
                "symbol",
                Quoted(f"{safe}_0_1"),
                _polyline([(-0.762, 1.27), (0, 2.54)]),
                _polyline([(0, 2.54), (0.762, 1.27)]),
                _polyline([(0, 0), (0, 2.54)]),
            ],
            ["symbol", Quoted(f"{safe}_1_1"), _pin_node("1", net_name, "power_in", 0, 0, 90, 0)],
            ["embedded_fonts", "no"],
        ]
        pins = (Pin("1", net_name, "power_in", 0, 0, 90, 0, 1),)
    else:
        raise SymbolError(f"unknown power symbol style {style!r}")
    return LibSymbol(lib_id=lib_id, node=node, pins=pins, units=1, power=True)


def _polyline(points: Iterable[tuple[float, float]]) -> Node:
    return [
        "polyline",
        ["pts", *(["xy", _fmt(x), _fmt(y)] for x, y in points)],
        ["stroke", ["width", "0"], ["type", "default"]],
        ["fill", ["type", "none"]],
    ]


# ----------------------------------------------------------------------
# symbol policy
# ----------------------------------------------------------------------

_KIND_SYMBOLS: Mapping[str, tuple[str, ...]] = {
    "resistor": ("Device:R",),
    "capacitor": ("Device:C", "Device:C_Polarized"),
    "inductor": ("Device:L",),
    "led": ("Device:LED",),
    "diode": ("Device:D", "Device:D_Schottky", "Device:D_Zener", "Device:D_TVS"),
    "crystal": ("Device:Crystal", "Device:Crystal_GND24"),
    "fuse": ("Device:Fuse", "Device:Polyfuse"),
    "battery": ("Device:Battery_Cell", "Device:Battery"),
    "switch": (
        "Switch:SW_Push",
        "Switch:SW_SPST",
        "Switch:SW_DIP_x01",
        "Device:RotaryEncoder",
        "Device:RotaryEncoder_Switch",
        "Device:RotaryEncoder_Switch_MP",
    ),
    "test-point": ("Connector:TestPoint",),
    "mechanical": (
        "Mechanical:MountingHole",
        "Mechanical:MountingHole_Pad",
        "Jumper:SolderJumper_2_Open",
    ),
    "mosfet": ("Device:Q_NMOS_GSD", "Device:Q_PMOS_GSD"),
    "transistor": ("Device:Q_NPN_BEC", "Device:Q_PNP_BEC"),
}
# Official libraries searched by MPN, as glob patterns over the pinned
# directory so every family present is covered without listing each one.
_MPN_LIB_PATTERNS = (
    "MCU_*",
    "Regulator_*",
    "Transistor_*",
    "Diode",
    "LED",
    "Sensor_*",
    "Interface_*",
    "Driver_*",
    "Power_*",
    "Memory_*",
    "Amplifier_*",
    "Comparator",
    "Reference_Voltage",
    "Timer*",
    "Logic_*",
    "Isolator*",
    "Converter_*",
    "Battery_Management",
    "RF_*",
    "Display_*",
    "Audio",
    "Relay*",
    "Connector",
    "Connector_Generic",
    "Switch",
    "Crystal",
    "Oscillator",
    "Jumper",
)
# Footprint library -> (symbol library, symbol-name pattern): the official
# footprint family says where the matching official symbols live.
_FOOTPRINT_HINTS: tuple[tuple[str, str, str], ...] = (
    ("Rotary_Encoder", "Device", r"^RotaryEncoder"),
    ("Connector_USB", "Connector", r"^USB_"),
    ("Connector_JST", "Connector_Generic", r"^Conn_0[12]x"),
    ("Connector_Molex", "Connector_Generic", r"^Conn_0[12]x"),
    ("Connector_PinHeader", "Connector_Generic", r"^Conn_0[12]x"),
    ("Connector_PinSocket", "Connector_Generic", r"^Conn_0[12]x"),
    ("Connector_Audio", "Connector", r"^AudioJack"),
    ("Connector_BarrelJack", "Connector", r"^Barrel_Jack"),
    ("Connector_Card", "Connector", r"^(SD|microSD|Micro_SD)"),
    ("Button_Switch", "Switch", r"^SW_"),
    ("Crystal", "Device", r"^Crystal"),
    ("Oscillator", "Oscillator", r"."),
    ("Fuse", "Device", r"^(Fuse|Polyfuse)"),
    ("Inductor", "Device", r"^L$"),
    ("LED_", "Device", r"^LED$"),
    ("Diode_", "Device", r"^D(_Schottky|_Zener|_TVS)?$"),
    ("TestPoint", "Connector", r"^TestPoint$"),
    ("MountingHole", "Mechanical", r"^MountingHole"),
    ("Battery", "Device", r"^Battery"),
    ("Jumper", "Jumper", r"^SolderJumper"),
)


def _canonical(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def _mpn_libraries(directory: Path) -> list[Path]:
    paths = sorted(Path(directory).glob("*.kicad_sym"))
    return [p for p in paths if any(fnmatch(p.stem, pattern) for pattern in _MPN_LIB_PATTERNS)]


@lru_cache(maxsize=None)
def _library_text_upper(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").upper()
    except OSError:
        return ""


def _mpn_candidates(mpn: str, directory: Path) -> list[str]:
    """Official symbols named for the MPN, exact first, then KiCad's ``x`` wildcards.

    ``STM32G0B1KBT6`` finds ``MCU_ST_STM32G0:STM32G0B1KBTx``; the exact part
    number stays the model value, the wildcard name is only the drawing.
    """
    token = _canonical(mpn)
    if len(token) < 4 or token == "NA":
        return []
    exact: list[str] = []
    wildcard: list[str] = []
    prefix: list[str] = []
    probe = token[:6]
    for path in _mpn_libraries(directory):
        # cheap pre-filter: a library that never mentions the first characters
        # of the part number cannot name it
        if probe not in _library_text_upper(str(path)):
            continue
        lib = path.stem
        for name in _library(str(path)):
            canonical = _canonical(name)
            if len(canonical) < 4:
                continue
            if canonical == token:
                exact.append(f"{lib}:{name}")
                continue
            if "X" in canonical:
                pattern = "^" + re.escape(canonical).replace("X", "[A-Z0-9]") + "$"
                if re.match(pattern, token):
                    wildcard.append(f"{lib}:{name}")
                    continue
            if token.startswith(canonical):
                prefix.append(f"{lib}:{name}")
    return exact + wildcard + prefix


def _footprint_candidates(footprint: str, directory: Path) -> list[str]:
    """Official symbols suggested by the footprint's library family."""
    if ":" not in footprint:
        return []
    fp_lib = footprint.split(":", 1)[0]
    found: list[str] = []
    for fp_prefix, sym_lib, pattern in _FOOTPRINT_HINTS:
        if not fp_lib.startswith(fp_prefix):
            continue
        path = Path(directory) / f"{sym_lib}.kicad_sym"
        if not path.is_file():
            continue
        for name in sorted(_library(str(path))):
            if re.search(pattern, name):
                found.append(f"{sym_lib}:{name}")
    return found


def connector_symbol(pad_count: int, rows: int = 1) -> str:
    if rows == 2:
        return f"Connector_Generic:Conn_02x{pad_count // 2:02d}_Odd_Even"
    return f"Connector_Generic:Conn_01x{pad_count:02d}"


def official_candidates(
    *,
    kind: str,
    mpn: str,
    footprint: str,
    pad_count: int,
    directory: Path,
) -> list[str]:
    """Ordered official symbol ids worth trying: MPN, kind, footprint family, generic connectors."""
    ordered: list[str] = []
    ordered += _mpn_candidates(mpn, directory)
    if kind != "connector":
        ordered += _KIND_SYMBOLS.get(kind, ())
    ordered += _footprint_candidates(footprint, directory)
    if kind == "connector" and pad_count:
        ordered.append(connector_symbol(pad_count))
        if pad_count % 2 == 0:
            ordered.append(connector_symbol(pad_count, rows=2))
    seen: set[str] = set()
    unique: list[str] = []
    for lib_id in ordered:
        if lib_id not in seen:
            seen.add(lib_id)
            unique.append(lib_id)
    return unique


def choose_symbol(
    *,
    kind: str,
    mpn: str,
    value: str,
    reference: str,
    model_pins: Iterable[str],
    board_pads: Iterable[str],
    directory: Path,
    override: str | None = None,
    pin_names: Mapping[str, str] | None = None,
    pin_sides: Mapping[str, str] | None = None,
    pin_pitch: float = 2.54,
    footprint: str = "",
    pads_source: str = "",
) -> SymbolChoice:
    """Official symbol first: the one whose pin numbers equal every footprint pad.

    ``board_pads`` should be the complete physical pad list (board, or the
    footprint file before a board exists); unused pins count. ``override``
    may be ``"Lib:Name"`` (must still match the pads) or ``"generic"``, which
    is refused while an official symbol fits — a box is a fallback, not a
    preference. The reason and the rejected list record the search.
    """
    model_pins = set(model_pins)
    pads = {pad for pad in board_pads if pad}
    source = pads_source or ("pads" if pads else "model pins")
    if not pads:
        pads = set(model_pins)
    names = dict(pin_names or {})
    pads_text = ",".join(sorted(pads, key=_pad_sort_key)) or "none"

    def generic(reason: str, candidates: Sequence[str] = (), rejected: Sequence[tuple[str, str]] = ()) -> SymbolChoice:
        numbers = sorted(pads | model_pins, key=_pad_sort_key)
        prefix_match = re.match(r"^[A-Z]+", reference)
        symbol = generic_symbol(
            _generic_name(mpn, value, reference),
            [(number, names.get(number, "")) for number in numbers],
            reference_prefix=prefix_match.group(0) if prefix_match else "U",
            sides=pin_sides,
            pitch=pin_pitch,
        )
        return SymbolChoice(symbol, reason, True, tuple(candidates), tuple(rejected), source)

    candidates = official_candidates(
        kind=kind, mpn=mpn, footprint=footprint, pad_count=len(pads), directory=directory
    )
    rejected: list[tuple[str, str]] = []
    official: tuple[str, LibSymbol] | None = None
    for lib_id in candidates:
        try:
            symbol = lib_symbol(lib_id, directory)
        except SymbolError as exc:
            rejected.append((lib_id, f"not loadable: {exc}"))
            continue
        mismatch = _mismatch(symbol, pads, model_pins)
        if mismatch is None:
            official = (lib_id, symbol)
            break
        rejected.append((lib_id, mismatch))

    if override == "generic":
        if official is not None:
            raise SymbolError(
                f"{reference}: generic box refused — official symbol {official[0]} matches all "
                f"{len(pads)} pads ({source}); place it (symbol={official[0]!r}) or drop the override"
            )
        return generic(
            f"generic box requested; no official symbol matches pads {pads_text} ({source})"
            + _rejected_text(rejected),
            candidates,
            rejected,
        )
    if override:
        symbol = lib_symbol(override, directory)
        mismatch = _mismatch(symbol, pads, model_pins)
        if mismatch is not None:
            raise SymbolError(
                f"{reference}: {override} pins {sorted(symbol.pin_numbers, key=_pad_sort_key)} "
                f"do not match footprint pads {sorted(pads, key=_pad_sort_key)} ({source}): {mismatch}"
            )
        return SymbolChoice(
            symbol,
            f"{override} requested; pins match all {len(pads)} pads ({source})",
            False,
            tuple(candidates),
            tuple(rejected),
            source,
        )
    if official is not None:
        lib_id, symbol = official
        return SymbolChoice(
            symbol,
            f"{lib_id}: official symbol, pins match all {len(pads)} pads ({source})",
            False,
            tuple(candidates),
            tuple(rejected),
            source,
        )
    if rejected:
        return generic(
            f"generic box: pads {pads_text} ({source}) do not match "
            + ", ".join(f"{lib_id} ({why})" for lib_id, why in rejected),
            candidates,
            rejected,
        )
    return generic(
        f"generic box: no official candidate for kind {kind!r}, mpn {mpn!r}, footprint {footprint or '-'!r}",
        candidates,
        rejected,
    )


def _rejected_text(rejected: Sequence[tuple[str, str]]) -> str:
    if not rejected:
        return ""
    return "; tried " + ", ".join(f"{lib_id} ({why})" for lib_id, why in rejected)


def _mismatch(symbol: LibSymbol, pads: set[str], model_pins: set[str]) -> str | None:
    """Why the symbol's pin numbers differ from the pads, or None when they fit."""
    numbers = {pin.number for pin in symbol.pins}
    problems: list[str] = []
    missing = sorted(numbers - pads, key=_pad_sort_key)
    if missing:
        problems.append("symbol pins without a pad: " + ",".join(missing))
    extra = sorted(pads - numbers, key=_pad_sort_key)
    if extra:
        problems.append("pads without a symbol pin: " + ",".join(extra))
    unmapped = sorted(model_pins - numbers, key=_pad_sort_key)
    if unmapped and not extra:
        problems.append("model pins without a symbol pin: " + ",".join(unmapped))
    return "; ".join(problems) or None


def _pad_sort_key(pad: str) -> tuple[int, int | str]:
    return (0, int(pad)) if pad.isdigit() else (1, pad)


def _generic_name(mpn: str, value: str, reference: str) -> str:
    base = mpn if mpn and mpn.upper() != "N/A" else value or reference
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", base)[:48] or reference


__all__ = [
    "GENERIC_LIB",
    "GRID",
    "LibSymbol",
    "POWER_LIB",
    "Pin",
    "SymbolChoice",
    "SymbolError",
    "choose_symbol",
    "connector_symbol",
    "generic_symbol",
    "lib_symbol",
    "library_names",
    "load_stock",
    "official_candidates",
    "power_symbol",
    "symbols_dir",
]
