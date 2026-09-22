import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pcbforge.netclasses import NetclassError, set_netclass, rgba, color_legend, presentation_settings, matches_pattern
from pcbforge.circuit import presentation_fingerprint, fingerprint_inputs, export_preview
from pcbforge.cli import main
from tests.native_fixture import seed_native
from tests.test_pcb_update import SPEC


PROJECT = {"net_settings": {"meta": {"version": 3}, "classes": [
    {"name": "Default", "priority": 2147483647, "track_width": 0.25,
     "schematic_color": "rgba(0, 0, 0, 0.000)", "pcb_color": "rgba(0, 0, 0, 0.000)",
     "wire_width": 6, "bus_width": 12, "line_style": 0}],
    "netclass_patterns": [], "netclass_assignments": None}, "custom": {"keep": True}}


class NetclassTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "spec.md").write_text(SPEC)
        self.path = self.root / "test.kicad_pro"
        self.path.write_text(json.dumps(PROJECT))
        self.graph = SimpleNamespace(nets={"+3V3": (), "GND": (), "USB_D+": (), "USB_D-": (), "/child/DATA[0]": ()})
        patch = mock.patch("pcbforge.netclasses.extract", return_value=self.graph)
        self.extract = patch.start()
        self.addCleanup(patch.stop)

    def read(self):
        return json.loads(self.path.read_text())

    def test_palette_and_gui_overrides_are_idempotent(self):
        self.assertTrue(set_netclass(self.root, name="power", nets=["+3V3"], role="power"))
        data = self.read()
        item = data["net_settings"]["classes"][-1]
        self.assertEqual(item["schematic_color"], rgba("#D55E00"))
        self.assertEqual(item["track_width"], 0.25)
        self.assertEqual(item["priority"], 0)
        self.assertEqual(data["custom"], {"keep": True})
        item["schematic_color"] = rgba("#123456")
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        self.assertFalse(set_netclass(self.root, name="power", nets=["+3V3"], role="power"))
        self.assertEqual(self.path.read_bytes(), before)
        set_netclass(self.root, name="power", nets=["+3V3"], color="#abcdef")
        item = self.read()["net_settings"]["classes"][-1]
        self.assertEqual(item["pcb_color"], rgba("#ABCDEF"))
        self.assertEqual(item["schematic_color"], item["pcb_color"])

    def test_unknown_net_and_invalid_color_do_not_write(self):
        before = self.path.read_bytes()
        for nets, color in [(["MISSING"], "#000000"), (["GND"], "red"), (["GND*"], "#000000")]:
            with self.assertRaises(NetclassError):
                set_netclass(self.root, name="ground", nets=nets, color=color)
        self.assertEqual(self.path.read_bytes(), before)

    def test_pattern_and_explicit_conflicts_do_not_write(self):
        for settings in [
            {"netclass_patterns": [{"netclass": "Other", "pattern": "USB_*"}]},
            {"netclass_assignments": {"USB_D+": "Other"}},
            {"netclass_assignments": {"USB_D+": ["Other"]}},
        ]:
            data = copy.deepcopy(PROJECT)
            data["net_settings"].update(settings)
            self.path.write_text(json.dumps(data))
            before = self.path.read_bytes()
            with self.assertRaisesRegex(NetclassError, "conflicting"):
                set_netclass(self.root, name="usb", nets=["USB_D+", "USB_D-"], role="usb")
            self.assertEqual(self.path.read_bytes(), before)

    def test_hierarchical_names_cli_and_legend(self):
        self.assertEqual(main(["set-netclass", str(self.root), "--name", "digital", "--net", "/child/DATA[0]", "--role", "digital"]), 0)
        data = self.read()
        self.assertEqual(data["net_settings"]["netclass_patterns"][0]["pattern"], "/child/DATA[0]")
        self.assertIn("/child/DATA[0]", color_legend(data))
        self.assertTrue(matches_pattern("/child/DATA[0]", "/child/DATA[0]"))
        self.assertFalse(matches_pattern("/child/DATA0", "/child/DATA[0]"))

    def test_routing_only_class_does_not_change_presentation(self):
        data = copy.deepcopy(PROJECT)
        before = presentation_settings(data)
        item = copy.deepcopy(data["net_settings"]["classes"][0])
        item.update(name="pcbforge:routing", priority=0, track_width=0.8)
        data["net_settings"]["classes"].append(item)
        data["net_settings"]["netclass_patterns"].append({"pattern": "GND", "netclass": "pcbforge:routing"})
        self.assertEqual(before, presentation_settings(data))

    def test_real_capture_palette_and_board_preservation(self):
        from pcbforge.schematic import extract
        from pcbforge.schematic_edit import SchematicDocument
        path = seed_native(self.root, evidence=False)
        doc = SchematicDocument.load(path)
        for ref, x, nets in [("R2", 90, ("USB_D+", "USB_D-")), ("R3", 130, ("GPIO", "GND"))]:
            doc.add_symbol(ref, "Device:R", (x, 70), value="10k", footprint="Resistor_SMD:R_0603_1608Metric")
            for pin, net in zip(("1", "2"), nets):
                doc.label(net, doc.pin_position(ref, pin), kind="global_label")
        path.write_text(doc.serialize())
        board = self.root / "test.kicad_pcb"
        board.write_bytes(b"untouched PCB")
        self.extract.side_effect = extract
        for name, nets in [("power", ["+3V3"]), ("ground", ["GND"]), ("usb", ["USB_D+", "USB_D-"])]:
            set_netclass(self.root, name=name, nets=nets, role=name)
        graph = extract(path)
        self.assertIn("GPIO", graph.nets)
        data = self.read()
        self.assertFalse(any(p["pattern"] == "GPIO" for p in data["net_settings"]["netclass_patterns"]))
        svg = next(export_preview(self.root).glob("*.svg")).read_text().lower()
        for color in ("#d55e00", "#808080", "#aa66cc"):
            self.assertIn(color, svg)
        self.assertEqual(board.read_bytes(), b"untouched PCB")
        self.assertFalse(set_netclass(self.root, name="usb", nets=["USB_D+", "USB_D-"], role="usb"))

    def test_native_preview_and_presentation_only_invalidation(self):
        seed_native(self.root, evidence=False)
        self.extract.side_effect = None
        electrical = fingerprint_inputs(self.root)
        presentation = presentation_fingerprint(self.root)
        set_netclass(self.root, name="power", nets=["+3V3"], role="power")
        self.assertEqual(electrical, fingerprint_inputs(self.root))
        self.assertNotEqual(presentation, presentation_fingerprint(self.root))
        presentation = presentation_fingerprint(self.root)
        data = self.read()
        item = data["net_settings"]["classes"][-1]
        item["pcb_color"] = rgba("#123456")
        item["track_width"] = 0.7
        self.path.write_text(json.dumps(data))
        self.assertEqual(presentation, presentation_fingerprint(self.root))
        preview = export_preview(self.root)
        svg = next(preview.glob("*.svg")).read_text().lower()
        self.assertIn("#d55e00", svg)


if __name__ == "__main__":
    unittest.main()
