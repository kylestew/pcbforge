from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.cli import main
from pcbforge.initialize import (
    InitError,
    InitInputError,
    InitResult,
    initialize_project,
    read_spec,
)
from pcbforge.policy import render_default_policy
from pcbforge.status import (
    StatusError,
    StatusInputError,
    approve_phase,
    mark_policy,
    read_status_document,
    review_phase,
    write_status,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]

ARCHITECTURE_FILES = {
    "docs/architecture.md": """<!-- pcbforge-architecture-diagram-schema: 2 -->
# garden-logger architecture

> Architecture only: functional modules and typed interfaces. No parts, values,
> footprints, MCU pins, CubeMX configuration, placement, or routing.

## Functional graph

```mermaid
flowchart LR
    power["Power tree"]:::project_local
    mcu{{"Generic MCU"}}:::mcu
    external_io(["External I/O"]):::external

    power -->|+3V3 supply| mcu
    mcu <-->|USB full-speed| external_io
    mcu <-->|sensor I2C| external_io
    mcu ---|ADC input| external_io
    mcu <-->|debug UART| external_io
    mcu <-->|programming SWD| external_io

    classDef project_local fill:#d9ecff,stroke:#23618f,color:#111
    classDef mcu fill:#eadcff,stroke:#67428f,color:#111
    classDef external fill:#f1f1f1,stroke:#555,color:#111,stroke-dasharray: 4 2
```

## Legend

- Rectangle: project-local module
- Hexagon: generic MCU boundary
- Rounded dashed node: external boundary
""",

}


def spec_text(
    *,
    name: str = "garden-logger",
    layers: int = 2,
    extra: str = "",
) -> str:
    return f"""---
spec_schema: 1
name: {name}
layers: {layers}
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: [usb-fs, i2c, adc]
board_mm: [50, 40]
{extra}---
# Content deliberately ignored by init

unknown_body_key: true
"""


class FakeRunner:
    def __init__(
        self,
        *,
        fail_build: bool = False,
        dirty_checkout: bool = False,
    ) -> None:
        self.fail_build = fail_build
        self.dirty_checkout = dirty_checkout
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        cwd = Path(cwd)
        self.calls.append((command, cwd))
        if command[-1:] == ["self-check"]:
            return subprocess.CompletedProcess(command, 0, "0.15.7\n", "")
        if command[-1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, "10.0.3\n", "")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[-2:] == ["status", "--short"]:
            output = " M pcbforge/status.py\n" if self.dirty_checkout else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if "export" in command:
            if self.fail_build:
                return subprocess.CompletedProcess(command, 1, "", "induced failure")
            if not list(cwd.glob("*.kicad_sch")) or not list(cwd.glob("*.kicad_pcb")):
                return subprocess.CompletedProcess(command, 1, "", "scaffold missing")
            return subprocess.CompletedProcess(command, 0, "native export passed", "")
        raise AssertionError(f"unexpected command: {command}")


class SpecTests(unittest.TestCase):
    def test_valid_spec_applies_defaults_and_ignores_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spec.md"
            path.write_text(spec_text(), encoding="utf-8")
            spec = read_spec(path)

        self.assertEqual(spec.name, "garden-logger")
        self.assertEqual(spec.layers, 2)
        self.assertEqual(spec.board_mm, (50.0, 40.0))
        self.assertEqual(spec.qty, 5)
        self.assertTrue(spec.debug_uart)
        self.assertEqual(spec.connectors, ())

    def test_optional_fields_are_normalized(self) -> None:
        extra = """connectors: [usb-c, qwiic]
mounting: 4x M3
qty: 10
bom_ceiling_usd: 8
modules_planned: [power-tree]
debug_uart: false
special: [low-power]
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spec.md"
            path.write_text(spec_text(extra=extra), encoding="utf-8")
            spec = read_spec(path)

        self.assertEqual(spec.connectors, ("usb-c", "qwiic"))
        self.assertEqual(spec.mounting, "4x M3")
        self.assertEqual(spec.qty, 10)
        self.assertEqual(spec.bom_ceiling_usd, 8.0)
        self.assertFalse(spec.debug_uart)
        self.assertEqual(spec.special, ("low-power",))

    def test_reports_multiple_schema_errors(self) -> None:
        invalid = """---
