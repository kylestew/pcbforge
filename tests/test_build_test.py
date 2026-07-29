from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.artifact_hash import ArtifactHashError, semantic_bom_sha256
from pcbforge.build_test import (
    BUILD_TEST_REPORT,
    AssertionLocation,
    BomComponent,
    BuildTestError,
    BuildTestInputError,
    BuildTestResult,
    ato_source_semantic_bytes,
    check_build_test,
    fingerprint_inputs,
    read_build_test_contract,
    saved_report_status,
)
from pcbforge.cli import main

TOOL_ROOT = Path(__file__).resolve().parents[1]


SPEC = """---
spec_schema: 1
name: garden-logger
layers: 2
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: [i2c]
board_mm: [50, 40]
---
# Garden logger
"""

BOARD = """(kicad_pcb
  (version 20241229)
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (at 110 120 0)
    (property "Reference" "R1")
    (pad "1" smd rect
      (at -0.8 0)
      (net 1 "GND")
    )
    (pad "2" smd rect
      (at 0.8 0)
      (net 2 "+3V3")
    )
  )
  (gr_line
    (start 100 100)
    (end 150 100)
    (stroke (width 0.05) (type default))
    (layer "Edge.Cuts")
  )
)
"""

CONTRACT = """build_test_schema: 1
build: default
bom:
  - lcsc: C25804
    mpn: 0603WAF1002T5E
    footprint: Resistor_SMD:R_0603_1608Metric
    quantity: 1
board_footprints: 1
assertions:
  - rail-3v3-tolerance
"""


class FakeRunner:
    def __init__(
        self,
        *,
        mutate_board: bool = False,
        fail: bool = False,
        build_ids: tuple[str, ...] = (),
    ) -> None:
        self.mutate_board = mutate_board
        self.fail = fail
        self.build_ids = iter(build_ids)
        self.calls: list[list[str]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        self.calls.append(command)
        if self.mutate_board:
            board = Path(cwd) / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "(at 110 120 0)",
                    "(at 111 120 0)",
                ),
                encoding="utf-8",
            )
        build_id = next(self.build_ids, None)
        if build_id is not None:
            bom = (
                Path(cwd)
                / "build"
                / "builds"
                / "default"
                / "default.bom.json"
            )
            payload = json.loads(bom.read_text(encoding="utf-8"))
            payload["build_id"] = build_id
            bom.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            1 if self.fail else 0,
            "" if self.fail else "frozen build passed\n",
            "assertion failed\n" if self.fail else "",
        )


