from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.cli import main
from pcbforge.artifact_hash import semantic_pin_bytes
from pcbforge import circuit_review
from pcbforge.circuit_review import (
    CircuitReviewError,
    CircuitReviewInputError,
    capture_implementation_baseline,
    check_circuit_review,
    circuit_model_fingerprint,
    circuit_review_status_fingerprint,
    read_circuit_model,
)
from pcbforge.status import (
    CheckRecord,
    StatusDocument,
    StatusEvent,
    StatusInputError,
    _phase_approval_fingerprint,
    inspect_status,
    read_status_document,
    run_status_checks,
    write_status,
)


from tests.kicad_fake import FakeKicad, audit_text, schematic_text

TOOL_ROOT = Path(__file__).resolve().parents[1]

SPEC = """---
spec_schema: 1
name: garden-logger
layers: 2
stm32_family: L0
power_in: battery-aa
rails: [+3V3]
peripherals: [other]
board_mm: [50, 40]
---
# garden logger
"""

BOARD = """(kicad_pcb
  (version 20240108)
  (generator pcbforge)
  (footprint "Resistor_SMD.pretty:R_0603_1608Metric"
    (layer "F.Cu")
    (at 110 120 0)
    (property "Reference" "R1")
    (property "Value" "1k")
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu")
      (net 1 "+3V3"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu")
      (net 2 "GND")))
)
"""

MODEL = """circuit_model_schema: 1
components:
  - reference: R1
    kind: resistor
    value: 1k
    footprint: Resistor_SMD:R_0603_1608Metric
    mpn: RC0603FR-071KL
    lcsc: C21190
    purpose: Limits current in the reviewed branch.
nets:
  - id: supply
    display_name: +3V3
    compiler_name: +3V3
    nodes: [R1.1]
  - id: ground
    display_name: GND
    compiler_name: GND
    nodes: [R1.2]
groups:
  - id: reviewed-branch
    title: Reviewed branch
    purpose: Shows the complete current-limiting branch.
    references: [R1]
paths:
  - id: current-path
    title: Supply to ground
    purpose: Current crosses R1 from the supply to ground.
    nodes: [R1.1, R1.2]
"""


class CircuitReviewFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.kicad = FakeKicad()
        patcher = mock.patch.object(circuit_review, "KICAD_RUNNER", self.kicad)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_schematic(
        self,
        project: Path,
        model_path: Path | None = None,
        *,
        audit_sha: str | None = None,
        **overrides,
    ) -> str:
        model_path = model_path or project / "review" / "circuit" / "circuit.yaml"
        model_hash = circuit_model_fingerprint(read_circuit_model(model_path))
        sheet = project / "garden-logger.kicad_sch"
        text = schematic_text(model_hash, **overrides)
        sheet.write_text(text, encoding="utf-8")
        sha = audit_sha if audit_sha is not None else hashlib.sha256(text.encode("utf-8")).hexdigest()
        (project / "review" / "circuit" / "schematic.audit.json").write_text(
            audit_text(model_hash, schematic_sha256=sha), encoding="utf-8"
        )
        return model_hash

    def project(self, root: Path) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        guidance = (
            "  agents_schema: 1\n"
            "  architect_schema: 1\n"
            "  architecture_diagram_schema: 1\n"
            "  mcu_schema: 1\n"
            "  circuit_schema: 1\n"
            "  circuit_review_schema: 3\n"
            "  build_test_schema: 1\n"
            "  layout_handoff_schema: 1\n"
            "  approval_schema: 1\n"
            "  policy_schema: 1\n"
            "  status_schema: 1\n"
        )
        (project / ".pcbforge").write_text(
            f"""schema: 1
project: garden-logger
pcbforge:
  revision: old
  dirty: false
guidance:
{guidance}""",
            encoding="utf-8",
        )
        (project / "AGENTS.md").write_text(
            "<!-- pcbforge-agents-schema: 1 -->\n# generated\n",
            encoding="utf-8",
        )
        (project / "ato.yaml").write_text(
            "builds:\n  default:\n    entry: src/main.ato:App\n",
            encoding="utf-8",
        )
        (project / "src").mkdir()
        (project / "src" / "main.ato").write_text(
            "module App:\n    pass\n",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_pcb").write_text(BOARD, encoding="utf-8")
        (project / "garden-logger.kicad_pro").write_text(
            json.dumps(
                {
                    "meta": {"filename": "garden-logger.kicad_pro", "version": 1},
                    "net_settings": {
                        "classes": [{"name": "Default", "clearance": 0.2}],
                        "netclass_patterns": [],
                    },
                    "user_key": {"kept": True},
                }
            ),
            encoding="utf-8",
        )
        (project / "docs").mkdir()
        (project / "docs" / "architecture.md").write_text(
            "# architecture\n",
            encoding="utf-8",
        )
        proposal_name = "circuit-proposal.md"
        final_name = "circuit-review.md"
        (project / "docs" / proposal_name).write_text(
            "# PCBForge review-only proposal\n",
            encoding="utf-8",
        )
        (project / "docs" / final_name).write_text(
            "# PCBForge review-only final review\n",
            encoding="utf-8",
        )
        review_name = "circuit"
        review = project / "review" / review_name
        review.mkdir(parents=True)
        model_path = review / "circuit.yaml"
        model_path.write_text(MODEL, encoding="utf-8")
        self.write_schematic(project, model_path)
        (project / "circuit-review.yaml").write_text(
            f"""circuit_review_schema: 3
build: default
model: review/{review_name}/circuit.yaml
schematic: garden-logger.kicad_sch
proposal_narrative: docs/{proposal_name}
final_narrative: docs/{final_name}
""",
            encoding="utf-8",
        )
        bom = project / "build" / "builds" / "default"
        bom.mkdir(parents=True)
        (bom / "default.bom.json").write_text(
            json.dumps(
                {
                    "components": [
                        {
                            "lcsc": "C21190",
                            "mpn": "RC0603FR-071KL",
                            "value": "1k",
                            "package": "Resistor_SMD.pretty:R_0603_1608Metric",
                            "usages": [{"designator": "R1"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        capture_implementation_baseline(project)
        return project


class CircuitReviewTests(CircuitReviewFixture):
    def test_proposal_writes_stable_authored_schematic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            first = check_circuit_review(project, "proposal", write=True)
            second = check_circuit_review(project, "proposal")
            evidence = json.loads(
                (project / first.evidence_path).read_text(encoding="utf-8")
            )
            baseline = json.loads(
                (project / "review" / "circuit" / "source-baseline.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertTrue(first.wrote)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.components, 1)
        self.assertEqual(first.diagram_warnings, ())
        self.assertEqual(evidence["schematic_audit"]["warnings"], [])
        self.assertEqual(evidence["schematic"], "garden-logger.kicad_sch")
        self.assertEqual(evidence["material_differences"], [])
        self.assertNotIn("erc", evidence)
        self.assertEqual(baseline["source_baseline_schema"], 1)

    def test_schematic_must_match_the_exact_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            schematic = project / "garden-logger.kicad_sch"
            schematic.write_text(schematic_text("0" * 64), encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "fingerprint is missing or stale"):
                check_circuit_review(project, "proposal", write=True)
            model_hash = self.write_schematic(project, marker=False)
            with self.assertRaisesRegex(CircuitReviewInputError, "review-only"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project, value="2k2")
            with self.assertRaisesRegex(CircuitReviewError, "value '2k2'"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project, group_title="Wrong title")
            with self.assertRaisesRegex(CircuitReviewError, "group titles missing"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project, version="20231120")
            with self.assertRaisesRegex(CircuitReviewInputError, "format version"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project, project_name="other")
            with self.assertRaisesRegex(CircuitReviewInputError, "instances belong to project"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project, audit_sha="f" * 64)
            with self.assertRaisesRegex(CircuitReviewInputError, "modified outside"):
                check_circuit_review(project, "proposal", write=True)
            self.write_schematic(project)
            check_circuit_review(project, "proposal", write=True)
            self.assertTrue(any(call[1:3] == ["sch", "erc"] for call in self.kicad.calls))
            self.assertTrue(
                any(call[1:4] == ["sch", "export", "netlist"] for call in self.kicad.calls)
            )
        self.assertEqual(len(model_hash), 64)

    def test_erc_errors_and_netlist_parity_block_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.kicad.erc = ["Pin not connected"]
            with self.assertRaisesRegex(CircuitReviewError, "(?s)ERC errors.*Pin not connected"):
                check_circuit_review(project, "proposal", write=True)
            self.kicad.erc = []
            self.kicad.nets = {"+3V3": ["R1.1", "R1.2"]}
            with self.assertRaisesRegex(CircuitReviewError, "missing proposed endpoint sets: ground, supply"):
                check_circuit_review(project, "proposal", write=True)
            self.kicad.nets = {"+3V3": ["R1.1"], "GND": ["R1.2"], "EXTRA": ["R1.3"]}
            with self.assertRaisesRegex(CircuitReviewError, "unproposed endpoint sets: EXTRA"):
                check_circuit_review(project, "proposal", write=True)
            self.kicad.nets = {"+3V3": ["R1.1"], "GND": ["R1.2"]}
            self.kicad.fail = True
            with self.assertRaisesRegex(CircuitReviewInputError, "could not run ERC"):
                check_circuit_review(project, "proposal", write=True)

    def test_multi_node_net_names_must_match_display_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            model_path = project / "review" / "circuit" / "circuit.yaml"
            model_path.write_text(
                MODEL.replace("nodes: [R1.1]", "nodes: [R1.1, R1.2]").replace(
                    "  - id: ground\n    display_name: GND\n    compiler_name: GND\n    nodes: [R1.2]\n",
                    "",
                ),
                encoding="utf-8",
            )
            self.write_schematic(project)
            capture_implementation_baseline(project)
            self.kicad.nets = {"WRONG": ["R1.1", "R1.2"]}
            with self.assertRaisesRegex(CircuitReviewError, "named 'WRONG', expected '\\+3V3'"):
                check_circuit_review(project, "proposal", write=True)
            self.kicad.nets = {"Net-(R1-Pad1)": ["R1.1", "R1.2"]}
            result = check_circuit_review(project, "proposal", write=True)
            self.assertIn("[unlabeled-net] net supply", result.diagram_warnings[0])
            self.kicad.nets = {"+3V3": ["R1.1", "R1.2"]}
            result = check_circuit_review(project, "proposal", write=True)
            self.assertEqual(result.diagram_warnings, ())

    def test_audit_sidecar_is_required_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            audit = project / "review" / "circuit" / "schematic.audit.json"
            model_hash = circuit_model_fingerprint(
                read_circuit_model(project / "review" / "circuit" / "circuit.yaml")
            )
            sheet_sha = hashlib.sha256((project / "garden-logger.kicad_sch").read_bytes()).hexdigest()
            audit.unlink()
            with self.assertRaisesRegex(CircuitReviewInputError, "missing schematic.audit.json"):
                check_circuit_review(project, "proposal", write=True)
            audit.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "invalid JSON"):
                check_circuit_review(project, "proposal", write=True)
            audit.write_text(audit_text("1" * 64), encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "audit is stale"):
                check_circuit_review(project, "proposal", write=True)
            audit.write_text(audit_text(model_hash, bound=(), schematic_sha256=sheet_sha), encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "bound_component_refs"):
                check_circuit_review(project, "proposal", write=True)
            audit.write_text(
                audit_text(model_hash, warnings=[{"code": "text-text-overlap", "message": "a vs b"}], schematic_sha256=sheet_sha),
                encoding="utf-8",
            )
            result = check_circuit_review(project, "proposal", write=True)
            evidence = json.loads((project / result.evidence_path).read_text(encoding="utf-8"))

        self.assertEqual(result.diagram_warnings, ("[text-text-overlap] a vs b",))
        self.assertEqual(evidence["schematic_audit"]["warnings"][0]["code"], "text-text-overlap")

    def test_review_directory_must_not_hold_a_kicad_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "review" / "circuit" / "circuit.kicad_pro").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "must not contain a KiCad project"):
                check_circuit_review(project, "proposal", write=True)

    def test_schema_one_contract_explains_the_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "circuit-review.yaml").write_text(
                "circuit_review_schema: 1\nbuild: default\nmodel: review/circuit/circuit.yaml\n"
                "diagram: review/circuit/circuit.svg\nproposal_narrative: docs/circuit-proposal.md\n"
                "final_narrative: docs/circuit-review.md\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CircuitReviewInputError, "schema 1 is no longer supported"):
                check_circuit_review(project, "proposal", write=True)
            (project / "circuit-review.yaml").write_text(
                "circuit_review_schema: 3\nbuild: default\nmodel: review/circuit/circuit.yaml\n"
                "schematic: review/circuit/circuit.kicad_sch\nproposal_narrative: docs/circuit-proposal.md\n"
                "final_narrative: docs/circuit-review.md\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CircuitReviewInputError, "expected garden-logger.kicad_sch in the project root"):
                check_circuit_review(project, "proposal", write=True)

    def test_board_linked_to_a_schematic_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    '(property "Reference" "R1")',
                    '(property "Reference" "R1")\n    (path "/00000000-0000-0000-0000-000000000001/abc")\n'
                    '    (property "Sheetfile" "garden-logger.kicad_sch")',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CircuitReviewInputError, "footprints R1 carry schematic links"):
                check_circuit_review(project, "proposal", write=True)

    def test_model_rejects_broken_paths_and_duplicate_pin_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "circuit.yaml"
            path.write_text(
                MODEL.replace("nodes: [R1.2]", "nodes: [R1.1]"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CircuitReviewInputError,
                "already assigned",
            ):
                read_circuit_model(path)
            path.write_text(MODEL + "components: []\n", encoding="utf-8")
            with self.assertRaisesRegex(CircuitReviewInputError, "duplicate key"):
                read_circuit_model(path)

    def test_source_change_blocks_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "src" / "main.ato").write_text(
                "module App:\n    signal = 1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CircuitReviewError,
                "physical source or board topology changed",
            ):
                check_circuit_review(project, "proposal", write=True)

    def test_build_test_assertion_preserves_proposal_and_final_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            proposal = check_circuit_review(project, "proposal", write=True)
            final = check_circuit_review(project, "final", write=True)
            final_evidence = (
                project / final.evidence_path
            ).read_text(encoding="utf-8")
            source = project / "src" / "main.ato"
            source.write_text(
                source.read_text(encoding="utf-8")
                + """    # pcbforge-test: rail-3v3-tolerance
    assert 3.3V within 3.3V +/- 5%
""",
                encoding="utf-8",
            )

            proposal_after = check_circuit_review(project, "proposal")
            final_after = check_circuit_review(project, "final")
            final_evidence_after = (
                project / final.evidence_path
            ).read_text(encoding="utf-8")

        self.assertEqual(proposal.fingerprint, proposal_after.fingerprint)
        self.assertEqual(final.fingerprint, final_after.fingerprint)
        self.assertEqual(final_evidence, final_evidence_after)

    def test_final_compares_model_directly_with_bom_and_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            check_circuit_review(project, "proposal", write=True)
            result = check_circuit_review(project, "final", write=True)
            bom = project / "build" / "builds" / "default" / "default.bom.json"
            payload = json.loads(bom.read_text(encoding="utf-8"))
            payload["components"][0]["value"] = "2k"
            bom.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                CircuitReviewError,
                "proposed value '1k', compiled '2k'",
            ):
                check_circuit_review(project, "final", write=True)

        self.assertEqual(result.connected_pins, 2)

    def test_final_accepts_unfitted_pcb_features_and_valueless_atomic_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "\n)\n",
                    """
  (footprint "TestPoint.pretty:TestPoint_Pad_D1.5mm"
    (layer "F.Cu")
    (at 0 0 0)
    (property "Reference" "TP1")
    (property "Value" "TestPoint")
    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8)
      (layers "*.Cu" "*.Mask")
      (net 3 "SENSE")))
)
""",
                ),
                encoding="utf-8",
            )
            model_path = project / "review" / "circuit" / "circuit.yaml"
            model_path.write_text(
                MODEL.replace(
                    "nets:\n",
                    """  - reference: TP1
    kind: test-point
    value: 1.5mm test pad
    footprint: TestPoint:TestPoint_Pad_D1.5mm
    mpn: TestPoint_Pad_D1.5mm
    lcsc: N/A
    purpose: Exposes the reviewed sense node.
nets:
""",
                )
                .replace(
                    "groups:\n",
                    """  - id: sense
    display_name: SENSE
    compiler_name: SENSE
    nodes: [TP1.1]
groups:
""",
                )
                .replace(
                    "references: [R1]",
                    "references: [R1, TP1]",
                ),
                encoding="utf-8",
            )
            model_hash = self.write_schematic(project, model_path)
            schematic = project / "garden-logger.kicad_sch"
            schematic.write_text(
                schematic.read_text(encoding="utf-8").replace(
                    "  (sheet_instances",
                    """  (symbol (lib_id "Connector:TestPoint") (at 70 50 0) (unit 1) (uuid "s2")
    (property "Reference" "TP1" (at 72 50 0) (effects (font (size 1.27 1.27))))
    (property "Value" "1.5mm test pad" (at 72 52 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "TestPoint:TestPoint_Pad_D1.5mm" (at 70 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "pcbforge_group" "reviewed-branch" (at 70 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "pcbforge_purpose" "Exposes the reviewed sense node." (at 70 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
  )
  (sheet_instances""",
                ),
                encoding="utf-8",
            )
            (project / "review" / "circuit" / "schematic.audit.json").write_text(
                audit_text(
                    model_hash,
                    bound=("R1", "TP1"),
                    schematic_sha256=hashlib.sha256(schematic.read_bytes()).hexdigest(),
                ),
                encoding="utf-8",
            )
            self.kicad.nets = {"+3V3": ["R1.1"], "GND": ["R1.2"], "SENSE": ["TP1.1"]}
            bom = project / "build" / "builds" / "default" / "default.bom.json"
            payload = json.loads(bom.read_text(encoding="utf-8"))
            payload["components"][0]["value"] = ""
            bom.write_text(json.dumps(payload), encoding="utf-8")
            capture_implementation_baseline(project)
            check_circuit_review(project, "proposal", write=True)
            result = check_circuit_review(project, "final", write=True)

        self.assertEqual(result.components, 2)
        self.assertEqual(result.connected_pins, 3)

    def test_spatial_board_edit_does_not_stale_circuit_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            bom = project / "build" / "builds" / "default" / "default.bom.json"
            payload = json.loads(bom.read_text(encoding="utf-8"))
            payload["build_id"] = "first-build"
            bom.write_text(json.dumps(payload), encoding="utf-8")
            check_circuit_review(project, "proposal", write=True)
            first = check_circuit_review(project, "final", write=True)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "(at 110 120 0)",
                    "(at 114 125 90)",
                ),
                encoding="utf-8",
            )
            payload["build_id"] = "second-build"
            bom.write_text(json.dumps(payload), encoding="utf-8")
            second = check_circuit_review(project, "final")

        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_cli_reports_circuit_review_result_and_input_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.assertEqual(
                main(
                    [
                        "check-circuit-review",
                        str(project),
                        "--stage",
                        "proposal",
                        "--write",
                    ]
                ),
                0,
            )
            (project / "circuit-review.yaml").write_text(
                "circuit_review_schema: 1\n",
                encoding="utf-8",
            )
            self.assertEqual(
                main(
                    [
                        "check-circuit-review",
                        str(project),
                        "--stage",
                        "proposal",
                    ]
                ),
                2,
            )

    def test_status_records_current_circuit_check_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))

            def runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 1, "", "not available")

            checked = run_status_checks(
                project,
                StatusDocument("", (), {}),
                runner=runner,
                write_reports=True,
            )

        self.assertEqual(checked.checks["circuit-proposal"].outcome, "pass")
        self.assertEqual(checked.checks["circuit-final"].outcome, "pass")

    def test_pinned_tool_revision_does_not_invalidate_gate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary)).resolve()
            pin = project / ".pcbforge"
            original = pin.read_text(encoding="utf-8")
            before = circuit_review_status_fingerprint(project, "proposal")

            pin.write_text(
                original.replace("revision: old", "revision: new"),
                encoding="utf-8",
            )
            after_revision = circuit_review_status_fingerprint(project, "proposal")

            pin.write_text(
                original.replace("  dirty: false", "  dirty: true"),
                encoding="utf-8",
            )
            after_dirty = circuit_review_status_fingerprint(project, "proposal")

            pin.write_text(
                original.replace("  circuit_schema: 1", "  circuit_schema: 2"),
                encoding="utf-8",
            )
            after_guidance = circuit_review_status_fingerprint(project, "proposal")

        self.assertEqual(before, after_revision)
        self.assertEqual(before, after_dirty)
        self.assertNotEqual(before, after_guidance)

    def test_malformed_pin_still_binds_its_raw_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pin = project / ".pcbforge"
            pin.write_text("schema: [1\n", encoding="utf-8")
            first = semantic_pin_bytes(pin)
            pin.write_text("schema: [2\n", encoding="utf-8")
            second = semantic_pin_bytes(pin)

        self.assertEqual(first, b"schema: [1\n")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
