from __future__ import annotations

import hashlib
import json
import os
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

TOOL_ROOT = Path(__file__).resolve().parents[1]

ARCHITECTURE_FILES = {
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
    def __init__(self, *, fail_build: bool = False) -> None:
        self.fail_build = fail_build
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
            return subprocess.CompletedProcess(command, 0, "", "")
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
    def _project(self, root: Path, name: str, *, layers: int = 2) -> Path:
        project = root / name
        project.mkdir()
        (project / "spec.md").write_text(
            spec_text(name=name, layers=layers),
            encoding="utf-8",
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
            self.assertEqual(pin_data["schema"], 3)
            self.assertEqual(pin_data["guidance"]["agents_schema"], 3)
            self.assertEqual(pin_data["guidance"]["architect_schema"], 2)
            self.assertEqual(pin_data["guidance"]["mcu_schema"], 1)

            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("pcbforge-agents-schema: 3", agents)
            self.assertIn("Never place, route, move", agents)
            self.assertIn("ready for architect", agents.lower())
            self.assertIn("/agent/architect.md", agents)
            self.assertIn("/agent/mcu.md", agents)
            self.assertIn("/modules/index.md", agents)
            self.assertIn("ARCHITECT approved", agents)
            self.assertIn("Do not choose parts", agents)
            self.assertIn("check-ioc", agents)
            self.assertIn("optional CubeMX 6.18 review", agents)

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

    def test_compiler_failure_leaves_project_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root, "garden-logger")
            with self.assertRaisesRegex(InitError, "smoke test failed"):
                initialize_project(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(fail_build=True),
                )
            self.assertEqual([path.name for path in project.iterdir()], ["spec.md"])
            self.assertEqual(list(root.glob(".garden-logger.pcbforge-init-*")), [])


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
            "pcbforge-architect-schema: 2",
            "src/modules/<snake_case>.ato",
            "src/mcu.ato",
            "ElectricPower",
            "USB2_0_IF",
            "spec-to-module coverage",
            "board hash",
            "ARCHITECT approved",
            "AI-led MCU workflow",
        ):
            self.assertIn(required, playbook)

        mcu_playbook = (TOOL_ROOT / "agent" / "mcu.md").read_text(encoding="utf-8")
        for required in (
            "pcbforge-mcu-schema: 1",
            "firmware/<project>.ioc",
            "DEBUG_UART_TX",
            "check-ioc",
            "optional and is not an approval gate",
            "one-to-one audit",
            "IMPLEMENT as the next phase",
        ):
            self.assertIn(required, mcu_playbook)

        catalog = (TOOL_ROOT / "modules" / "index.md").read_text(encoding="utf-8")
        self.assertIn("No modules have been published yet", catalog)
        self.assertIn("| Module | Version | Proven on | Interfaces | Render |", catalog)

    def test_architecture_fixture_contains_interfaces_but_no_parts(self) -> None:
        source = "\n".join(ARCHITECTURE_FILES.values()).lower()
        for interface in ("electricpower", "i2c", "uart", "usb2_0_if", "swd"):
            self.assertIn(interface, source)
        for forbidden in ("component ", "footprint", "lcsc", "partnumber"):
            self.assertNotIn(forbidden, source)


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
                for relative, source in ARCHITECTURE_FILES.items():
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
