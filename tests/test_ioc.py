from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.cli import main
from pcbforge.ioc import (
    IocCheckResult,
    IocProjectError,
    IocValidationError,
    PinAssignment,
    check_ioc,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]


def spec_text(
    *,
    name: str = "garden-logger",
    family: str = "G0",
    peripherals: str = "[usb-fs, i2c, adc]",
    debug_uart: bool = True,
) -> str:
    return f"""---
spec_schema: 1
name: {name}
layers: 2
stm32_family: {family}
power_in: usb-c
rails: [+3V3]
peripherals: {peripherals}
board_mm: [50, 40]
debug_uart: {str(debug_uart).lower()}
---
# Test project
"""


def valid_ioc() -> str:
    return """#MicroXplorer Configuration settings - do not modify
File.Version=6
Mcu.CPN=STM32G0B1CBT6
Mcu.Family=STM32G0
Mcu.IP0=ADC1
Mcu.IP1=I2C1
Mcu.IP2=SYS
Mcu.IP3=USART1
Mcu.IP4=USB_DRD_FS
Mcu.IPNb=5
Mcu.Name=STM32G0B1C(B-C-E)Tx
Mcu.Package=LQFP48
Mcu.Pin0=PA0
Mcu.Pin1=PA9
Mcu.Pin2=PA10
Mcu.Pin3=PA11
Mcu.Pin4=PA12
Mcu.Pin5=PA13
Mcu.Pin6=PA14
Mcu.Pin7=PB6
Mcu.Pin8=PB7
Mcu.Pin9=VP_SYS_VS_Systick
Mcu.PinsNb=10
MxCube.Version=6.18.0
PA0.GPIO_Label=CLIMATE_ADC
PA0.Signal=ADC1_IN0
PA9.GPIO_Label=DEBUG_UART_TX
PA9.Mode=Asynchronous
PA9.Signal=USART1_TX
PA10.GPIO_Label=DEBUG_UART_RX
PA10.Mode=Asynchronous
PA10.Signal=USART1_RX
PA11.Signal=USB_DM
PA12.Signal=USB_DP
PA13.Mode=Serial_Wire
PA13.Signal=SYS_JTMS-SWDIO
PA14.Mode=Serial_Wire
PA14.Signal=SYS_JTCK-SWCLK
PB6.GPIO_Label=CLIMATE_I2C_SCL
PB6.Mode=I2C
PB6.Signal=I2C1_SCL
PB7.GPIO_Label=CLIMATE_I2C_SDA
PB7.Mode=I2C
PB7.Signal=I2C1_SDA
VP_SYS_VS_Systick.Mode=SysTick
VP_SYS_VS_Systick.Signal=SYS_VS_Systick
"""


