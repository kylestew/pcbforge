from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge import circuit_review, kicad_sym, sexpr
from pcbforge.cli import main
from pcbforge.kicad_sch import (
    REVIEW_MARKER,
    Placed,
    RenderResult,
    ReviewSchematic,
    SchematicError,
    probe_text,
)
from pcbforge.sch_lint import Box, SheetGeometry, TextBox, lint
from tests.kicad_fake import FIXTURE_SYMBOLS, FakeKicad
from tests.test_circuit_review import CircuitReviewFixture

TOOL_ROOT = Path(__file__).resolve().parents[1]
KICAD9 = Path("/Applications/KiCad 9/KiCad.app/Contents/MacOS/kicad-cli")

LDO_MODEL = """circuit_model_schema: 1
components:
  - {reference: J1, kind: connector, value: 5V IN, footprint: "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", mpn: PH2, lcsc: C1, purpose: External 5 V input.}
  - {reference: U1, kind: ic, value: AMS1117-3.3, footprint: "Package_TO_SOT_SMD:SOT-223-3_TabPin2", mpn: AMS1117-3.3, lcsc: C2, purpose: 3.3 V LDO.}
  - {reference: C1, kind: capacitor, value: 10u, footprint: "Capacitor_SMD:C_0805_2012Metric", mpn: CAP1, lcsc: C3, purpose: Input bulk.}
  - {reference: C2, kind: capacitor, value: 22u, footprint: "Capacitor_SMD:C_0805_2012Metric", mpn: CAP2, lcsc: C4, purpose: Output bulk.}
  - {reference: R1, kind: resistor, value: 1k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES1, lcsc: C5, purpose: LED current limit.}
  - {reference: D1, kind: led, value: PWR, footprint: "LED_SMD:LED_0603_1608Metric", mpn: LED1, lcsc: C6, purpose: Power indicator.}
  - {reference: Q1, kind: mosfet, value: AO3401A, footprint: "Package_TO_SOT_SMD:SOT-23", mpn: AO3401A, lcsc: C7, purpose: Reverse protection.}
nets:
  - {id: vbus, display_name: VBUS, compiler_name: VBUS, nodes: [J1.1, Q1.3]}
  - {id: vin, display_name: VIN, compiler_name: VIN, nodes: [Q1.2, C1.1, U1.3]}
  - {id: rail-3v3, display_name: +3V3, compiler_name: +3V3, nodes: [U1.2, C2.1, R1.1]}
  - {id: ground, display_name: GND, compiler_name: GND, nodes: [J1.2, C1.2, U1.1, C2.2, D1.1, Q1.1]}
  - {id: led-anode, display_name: LED_A, compiler_name: LED_A, nodes: [R1.2, D1.2]}
groups:
  - {id: power, title: Power input and regulation, purpose: 5 V in to 3.3 V out., references: [J1, Q1, C1, U1, C2]}
  - {id: indicator, title: Power indicator, purpose: Shows the rail is up., references: [R1, D1]}
paths:
  - {id: supply, title: Supply path, purpose: VBUS through the LDO to +3V3., nodes: [J1.1, Q1.3, Q1.2, U1.3, U1.2, R1.1]}
"""

LDO_NETS = {
    "VBUS": ["J1.1", "Q1.3"],
    "VIN": ["Q1.2", "C1.1", "U1.3"],
    "+3V3": ["U1.2", "C2.1", "R1.1"],
    "GND": ["J1.2", "C1.2", "U1.1", "C2.2", "D1.1", "Q1.1"],
    "LED_A": ["R1.2", "D1.2"],
}


