from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from pcbforge.schematic import (
    SchematicError,
    baseline_is_current,
    capture_implementation_baseline,
    check_schematic,
    parse_kicad_netlist,
)
from pcbforge.status import (
    StatusInputError,
    migrate_schematic_review,
    read_status_document,
)


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


def netlist(*, value: str = "1k", net: str = "+3V3") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R1">
      <value>{value}</value>
      <footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields>
        <field name="MPN">RC0603FR-071KL</field>
        <field name="LCSC">C21190</field>
      </fields>
    </comp>
  </components>
  <nets>
    <net code="1" name="{net}">
      <node ref="R1" pin="1"/>
    </net>
    <net code="2" name="GND">
      <node ref="R1" pin="2"/>
    </net>
  </nets>
</export>
"""


class FakeKiCad:
    def __init__(
        self,
        *,
        value: str = "1k",
        net: str = "+3V3",
        violations: bool = False,
        mutate_board: bool = False,
    ) -> None:
        self.value = value
        self.net = net
        self.violations = violations
        self.mutate_board = mutate_board
        self.calls: list[list[str]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        self.calls.append(command)
        output = Path(command[command.index("--output") + 1])
        if "netlist" in command:
            output.write_text(
                netlist(value=self.value, net=self.net),
                encoding="utf-8",
            )
        elif "erc" in command:
            payload = (
                {"violations": [{"description": "pin is unconnected"}]}
                if self.violations
                else {"violations": []}
            )
            output.write_text(json.dumps(payload), encoding="utf-8")
        elif "svg" in command:
            output.mkdir(parents=True, exist_ok=True)
            (output / "main.svg").write_text(
                "<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n",
                encoding="utf-8",
            )
            if self.mutate_board:
                board = Path(cwd) / "garden-logger.kicad_pcb"
                board.write_text(
                    board.read_text(encoding="utf-8").replace(
                        "(at 110 120 0)",
                        "(at 111 120 0)",
                    ),
                    encoding="utf-8",
                )
        return subprocess.CompletedProcess(
            command,
            1 if self.violations and "erc" in command else 0,
            "",
            "",
        )


class SchematicFixture(unittest.TestCase):
    def project(self, root: Path) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        (project / ".pcbforge").write_text(
            """schema: 12
guidance:
  schematic_review_schema: 1
""",
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
        (project / "garden-logger.kicad_pcb").write_text(
            BOARD,
            encoding="utf-8",
        )
        (project / "docs").mkdir()
        (project / "docs" / "architecture.md").write_text(
            "# architecture\n",
            encoding="utf-8",
        )
        (project / "docs" / "implementation-proposal.md").write_text(
            "# PCBForge review-only proposal\n\nR1 limits current.\n",
            encoding="utf-8",
        )
        (project / "docs" / "implementation-review.md").write_text(
            "# PCBForge review-only final\n\nParity review.\n",
            encoding="utf-8",
        )
        for stage in ("proposal", "final"):
            directory = project / "review" / "implement" / stage
            directory.mkdir(parents=True)
            (directory / "main.kicad_sch").write_text(
                f"(kicad_sch (generator eeschema) (comment \"{stage}\"))\n",
                encoding="utf-8",
            )
        (project / "schematic-review.yaml").write_text(
            """schematic_review_schema: 1