spec_schema: true
name: Bad Name
layers: 3
stm32_family: X9
power_in: mains
rails: []
peripherals: [ethernet]
board_mm: [true, -1]
surprise: value
---
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spec.md"
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaises(InitInputError) as raised:
                read_spec(path)

        message = str(raised.exception)
        self.assertIn("unknown keys", message)
        self.assertIn("spec_schema", message)
        self.assertIn("name:", message)
        self.assertIn("layers:", message)
        self.assertIn("stm32_family:", message)
        self.assertIn("power_in:", message)
        self.assertIn("rails:", message)
        self.assertIn("peripherals[0]", message)
        self.assertIn("board_mm:", message)

    def test_rejects_duplicate_keys(self) -> None:
        duplicate = spec_text().replace("layers: 2", "layers: 2\nlayers: 4")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spec.md"
            path.write_text(duplicate, encoding="utf-8")
            with self.assertRaisesRegex(InitInputError, "duplicate key"):
                read_spec(path)

    def test_rejects_missing_delimiters_and_non_mapping_yaml(self) -> None:
        cases = {
            "no opening": "name: board\n",
            "no closing": "---\nname: board\n",
            "not mapping": "---\n- one\n- two\n---\n",
        }
        for label, contents in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "spec.md"
                path.write_text(contents, encoding="utf-8")
                with self.assertRaises(InitInputError):
                    read_spec(path)