def draw_ldo(sch: ReviewSchematic) -> None:
    """A tidy reference drawing of LDO_MODEL used by several tests."""
    j1 = sch.place("J1", (40.64, 63.5))
    q1 = sch.place("Q1", (63.5, 58.42), rotation=90)  # D left, S right, G down
    u1 = sch.place("U1", (101.6, 55.88))
    c1 = sch.place("C1", (81.28, 66.04))
    c2 = sch.place("C2", (127.0, 66.04))
    r1 = sch.place("R1", (160.02, 49.53))
    d1 = sch.place("D1", below=r1, gap=5.08, rotation=90)
    vin_y = u1.pin(3)[1]
    gnd_y = 81.28
    p = sch.pin
    sch.wire(p("J1", 1), (p("J1", 1)[0], vin_y), path="supply")
    sch.connect((p("J1", 1)[0], vin_y), p("Q1", 3), path="supply")
    sch.label_at((p("J1", 1)[0], vin_y), "vbus", direction="up")
    sch.connect(p("Q1", 2), p("U1", 3), path="supply")
    sch.drop("C1", 1, vin_y)
    sch.power_at((p("C1", 1)[0], vin_y), "vin", flag=True)
    sch.label("U1", 2, "rail-3v3", length=0)
    sch.label("R1", 1, "rail-3v3", direction="up", path="supply")
    sch.drop("C2", 1, u1.pin(2)[1])
    sch.power_at((p("C2", 1)[0], u1.pin(2)[1]), "rail-3v3")
    for ref, pin in (("J1", 2), ("C1", 2), ("U1", 1), ("C2", 2)):
        sch.drop(ref, pin, gnd_y)
    sch.label("Q1", 1, "ground")
    sch.wire((p("J1", 2)[0], gnd_y), (p("C2", 2)[0], gnd_y))
    sch.power_at((p("U1", 1)[0], gnd_y), "ground", direction="down", flag=True)
    sch.power("D1", 1, "ground", direction="down")
    sch.connect(p("R1", 2), p("D1", 2))
    sch.label_at((p("R1", 2)[0], (p("R1", 2)[1] + p("D1", 2)[1]) / 2), "led-anode", direction="right")


class SexprTests(unittest.TestCase):
    def test_round_trip_keeps_quoted_strings_and_nesting(self) -> None:
        text = '(a "b c" (d 1.5 "q\\"x") (e))'
        node = sexpr.parse(text)
        self.assertEqual(sexpr.head(node), "a")
        self.assertEqual(sexpr.atom(node, 1), "b c")
        self.assertEqual(sexpr.atom(sexpr.child(node, "d"), 2), 'q"x')
        self.assertEqual(sexpr.number(sexpr.child(node, "d"), 1), 1.5)
        self.assertEqual(sexpr.parse(sexpr.dumps(node)), node)

    def test_rejects_unbalanced_and_multiple_roots(self) -> None:
        for text in ("(a (b)", "(a))", "(a) (b)", "a"):
            with self.assertRaises(sexpr.SExprError):
                sexpr.parse(text)