build: default
proposal_root: review/implement/proposal/main.kicad_sch
final_root: review/implement/final/main.kicad_sch
proposal_narrative: docs/implementation-proposal.md
final_narrative: docs/implementation-review.md
""",
            encoding="utf-8",
        )
        bom_dir = project / "build" / "builds" / "default"
        bom_dir.mkdir(parents=True)
        (bom_dir / "default.bom.json").write_text(
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


class SchematicTests(SchematicFixture):
    def test_parser_preserves_identity_and_physical_pin_nets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "netlist.xml"
            path.write_text(netlist(), encoding="utf-8")
            graph = parse_kicad_netlist(path)

        self.assertEqual(graph.components[0].reference, "R1")
        self.assertEqual(graph.components[0].mpn, "RC0603FR-071KL")
        self.assertEqual(graph.components[0].lcsc, "C21190")
        self.assertIn(("+3V3", (("R1", "1"),)), graph.nets)

    def test_proposal_writes_stable_erc_and_svg_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            runner = FakeKiCad()
            first = check_schematic(
                project,
                "proposal",
                tool_root=TOOL_ROOT,
                runner=runner,
                write=True,
            )
            second = check_schematic(
                project,
                "proposal",
                tool_root=TOOL_ROOT,
                runner=runner,
            )
            evidence_exists = (project / first.evidence_path).is_file()
            render_exists = (project / first.render_paths[0]).is_file()

        self.assertTrue(first.wrote)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.components, 1)
        self.assertEqual(first.nets, 2)
        self.assertTrue(evidence_exists)
        self.assertTrue(render_exists)

    def test_source_change_blocks_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            source = project / "src" / "main.ato"
            source.write_text("module App:\n    signal = 1\n", encoding="utf-8")
            current, detail = baseline_is_current(project)
            with self.assertRaisesRegex(
                SchematicError,
                "physical source or board topology changed",
            ):
                check_schematic(
                    project,
                    "proposal",
                    tool_root=TOOL_ROOT,
                    runner=FakeKiCad(),
                    write=True,
                )

        self.assertFalse(current)
        self.assertIn("physical source", detail)

    def test_erc_violation_and_review_pcb_are_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(SchematicError, "ERC has 1"):
                check_schematic(
                    project,
                    "proposal",
                    tool_root=TOOL_ROOT,
                    runner=FakeKiCad(violations=True),
                    write=True,
                )
            (project / "review" / "implement" / "proposal" / "bad.kicad_pcb").write_text(
                "(kicad_pcb)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SchematicError,
                "must not contain a KiCad PCB",
            ):
                check_schematic(
                    project,
                    "proposal",
                    tool_root=TOOL_ROOT,
                    runner=FakeKiCad(),
                    write=True,
                )

    def test_final_requires_proposal_and_compiled_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            check_schematic(
                project,
                "proposal",
                tool_root=TOOL_ROOT,
                runner=FakeKiCad(),
                write=True,
            )
            result = check_schematic(
                project,
                "final",
                tool_root=TOOL_ROOT,
                runner=FakeKiCad(),
                write=True,
            )
            with self.assertRaisesRegex(
                SchematicError,
                "differs electrically or by part identity",
            ):
                check_schematic(
                    project,
                    "final",
                    tool_root=TOOL_ROOT,
                    runner=FakeKiCad(value="2k"),
                    write=True,
                )

        self.assertEqual(result.components, 1)
        self.assertEqual(result.connected_pins, 2)

    def test_schematic_checker_never_allows_product_board_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(
                SchematicError,
                "changed the Atopile-owned PCB",
            ):
                check_schematic(
                    project,
                    "proposal",
                    tool_root=TOOL_ROOT,
                    runner=FakeKiCad(mutate_board=True),
                    write=True,
                )

    def test_schema_eleven_migration_updates_guidance_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            for path in (
                project / "schematic-review.yaml",
                project / "docs" / "implementation-proposal.md",
                project / "docs" / "implementation-review.md",
            ):
                path.unlink()
            (project / ".pcbforge").write_text(
                """schema: 11
project: garden-logger
pcbforge:
  revision: old
  dirty: false
guidance:
  agents_schema: 11
  mcu_schema: 1
  implement_schema: 1
  brief_schema: 1
  approval_schema: 2
  status_schema: 1
""",
                encoding="utf-8",
            )
            (project / "AGENTS.md").write_text(
                "<!-- pcbforge-agents-schema: 11 -->\n# old guidance\n",
                encoding="utf-8",
            )
            migration = migrate_schematic_review(
                project,
                tool_root=TOOL_ROOT,
                now="2026-07-28T18:00:00+00:00",
            )
            second = migrate_schematic_review(project, tool_root=TOOL_ROOT)
            pins = yaml.safe_load(
                (project / ".pcbforge").read_text(encoding="utf-8")
            )
            document = read_status_document(project)

        self.assertTrue(migration.wrote)
        self.assertFalse(second.wrote)
        self.assertEqual(pins["schema"], 12)
        self.assertEqual(pins["guidance"]["agents_schema"], 12)
        self.assertEqual(pins["guidance"]["schematic_review_schema"], 1)
        self.assertEqual(document.events, ())

    def test_migration_requires_explicit_adoption_for_existing_step_five_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / ".pcbforge").write_text(
                """schema: 11
guidance:
  agents_schema: 11
""",
                encoding="utf-8",
            )
            (project / "AGENTS.md").write_text(
                "<!-- pcbforge-agents-schema: 11 -->\n# old guidance\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StatusInputError,
                "--adopt-existing",
            ):
                migrate_schematic_review(project, tool_root=TOOL_ROOT)


if __name__ == "__main__":
    unittest.main()
