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
    read_status_document,
    review_phase,
    write_status,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]

ARCHITECTURE_FILES = {
    "docs/architecture.md": """<!-- pcbforge-architecture-diagram-schema: 1 -->
# garden-logger architecture

> Architecture only: functional modules and typed interfaces. No parts, values,
> footprints, MCU pins, CubeMX configuration, placement, or routing.

## Functional graph

```mermaid
flowchart LR
    power["Power tree"]:::project_local
    mcu{{"Generic MCU"}}:::mcu
    external_io(["External I/O"]):::external

    power -->|+3V3 ElectricPower| mcu
    mcu <-->|USB USB2_0_IF| external_io
    mcu <-->|sensor I2C| external_io
    mcu ---|ADC ElectricSignal| external_io
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
    "src/modules/power_tree.ato": """import ElectricPower

module PowerTree:
    power_in = new ElectricPower
    rail_3v3 = new ElectricPower
""",
    "src/mcu.ato": """import ElectricPower
import ElectricSignal
import I2C
import SWD
import UART
import USB2_0_IF

module Mcu:
    power = new ElectricPower
    usb = new USB2_0_IF
    i2c = new I2C
    adc = new ElectricSignal
    debug_uart = new UART
    swd = new SWD
""",
    "src/modules/external_io.ato": """import ElectricSignal
import I2C
import SWD
import UART
import USB2_0_IF

module ExternalIo:
    usb = new USB2_0_IF
    i2c = new I2C
    adc = new ElectricSignal
    debug_uart = new UART
    swd = new SWD
""",
    "src/main.ato": """from "modules/power_tree.ato" import PowerTree
from "mcu.ato" import Mcu
from "modules/external_io.ato" import ExternalIo

module App:
    power = new PowerTree
    mcu = new Mcu
    external_io = new ExternalIo

    power.rail_3v3 ~ mcu.power
    mcu.usb ~ external_io.usb
    mcu.i2c ~ external_io.i2c
    mcu.adc ~ external_io.adc
    mcu.debug_uart ~ external_io.debug_uart
    mcu.swd ~ external_io.swd
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
            return subprocess.CompletedProcess(command, 0, "9.0.9\n", "")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[-2:] == ["status", "--short"]:
            output = " M pcbforge/status.py\n" if self.dirty_checkout else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if "build" in command:
            if self.fail_build:
                return subprocess.CompletedProcess(command, 1, "", "induced failure\n")
            if not (cwd / "ato.yaml").is_file() or not list(cwd.glob("*.kicad_pcb")):
                return subprocess.CompletedProcess(command, 1, "", "scaffold missing\n")
            return subprocess.CompletedProcess(command, 0, "build passed\n", "")
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

    def test_generates_two_layer_scaffold_and_runs_smoke_build(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            result = initialize_project(project, tool_root=TOOL_ROOT, runner=runner)

            self.assertEqual(result, InitResult("garden-logger", project.resolve()))
            self.assertTrue((project / ".pcbforge").is_file())
            self.assertTrue((project / "src" / "main.ato").is_file())
            self.assertTrue((project / "bom" / ".gitkeep").is_file())
            self.assertTrue((project / "fab" / ".gitkeep").is_file())
            self.assertTrue((project / "firmware" / ".gitkeep").is_file())
            self.assertTrue((project / "STATUS.md").is_file())
            self.assertFalse((project / "docs").exists())
            self.assertFalse((project / "build").exists())

            ato_yaml = (project / "ato.yaml").read_text(encoding="utf-8")
            self.assertIn("entry: src/main.ato:App", ato_yaml)
            self.assertIn("layout: ./garden-logger.kicad_pcb", ato_yaml)

            board = (project / "garden-logger.kicad_pcb").read_text(encoding="utf-8")
            self.assertIn('(0 "F.Cu" signal)', board)
            self.assertIn('(2 "B.Cu" signal)', board)
            self.assertNotIn('"In1.Cu"', board)
            self.assertIn("(start 100 100)", board)
            self.assertIn("(end 150 100)", board)
            self.assertIn("(end 150 140)", board)

            project_data = json.loads(
                (project / "garden-logger.kicad_pro").read_text(encoding="utf-8")
            )
            rules = project_data["board"]["design_settings"]["rules"]
            self.assertEqual(rules["min_clearance"], 0.2)
            self.assertEqual(rules["min_via_drill"], 0.3)
            self.assertEqual(
                project_data["net_settings"]["classes"][0]["track_width"],
                0.25,
            )

            pins = (project / ".pcbforge").read_text(encoding="utf-8")
            self.assertIn("atopile: 0.15.7", pins)
            self.assertIn("kicad: 9.0.9", pins)
            self.assertIn("jlc-2layer-conservative-v1", pins)
            pin_data = yaml.safe_load(pins)
            self.assertEqual(pin_data["schema"], 15)
            self.assertNotIn("brief_schema", pin_data["guidance"])
            self.assertEqual(pin_data["guidance"]["layout_handoff_schema"], 1)
            self.assertEqual(pin_data["guidance"]["approval_schema"], 6)
            self.assertEqual(pin_data["guidance"]["agents_schema"], 16)
            self.assertEqual(pin_data["guidance"]["policy_schema"], 1)
            self.assertEqual(pin_data["guidance"]["architect_schema"], 5)
            self.assertEqual(
                pin_data["guidance"]["architecture_diagram_schema"],
                1,
            )
            self.assertEqual(pin_data["guidance"]["mcu_schema"], 4)
            self.assertEqual(pin_data["guidance"]["circuit_schema"], 1)
            self.assertNotIn("implement_schema", pin_data["guidance"])
            self.assertEqual(pin_data["guidance"]["build_test_schema"], 1)
            self.assertEqual(pin_data["guidance"]["status_schema"], 4)
            self.assertEqual(
                pin_data["guidance"]["circuit_review_schema"],
                2,
            )
            self.assertEqual(
                pin_data["policy"]["profile"],
                "pcbforge-standard-v1",
            )
            self.assertEqual(
                pin_data["policy"]["baseline_approval"],
                "spec",
            )
            self.assertRegex(
                pin_data["policy"]["profile_sha256"],
                r"^[0-9a-f]{64}$",
            )

            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("pcbforge-agents-schema: 16", agents)
            self.assertIn("agent/layout-handoff.md", agents)
            self.assertIn("Never place, route, move", agents)
            self.assertIn("opens ARCHITECT directly", agents)
            self.assertIn("/agent/architect.md", agents)
            self.assertIn("/agent/mcu.md", agents)
            self.assertIn("/agent/circuit.md", agents)
            self.assertNotIn("/agent/implement.md", agents)
            self.assertIn("/modules/index.md", agents)
            self.assertIn("## Decision authority", agents)
            self.assertIn("never originate", agents)
            self.assertIn("status review architect --stage proposal", agents)
            self.assertIn("Final approval captures the pre-CIRCUIT source baseline", agents)
            self.assertIn("status approve architect", agents)
            self.assertIn("## Manufacturing and technology policy", agents)
            self.assertIn("pcbforge check-policy", agents)
            self.assertIn("policy confirm-sourcing", agents)
            self.assertIn("Do not generate a KiCad schematic", agents)
            self.assertIn("check-circuit-review --stage proposal", agents)
            self.assertIn("review/circuit/circuit.svg", agents)
            self.assertIn("Do not choose non-MCU parts", agents)
            self.assertIn("docs/architecture.md", agents)
            self.assertIn("pcbforge-architecture-diagram-schema: 1", agents)
            self.assertIn("Audit every functional `App` instance", agents)
            self.assertIn("diagram:", agents)
            self.assertIn("check-ioc", agents)
            self.assertIn("optional CubeMX review", agents)
            self.assertIn("check-parts", agents)
            self.assertIn(
                "CIRCUIT cannot\n    become ready while build, IOC, parts, policy",
                agents,
            )
            self.assertIn("build-test.yaml", agents)
            self.assertIn("docs/build-test.md", agents)
            self.assertIn("docs/placement-brief.md", agents)
            self.assertNotIn("generates `brief.md`", agents)

        build_calls = [call for call in runner.calls if "build" in call[0]]
        self.assertEqual(len(build_calls), 1)
        self.assertNotEqual(build_calls[0][1].name, "garden-logger")

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
        for conflict in (".pcbforge", "AGENTS.md", "src"):
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

    def test_rejects_legacy_unbound_spec_approval_before_init(self) -> None:
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

    def test_compiler_failure_leaves_project_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "garden-logger")
            dashboard_before = (project / "STATUS.md").read_bytes()
            with self.assertRaisesRegex(InitError, "smoke test failed"):
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
            self.assertIn("1 of 8 required phases complete", dashboard)
            self.assertIn("**Phase:** 2. ARCHITECT — Ready", dashboard)
            self.assertIn(
                "SPEC → ARCHITECT: initialize | Tool | ✅ Complete",
                dashboard,
            )

    def test_failed_init_does_not_mutate_pre_init_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), "garden-logger")
            before = (project / "STATUS.md").read_bytes()

            with self.assertRaisesRegex(InitError, "smoke test failed"):
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
    def test_architect_playbook_and_empty_catalog_are_explicit(self) -> None:
        playbook = (TOOL_ROOT / "agent" / "architect.md").read_text(encoding="utf-8")
        for required in (
            "pcbforge-architect-schema: 5",
            "src/modules/<snake_case>.ato",
            "src/mcu.ato",
            "ElectricPower",
            "USB2_0_IF",
            "spec-to-module coverage",
            "board hash",
            "status review architect --stage proposal",
            "status approve architect",
            "MCU workstream",
            "docs/architecture.md",
            "pcbforge-architecture-diagram-schema: 1",
            "source-to-diagram audit",
            "artifact fingerprint",
        ):
            self.assertIn(required, playbook)

        mcu_playbook = (TOOL_ROOT / "agent" / "mcu.md").read_text(encoding="utf-8")
        for required in (
            "pcbforge-mcu-schema: 4",
            "firmware/<project>.ioc",
            "DEBUG_UART_TX",
            "check-ioc",
            "optional and is not an approval gate",
            "one-to-one audit",
            "CIRCUIT becomes the",
            "next phase",
            "source-baseline.json",
        ):
            self.assertIn(required, mcu_playbook)

        circuit_playbook = (TOOL_ROOT / "agent" / "circuit.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "pcbforge-circuit-schema: 1",
            "Device:R",
            "Resistor_SMD:R_0603_1608Metric",
            "supplier/BOM",
            "pcbforge check-parts",
            "`circuit-final`",
            "policy approve-exception",
            "status review circuit --stage proposal",
            "build-test.yaml",
            "pcbforge-test",
            "docs/build-test.md",
        ):
            self.assertIn(required, circuit_playbook)

        catalog = (TOOL_ROOT / "modules" / "index.md").read_text(encoding="utf-8")
        self.assertIn("No modules have been published yet", catalog)
        self.assertIn("| Module | Version | Proven on | Interfaces | Render |", catalog)

    def test_architecture_fixture_contains_interfaces_but_no_parts(self) -> None:
        source = "\n".join(
            contents
            for path, contents in ARCHITECTURE_FILES.items()
            if path.endswith(".ato")
        ).lower()
        for interface in ("electricpower", "i2c", "uart", "usb2_0_if", "swd"):
            self.assertIn(interface, source)
        for forbidden in ("component ", "footprint", "lcsc", "partnumber"):
            self.assertNotIn(forbidden, source)

    def test_architecture_fixture_mermaid_matches_top_level_graph(self) -> None:
        diagram = ARCHITECTURE_FILES["docs/architecture.md"]
        self.assertIn("pcbforge-architecture-diagram-schema: 1", diagram)
        self.assertIn("Architecture only", diagram)
        graph = diagram.split("```mermaid\n", 1)[1].split("\n```", 1)[0]

        for node in ("power[", "mcu{{", "external_io("):
            self.assertEqual(graph.count(node), 1)
        for interface in (
            "ElectricPower",
            "USB2_0_IF",
            "I2C",
            "ElectricSignal",
            "UART",
            "SWD",
        ):
            self.assertEqual(graph.count(interface), 1)

        source_edges = ARCHITECTURE_FILES["src/main.ato"].count(" ~ ")
        diagram_edges = sum(
            1 for line in graph.splitlines() if re.search(r"(?:-->|<-->|---)", line)
        )
        self.assertEqual(diagram_edges, source_edges)
        for forbidden in ("footprint", "lcsc", "stm32g0", "pa0", "cubemx"):
            self.assertNotIn(forbidden, graph.lower())