class SymbolTests(unittest.TestCase):
    def test_symbols_dir_follows_the_kicad9_shim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            fake = root / "KiCad.app" / "Contents"
            (fake / "MacOS").mkdir(parents=True)
            (fake / "SharedSupport" / "symbols").mkdir(parents=True)
            (root / "scripts" / "kicad-cli").write_text(
                f'#!/bin/sh\nKICAD9_CLI="{fake / "MacOS" / "kicad-cli"}"\n', encoding="utf-8"
            )
            self.assertEqual(kicad_sym.symbols_dir(root), fake / "SharedSupport" / "symbols")
            (root / "scripts" / "kicad-cli").write_text("#!/bin/sh\n", encoding="utf-8")
            with self.assertRaisesRegex(kicad_sym.SymbolError, "KICAD9_CLI"):
                kicad_sym.symbols_dir(root)

    def test_stock_symbol_is_renamed_and_pins_parsed(self) -> None:
        symbol = kicad_sym.lib_symbol("Device:R", FIXTURE_SYMBOLS)
        self.assertEqual(symbol.lib_id, "Device:R")
        self.assertEqual(sexpr.atom(symbol.node), "Device:R")
        self.assertEqual(
            [sexpr.atom(sub) for sub in sexpr.children(symbol.node, "symbol")],
            ["R_0_1", "R_1_1"],
        )
        self.assertEqual({pin.number: (pin.x, pin.y) for pin in symbol.pins}, {"1": (0.0, 3.81), "2": (0.0, -3.81)})
        self.assertEqual(symbol.bbox(), (-1.016, -3.81, 1.016, 3.81))

    def test_extends_is_flattened_with_child_properties(self) -> None:
        symbol = kicad_sym.lib_symbol("Transistor_FET:AO3401A", FIXTURE_SYMBOLS)
        self.assertIsNone(sexpr.child(symbol.node, "extends"))
        self.assertEqual(
            [sexpr.atom(sub) for sub in sexpr.children(symbol.node, "symbol")],
            ["AO3401A_0_1", "AO3401A_1_1"],
        )
        values = {sexpr.atom(p, 1): sexpr.atom(p, 2) for p in sexpr.children(symbol.node, "property")}
        self.assertEqual(values["Value"], "AO3401A")
        self.assertEqual(symbol.pin_numbers, frozenset({"1", "2", "3"}))
        regulator = kicad_sym.lib_symbol("Regulator_Linear:AMS1117-3.3", FIXTURE_SYMBOLS)
        self.assertEqual(regulator.pin_numbers, frozenset({"1", "2", "3"}))

    def test_multi_unit_symbol_reports_units(self) -> None:
        symbol = kicad_sym.lib_symbol("Amplifier_Operational:LM358", FIXTURE_SYMBOLS)
        self.assertEqual(symbol.units, 3)
        self.assertEqual(symbol.pin("1", unit=1).number, "1")
        with self.assertRaises(kicad_sym.SymbolError):
            symbol.pin("1", unit=2)

    def test_generic_box_puts_pins_on_grid(self) -> None:
        symbol = kicad_sym.generic_symbol("FOO", [("1", "VDD"), ("2", "GND"), ("3", "SDA"), ("4", "SCL"), ("5", "INT")])
        self.assertEqual(symbol.lib_id, "pcbforge:FOO")
        by_number = {pin.number: pin for pin in symbol.pins}
        self.assertEqual(by_number["1"].rotation, 270)  # top
        self.assertEqual(by_number["2"].rotation, 90)  # bottom
        self.assertEqual({by_number["3"].rotation, by_number["5"].rotation}, {0, 180})
        for pin in symbol.pins:
            self.assertAlmostEqual(pin.x / 1.27, round(pin.x / 1.27), places=6)
            self.assertAlmostEqual(pin.y / 1.27, round(pin.y / 1.27), places=6)

    def test_power_symbol_value_is_the_net_name(self) -> None:
        rail = kicad_sym.power_symbol("+3V0", "rail")
        values = {sexpr.atom(p, 1): sexpr.atom(p, 2) for p in sexpr.children(rail.node, "property")}
        self.assertEqual(values["Value"], "+3V0")
        self.assertTrue(rail.power)
        self.assertEqual(rail.pins[0].electrical, "power_in")
        flag = kicad_sym.power_symbol("x", "flag")
        self.assertEqual(flag.pins[0].electrical, "power_out")

    def test_choice_prefers_stock_when_pads_match_else_box(self) -> None:
        common = dict(value="v", reference="U1", model_pins={"1", "2"}, directory=FIXTURE_SYMBOLS)
        choice = kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"1", "2"}, **common)
        self.assertEqual(choice.symbol.lib_id, "Device:R")
        self.assertFalse(choice.generic)
        choice = kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"A", "K"}, **dict(common, model_pins={"A"}))
        self.assertTrue(choice.generic)
        self.assertIn("do not match Device:R", choice.reason)
        choice = kicad_sym.choose_symbol(kind="mosfet", mpn="AO3401A", board_pads={"1", "2", "3"}, **dict(common, model_pins={"1"}))
        self.assertEqual(choice.symbol.lib_id, "Transistor_FET:AO3401A")
        choice = kicad_sym.choose_symbol(kind="mechanical", mpn="MountingHole_3.2mm_M3", board_pads={""}, **dict(common, model_pins=set()))
        self.assertEqual(choice.symbol.lib_id, "Mechanical:MountingHole")
        choice = kicad_sym.choose_symbol(kind="connector", mpn="x", board_pads={str(i) for i in range(1, 11)}, override="Connector_Generic:Conn_02x05_Odd_Even", **dict(common, model_pins={"1"}))
        self.assertEqual(choice.symbol.lib_id, "Connector_Generic:Conn_02x05_Odd_Even")
        with self.assertRaisesRegex(kicad_sym.SymbolError, "do not match footprint pads"):
            kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"1", "2", "3"}, override="Device:R", **common)
        choice = kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"1", "2"}, override="generic", **common)
        self.assertTrue(choice.generic)


class PlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resistor = kicad_sym.lib_symbol("Device:R", FIXTURE_SYMBOLS)
        self.led = kicad_sym.lib_symbol("Device:LED", FIXTURE_SYMBOLS)

    def test_pin_tips_follow_rotation_and_mirror(self) -> None:
        cases = {
            (0, None): {"1": (101.6, 97.79), "2": (101.6, 105.41)},
            (90, None): {"1": (97.79, 101.6), "2": (105.41, 101.6)},
            (180, None): {"1": (101.6, 105.41), "2": (101.6, 97.79)},
            (270, None): {"1": (105.41, 101.6), "2": (97.79, 101.6)},
            (0, "x"): {"1": (101.6, 105.41), "2": (101.6, 97.79)},
        }
        for (rotation, mirror), expected in cases.items():
            placed = Placed("R1", self.resistor, (101.6, 101.6), rotation, mirror, 1)
            self.assertEqual(placed.pins, expected, (rotation, mirror))

    def test_outward_direction_points_away_from_the_body(self) -> None:
        placed = Placed("D1", self.led, (100.0, 100.0), 0, None, 1)
        self.assertEqual(placed.pin_outward(1), (-1.0, 0.0))
        self.assertEqual(placed.pin_outward(2), (1.0, 0.0))
        rotated = Placed("D1", self.led, (100.0, 100.0), 90, None, 1)
        self.assertEqual(rotated.pin_outward(1), (0.0, 1.0))


class SchematicFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.kicad = FakeKicad(LDO_NETS)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model_path = self.root / "circuit.yaml"
        self.model_path.write_text(LDO_MODEL, encoding="utf-8")
        self.output = self.root / "review" / "circuit" / "circuit.kicad_sch"

    def schematic(self, **kwargs) -> ReviewSchematic:
        kwargs.setdefault("board_pads", {"J1": {"1", "2"}, "Q1": {"1", "2", "3"}})
        return ReviewSchematic(
            model_path=self.model_path,
            output_path=self.output,
            title="LDO",
            desc="Test drawing.",
            tool_root=TOOL_ROOT,
            symbols_dir=FIXTURE_SYMBOLS,
            runner=self.kicad,
            **kwargs,
        )


class WriterTests(SchematicFixture):
    def test_reference_drawing_is_clean_and_deterministic(self) -> None:
        sch = self.schematic()
        draw_ldo(sch)
        result = sch.save()
        first = self.output.read_bytes()
        self.assertIsInstance(result, RenderResult)
        self.assertEqual([w.payload() for w in result.warnings], [])
        self.assertEqual(set(result.symbol_choices), {"J1", "U1", "C1", "C2", "R1", "D1", "Q1"})
        self.assertFalse(any(c.generic for c in result.symbol_choices.values()))
        audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["schema"], circuit_review.SCHEMATIC_AUDIT_SCHEMA)
        self.assertEqual(audit["bound_component_refs"], ["C1", "C2", "D1", "J1", "Q1", "R1", "U1"])
        self.assertEqual(audit["nets"]["rail-3v3"], {"display_name": "+3V3", "compiler_name": "+3V3"})

        second = self.schematic()
        draw_ldo(second)
        second.save()
        self.assertEqual(first, self.output.read_bytes())

        root = sexpr.parse(first.decode("utf-8"))
        self.assertEqual(sexpr.atom(sexpr.child(root, "version")), circuit_review.SCH_FORMAT_VERSION)
        lib_ids = {sexpr.atom(node) for node in sexpr.children(sexpr.child(root, "lib_symbols"), "symbol")}
        self.assertIn("Transistor_FET:AO3401A", lib_ids)
        self.assertIn("pcbforge_power:+3V3", lib_ids)
        self.assertIn("pcbforge_power:PWR_FLAG", lib_ids)
        texts = [sexpr.atom(node) for node in sexpr.children(root, "text")]
        self.assertTrue(any(REVIEW_MARKER in t for t in texts))
        self.assertTrue(any("Power input and regulation" == t for t in texts))
        self.assertTrue(any("Component register" in t and "R1  1k" in t for t in texts))
        self.assertTrue(any("+3V3 · +3V3" in t for t in texts))
        symbols = {
            sexpr.atom([p for p in sexpr.children(s, "property") if sexpr.atom(p, 1) == "Reference"][0], 2): s
            for s in sexpr.children(root, "symbol")
        }
        props = {sexpr.atom(p, 1): sexpr.atom(p, 2) for p in sexpr.children(symbols["R1"], "property")}
        self.assertEqual(props["Footprint"], "Resistor_SMD:R_0603_1608Metric")
        self.assertEqual(props["pcbforge_group"], "indicator")
        self.assertEqual(props["pcbforge_purpose"], "LED current limit.")
        self.assertEqual(len(sexpr.children(root, "rectangle")), 2)
        coloured = [w for w in sexpr.children(root, "wire") if sexpr.child(sexpr.child(w, "stroke"), "color")]
        self.assertTrue(coloured)
        self.assertTrue(sexpr.children(root, "junction"))

    def test_wires_are_split_at_junctions_and_pins(self) -> None:
        sch = self.schematic()
        draw_ldo(sch)
        sch.save()
        root = sexpr.parse(self.output.read_text(encoding="utf-8"))
        segments = []
        for wire in sexpr.children(root, "wire"):
            pts = [(sexpr.number(xy, 1), sexpr.number(xy, 2)) for xy in sexpr.children(sexpr.child(wire, "pts"), "xy")]
            segments.append(tuple(pts))
        junctions = {
            (sexpr.number(sexpr.child(j, "at"), 1), sexpr.number(sexpr.child(j, "at"), 2))
            for j in sexpr.children(root, "junction")
        }
        for a, b in segments:
            for point in junctions:
                strictly_inside = (
                    (a[0] == b[0] == point[0] and min(a[1], b[1]) < point[1] < max(a[1], b[1]))
                    or (a[1] == b[1] == point[1] and min(a[0], b[0]) < point[0] < max(a[0], b[0]))
                )
                self.assertFalse(strictly_inside, f"junction {point} inside {a}->{b}")

    def test_contract_errors(self) -> None:
        sch = self.schematic()
        with self.assertRaisesRegex(SchematicError, "unknown component reference"):
            sch.place("R9", (0, 0))
        with self.assertRaisesRegex(SchematicError, "pass at="):
            sch.place("R1")
        r1 = sch.place("R1", (50.8, 50.8))
        with self.assertRaisesRegex(SchematicError, "already placed"):
            sch.place("R1", (60, 50))
        with self.assertRaisesRegex(SchematicError, "not orthogonal"):
            sch.wire((0, 0), (10, 10))
        with self.assertRaisesRegex(SchematicError, "unknown net id"):
            sch.label("R1", 1, "nope")
        with self.assertRaisesRegex(SchematicError, "unknown path id"):
            sch.wire((0, 0), (10, 0), path="nope")
        with self.assertRaisesRegex(SchematicError, "must be placed; missing: C1, C2, D1, J1, Q1, U1"):
            sch.save()
        self.assertEqual(r1.pin(1), (50.8, 46.99))

    def test_missing_path_and_empty_group_are_rejected(self) -> None:
        sch = self.schematic()
        for index, ref in enumerate(("J1", "Q1", "U1", "C1", "C2", "R1", "D1")):
            sch.place(ref, (30 + index * 25.4, 60))
        with self.assertRaisesRegex(SchematicError, "path=.*supply"):
            sch.save()

    def test_single_node_nets_get_no_connect_markers(self) -> None:
        self.model_path.write_text(
            LDO_MODEL.replace("nodes: [R1.2, D1.2]", "nodes: [R1.2]").replace(
                "groups:", "  - {id: nc-anode, display_name: NC, compiler_name: NC_D1_2, nodes: [D1.2]}\ngroups:"
            ),
            encoding="utf-8",
        )
        self.kicad.nets = dict(LDO_NETS, LED_A=["R1.2"], **{"unconnected-(D1-A-Pad2)": ["D1.2"]})
        sch = self.schematic()
        j1 = sch.place("J1", (40.64, 63.5))
        q1 = sch.place("Q1", (63.5, 58.42), rotation=90)
        u1 = sch.place("U1", (101.6, 55.88))
        sch.place("C1", (81.28, 66.04))
        sch.place("C2", (127.0, 66.04))
        r1 = sch.place("R1", (160.02, 49.53))
        sch.place("D1", below=r1, gap=10.16, rotation=90)
        vin_y = u1.pin(3)[1]
        p = sch.pin
        sch.wire(p("J1", 1), (p("J1", 1)[0], vin_y), path="supply")
        sch.connect((p("J1", 1)[0], vin_y), p("Q1", 3), path="supply")
        sch.label_at((p("J1", 1)[0], vin_y), "vbus", direction="up")
        sch.connect(p("Q1", 2), p("U1", 3), path="supply")
        sch.drop("C1", 1, vin_y)
        sch.power_at((p("C1", 1)[0], vin_y), "vin", flag=True)
        sch.label("U1", 2, "rail-3v3", length=0)
        sch.label("R1", 1, "rail-3v3", direction="up")
        sch.drop("C2", 1, u1.pin(2)[1])
        sch.power_at((p("C2", 1)[0], u1.pin(2)[1]), "rail-3v3")
        for ref, pin in (("J1", 2), ("C1", 2), ("U1", 1), ("C2", 2)):
            sch.drop(ref, pin, 81.28)
        sch.label("Q1", 1, "ground")
        sch.wire((p("J1", 2)[0], 81.28), (p("C2", 2)[0], 81.28))
        sch.power_at((p("U1", 1)[0], 81.28), "ground", direction="down", flag=True)
        sch.power("D1", 1, "ground", direction="down")
        sch.label("R1", 2, "led-anode", direction="down")
        result = sch.save()
        root = sexpr.parse(self.output.read_text(encoding="utf-8"))
        no_connects = sexpr.children(root, "no_connect")
        self.assertEqual(len(no_connects), 1)
        self.assertEqual(
            (sexpr.number(sexpr.child(no_connects[0], "at"), 1), sexpr.number(sexpr.child(no_connects[0], "at"), 2)),
            sch.pin("D1", 2),
        )
        self.assertEqual([w.code for w in result.warnings], [])

    def test_gate_failures_surface_as_schematic_errors(self) -> None:
        self.kicad.erc = ["Pin not connected"]
        sch = self.schematic()
        draw_ldo(sch)
        with self.assertRaisesRegex(SchematicError, "ERC errors"):
            sch.save()
        self.kicad.erc = []
        self.kicad.nets = dict(LDO_NETS, LED_A=["R1.2", "D1.1"])
        sch = self.schematic()
        draw_ldo(sch)
        with self.assertRaisesRegex(SchematicError, "missing proposed endpoint sets: led-anode"):
            sch.save()

    def test_wire_through_a_pin_fails_at_the_call(self) -> None:
        sch = self.schematic()
        j1 = sch.place("J1", (40.64, 63.5), mirror="y")           # pins on the right, pin 1 top
        with self.assertRaisesRegex(SchematicError, r"runs through pin J1\.2"):
            sch.power("J1", 1, "vbus", direction="down", length=5.08)
        sch.wire((80.01, 50.8), (80.01, 71.12))
        with self.assertRaisesRegex(SchematicError, r"pin R1\.\d lands on the wire"):
            sch.place("R1", (80.01, 60.96))                           # both pins on the wire
        self.assertEqual(j1.pin_side(1), "right")
        self.assertEqual(Placed("R1", sch.symbol_for("R1").symbol, (0.0, 0.0), 0, None, 1).pin_side(1), "up")

    def test_preflight_names_the_element_that_joins_two_nets(self) -> None:
        sch = self.schematic()
        draw_ldo(sch)
        # a GND power symbol dropped onto the LED_A label point joins the nets
        point = (sch.pin("R1", 2)[0], (sch.pin("R1", 2)[1] + sch.pin("D1", 2)[1]) / 2)
        sch.power_at(point, "ground", direction="right")
        with self.assertRaisesRegex(SchematicError, r"short: model nets ground, led-anode are joined — first joined by power symbol GND"):
            sch.save()

    def test_open_net_is_reported_as_a_warning(self) -> None:
        self.kicad.nets = dict(LDO_NETS, LED_A=["R1.2"], **{"unconnected-(D1-A-Pad2)": ["D1.2"]})
        sch = self.schematic()
        draw_ldo(sch)
        # remove the R1->D1 wire by rebuilding without it is heavy; instead mark D1.2 via a
        # label on another point so the nets stay separate pieces on the sheet
        sch = self.schematic()
        j1 = sch.place("J1", (40.64, 63.5))
        q1 = sch.place("Q1", (63.5, 58.42), rotation=90)
        u1 = sch.place("U1", (101.6, 55.88))
        sch.place("C1", (81.28, 66.04))
        sch.place("C2", (127.0, 66.04))
        r1 = sch.place("R1", (160.02, 49.53))
        sch.place("D1", below=r1, gap=10.16, rotation=90)
        p = sch.pin
        vin_y = u1.pin(3)[1]
        sch.wire(p("J1", 1), (p("J1", 1)[0], vin_y), path="supply")
        sch.connect((p("J1", 1)[0], vin_y), p("Q1", 3), path="supply")
        sch.label_at((p("J1", 1)[0], vin_y), "vbus", direction="up")
        sch.connect(p("Q1", 2), p("U1", 3), path="supply")
        sch.drop("C1", 1, vin_y)
        sch.power_at((p("C1", 1)[0], vin_y), "vin", flag=True)
        sch.label("U1", 2, "rail-3v3", length=0)
        sch.label("R1", 1, "rail-3v3", direction="up")
        sch.drop("C2", 1, u1.pin(2)[1])
        sch.power_at((p("C2", 1)[0], u1.pin(2)[1]), "rail-3v3")
        for ref, pin in (("J1", 2), ("C1", 2), ("U1", 1), ("C2", 2)):
            sch.drop(ref, pin, 81.28)
        sch.label("Q1", 1, "ground")
        sch.wire((p("J1", 2)[0], 81.28), (p("C2", 2)[0], 81.28))
        sch.power_at((p("U1", 1)[0], 81.28), "ground", direction="down", flag=True)
        sch.power("D1", 1, "ground", direction="down")
        sch.label("R1", 2, "led-anode", direction="down")   # D1.2 left unconnected: open net
        self.kicad.nets = dict(LDO_NETS, LED_A=["R1.2"], **{"unconnected-(D1-A-Pad2)": ["D1.2"]})
        with self.assertRaisesRegex(SchematicError, "missing proposed endpoint sets: led-anode"):
            result = sch.save()
        # the pre-flight warning is visible in the failure text
        try:
            sch.save()
        except SchematicError as exc:
            self.assertIn("[open-net] net led-anode is drawn as 2 disconnected pieces", str(exc))

    def test_probe_lists_rotations_and_sides(self) -> None:
        sch = self.schematic()
        text = probe_text(sch, ["R1", "J1"])
        self.assertIn("R1: Device:R (stock", text)
        self.assertIn("rail-3v3", text)
        self.assertIn("rot0 +miry", text)
        lines = [l for l in text.splitlines() if l.strip().startswith("1 ")]
        self.assertTrue(any("up    (+0.00,-3.81)" in l and "left  (-3.81,+0.00)" in l for l in lines))
        with self.assertRaisesRegex(SchematicError, "unknown component reference"):
            probe_text(sch, ["R9"])

    def test_generic_box_when_pads_do_not_match(self) -> None:
        sch = self.schematic(board_pads={"D1": {"A", "K"}})
        sch.pin_names("D1", {"A": "anode", "K": "cathode"})
        choice = sch.symbol_for("D1")
        self.assertTrue(choice.generic)
        self.assertEqual(choice.symbol.lib_id, "pcbforge:LED1")
        self.assertEqual({pin.number for pin in choice.symbol.pins}, {"A", "K", "1", "2"})


