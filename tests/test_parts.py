from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.cli import main
from pcbforge.parts import check_parts, render_parts_audit


SPEC = """---
spec_schema: 1
name: parts-audit
layers: 2
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: []
board_mm: [20, 20]
---
# Parts audit
"""


def atomic_part(
    name: str,
    *,
    prefix: str,
    footprint: str,
    symbol: str,
    description: str = "",
    pins: int = 2,
) -> str:
    pin_lines = "\n".join(f"    pin {index}" for index in range(1, pins + 1))
    return f"""#pragma experiment("TRAITS")
import has_designator_prefix
import is_atomic_part

component {name}:
    \"\"\"{description}\"\"\"
    trait is_atomic_part<manufacturer="Example", partnumber="{name}", footprint="{footprint}", symbol="{symbol}">
    trait has_designator_prefix<prefix="{prefix}">
{pin_lines}
"""


class PartsAuditTests(unittest.TestCase):
    def project(self, root: Path) -> Path:
        project = root / "parts-audit"
        (project / "src" / "parts").mkdir(parents=True)
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        return project

    def add_part(self, project: Path, name: str, source: str) -> None:
        directory = project / "src" / "parts" / name
        directory.mkdir()
        (directory / f"{name}.ato").write_text(source, encoding="utf-8")

    def test_blocks_local_assets_for_standard_0603_resistor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.add_part(
                project,
                "R_10K_0603",
                atomic_part(
                    "R_10K_0603",
                    prefix="R",
                    footprint="R0603.kicad_mod",
                    symbol="R_10K_0603.kicad_sym",
                    description="10 kohm 0603 resistor",
                ),
            )
            result = check_parts(project)
            rendered = render_parts_audit(result)

        self.assertFalse(result.ok)
        self.assertEqual(result.scanned_parts, 1)
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0].expected_symbol, "Device:R")
        self.assertEqual(
            result.violations[0].expected_footprint,
            "Resistor_SMD:R_0603_1608Metric",
        )
        self.assertIn("keep the exact MPN/LCSC selection", rendered)

    def test_blocks_capacitor_and_led_but_allows_official_library_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.add_part(
                project,
                "C_4U7_0805",
                atomic_part(
                    "C_4U7_0805",
                    prefix="C",
                    footprint="C0805.kicad_mod",
                    symbol="C_4U7_0805.kicad_sym",
                    description="ceramic capacitor",
                ),
            )
            self.add_part(
                project,
                "LED_GREEN_0603",
                atomic_part(
                    "LED_GREEN_0603",
                    prefix="D",
                    footprint="LED0603-RD.kicad_mod",
                    symbol="LED_GREEN_0603.kicad_sym",
                    description="green LED",
                ),
            )
            self.add_part(
                project,
                "R_OFFICIAL_0603",
                atomic_part(
                    "R_OFFICIAL_0603",
                    prefix="R",
                    footprint="Resistor_SMD:R_0603_1608Metric",
                    symbol="Device:R",
                    description="exact MPN C25804 0603 resistor",
                ),
            )
            result = check_parts(project)

        self.assertEqual(result.scanned_parts, 3)
        self.assertEqual(
            {violation.component for violation in result.violations},
            {"C_4U7_0805", "LED_GREEN_0603"},
        )
        expected = {
            violation.component: violation.expected_footprint
            for violation in result.violations
        }
        self.assertEqual(
            expected["C_4U7_0805"],
            "Capacitor_SMD:C_0805_2012Metric",
        )
        self.assertEqual(
            expected["LED_GREEN_0603"],
            "LED_SMD:LED_0603_1608Metric",
        )

    def test_allows_noncommodity_and_multipin_local_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.add_part(
                project,
                "SENSOR_DFN8",
                atomic_part(
                    "SENSOR_DFN8",
                    prefix="U",
                    footprint="DFN8.kicad_mod",
                    symbol="SENSOR_DFN8.kicad_sym",
                    pins=8,
                ),
            )
            self.add_part(
                project,
                "R_ARRAY_0603",
                atomic_part(
                    "R_ARRAY_0603",
                    prefix="R",
                    footprint="R_ARRAY_0603.kicad_mod",
                    symbol="R_ARRAY_0603.kicad_sym",
                    pins=4,
                ),
            )
            result = check_parts(project)

        self.assertTrue(result.ok)
        self.assertEqual(result.scanned_parts, 2)

    def test_cli_returns_one_for_policy_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.add_part(
                project,
                "R_0603",
                atomic_part(
                    "R_0603",
                    prefix="R",
                    footprint="R0603.kicad_mod",
                    symbol="R_0603.kicad_sym",
                ),
            )
            with mock.patch("builtins.print") as output:
                exit_code = main(["check-parts", str(project)])

        self.assertEqual(exit_code, 1)
        self.assertIn(
            "Resistor_SMD:R_0603_1608Metric",
            "\n".join(str(call.args[0]) for call in output.call_args_list),
        )


if __name__ == "__main__":
    unittest.main()