class BuildTestFixture(unittest.TestCase):
    def project(self, root: Path) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        (project / ".pcbforge").write_text(
            """schema: 11
toolchain:
  atopile: 0.15.7
  kicad: 9.0.9
  uv_lock_sha256: abc123
guidance:
  build_test_schema: 1
  brief_schema: 1
  approval_schema: 2
  policy_schema: 1
""",
            encoding="utf-8",
        )
        (project / "ato.yaml").write_text(
            """builds:
  default:
    entry: src/main.ato:App
""",
            encoding="utf-8",
        )
        (project / "build-test.yaml").write_text(CONTRACT, encoding="utf-8")
        (project / "src").mkdir()
        (project / "src" / "main.ato").write_text(
            """module App:
    # pcbforge-test: rail-3v3-tolerance
    assert 3.3V within 3.3V +/- 5%
""",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_pcb").write_text(BOARD, encoding="utf-8")
        (project / "garden-logger.kicad_pro").write_text("{}\n", encoding="utf-8")
        (project / "garden-logger.kicad_dru").write_text(
            "(version 1)\n",
            encoding="utf-8",
        )
        build = project / "build" / "builds" / "default"
        build.mkdir(parents=True)
        (project / "build" / "manifest.json").write_text(
            '{"version": "2.0"}\n',
            encoding="utf-8",
        )
        (build / "default.bom.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "components": [
                        {
                            "lcsc": "C25804",
                            "mpn": "0603WAF1002T5E",
                            "package": "Resistor_SMD:R_0603_1608Metric",
                            "quantity": 1,
                            "usages": [{"designator": "R1"}],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (build / "default.bom.csv").write_text(
            "Designator,Footprint,Quantity,Value,Manufacturer,Partnumber,"
            "LCSC Part #\n"
            "R1,Resistor_SMD:R_0603_1608Metric,1,10k,UNI-ROYAL,"
            "0603WAF1002T5E,C25804\n",
            encoding="utf-8",
        )
        return project


class ContractTests(BuildTestFixture):
    def test_semantic_bom_hash_ignores_only_top_level_build_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            bom = project / "build" / "builds" / "default" / "default.bom.json"
            first = semantic_bom_sha256(bom)
            payload = json.loads(bom.read_text(encoding="utf-8"))
            payload["build_id"] = "run-one"
            bom.write_text(json.dumps(payload), encoding="utf-8")
            second = semantic_bom_sha256(bom)
            payload["components"][0]["value"] = "10k"
            bom.write_text(json.dumps(payload), encoding="utf-8")
            changed = semantic_bom_sha256(bom)

        self.assertEqual(first, second)
        self.assertNotEqual(second, changed)

    def test_semantic_bom_hash_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "default.bom.json"
            path.write_text("{not json\n", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactHashError, "invalid compiler BOM"):
                semantic_bom_sha256(path)

    def test_source_semantics_remove_only_valid_marker_assert_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "main.ato"
            source.write_bytes(
                b"module App:\r\n"
                b"    pass\r\n"
                b"    # pcbforge-test: rail-3v3-tolerance\r\n"
                b"    assert 3.3V within 3.3V +/- 5%\r\n"
                b"    assert 1V within 1V\r\n"
                b"    # pcbforge-test: Not-Kebab\r\n"
                b"    assert 2V within 2V\r\n"
                b"    # pcbforge-test: non-adjacent\r\n"
                b"\r\n"
                b"    assert 4V within 4V\r\n"
            )

            semantic = ato_source_semantic_bytes(source)

        self.assertNotIn(b"rail-3v3-tolerance", semantic)
        self.assertNotIn(b"3.3V within", semantic)
        self.assertIn(b"assert 1V within 1V", semantic)
        self.assertIn(b"Not-Kebab", semantic)
        self.assertIn(b"assert 2V within 2V", semantic)
        self.assertIn(b"non-adjacent\r\n\r\n", semantic)
        self.assertIn(b"assert 4V within 4V", semantic)
        self.assertNotIn(b"\n", semantic.replace(b"\r\n", b""))

    def test_reads_strict_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = read_build_test_contract(self.project(Path(temporary)))

        self.assertEqual(contract.build, "default")
        self.assertEqual(contract.bom[0].lcsc, "C25804")
        self.assertEqual(contract.board_footprints, 1)
        self.assertEqual(contract.assertions, ("rail-3v3-tolerance",))

    def test_rejects_duplicate_lcsc_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "build-test.yaml").write_text(
                CONTRACT.replace(
                    "board_footprints: 1",
                    """  - lcsc: C25804
    mpn: DUPLICATE
    footprint: Device:R
    quantity: 1
unknown: forbidden
board_footprints: 1""",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BuildTestInputError,
                "(?s)unknown keys.*duplicate LCSC",
            ):
                read_build_test_contract(project)

    def test_rejects_unmigrated_project_guidance_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pins = project / ".pcbforge"
            pins.write_text(
                pins.read_text(encoding="utf-8").replace(
                    "schema: 11",
                    "schema: 9",
                ),
                encoding="utf-8",
            )
            runner = FakeRunner()
            with self.assertRaisesRegex(
                BuildTestInputError,
                "not migrated for CIRCUIT acceptance",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=runner,
                )

        self.assertEqual(runner.calls, [])


class CheckerTests(BuildTestFixture):
    def test_allows_canonical_unfitted_pcb_features(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            contract = project / "build-test.yaml"
            contract.write_text(
                contract.read_text(encoding="utf-8").replace(
                    "board_footprints: 1",
                    "board_footprints: 3",
                ),
                encoding="utf-8",
            )
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "  (gr_line",
                    """  (footprint "MountingHole.pretty:MountingHole_3.2mm_M3"
    (layer "F.Cu")
    (at 105 105)
    (property "Reference" "H1")
  )
  (footprint "TestPoint.pretty:TestPoint_Pad_D1.5mm"
    (layer "B.Cu")
    (at 106 106)
    (property "Reference" "TP1")
    (pad "1" thru_hole circle
      (at 0 0)
      (net 1 "GND")
    )
  )
  (gr_line""",
                ),
                encoding="utf-8",
            )

            result = check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                write_report=True,
            )
            report = (project / BUILD_TEST_REPORT).read_text(encoding="utf-8")

        self.assertEqual(result.footprint_count, 3)
        self.assertIn("H1, TP1", report)

    def test_rejects_non_bom_reference_with_unrelated_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            contract = project / "build-test.yaml"
            contract.write_text(
                contract.read_text(encoding="utf-8").replace(
                    "board_footprints: 1",
                    "board_footprints: 2",
                ),
                encoding="utf-8",
            )
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "  (gr_line",
                    """  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical"
    (layer "B.Cu")
    (at 106 106)
    (property "Reference" "TP1")
    (pad "1" thru_hole circle
      (at 0 0)
      (net 1 "GND")
    )
  )
  (gr_line""",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                BuildTestError,
                "unsupported non-BOM references: TP1",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

    def test_frozen_build_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(
                BuildTestError,
                "frozen build failed",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(fail=True),
                )

    def test_missing_compiler_artifact_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "build" / "builds" / "default" / "default.bom.csv").unlink()
            with self.assertRaisesRegex(BuildTestError, "missing BOM CSV"):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

    def test_pcb_footprint_must_match_bom_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "Resistor_SMD:R_0603_1608Metric",
                    "Resistor_SMD:R_0805_2012Metric",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BuildTestError,
                "PCB footprint.*expected",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

    def test_passes_exact_evidence_and_writes_stable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            runner = FakeRunner()
            first = check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=runner,
                write_report=True,
            )
            second = check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=runner,
                write_report=True,
            )

            report = (project / BUILD_TEST_REPORT).read_text(encoding="utf-8")
            report_ok, detail = saved_report_status(
                project,
                fingerprint_inputs(project),
            )

        self.assertTrue(first.wrote_report)
        self.assertFalse(second.wrote_report)
        self.assertEqual(first.footprint_count, 1)
        self.assertEqual(first.net_count, 2)
        self.assertIn("Exact BOM", report)
        self.assertIn("| Fitted components | 1 |", report)
        self.assertIn("| Unfitted PCB features | 0 |", report)
        self.assertIn("rail-3v3-tolerance", report)
        self.assertTrue(report_ok, detail)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn(
            ["--build", "default"],
            [
                runner.calls[0][index : index + 2]
                for index in range(len(runner.calls[0]) - 1)
            ],
        )

    def test_volatile_build_ids_produce_byte_identical_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            runner = FakeRunner(build_ids=("run-one", "run-two"))
            first = check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=runner,
                write_report=True,
            )
            first_bytes = (project / BUILD_TEST_REPORT).read_bytes()
            second = check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=runner,
                write_report=True,
            )
            second_bytes = (project / BUILD_TEST_REPORT).read_bytes()

        self.assertTrue(first.wrote_report)
        self.assertFalse(second.wrote_report)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertIn(b"| BOM JSON |", second_bytes)
        self.assertIn(b"| Semantic BOM |", second_bytes)

    def test_bom_mismatch_fails_without_overwriting_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            report = project / BUILD_TEST_REPORT
            report.parent.mkdir()
            report.write_text("last passing report\n", encoding="utf-8")
            bom = project / "build" / "builds" / "default" / "default.bom.json"
            bom.write_text(
                bom.read_text(encoding="utf-8").replace("C25804", "C99999"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                BuildTestError,
                "missing expected LCSC",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                    write_report=True,
                )

            self.assertEqual(
                report.read_text(encoding="utf-8"),
                "last passing report\n",
            )

    def test_missing_or_unlisted_assertion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            source = project / "src" / "main.ato"
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "rail-3v3-tolerance",
                    "other-check",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                BuildTestError,
                "missing source assertions",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

    def test_spatial_mutation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(
                BuildTestError,
                "changed footprint placement",
            ):
                check_build_test(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(mutate_board=True),
                )

    def test_source_change_makes_saved_report_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                write_report=True,
            )
            source = project / "src" / "main.ato"
            source.write_text(
                source.read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            )
            ok, detail = saved_report_status(
                project,
                fingerprint_inputs(project),
            )

        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_assertion_change_makes_saved_report_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                write_report=True,
            )
            source = project / "src" / "main.ato"
            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "assert 3.3V within 3.3V +/- 5%",
                    "assert 3.3V within 3.3V +/- 2%",
                ),
                encoding="utf-8",
            )
            ok, detail = saved_report_status(
                project,
                fingerprint_inputs(project),
            )

        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_spatial_edit_does_not_make_step_six_report_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            check_build_test(
                project,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                write_report=True,
            )
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8")
                .replace("(at 110 120 0)", "(at 115 125 90)")
                .replace("(start 100 100)", "(start 99 99)"),
                encoding="utf-8",
            )
            ok, detail = saved_report_status(
                project,
                fingerprint_inputs(project),
            )

        self.assertTrue(ok, detail)


class CliTests(unittest.TestCase):
    def test_check_build_test_cli_reports_pass(self) -> None:
        result = BuildTestResult(
            "default",
            (
                BomComponent(
                    "C25804",
                    "0603WAF1002T5E",
                    "Resistor_SMD:R_0603_1608Metric",
                    1,
                    ("R1",),
                ),
            ),
            (AssertionLocation("rail-3v3-tolerance", Path("src/main.ato"), 3),),
            1,
            2,
            "abc",
            "report",
            BUILD_TEST_REPORT,
            True,
        )
        with (
            mock.patch("pcbforge.cli.check_build_test", return_value=result),
            mock.patch("builtins.print") as output,
        ):
            exit_code = main(["check-build-test", "--write-report", "/tmp/project"])

        self.assertEqual(exit_code, 0)
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("build + test passed", rendered)
        self.assertIn("updated docs/build-test.md", rendered)


if __name__ == "__main__":
    unittest.main()