class LintTests(unittest.TestCase):
    def geometry(self, **overrides) -> SheetGeometry:
        base = dict(
            texts=(),
            symbols={"R1": Box(10, 10, 12, 18)},
            symbol_pins={"R1": frozenset({(11.0, 10.0), (11.0, 18.0)})},
            wires=(),
            junctions=frozenset(),
            pin_tips=frozenset({(11.0, 10.0), (11.0, 18.0)}),
            label_points=frozenset(),
            group_boxes={"g": Box(0, 0, 30, 30)},
            group_of={"R1": "g"},
        )
        base.update(overrides)
        return SheetGeometry(**base)

    def codes(self, geometry: SheetGeometry) -> set[str]:
        return {w.code for w in lint(geometry)}

    def test_each_code_fires_on_its_minimal_case(self) -> None:
        text = TextBox("a", Box(10, 11, 14, 13))
        self.assertIn("text-symbol-overlap", self.codes(self.geometry(texts=(text,))))
        self.assertIn(
            "text-text-overlap",
            self.codes(self.geometry(texts=(TextBox("a", Box(20, 20, 24, 22)), TextBox("b", Box(21, 20, 25, 22))))),
        )
        self.assertIn(
            "text-wire-overlap",
            self.codes(self.geometry(texts=(TextBox("a", Box(20, 20, 24, 22)),), wires=(((18.0, 21.0), (26.0, 21.0)),), junctions=frozenset(), label_points=frozenset({(18.0, 21.0), (26.0, 21.0)}))),
        )
        self.assertIn("symbol-overlap", self.codes(self.geometry(symbols={"R1": Box(10, 10, 12, 18), "R2": Box(11, 12, 14, 20)})))
        self.assertIn(
            "wire-symbol-overlap",
            self.codes(self.geometry(wires=(((5.0, 14.0), (20.0, 14.0)),), label_points=frozenset({(5.0, 14.0), (20.0, 14.0)}))),
        )
        self.assertIn(
            "wire-crossing",
            self.codes(self.geometry(wires=(((20.0, 20.0), (28.0, 20.0)), ((24.0, 16.0), (24.0, 24.0))), label_points=frozenset({(20.0, 20.0), (28.0, 20.0), (24.0, 16.0), (24.0, 24.0)}))),
        )
        self.assertIn(
            "overlapping-wire-runs",
            self.codes(self.geometry(wires=(((20.0, 20.0), (28.0, 20.0)), ((24.0, 20.0), (29.0, 20.0))), label_points=frozenset({(20.0, 20.0), (28.0, 20.0), (24.0, 20.0), (29.0, 20.0)}))),
        )
        self.assertIn("dangling-wire-end", self.codes(self.geometry(wires=(((20.0, 20.0), (28.0, 20.0)),))))
        self.assertIn(
            "wire-passes-pin",
            self.codes(self.geometry(wires=(((11.0, 5.0), (11.0, 25.0)),), label_points=frozenset({(11.0, 5.0), (11.0, 25.0)}))),
        )
        self.assertIn("group-boxes-overlap", self.codes(self.geometry(group_boxes={"g": Box(0, 0, 30, 30), "h": Box(20, 20, 40, 40)})))
        self.assertIn("symbol-outside-group", self.codes(self.geometry(group_boxes={"g": Box(0, 0, 11, 11)})))
        self.assertIn(
            "wire-crosses-group-box",
            self.codes(self.geometry(wires=(((11.0, 18.0), (11.0, 40.0)),), label_points=frozenset({(11.0, 40.0)}))),
        )

    def test_clean_geometry_has_no_warnings(self) -> None:
        geometry = self.geometry(
            wires=(((11.0, 18.0), (11.0, 24.0)),),
            label_points=frozenset({(11.0, 24.0)}),
            texts=(TextBox("R1", Box(13, 12, 16, 14), "R1"),),
        )
        self.assertEqual(lint(geometry), [])