class FakeCubeMxRunner:
    def __init__(
        self,
        *,
        version_returncode: int = 0,
        check_returncode: int = 0,
        mutate_round_trip=None,
    ) -> None:
        self.version_returncode = version_returncode
        self.check_returncode = check_returncode
        self.mutate_round_trip = mutate_round_trip
        self.calls: list[list[str]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        self.calls.append(command)
        if command[-1:] == ["version"]:
            return subprocess.CompletedProcess(
                command,
                self.version_returncode,
                "6.18\n" if self.version_returncode == 0 else "",
                "CubeMX unavailable\n" if self.version_returncode else "",
            )

        if self.check_returncode:
            return subprocess.CompletedProcess(
                command,
                self.check_returncode,
                "",
                "configuration load failed\n",
            )

        script = Path(command[-1]).read_text(encoding="utf-8")
        source_match = re.search(r'^config load "(.+)"$', script, re.MULTILINE)
        target_match = re.search(r'^config saveext "(.+)"$', script, re.MULTILINE)
        assert source_match is not None
        assert target_match is not None
        source = Path(source_match.group(1))
        target = Path(target_match.group(1))
        contents = source.read_text(encoding="utf-8")
        if self.mutate_round_trip is not None:
            contents = self.mutate_round_trip(contents)
        target.write_text(contents, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "round trip passed\n", "")


class IocCheckTests(unittest.TestCase):
    def _project(
        self,
        root: Path,
        *,
        contents: str | None = None,
        family: str = "G0",
        peripherals: str = "[usb-fs, i2c, adc]",
        debug_uart: bool = True,
    ) -> Path:
        project = root / "garden-logger"
        (project / "firmware").mkdir(parents=True)
        (project / ".pcbforge").write_text("schema: 3\n", encoding="utf-8")
        (project / "spec.md").write_text(
            spec_text(
                family=family,
                peripherals=peripherals,
                debug_uart=debug_uart,
            ),
            encoding="utf-8",
        )
        (project / "firmware" / "garden-logger.ioc").write_text(
            contents if contents is not None else valid_ioc(),
            encoding="utf-8",
        )
        return project

    def test_validates_and_reports_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary))
            ioc_path = project / "firmware" / "garden-logger.ioc"
            before = ioc_path.read_bytes()
            runner = FakeCubeMxRunner(
                mutate_round_trip=lambda text: text + "GPIO.groupedBy=Groups\n"
            )

            result = check_ioc(project, tool_root=TOOL_ROOT, runner=runner)

            self.assertEqual(result.part_number, "STM32G0B1CBT6")
            self.assertEqual(result.family, "STM32G0")
            self.assertEqual(result.package, "LQFP48")
            self.assertEqual(result.pins[0].pin, "PA0")
            self.assertEqual(ioc_path.read_bytes(), before)
            self.assertEqual(len(runner.calls), 2)

    def test_static_validation_aggregates_contract_failures(self) -> None:
        broken = (
            valid_ioc()
            .replace("Mcu.Family=STM32G0", "Mcu.Family=STM32F4")
            .replace("Mcu.PinsNb=10", "Mcu.PinsNb=11")
            .replace("PA13.Signal=SYS_JTMS-SWDIO", "PA13.Signal=GPIO_Output")
            .replace("PA9.GPIO_Label=DEBUG_UART_TX\n", "")
        )
        runner = FakeCubeMxRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary), contents=broken)
            with self.assertRaises(IocValidationError) as raised:
                check_ioc(project, tool_root=TOOL_ROOT, runner=runner)

        message = str(raised.exception)
        self.assertIn("Mcu.Family", message)
        self.assertIn("Mcu.Pin entries", message)
        self.assertIn("SWD requires", message)
        self.assertIn("DEBUG_UART_TX", message)
        self.assertEqual(runner.calls, [])

    def test_rejects_duplicate_keys_before_running_cubemx(self) -> None:
        runner = FakeCubeMxRunner()
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(
                Path(temporary),
                contents=valid_ioc() + "Mcu.CPN=duplicate\n",
            )
            with self.assertRaisesRegex(IocValidationError, "duplicate key"):
                check_ioc(project, tool_root=TOOL_ROOT, runner=runner)
        self.assertEqual(runner.calls, [])

    def test_rejects_cubemx_failure_and_semantic_drift(self) -> None:
        runners = {
            "load failure": FakeCubeMxRunner(check_returncode=1),
            "semantic drift": FakeCubeMxRunner(
                mutate_round_trip=lambda text: text.replace(
                    "PA9.Signal=USART1_TX",
                    "PA9.Signal=USART2_TX",
                )
            ),
        }
        for label, runner in runners.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                project = self._project(Path(temporary))
                with self.assertRaises(IocValidationError):
                    check_ioc(project, tool_root=TOOL_ROOT, runner=runner)

    def test_reports_missing_project_artifacts_and_cubemx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(IocProjectError):
                check_ioc(root, tool_root=TOOL_ROOT, runner=FakeCubeMxRunner())

            project = self._project(root)
            with self.assertRaisesRegex(IocProjectError, "CubeMX unavailable"):
                check_ioc(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeCubeMxRunner(version_returncode=2),
                )


class IocCliTests(unittest.TestCase):
    def test_cli_prints_mapping(self) -> None:
        result = IocCheckResult(
            project_dir=Path("/tmp/garden-logger"),
            ioc_path=Path("/tmp/garden-logger/firmware/garden-logger.ioc"),
            part_number="STM32G0B1CBT6",
            family="STM32G0",
            package="LQFP48",
            pins=(
                PinAssignment(
                    pin="PA9",
                    label="DEBUG_UART_TX",
                    signal="USART1_TX",
                    mode="Asynchronous",
                ),
            ),
        )
        with (
            mock.patch("pcbforge.cli.check_ioc", return_value=result),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(main(["check-ioc", "/tmp/garden-logger"]), 0)
        rendered = "\n".join(
            " ".join(map(str, call.args)) for call in output.call_args_list
        )
        self.assertIn("STM32G0B1CBT6", rendered)
        self.assertIn("DEBUG_UART_TX", rendered)
        self.assertIn("semantic round-trip passed", rendered)

    def test_cli_exit_codes(self) -> None:
        for exception, expected in (
            (IocProjectError("missing project"), 2),
            (IocValidationError("bad mapping"), 1),
        ):
            with (
                self.subTest(exception=exception),
                mock.patch("pcbforge.cli.check_ioc", side_effect=exception),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(main(["check-ioc"]), expected)


@unittest.skipUnless(
    os.environ.get("PCBFORGE_RUN_REAL_INTEGRATION") == "1",
    "set PCBFORGE_RUN_REAL_INTEGRATION=1 to exercise STM32CubeMX 6.18",
)
class RealCubeMxIntegrationTests(unittest.TestCase):
    def test_roamer_fixture_round_trips_through_pinned_cubemx(self) -> None:
        fixture = (
            TOOL_ROOT
            / "pilots"
            / "roamer-rev-a"
            / "baseline"
            / "source"
            / "firmware"
            / "roamer_rev_a.ioc"
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "roamer-check"
            (project / "firmware").mkdir(parents=True)
            (project / ".pcbforge").write_text("schema: 3\n", encoding="utf-8")
            (project / "spec.md").write_text(
                spec_text(
                    name="roamer-check",
                    family="F1",
                    peripherals="[i2c]",
                    debug_uart=False,
                ),
                encoding="utf-8",
            )
            shutil.copy2(fixture, project / "firmware" / "roamer-check.ioc")

            result = check_ioc(project, tool_root=TOOL_ROOT)

        self.assertEqual(result.part_number, "STM32F103CBT6")
