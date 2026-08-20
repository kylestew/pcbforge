from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from pcbforge.circuit_review import (
    CircuitReviewInputError,
    circuit_model_fingerprint,
    read_circuit_model,
    validate_circuit_svg,
)
from pcbforge.diagram import DiagramError, ReviewDiagram

MODEL = """\
circuit_model_schema: 1
components:
  - reference: R1
    kind: resistor
    value: 10 kohm 1%
    footprint: Resistor_SMD:R_0603_1608Metric
    mpn: 0603WAF1002T5E
    lcsc: C25804
    purpose: Pulls the rail high for the test fixture.
  - reference: TP1
    kind: test-point
    value: 1.5 mm test pad
    footprint: TestPoint:TestPoint_Pad_D1.5mm
    mpn: TestPoint_Pad_D1.5mm
    lcsc: N/A
    purpose: Exposes the pulled rail.
nets:
  - id: rail
    display_name: RAIL
    nodes: [R1.1, TP1.1]
  - id: ground
    display_name: GND
    nodes: [R1.2]
groups:
  - id: fixture
    title: Test fixture pull-up
    purpose: Minimal drawable group.
    references: [R1, TP1]
paths:
  - id: pull
    title: Rail through the pull-up
    purpose: Shows current through R1.
    nodes: [R1.1, R1.2]
"""

MECHANICAL_MODEL = MODEL.replace(
    "nets:\n",
    "  - reference: H1\n"
    "    kind: mechanical\n"
    "    value: M2 clearance hole\n"
    "    footprint: MountingHole:MountingHole_2.2mm_M2\n"
    "    mpn: PCB-MOUNTING-HOLE\n"
    "    lcsc: N/A\n"
    "    purpose: Mounts the board.\n"
    "nets:\n",
).replace("references: [R1, TP1]", "references: [R1, TP1, H1]")

REGRESSION_MODEL = """\
circuit_model_schema: 1
components:
  - {reference: J1, kind: connector, value: FIELD IN, footprint: Connector:2Pin, mpn: J1, lcsc: N/A, purpose: Accepts field power.}
  - {reference: J2, kind: connector, value: FIELD OUT, footprint: Connector:2Pin, mpn: J2, lcsc: N/A, purpose: Passes field power onward.}
  - {reference: U1, kind: ic, value: BUCK, footprint: Package:SOIC-8, mpn: BUCK, lcsc: N/A, purpose: Converts field power.}
  - {reference: L1, kind: inductor, value: 47uH, footprint: Inductor:L_0603, mpn: L1, lcsc: N/A, purpose: Filters the switch output.}
  - {reference: C1, kind: capacitor, value: 22uF, footprint: Capacitor:C_0603, mpn: C1, lcsc: N/A, purpose: Bypasses the output rail.}
  - {reference: R1, kind: resistor, value: 10k, footprint: Resistor:R_0603, mpn: R1, lcsc: N/A, purpose: Loads the output rail.}
nets:
  - {id: field, display_name: +12V FIELD, nodes: [J1.1, J2.1, U1.1]}
  - {id: ground, display_name: GND, nodes: [J1.2, J2.2, U1.2, C1.2, R1.2]}
  - {id: switch, display_name: SWITCH, nodes: [U1.3, L1.1]}
  - {id: output, display_name: +4V, nodes: [L1.2, C1.1, R1.1]}
groups:
  - {id: power, title: Field power and converter support, purpose: Shows field pass-through and local conversion., references: [J1, J2, U1, L1, C1, R1]}
paths:
  - {id: pass-through, title: Field pass-through, purpose: Keeps the field trunk continuous., nodes: [J1.1, J2.1]}
  - {id: conversion, title: Local conversion, purpose: Shows the converter and output filter., nodes: [U1.3, L1.1, L1.2, C1.1]}
"""


class DiagramFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.model_path = self.tmp / "circuit.yaml"
        self.model_path.write_text(MODEL, encoding="utf-8")
        self.output_path = self.tmp / "circuit.svg"

    def make_diagram(self) -> ReviewDiagram:
        return ReviewDiagram(
            model_path=self.model_path,
            output_path=self.output_path,
            title="fixture circuit proposal",
            desc="Review-only diagram for the diagram helper tests.",
        )

    def draw_minimal(self, diagram: ReviewDiagram) -> None:
        elm = diagram.elm
        diagram.section("fixture", (0, 2))
        resistor = elm.Resistor().at((0, 0)).right().label("R1\n10k")
        diagram.component("R1", resistor)
        diagram.drawing += elm.Ground().at(resistor.end)
        diagram.netflag(resistor.start, "rail", "up", 0.6)
        diagram.testpoint(resistor.start, "TP1", "left", 1.0)


class DiagramTests(DiagramFixture):
    def test_save_produces_validator_clean_svg(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        result = diagram.save()

        model = read_circuit_model(self.model_path)
        validate_circuit_svg(self.output_path, model)
        self.assertEqual(result.fingerprint, circuit_model_fingerprint(model))
        self.assertEqual(result.collision_warnings, [])
        self.assertEqual(result.missing_component_symbols, [])
        self.assertEqual(result.warnings, [])
        self.assertNotIn("R1", result.missing_component_labels)

        root = ET.fromstring(self.output_path.read_bytes())
        self.assertEqual(
            root.get("data-pcbforge-model-sha256"),
            circuit_model_fingerprint(model),
        )
        text = " ".join(root.itertext())
        self.assertIn("review-only", text.casefold())
        self.assertIn("Component register", text)
        self.assertIn("Net register", text)
        metadata = next(
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "metadata"
            and element.get("id") == "pcbforge-diagram-audit"
        )
        audit = json.loads(metadata.text or "")
        self.assertEqual(audit["bound_component_refs"], ["R1", "TP1"])

    def test_every_group_needs_a_section(self) -> None:
        diagram = self.make_diagram()
        elm = diagram.elm
        diagram.drawing += elm.Resistor().at((0, 0)).down().label("R1")
        with self.assertRaisesRegex(DiagramError, "fixture"):
            diagram.save()

    def test_unknown_identifiers_fail_fast(self) -> None:
        diagram = self.make_diagram()
        with self.assertRaisesRegex(DiagramError, "unknown group id"):
            diagram.section("nope", (0, 0))
        with self.assertRaisesRegex(DiagramError, "unknown net id"):
            diagram.netflag((0, 0), "nope")
        with self.assertRaisesRegex(DiagramError, "unknown test point"):
            diagram.testpoint((0, 0), "TP9")
        with self.assertRaisesRegex(DiagramError, "unknown component reference"):
            diagram.component("R9", diagram.elm.Resistor())

    def test_note_does_not_satisfy_component_symbol_coverage(self) -> None:
        diagram = self.make_diagram()
        elm = diagram.elm
        diagram.section("fixture", (0, 2))
        resistor = elm.Resistor().at((0, 0)).right().label("10k")
        diagram.drawing += resistor
        diagram.drawing += elm.Ground().at(resistor.end)
        diagram.netflag(resistor.start, "rail", "up", 0.6)
        diagram.testpoint(resistor.start, "TP1", "left", 1.0)
        diagram.note((5, 2), "R1")
        result = diagram.save()
        self.assertNotIn("R1", result.missing_component_labels)
        self.assertEqual(result.missing_component_symbols, ["R1"])
        self.assertIn(
            "missing-component-symbol",
            {warning.code for warning in result.warnings},
        )

    def test_component_binding_allows_repeated_units(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.component(
            "R1", diagram.elm.Resistor().at((5, -3)).right().label("R1 unit B")
        )
        result = diagram.save()
        self.assertEqual(result.missing_component_symbols, [])

    def test_unconnected_mechanical_component_needs_no_symbol(self) -> None:
        self.model_path.write_text(MECHANICAL_MODEL, encoding="utf-8")
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        result = diagram.save()
        self.assertEqual(result.missing_component_symbols, [])

    def test_text_wire_overlap_is_reported(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.drawing += diagram.elm.Line().at((6, 0)).right().length(4)
        diagram.drawing += diagram.elm.Label().at((8, 0)).label("wire label")
        result = diagram.save()
        self.assertIn(
            "text-wire-overlap", {warning.code for warning in result.warnings}
        )

    def test_wire_symbol_overlap_is_reported(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.drawing += diagram.elm.Line().at((0.5, 0)).right().length(1.0)
        result = diagram.save()
        self.assertIn(
            "wire-symbol-overlap", {warning.code for warning in result.warnings}
        )

    def test_wire_crossing_requires_a_junction_dot(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.drawing += diagram.elm.Line().at((6, 0)).right().length(4)
        diagram.drawing += diagram.elm.Line().at((8, 1)).down().length(2)
        result = diagram.save()
        self.assertIn(
            "ambiguous-wire-crossing",
            {warning.code for warning in result.warnings},
        )

    def test_junction_dot_marks_an_intentional_wire_crossing(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.drawing += diagram.elm.Line().at((6, 0)).right().length(4)
        diagram.drawing += diagram.elm.Line().at((8, 1)).down().length(2)
        diagram.drawing += diagram.elm.Dot().at((8, 0))
        result = diagram.save()
        self.assertNotIn(
            "ambiguous-wire-crossing",
            {warning.code for warning in result.warnings},
        )

    def test_overlapping_wire_runs_are_reported(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.drawing += diagram.elm.Line().at((6, 0)).right().length(4)
        diagram.drawing += diagram.elm.Line().at((8, 0)).right().length(4)
        result = diagram.save()
        self.assertIn(
            "overlapping-wire-runs", {warning.code for warning in result.warnings}
        )

    def test_converter_support_regression_has_zero_warnings(self) -> None:
        self.model_path.write_text(REGRESSION_MODEL, encoding="utf-8")
        diagram = self.make_diagram()
        elm = diagram.elm
        diagram.section("power", (0, 9))
        j1 = diagram.component(
            "J1",
            elm.Ic(
                pins=[
                    elm.IcPin(name="+12V", pin="1", side="right", slot="2/2"),
                    elm.IcPin(name="GND", pin="2", side="right", slot="1/2"),
                ],
                size=(2, 2),
                leadlen=0.5,
            )
            .right()
            .at((0, 0))
            .label("J1 FIELD IN"),
        )
        j2 = diagram.component(
            "J2",
            elm.Ic(
                pins=[
                    elm.IcPin(name="+12V", pin="1", side="left", slot="2/2"),
                    elm.IcPin(name="GND", pin="2", side="left", slot="1/2"),
                ],
                size=(2, 2),
                leadlen=0.5,
            )
            .right()
            .at((14, 0))
            .label("J2 FIELD OUT"),
        )
        diagram.drawing += (
            elm.Line().at(j1.absanchors["+12V"]).to(j2.absanchors["+12V"])
        )
        diagram.drawing += elm.Line().at(j1.GND).to(j2.GND)

        u1 = diagram.component(
            "U1",
            elm.Ic(
                pins=[
                    elm.IcPin(name="IN", pin="1", side="left", slot="3/3"),
                    elm.IcPin(name="SW", pin="3", side="right", slot="3/3"),
                    elm.IcPin(name="GND", pin="2", side="bot"),
                ],
                size=(3, 3),
                leadlen=0.5,
            )
            .right()
            .at((4, 4))
            .label("U1 BUCK"),
        )
        branch = (j1.absanchors["+12V"][0] + 0.5, j1.absanchors["+12V"][1])
        diagram.drawing += elm.Dot().at(branch)
        diagram.drawing += elm.Wire("|-").at(branch).to(u1.IN)
        diagram.drawing += elm.Line().at(u1.GND).down().length(0.5)
        diagram.drawing += elm.Ground()

        l1 = diagram.component(
            "L1", elm.Inductor().at(u1.SW).right().length(2).label("L1 47uH")
        )
        rail = elm.Line().at(l1.end).right().length(4)
        diagram.drawing += rail
        c1_at = (l1.end[0] + 1.3, l1.end[1])
        diagram.drawing += elm.Dot().at(c1_at)
        c1 = diagram.component(
            "C1", elm.Capacitor().at(c1_at).down().length(1.4).label("C1 22uF")
        )
        diagram.drawing += elm.Ground().at(c1.end)
        r1_at = (l1.end[0] + 3.0, l1.end[1])
        diagram.drawing += elm.Dot().at(r1_at)
        r1 = diagram.component(
            "R1", elm.Resistor().at(r1_at).down().length(1.4).label("R1 10k")
        )
        diagram.drawing += elm.Ground().at(r1.end)

        result = diagram.save()
        self.assertEqual(result.missing_component_symbols, [])
        self.assertEqual(result.warnings, [])

    def test_collision_lint_flags_overlapping_labels(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        elm = diagram.elm
        diagram.drawing += elm.Label().at((8, 0)).label("first overlapping label")
        diagram.drawing += elm.Label().at((8.2, 0)).label("second overlapping label")
        result = diagram.save()
        self.assertTrue(result.collision_warnings)

    def test_generated_svg_stays_browser_safe(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.save()
        raw = self.output_path.read_text(encoding="utf-8")
        self.assertNotIn("<script", raw)
        self.assertNotIn("javascript:", raw)

    def test_stale_model_is_rejected_by_validator(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        diagram.save()
        changed = MODEL.replace("10 kohm 1%", "22 kohm 1%")
        self.model_path.write_text(changed, encoding="utf-8")
        model = read_circuit_model(self.model_path)
        with self.assertRaisesRegex(
            CircuitReviewInputError, "does not match circuit.yaml"
        ):
            validate_circuit_svg(self.output_path, model)


SCRIPT = '''\
from pathlib import Path

from pcbforge.diagram import ReviewDiagram

PROJECT = Path(__file__).resolve().parents[2]
diagram = ReviewDiagram(
    PROJECT,
    title="fixture circuit proposal",
    desc="Review-only diagram for the render-circuit CLI test.",
)
elm = diagram.elm
diagram.section("fixture", (0, 2))
resistor = diagram.component(
    "R1", elm.Resistor().at((0, 0)).right().label("R1\\n10k")
)
diagram.drawing += elm.Ground().at(resistor.end)
diagram.netflag(resistor.start, "rail", "up", 0.6)
diagram.testpoint(resistor.start, "TP1", "left", 1.0)
result = diagram.save()
print("fingerprint:", result.fingerprint)
'''


class RenderCircuitCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        review = self.project / "review" / "circuit"
        review.mkdir(parents=True)
        (review / "circuit.yaml").write_text(MODEL, encoding="utf-8")
        (self.project / "ato.yaml").write_text(
            "builds:\n  default:\n    entry: src/fixture.ato:Fixture\n",
            encoding="utf-8",
        )
        (self.project / "circuit-review.yaml").write_text(
            "circuit_review_schema: 1\n"
            "build: default\n"
            "model: review/circuit/circuit.yaml\n"
            "diagram: review/circuit/circuit.svg\n"
            "proposal_narrative: docs/circuit-proposal.md\n"
            "final_narrative: docs/circuit-review.md\n",
            encoding="utf-8",
        )
        self.script = review / "circuit_diagram.py"

    def test_render_circuit_runs_script_and_validates(self) -> None:
        from pcbforge.cli import main

        self.script.write_text(SCRIPT, encoding="utf-8")
        self.assertEqual(main(["render-circuit", str(self.project)]), 0)
        output = self.project / "review" / "circuit" / "circuit.svg"
        model = read_circuit_model(
            self.project / "review" / "circuit" / "circuit.yaml"
        )
        validate_circuit_svg(output, model)

    def test_render_circuit_prints_non_blocking_audit_warnings(self) -> None:
        from pcbforge.cli import main

        warning_script = SCRIPT.replace(
            "result = diagram.save()",
            "diagram.drawing += elm.Line().at((6, 0)).right().length(4)\n"
            "diagram.drawing += elm.Label().at((8, 0)).label('wire label')\n"
            "result = diagram.save()",
        )
        self.script.write_text(warning_script, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["render-circuit", str(self.project)])
        self.assertEqual(code, 0)
        self.assertIn("diagram warning [text-wire-overlap]", output.getvalue())

    def test_render_circuit_requires_script(self) -> None:
        from pcbforge.cli import main

        self.assertEqual(main(["render-circuit", str(self.project)]), 2)

    def test_render_circuit_reports_diagram_errors(self) -> None:
        from pcbforge.cli import main

        broken = SCRIPT.replace('diagram.section("fixture", (0, 2))\n', "")
        self.script.write_text(broken, encoding="utf-8")
        self.assertEqual(main(["render-circuit", str(self.project)]), 1)


if __name__ == "__main__":
    unittest.main()