class RenderCircuitCliTests(CircuitReviewFixture):
    SCRIPT = """from pathlib import Path
from pcbforge.kicad_sch import ReviewSchematic
from tests.kicad_fake import FIXTURE_SYMBOLS
PROJECT = Path(__file__).resolve().parents[2]
sch = ReviewSchematic(PROJECT, title="Garden logger", desc="Reviewed branch only.", symbols_dir=FIXTURE_SYMBOLS, board_pads={"R1": {"1", "2"}})
r1 = sch.place("R1", (50.8, 50.8))
sch.label("R1", 1, "supply", direction="up", path="current-path")
sch.label("R1", 2, "ground", direction="down")
result = sch.save()
"""

    def test_render_circuit_runs_the_script_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.assertEqual(main(["render-circuit", str(project)]), 2)
            with mock.patch("builtins.print") as printed:
                self.assertEqual(main(["render-circuit", "--probe", "all", str(project)]), 0)
            self.assertIn("R1: Device:R", printed.call_args_list[0].args[0])
            script = project / "review" / "circuit" / "circuit_schematic.py"
            script.write_text(self.SCRIPT, encoding="utf-8")
            with mock.patch("builtins.print") as printed:
                self.assertEqual(main(["render-circuit", str(project), "--svg"]), 0)
            lines = [call.args[0] for call in printed.call_args_list if call.args]
            self.assertTrue(any("symbol R1 -> Device:R" in line for line in lines))
            self.assertTrue(any("rendered and validated the circuit review schematic" in line for line in lines))
            self.assertTrue(any("preview review/circuit/preview/circuit.svg" in line for line in lines))
            self.assertTrue((project / "review" / "circuit" / "schematic.audit.json").is_file())
            self.assertTrue((project / "garden-logger.kicad_sch").is_file())
            sheet = sexpr.parse((project / "garden-logger.kicad_sch").read_text(encoding="utf-8"))
            root_uuid = sexpr.atom(sexpr.child(sheet, "uuid"))
            instances = [
                sexpr.atom(proj)
                for symbol in sexpr.children(sheet, "symbol")
                for inst in sexpr.children(symbol, "instances")
                for proj in sexpr.children(inst, "project")
            ]
            self.assertEqual(set(instances), {"garden-logger"})
            labels = {sexpr.atom(node) for node in sexpr.children(sheet, "label")}
            self.assertEqual(labels, {"+3V3", "GND"})
            project_file = json.loads((project / "garden-logger.kicad_pro").read_text(encoding="utf-8"))
            self.assertEqual(project_file["sheets"], [[root_uuid, "Root"]])
            self.assertEqual(project_file["net_settings"]["classes"][0]["name"], "Default")
            self.assertEqual(project_file["user_key"], {"kept": True})
            self.assertEqual(main(["check-circuit-review", str(project), "--stage", "proposal", "--write"]), 0)

            script.write_text(self.SCRIPT.replace('sch.label("R1", 2, "ground", direction="down")\n', ""), encoding="utf-8")
            self.kicad.nets = {"+3V3": ["R1.1"]}
            with mock.patch("sys.stderr"):
                self.assertEqual(main(["render-circuit", str(project)]), 1)


@unittest.skipUnless(KICAD9.is_file(), "pinned KiCad 9 not installed")
class RealKicadTests(unittest.TestCase):
    def test_reference_drawing_passes_real_erc_and_netlist_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_path = root / "circuit.yaml"
            model_path.write_text(LDO_MODEL, encoding="utf-8")
            sch = ReviewSchematic(
                model_path=model_path,
                output_path=root / "circuit.kicad_sch",
                title="LDO",
                desc="Real KiCad 9 gate.",
                tool_root=TOOL_ROOT,
                board_pads={"J1": {"1", "2"}, "Q1": {"1", "2", "3"}},
            )
            draw_ldo(sch)
            result = sch.save()
            self.assertEqual([w.payload() for w in result.warnings], [])


if __name__ == "__main__":
    unittest.main()
