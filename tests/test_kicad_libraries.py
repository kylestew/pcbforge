from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from pcbforge import kicad_sym, sexpr
from pcbforge.sch_lint import Box, SheetGeometry, TextBox, lint
FIXTURE_SYMBOLS = Path(__file__).parent / "fixtures/symbols"
TOOL_ROOT = Path(__file__).resolve().parents[1]
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
                f'#!/bin/sh\nKICAD_CLI="{fake / "MacOS" / "kicad-cli"}"\n', encoding="utf-8"
            )
            self.assertEqual(kicad_sym.symbols_dir(root), fake / "SharedSupport" / "symbols")
            (root / "scripts" / "kicad-cli").write_text("#!/bin/sh\n", encoding="utf-8")
            with self.assertRaisesRegex(kicad_sym.SymbolError, "KICAD_CLI"):
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
        self.assertEqual(choice.candidates, ("Device:R",))
        self.assertEqual(choice.rejected, (("Device:R", "symbol pins without a pad: 1,2; pads without a symbol pin: A,K"),))
        choice = kicad_sym.choose_symbol(kind="mosfet", mpn="AO3401A", board_pads={"1", "2", "3"}, **dict(common, model_pins={"1"}))
        self.assertEqual(choice.symbol.lib_id, "Transistor_FET:AO3401A")
        choice = kicad_sym.choose_symbol(kind="mechanical", mpn="MountingHole_3.2mm_M3", board_pads={""}, **dict(common, model_pins=set()))
        self.assertEqual(choice.symbol.lib_id, "Mechanical:MountingHole")
        choice = kicad_sym.choose_symbol(kind="connector", mpn="x", board_pads={str(i) for i in range(1, 11)}, override="Connector_Generic:Conn_02x05_Odd_Even", **dict(common, model_pins={"1"}))
        self.assertEqual(choice.symbol.lib_id, "Connector_Generic:Conn_02x05_Odd_Even")
        with self.assertRaisesRegex(kicad_sym.SymbolError, "do not match footprint pads"):
            kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"1", "2", "3"}, override="Device:R", **common)
        with self.assertRaisesRegex(kicad_sym.SymbolError, "generic box refused — official symbol Device:R matches"):
            kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"1", "2"}, override="generic", **common)
        choice = kicad_sym.choose_symbol(kind="resistor", mpn="x", board_pads={"A", "K"}, override="generic", **dict(common, model_pins={"A"}))
        self.assertTrue(choice.generic)
        self.assertIn("generic box requested; no official symbol matches", choice.reason)

    def test_official_search_covers_mpn_wildcards_and_footprint_families(self) -> None:
        common = dict(value="v", directory=FIXTURE_SYMBOLS)
        # the exact part number stays the value; KiCad's x-suffixed name is only the drawing
        choice = kicad_sym.choose_symbol(
            kind="ic", mpn="STM32G0B1KBT6", reference="U1", model_pins={"4", "5"},
            board_pads={str(n) for n in range(1, 33)}, footprint="Package_QFP:LQFP-32_7x7mm_P0.8mm",
            pads_source="official footprint", **common,
        )
        self.assertEqual(choice.symbol.lib_id, "MCU_ST_STM32G0:STM32G0B1KBTx")
        self.assertFalse(choice.generic)
        self.assertEqual(choice.reason, "MCU_ST_STM32G0:STM32G0B1KBTx: official symbol, pins match all 32 pads (official footprint)")
        # net-connected pins alone must not pass for the full package
        choice = kicad_sym.choose_symbol(
            kind="ic", mpn="STM32G0B1KBT6", reference="U1", model_pins={"4", "5"}, board_pads=set(), **common,
        )
        self.assertTrue(choice.generic)
        self.assertIn("MCU_ST_STM32G0:STM32G0B1KBTx (symbol pins without a pad:", choice.reason)
        # the footprint family finds the encoder symbol, mounting pad included
        choice = kicad_sym.choose_symbol(
            kind="switch", mpn="EC11E15244B2", reference="SW1", model_pins={"A", "B", "C", "S1", "S2"},
            board_pads={"A", "B", "C", "S1", "S2", "MP"},
            footprint="Rotary_Encoder:RotaryEncoder_Alps_EC11E-Switch_Vertical_H20mm_MountingHoles", **common,
        )
        self.assertEqual(choice.symbol.lib_id, "Device:RotaryEncoder_Switch_MP")
        self.assertIn("Switch:SW_Push", choice.candidates)
        self.assertEqual(choice.candidates.index("Device:RotaryEncoder"), choice.candidates.index("Device:RotaryEncoder_Switch_MP") - 2)
        choice = kicad_sym.choose_symbol(
            kind="connector", mpn="GT-USB-7010ASV", reference="J1", model_pins={"A1"},
            board_pads={"A1", "A12", "A4", "A5", "A6", "A7", "A8", "A9", "B1", "B12", "B4", "B5", "B6", "B7", "B8", "B9", "S1"},
            footprint="Connector_USB:USB_C_Receptacle_G-Switch_GT-USB-7010ASV", **common,
        )
        self.assertEqual(choice.symbol.lib_id, "Connector:USB_C_Receptacle_USB2.0_16P")
        # a keyed header has no official mapping: the fallback records what was tried and why
        choice = kicad_sym.choose_symbol(
            kind="connector", mpn="FTSH-105-01-L-DV-K", reference="J2", model_pins={"1", "2"},
            board_pads={"1", "2", "3", "4", "5", "6", "8", "9", "10"}, footprint="knob:SWD_Header_2x05_Keyed", pads_source="board pads", **common,
        )
        self.assertTrue(choice.generic)
        self.assertEqual(choice.candidates, ("Connector_Generic:Conn_01x09",))
        self.assertIn("(board pads) do not match Connector_Generic:Conn_01x09 (symbol pins without a pad: 7; pads without a symbol pin: 10)", choice.reason)
        self.assertEqual(choice.pads_source, "board pads")


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