class InitializeTests(unittest.TestCase):
    def _project(
        self,
        root: Path,
        name: str,
        *,
        layers: int = 2,
        approved: bool = True,
    ) -> Path:
        project = root / name
        project.mkdir()
        (project / "spec.md").write_text(
            spec_text(name=name, layers=layers),
            encoding="utf-8",
        )
        (project / "policy.yaml").write_text(
            render_default_policy(),
            encoding="utf-8",
        )
        if approved:
            review = review_phase(project, "spec", tool_root=TOOL_ROOT)
            approve_phase(
                project,
                "spec",
                review.fingerprint,
                "Requirements explicitly approved by user",
                tool_root=TOOL_ROOT,
            )
        return project

    def test_generates_native_two_layer_scaffold_and_runs_smoke_exports(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            result = initialize_project(project, tool_root=TOOL_ROOT, runner=runner)
            self.assertEqual(result, InitResult("garden-logger", project.resolve()))
            for name in ["garden-logger.kicad_sch", "garden-logger.kicad_pcb", "garden-logger.kicad_pro", "circuit-tests.yaml", "electrical-facts.yaml", "circuit-review.yaml", "STATUS.md", "AGENTS.md"]:
                self.assertTrue((project/name).is_file(),name)
            self.assertFalse((project/"ato.yaml").exists());self.assertFalse((project/"src/main.ato").exists())
            board=(project/"garden-logger.kicad_pcb").read_text()
            for text in ['(version 20260206)', '(0 "F.Cu" signal)', '(2 "B.Cu" signal)', '(start 100 100)', '(end 150 140)']:
                self.assertIn(text,board)
            pins=yaml.safe_load((project/".pcbforge").read_text())
            self.assertEqual(pins["schema"],2);self.assertEqual(pins["toolchain"]["kicad"],"10.0.3");self.assertNotIn("atopile",pins["toolchain"])
            self.assertEqual(pins["guidance"]["circuit_review_schema"],4)
            data=json.loads((project/"garden-logger.kicad_pro").read_text())
            self.assertEqual(data["board"]["design_settings"]["rules"]["min_clearance"],.2)
            self.assertEqual(len(data["sheets"]),1)
            self.assertIn("pcbforge-agents-schema: 2",(project/"AGENTS.md").read_text())
        self.assertEqual(len([c for c,_ in runner.calls if "export" in c]),2)

    def test_generates_four_copper_layers_and_four_layer_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "sensor-hub", layers=4)
            initialize_project(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
            board = (project / "sensor-hub.kicad_pcb").read_text(encoding="utf-8")
            self.assertIn('(4 "In1.Cu" power)', board)
            self.assertIn('(6 "In2.Cu" power)', board)
            pins = (project / ".pcbforge").read_text(encoding="utf-8")
            self.assertIn("jlc-4layer-conservative-v1", pins)

    def test_refuses_dirty_tool_checkout_without_writing_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            before = {
                path.relative_to(project): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }
            with self.assertRaisesRegex(InitError, "checkout is dirty"):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(dirty_checkout=True),
                )
            after = {
                path.relative_to(project): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file()
            }

        self.assertEqual(before, after)

    def test_rejects_directory_name_mismatch_without_writing(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "wrong-name"
            project.mkdir()
            (project / "spec.md").write_text(spec_text(), encoding="utf-8")
            with self.assertRaisesRegex(InitInputError, "must match"):
                initialize_project(project, tool_root=TOOL_ROOT, runner=runner)
            self.assertEqual([path.name for path in project.iterdir()], ["spec.md"])
        self.assertEqual(runner.calls, [])

    def test_refuses_initialized_or_conflicting_project_without_overwrite(self) -> None:
        for conflict in (".pcbforge", "AGENTS.md", "circuit-tests.yaml"):
            with (
                self.subTest(conflict=conflict),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = self._project(Path(temporary), "garden-logger")
                path = project / conflict
                if conflict == "src":
                    path.mkdir()
                else:
                    path.write_text("user data\n", encoding="utf-8")
                with self.assertRaises(InitInputError):
                    initialize_project(
                        project,
                        tool_root=TOOL_ROOT,
                        runner=FakeRunner(),
                    )
                if path.is_file():
                    self.assertEqual(path.read_text(encoding="utf-8"), "user data\n")

    def test_requires_current_spec_approval_before_init(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(
                Path(temporary),
                "garden-logger",
                approved=False,
            )

            with self.assertRaisesRegex(
                InitInputError,
                "SPEC does not have current artifact-bound explicit user approval",
            ):
                initialize_project(project, tool_root=TOOL_ROOT, runner=runner)

            self.assertEqual(
                sorted(path.name for path in project.iterdir()),
                ["policy.yaml", "spec.md"],
            )
        self.assertEqual(runner.calls, [])

    def test_missing_policy_blocks_spec_approval_and_initialization(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(
                Path(temporary),
                "garden-logger",
                approved=False,
            )
            (project / "policy.yaml").unlink()

            with self.assertRaisesRegex(
                StatusInputError,
                "missing policy.yaml",
            ):
                review = review_phase(project, "spec", tool_root=TOOL_ROOT)
                approve_phase(
                    project,
                    "spec",
                    review.fingerprint,
                    "User approved requirements",
                    tool_root=TOOL_ROOT,
                )
            with self.assertRaisesRegex(
                InitInputError,
                "explicit user approval",
            ):
                initialize_project(project, tool_root=TOOL_ROOT, runner=runner)

        self.assertEqual(runner.calls, [])

    def test_rejects_spec_approval_without_a_fingerprint_before_init(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            dashboard = (project / "STATUS.md").read_text(encoding="utf-8")
            dashboard = re.sub(
                r"(?m)^\s+approval_fingerprint: .+\n",
                "",
                dashboard,
            )
            (project / "STATUS.md").write_text(dashboard, encoding="utf-8")

            with self.assertRaisesRegex(
                InitInputError,
                "artifact-bound explicit user approval",
            ):
                initialize_project(project, tool_root=TOOL_ROOT, runner=runner)

        self.assertEqual(runner.calls, [])

    def test_native_export_failure_leaves_project_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "garden-logger")
            dashboard_before = (project / "STATUS.md").read_bytes()
            with self.assertRaisesRegex(InitError, "smoke test"):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(fail_build=True),
                )
            self.assertEqual(
                sorted(path.name for path in project.iterdir()),
                ["STATUS.md", "policy.yaml", "spec.md"],
            )
            self.assertEqual((project / "STATUS.md").read_bytes(), dashboard_before)
            self.assertEqual(list(root.glob(".garden-logger.pcbforge-init-*")), [])

    def test_preserves_pre_init_dashboard_and_refreshes_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(
                Path(temporary),
                "garden-logger",
                approved=False,
            )
            write_status(project, now="2026-07-26T10:00:00+00:00")
            review = review_phase(project, "spec", tool_root=TOOL_ROOT)
            approve_phase(
                project,
                "spec",
                review.fingerprint,
                "Requirements approved",
                tool_root=TOOL_ROOT,
                now="2026-07-26T11:00:00+00:00",
            )

            initialize_project(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

            report = read_status_document(project)
            self.assertEqual(len(report.events), 1)
            self.assertEqual(report.events[0].phase, "spec")
            dashboard = (project / "STATUS.md").read_text(encoding="utf-8")
            self.assertIn("1 of 6 required phases complete", dashboard)
            self.assertIn("**Current:** 2. ARCHITECT — Awaiting approval", dashboard)
            self.assertIn(
                "**Just completed:** SPEC → ARCHITECT: initialize",
                dashboard,
            )
            self.assertIn(
                "SPEC → ARCHITECT: initialize | Tool | ✅ Complete",
                dashboard,
            )

    def test_init_honors_approved_preproject_spec_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(
                Path(temporary),
                "garden-logger",
                approved=False,
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["manufacturing"]["thickness_mm"] = 0.8
            policy["exceptions"] = [
                {
                    "id": "allow-0-8-mm-fr4",
                    "rule": "manufacturing.thickness",
                    "scope": "project",
                    "rationale": "Improve through-board light transmission.",
                }
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            write_status(project)
            mark_policy(
                project,
                "exception-approved",
                "User approved 0.8 mm FR4 for the optical experiment.",
                subject="allow-0-8-mm-fr4",
                tool_root=TOOL_ROOT,
            )
            review = review_phase(project, "spec", tool_root=TOOL_ROOT)
            approve_phase(
                project,
                "spec",
                review.fingerprint,
                "Requirements approved",
                tool_root=TOOL_ROOT,
            )

            initialize_project(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

            self.assertTrue((project / ".pcbforge").is_file())
            document = read_status_document(project)
            self.assertEqual(
                document.policy_events[-1].subject,
                "allow-0-8-mm-fr4",
            )

    def test_failed_init_does_not_mutate_pre_init_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            before = (project / "STATUS.md").read_bytes()

            with self.assertRaisesRegex(InitError, "smoke test"):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(fail_build=True),
                )

            self.assertEqual((project / "STATUS.md").read_bytes(), before)

    def test_status_failure_rolls_back_committed_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            dashboard_before = (project / "STATUS.md").read_bytes()

            with (
                mock.patch(
                    "pcbforge.status.write_status",
                    side_effect=StatusError("simulated dashboard failure"),
                ),
                self.assertRaisesRegex(InitError, "scaffold rolled back"),
            ):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

            self.assertEqual(
                sorted(path.name for path in project.iterdir()),
                ["STATUS.md", "policy.yaml", "spec.md"],
            )
            self.assertEqual(
                (project / "STATUS.md").read_bytes(),
                dashboard_before,
            )

    def test_invalid_pre_init_dashboard_blocks_before_scaffolding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            (project / "STATUS.md").write_text(
                """---
pcbforge_status_schema: 99
events: []
checks: {}
---
""",
                encoding="utf-8",
            )
            before = sorted(path.name for path in project.iterdir())

            with self.assertRaisesRegex(InitInputError, "STATUS.md"):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

            self.assertEqual(sorted(path.name for path in project.iterdir()), before)


class CliTests(unittest.TestCase):
    def test_cli_success(self) -> None:
        result = InitResult("garden-logger", Path("/tmp/garden-logger"))
        with (
            mock.patch("pcbforge.cli.initialize_project", return_value=result),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(main(["init", "/tmp/garden-logger"]), 0)
        self.assertIn("initialized garden-logger", output.call_args_list[0].args[0])

    def test_cli_defaults_to_current_directory(self) -> None:
        result = InitResult("garden-logger", Path("/tmp/garden-logger"))
        with (
            mock.patch(
                "pcbforge.cli.initialize_project",
                return_value=result,
            ) as initialize,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["init"]), 0)
        initialize.assert_called_once_with(Path("."))

    def test_cli_input_and_runtime_exit_codes(self) -> None:
        for exception, expected in (
            (InitInputError("bad spec"), 2),
            (InitError("compiler failed"), 1),
        ):
            with (
                self.subTest(exception=exception),
                mock.patch("pcbforge.cli.initialize_project", side_effect=exception),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(main(["init"]), expected)


class GuidanceTests(unittest.TestCase):
    def test_spec_interview_presents_baseline_design_bias_questions(self) -> None:
        playbook = " ".join(
            (TOOL_ROOT / "agent" / "spec-interview.md")
            .read_text(encoding="utf-8")
            .split()
        )
        for required in (
            "present the three baseline",
            "When goals conflict",
            "Do you want a BOM bias?",
            "How much optimization risk do you accept?",
            "fewer total components",
            "fewer unique BOM lines",
            "fewer JLC extended parts",
            "## Design priorities",
        ):
            self.assertIn(required, playbook)

    def test_architect_playbook_and_empty_catalog_are_explicit(self) -> None:
        playbook = (TOOL_ROOT / "agent" / "architect.md").read_text(encoding="utf-8")
        for required in ("pcbforge-architect-schema: 2", "docs/architecture.md", "functional blocks", "finish-architect", "architecture-baseline.json"):
            self.assertIn(required, playbook)
        mcu = (TOOL_ROOT / "agent/mcu.md").read_text()
        for required in ("firmware/<project>.ioc", "check-ioc", "one-to-one audit", "electrical-facts.yaml"):
            self.assertIn(required, mcu)
        circuit = (TOOL_ROOT / "agent/circuit.md").read_text()
        for required in ("Device:R", "Resistor_SMD:R_0603_1608Metric", "check-circuit", "prepare-pcb-update", "finish-circuit"):
            self.assertIn(required, circuit)
        catalog = (TOOL_ROOT / "modules/index.md").read_text()
        self.assertIn("native", catalog)
        self.assertIn("No", catalog)

    def test_approval_commands_live_only_in_the_operating_manual(self) -> None:
        manual = (TOOL_ROOT / "agent" / "operating-manual.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "status review <phase>",
            "status approve <phase> --last-reviewed",
            "status review architect --stage proposal",
            "CIRCUIT uses a single final schematic review",
            "status review layout --stage handoff",
            "placement brief",
            "status review --cascade",
            "status renew --last-reviewed",
        ):
            self.assertIn(required, manual)

        for name in (
            "architect.md",
            "electrical-tests.md",
            "circuit.md",
            "layout-handoff.md",
            "spec-interview.md",
        ):
            playbook = (TOOL_ROOT / "agent" / name).read_text(encoding="utf-8")
            self.assertIn("standard review and approval protocol", playbook)
            self.assertNotIn("status approve", playbook)

    def test_workflow_document_is_the_single_normative_phase_map(self) -> None:
        workflow = (TOOL_ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
        readme = (TOOL_ROOT / "README.md").read_text(encoding="utf-8")
        design = (TOOL_ROOT / "DESIGN.md").read_text(encoding="utf-8")

        self.assertIn("normative process map", workflow)
        self.assertIn("seven numbered phases, six required", workflow)
        self.assertIn("seven required human decisions", workflow)
        self.assertIn("[WORKFLOW.md](WORKFLOW.md)", readme)
        self.assertNotIn("## 1. SPEC", readme)
        self.assertIn("saved KiCad schematic", design)
        self.assertNotIn("\n## Workflow\n", design)
        self.assertNotIn("### Phase ", design)

    def test_architecture_fixture_describes_functional_interfaces(self):
        diagram = ARCHITECTURE_FILES["docs/architecture.md"]
        for interface in ("+3V3", "USB", "I2C", "ADC", "UART", "SWD"):
            self.assertIn(interface, diagram)
        self.assertEqual(set(ARCHITECTURE_FILES), {"docs/architecture.md"})


@unittest.skipUnless(
    os.environ.get("PCBFORGE_RUN_REAL_INTEGRATION") == "1",
    "set PCBFORGE_RUN_REAL_INTEGRATION=1 to exercise pinned external tools",
)
class RealToolchainIntegrationTests(unittest.TestCase):
    def test_two_and_four_layer_scaffolds_load_in_real_kicad(self):
        from pcbforge.status import inspect_status
        from pcbforge.schematic import extract
        for layers in (2, 4):
            with self.subTest(layers=layers), tempfile.TemporaryDirectory() as temporary:
                name = f"integration-{layers}layer"
                project = Path(temporary) / name
                project.mkdir()
                (project / "spec.md").write_text(spec_text(name=name, layers=layers))
                (project / "policy.yaml").write_text(render_default_policy())
                def run(*args):
                    result = subprocess.run([str(TOOL_ROOT/'scripts/pcbforge'), *args, str(project)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                    return result.stdout
                run('status','review','spec')
                run('status','approve','spec','--last-reviewed','--note','Synthetic integration fixture approval')
                self.assertIn('native KiCad smoke test passed', run('init'))
                pins = yaml.safe_load((project/'.pcbforge').read_text())
                self.assertEqual(pins['toolchain']['kicad'], '10.0.3')
                self.assertFalse(pins['pcbforge']['dirty'])
                self.assertEqual(extract(project/f'{name}.kicad_sch').components, ())
                board = project/f'{name}.kicad_pcb'
                text = board.read_text()
                self.assertEqual('"In1.Cu"' in text, layers == 4)
                self.assertEqual('"In2.Cu"' in text, layers == 4)
                self.assertEqual(inspect_status(project).current.phase.key, 'architect')
                report = project/'drc-report.json'
                result = subprocess.run([str(TOOL_ROOT/'scripts/kicad-cli'), 'pcb','drc','--format','json','--output',str(report),str(board)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