@unittest.skipUnless(
    os.environ.get("PCBFORGE_RUN_REAL_INTEGRATION") == "1",
    "set PCBFORGE_RUN_REAL_INTEGRATION=1 to exercise pinned external tools",
)
class RealToolchainIntegrationTests(unittest.TestCase):
    def test_two_and_four_layer_scaffolds_build_and_load_in_kicad(self) -> None:
        for layers in (2, 4):
            with (
                self.subTest(layers=layers),
                tempfile.TemporaryDirectory() as temporary,
            ):
                name = f"integration-{layers}layer"
                project = Path(temporary) / name
                project.mkdir()
                (project / "spec.md").write_text(
                    spec_text(name=name, layers=layers),
                    encoding="utf-8",
                )
                (project / "policy.yaml").write_text(
                    render_default_policy(),
                    encoding="utf-8",
                )

                status_draft = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "--write",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    status_draft.returncode,
                    0,
                    status_draft.stdout + status_draft.stderr,
                )
                spec_review = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "review",
                        "spec",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    spec_review.returncode,
                    0,
                    spec_review.stdout + spec_review.stderr,
                )
                spec_fingerprint = re.search(
                    r"approval fingerprint: ([0-9a-f]{64})",
                    spec_review.stdout,
                )
                self.assertIsNotNone(spec_fingerprint)
                spec_gate = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "approve",
                        "spec",
                        "--fingerprint",
                        spec_fingerprint.group(1),
                        "--note",
                        "Integration requirements approved",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    spec_gate.returncode,
                    0,
                    spec_gate.stdout + spec_gate.stderr,
                )

                initialized = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "init",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    initialized.returncode,
                    0,
                    initialized.stdout + initialized.stderr,
                )
                self.assertIn("compiler smoke test passed", initialized.stdout)

                init_review = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "review",
                        "init",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    init_review.returncode,
                    0,
                    init_review.stdout + init_review.stderr,
                )
                init_fingerprint = re.search(
                    r"approval fingerprint: ([0-9a-f]{64})",
                    init_review.stdout,
                )
                self.assertIsNotNone(init_fingerprint)
                init_gate = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "approve",
                        "init",
                        "--fingerprint",
                        init_fingerprint.group(1),
                        "--note",
                        "Integration scaffold approved",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    init_gate.returncode,
                    0,
                    init_gate.stdout + init_gate.stderr,
                )

                board = project / f"{name}.kicad_pcb"
                board_text = board.read_text(encoding="utf-8")
                if layers == 4:
                    self.assertIn('(4 "In1.Cu" power)', board_text)
                    self.assertIn('(6 "In2.Cu" power)', board_text)
                else:
                    self.assertNotIn('"In1.Cu"', board_text)
                self.assertIn("(start 100 100)", board_text)
                self.assertIn("(end 150 140)", board_text)

                board_hash_before = hashlib.sha256(board.read_bytes()).hexdigest()
                diagram = project / "docs" / "architecture.md"
                diagram.parent.mkdir(parents=True, exist_ok=True)
                diagram.write_text(
                    ARCHITECTURE_FILES["docs/architecture.md"],
                    encoding="utf-8",
                )
                proposal_gate = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "mark",
                        "architect",
                        "proposal-approved",
                        "--note",
                        "Integration proposal approved; diagram: docs/architecture.md",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    proposal_gate.returncode,
                    0,
                    proposal_gate.stdout + proposal_gate.stderr,
                )

                for relative, source in ARCHITECTURE_FILES.items():
                    if relative == "docs/architecture.md":
                        continue
                    path = project / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(source, encoding="utf-8")

                architecture_build = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "ato"),
                        "build",
                        "--verbose",
                    ],
                    cwd=project,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    architecture_build.returncode,
                    0,
                    architecture_build.stdout + architecture_build.stderr,
                )
                board_hash_after = hashlib.sha256(board.read_bytes()).hexdigest()
                self.assertEqual(board_hash_after, board_hash_before)
                self.assertNotIn("(footprint ", board.read_text(encoding="utf-8"))

                architecture_review = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "review",
                        "architect",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    architecture_review.returncode,
                    0,
                    architecture_review.stdout + architecture_review.stderr,
                )
                architecture_fingerprint = re.search(
                    r"approval fingerprint: ([0-9a-f]{64})",
                    architecture_review.stdout,
                )
                self.assertIsNotNone(architecture_fingerprint)
                architecture_gate = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "pcbforge"),
                        "status",
                        "approve",
                        "architect",
                        "--fingerprint",
                        architecture_fingerprint.group(1),
                        "--note",
                        "Integration graph approved; diagram: docs/architecture.md",
                        str(project),
                    ],
                    cwd=TOOL_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    architecture_gate.returncode,
                    0,
                    architecture_gate.stdout + architecture_gate.stderr,
                )
                dashboard = (project / "STATUS.md").read_text(encoding="utf-8")
                self.assertIn("3 of 12 required phases complete", dashboard)

                report = project / "drc-report.json"
                completed = subprocess.run(
                    [
                        str(TOOL_ROOT / "scripts" / "kicad-cli"),
                        "pcb",
                        "drc",
                        "--format",
                        "json",
                        "--output",
                        str(report),
                        str(board),
                    ],
                    cwd=project,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
