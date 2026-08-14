from __future__ import annotations

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
        resistor = elm.Resistor().at((0, 0)).down().label("R1\n10k")
        diagram.drawing += resistor
        diagram.drawing += elm.Ground().at(resistor.end)
        diagram.netflag(resistor.start, "rail", "up", 0.6)
        diagram.testpoint(resistor.start, "TP1", "right", 1.0)


class DiagramTests(DiagramFixture):
    def test_save_produces_validator_clean_svg(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        result = diagram.save()

        model = read_circuit_model(self.model_path)
        validate_circuit_svg(self.output_path, model)
        self.assertEqual(result.fingerprint, circuit_model_fingerprint(model))
        self.assertEqual(result.collision_warnings, [])
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

    def test_collision_lint_flags_overlapping_labels(self) -> None:
        diagram = self.make_diagram()
        self.draw_minimal(diagram)
        elm = diagram.elm
        diagram.drawing += (
            elm.Label().at((8, 0)).label("first overlapping label")
        )
        diagram.drawing += (
            elm.Label().at((8.2, 0)).label("second overlapping label")
        )
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
resistor = elm.Resistor().at((0, 0)).down().label("R1\\n10k")
diagram.drawing += resistor
diagram.drawing += elm.Ground().at(resistor.end)
diagram.netflag(resistor.start, "rail", "up", 0.6)
diagram.testpoint(resistor.start, "TP1", "right", 1.0)
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
