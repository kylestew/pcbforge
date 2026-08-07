from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.build_test import fingerprint_inputs
from pcbforge.cli import main
from pcbforge.fab import (
    ARCHIVE_SUFFIX,
    DRC_REPORT_FILENAME,
    JLC_BOM_FILENAME,
    JLC_CPL_FILENAME,
    MANIFEST_FILENAME,
    POS_FILENAME,
    FabError,
    FabFile,
    FabInputError,
    FabResult,
    check_fab,
    generate_fab,
    read_manifest,
)
from pcbforge.markdown_metadata import metadata_yaml
from pcbforge.policy import load_policy_profile, render_default_policy

TOOL_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN_LOCK_HASH = hashlib.sha256(
    (TOOL_ROOT / "toolchain" / "uv.lock").read_bytes()
).hexdigest()
RULES_HASH = hashlib.sha256(
    (TOOL_ROOT / "rules" / "jlc-2layer.json").read_bytes()
).hexdigest()

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
    (pad "1" smd rect (at -0.8 0) (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (net 2 "+3V3"))
  )
  (footprint "Package_QFP:LQFP-32"
    (layer "F.Cu")
    (at 120 110 90)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0) (net 2 "+3V3"))
    (pad "2" smd rect (at 1 0) (net 1 "GND"))
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
  - lcsc: C8734
    mpn: STM32G031K8T6
    footprint: Package_QFP:LQFP-32
    quantity: 1
board_footprints: 2
assertions:
  - rail-3v3-tolerance
"""

STATUS = """---
pcbforge_status_schema: 1
updated_at: '2026-08-03T10:00:00+00:00'
events:
- at: '2026-08-03T09:00:00+00:00'
  phase: spec
  action: complete
  note: approved
  approval_fingerprint: {hash}
  content_fingerprint: {hash}
- at: '2026-08-03T09:10:00+00:00'
  phase: circuit
  action: complete
  note: approved
  approval_fingerprint: {hash}
  content_fingerprint: {hash}
- at: '2026-08-03T09:20:00+00:00'
  phase: layout
  action: complete
  note: approved
  approval_fingerprint: {hash}
  content_fingerprint: {hash}
- at: '2026-08-03T09:30:00+00:00'
  phase: verify
  action: complete
  note: approved
  approval_fingerprint: {hash}
  content_fingerprint: {hash}
policy_events: []
transition_events: []
checks: {{}}
---
# fixture
""".format(hash="a" * 64)

PLACEMENTS = (("R1", "110.0000", "120.0000", "0.000000", "top"),
              ("U1", "120.0000", "110.0000", "90.000000", "bottom"))


class FakeKicad:
    """Stand-in for the pinned KiCad 9 CLI used by fab generation."""

    def __init__(
        self,
        *,
        stem: str = "garden-logger",
        drc_violations: int = 0,
        skip_layers: tuple[str, ...] = (),
        omit_placements: tuple[str, ...] = (),
        fail: str = "",
        clock: int = 0,
    ) -> None:
        self.stem = stem
        self.drc_violations = drc_violations
        self.skip_layers = skip_layers
        self.omit_placements = omit_placements
        self.fail = fail
        self.clock = clock
        self.calls: list[list[str]] = []

    def _value(self, command: list[str], flag: str) -> str:
        return command[command.index(flag) + 1]

    def _stamp(self) -> str:
        self.clock += 1
        return f"2026-08-03T12:00:{self.clock:02d}+01:00"

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        self.calls.append(command)
        verb = command[3] if command[2] == "export" else command[2]
        if self.fail and self.fail in verb:
            return subprocess.CompletedProcess(command, 1, "", "export failed\n")
        if verb == "gerbers":
            output = Path(self._value(command, "--output"))
            for layer in self._value(command, "--layers").split(","):
                if layer in self.skip_layers:
                    continue
                name = f"{self.stem}-{layer.replace('.', '_')}.gbr"
                (output / name).write_text(
                    f"G04 #@! TF.CreationDate,{self._stamp()}*\n"
                    f"G04 Created by KiCad (PCBNEW 9.0.9) date {self._stamp()}*\n"
                    f"G04 layer {layer}*\nM02*\n",
                    encoding="utf-8",
                )
            (output / f"{self.stem}-job.gbrjob").write_text(
                json.dumps(
                    {
                        "Header": {"GenerationSoftware": "KiCad"},
                        "GeneralSpecs": {"CreationDate": self._stamp()},
                    }
                ),
                encoding="utf-8",
            )
        elif verb == "drill":
            output = Path(self._value(command, "--output"))
            for kind in ("PTH", "NPTH"):
                (output / f"{self.stem}-{kind}.drl").write_text(
                    f"M48\n; DRILL file {{KiCad 9.0.9}} date {self._stamp()}\n"
                    "METRIC\nT1C0.600\nM30\n",
                    encoding="utf-8",
                )
        elif verb == "pos":
            rows = "\n".join(
                f'"{ref}","","PKG",{x},{y},{rot},{side}'
                for ref, x, y, rot, side in PLACEMENTS
                if ref not in self.omit_placements
            )
            Path(self._value(command, "--output")).write_text(
                "Ref,Val,Package,PosX,PosY,Rot,Side\n" + rows + "\n",
                encoding="utf-8",
            )
        elif verb == "drc":
            Path(self._value(command, "--output")).write_text(
                json.dumps(
                    {
                        "$schema": "https://schemas.kicad.org/drc.v1.json",
                        "date": self._stamp(),
                        "kicad_version": "9.0.9",
                        "coordinate_units": "mm",
                        "violations": [
                            {"severity": "error"} for _ in range(self.drc_violations)
                        ],
                        "unconnected_items": [],
                        "schematic_parity": [],
                    }
                ),
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")


class FabFixture(unittest.TestCase):
    def project(self, root: Path, *, verified: bool = True) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        policy_data = yaml.safe_load(render_default_policy())
        for assurance in policy_data["assurances"].values():
            assurance["evidence"] = ["fixture evidence"]
        (project / "policy.yaml").write_text(
            yaml.safe_dump(policy_data, sort_keys=False),
            encoding="utf-8",
        )
        _, _, policy_hash = load_policy_profile(TOOL_ROOT)
        (project / ".pcbforge").write_text(
            f"""schema: 1
toolchain:
  atopile: 0.15.7
  kicad: 9.0.9
  uv_lock_sha256: {TOOLCHAIN_LOCK_HASH}
guidance:
  build_test_schema: 1
  layout_handoff_schema: 1
  approval_schema: 1
  policy_schema: 1
policy:
  profile: pcbforge-standard-v1
  profile_sha256: {policy_hash}
  baseline_approval: spec
rules:
  profile: jlc-2layer-conservative-v1
  profile_sha256: {RULES_HASH}
""",
            encoding="utf-8",
        )
        (project / "ato.yaml").write_text(
            "builds:\n  default:\n    entry: src/main.ato:App\n",
            encoding="utf-8",
        )
        (project / "build-test.yaml").write_text(CONTRACT, encoding="utf-8")
        (project / "src").mkdir()
        (project / "src" / "main.ato").write_text(
            "module App:\n    pass\n",
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
                    "build_id": "fixture",
                    "components": [
                        {
                            "lcsc": "C25804",
                            "mpn": "0603WAF1002T5E",
                            "package": "Resistor_SMD:R_0603_1608Metric",
                            "quantity": 1,
                            "usages": [{"designator": "R1"}],
                        },
                        {
                            "lcsc": "C8734",
                            "mpn": "STM32G031K8T6",
                            "package": "Package_QFP:LQFP-32",
                            "quantity": 1,
                            "usages": [{"designator": "U1"}],
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (build / "default.bom.csv").write_text(
            "Designator,Footprint,Quantity,Value,Manufacturer,Partnumber,"
            "LCSC Part #\nR1,Resistor_SMD:R_0603_1608Metric,1,10k,UNI-ROYAL,"
            "0603WAF1002T5E,C25804\n",
            encoding="utf-8",
        )
        (project / "fab").mkdir()
        (project / "fab" / ".gitkeep").write_text("", encoding="utf-8")
        (project / "docs").mkdir()
        (project / "docs" / "build-test.md").write_text(
            f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {fingerprint_inputs(project)}
---
# Pass
""",
            encoding="utf-8",
        )
        if verified:
            (project / "STATUS.md").write_text(STATUS, encoding="utf-8")
        return project

    @contextlib.contextmanager
    def verify_complete(self):
        """Present the fixture project as a currently approved VERIFY."""
        with (
            mock.patch(
                "pcbforge.status._static_evidence",
                return_value=(True, "fixture evidence", True),
            ),
            mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ),
            mock.patch(
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.baseline,
            ),
            mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.handoff,
            ),
        ):
            yield

    def generate(self, project: Path, runner=None):
        with self.verify_complete():
            return generate_fab(
                project,
                tool_root=TOOL_ROOT,
                runner=runner or FakeKicad(),
            )


class GenerationTests(FabFixture):
    def test_generates_validated_packet_and_records_the_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            board_before = (project / "garden-logger.kicad_pcb").read_bytes()
            result = self.generate(project)
            fab = project / "fab"
            names = sorted(path.name for path in fab.iterdir())
            manifest = read_manifest(project)
            document = yaml.safe_load(
                metadata_yaml(
                    (project / "STATUS.md").read_text(encoding="utf-8")
                )
            )
            board_after = (project / "garden-logger.kicad_pcb").read_bytes()

        self.assertTrue(result.recorded)
        self.assertEqual(board_after, board_before)
        for expected in (
            JLC_BOM_FILENAME,
            JLC_CPL_FILENAME,
            DRC_REPORT_FILENAME,
            MANIFEST_FILENAME,
            POS_FILENAME,
            f"garden-logger{ARCHIVE_SUFFIX}",
            "garden-logger-Edge_Cuts.gbr",
            "garden-logger-PTH.drl",
        ):
            self.assertIn(expected, names)
        self.assertEqual(manifest["pcbforge_fab_schema"], 1)
        self.assertEqual(manifest["rules_profile"], "jlc-2layer-conservative-v1")
        self.assertEqual(manifest["sources"]["board_sha256"], hashlib.sha256(
            board_before
        ).hexdigest())
        self.assertEqual(
            [event["transition"] for event in document["transition_events"]],
            ["fab-out"],
        )

    def test_manifest_commands_are_machine_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            manifest = read_manifest(project)

        rendered = json.dumps(manifest["commands"])
        self.assertIn("kicad-cli", rendered)
        self.assertNotIn(str(TOOL_ROOT), rendered)
        self.assertNotIn(temporary, rendered)

    def test_jlc_files_carry_exact_designators_and_placements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            bom = (project / "fab" / JLC_BOM_FILENAME).read_text(encoding="utf-8")
            cpl = (project / "fab" / JLC_CPL_FILENAME).read_text(encoding="utf-8")

        self.assertEqual(
            bom,
            "Comment,Designator,Footprint,LCSC Part #\n"
            "0603WAF1002T5E,R1,R_0603_1608Metric,C25804\n"
            "STM32G031K8T6,U1,LQFP-32,C8734\n",
        )
        self.assertEqual(
            cpl,
            "Designator,Mid X,Mid Y,Layer,Rotation\n"
            "R1,110.0000,120.0000,top,0.0000\n"
            "U1,120.0000,110.0000,bottom,90.0000\n",
        )

    def test_regeneration_is_identical_apart_from_kicad_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            first = self.generate(project, runner=FakeKicad(clock=0))
            first_raw = (project / "fab" / "garden-logger-F_Cu.gbr").read_bytes()
            second = self.generate(project, runner=FakeKicad(clock=50))
            second_raw = (project / "fab" / "garden-logger-F_Cu.gbr").read_bytes()

        by_name = {item.name: item for item in first.files}
        self.assertNotEqual(first_raw, second_raw)
        for item in second.files:
            self.assertEqual(
                item.normalized_sha256,
                by_name[item.name].normalized_sha256,
                f"{item.name} changed beyond its timestamp",
            )
        self.assertTrue(second.recorded)

    def test_stale_packet_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            stale = project / "fab" / "garden-logger-Old_Layer.gbr"
            stale.write_text("stale\n", encoding="utf-8")
            self.generate(project)
            names = {path.name for path in (project / "fab").iterdir()}

        self.assertNotIn(stale.name, names)
        self.assertIn(".gitkeep", names)

    def test_archive_is_deterministic_and_carries_the_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            archive = project / "fab" / f"garden-logger{ARCHIVE_SUFFIX}"
            with zipfile.ZipFile(archive) as opened:
                names = sorted(opened.namelist())
                stamps = {info.date_time for info in opened.infolist()}
                board = opened.read("garden-logger.kicad_pcb")

        self.assertEqual(stamps, {(1980, 1, 1, 0, 0, 0)})
        self.assertIn("garden-logger.kicad_pcb", names)
        self.assertIn(JLC_BOM_FILENAME, names)
        self.assertEqual(board.decode(), BOARD)


class RefusalTests(FabFixture):
    def test_refuses_before_verify_is_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), verified=False)
            (project / "STATUS.md").write_text(
                "---\npcbforge_status_schema: 1\nupdated_at: ''\n"
                "events: []\npolicy_events: []\ntransition_events: []\n"
                "checks: {}\n---\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FabInputError, "before VERIFY is approved"):
                generate_fab(project, tool_root=TOOL_ROOT, runner=FakeKicad())
            wrote = [
                path.name
                for path in (project / "fab").iterdir()
                if path.name != ".gitkeep"
            ]

        self.assertEqual(wrote, [])

    def test_refuses_when_circuit_acceptance_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "src" / "main.ato").write_text(
                "module App:\n    pass\n    # changed\n",
                encoding="utf-8",
            )
            with (
                self.verify_complete(),
                self.assertRaisesRegex(FabInputError, "CIRCUIT acceptance"),
            ):
                generate_fab(project, tool_root=TOOL_ROOT, runner=FakeKicad())

    def test_refuses_when_the_board_no_longer_passes_drc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with (
                self.verify_complete(),
                self.assertRaisesRegex(FabError, "no longer passes DRC"),
            ):
                generate_fab(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeKicad(drc_violations=2),
                )
            wrote = [
                path.name
                for path in (project / "fab").iterdir()
                if path.name != ".gitkeep"
            ]

        self.assertEqual(wrote, [])

    def test_refuses_when_an_assembly_part_has_no_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with (
                self.verify_complete(),
                self.assertRaisesRegex(FabError, "missing from the placement file"),
            ):
                generate_fab(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeKicad(omit_placements=("U1",)),
                )

    def test_refuses_when_a_required_layer_is_not_plotted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with (
                self.verify_complete(),
                self.assertRaisesRegex(FabError, "missing plotted layer Edge.Cuts"),
            ):
                generate_fab(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeKicad(skip_layers=("Edge.Cuts",)),
                )

    def test_reports_a_failed_export_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with (
                self.verify_complete(),
                self.assertRaisesRegex(FabError, "drill export failed"),
            ):
                generate_fab(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeKicad(fail="drill"),
                )


class CheckTests(FabFixture):
    def test_check_passes_for_a_freshly_generated_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            result = check_fab(project, tool_root=TOOL_ROOT)

        self.assertEqual(result.component_count, 2)
        self.assertEqual(result.placement_count, 2)

    def test_check_detects_a_hand_edited_packet_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            bom = project / "fab" / JLC_BOM_FILENAME
            bom.write_text(
                bom.read_text(encoding="utf-8").replace("C8734", "C0000"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FabError, "was modified after generation"):
                check_fab(project, tool_root=TOOL_ROOT)

    def test_check_detects_a_changed_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.generate(project)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "(at 110 120 0)",
                    "(at 111 120 0)",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FabError, "board changed"):
                check_fab(project, tool_root=TOOL_ROOT)

    def test_check_requires_a_generated_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(FabInputError, "missing fab/manifest.json"):
                check_fab(project, tool_root=TOOL_ROOT)


class CliTests(unittest.TestCase):
    def result(self) -> FabResult:
        return FabResult(
            Path("/tmp/project"),
            "garden-logger",
            (FabFile(JLC_BOM_FILENAME, "a" * 64, "b" * 64, 12),),
            f"garden-logger{ARCHIVE_SUFFIX}",
            ("F.Cu", "B.Cu"),
            2,
            2,
            True,
        )

    def test_cli_reports_results_and_error_categories(self) -> None:
        with (
            mock.patch("pcbforge.cli.generate_fab", return_value=self.result()),
            mock.patch("builtins.print") as output,
        ):
            generated = main(["fab-out", "/tmp/project"])
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)

        self.assertEqual(generated, 0)
        self.assertIn("recorded the VERIFY → ORDER FAB-OUT transition", rendered)
        self.assertIn("PCB unchanged", rendered)

        with (
            mock.patch(
                "pcbforge.cli.check_fab",
                side_effect=FabInputError("verify first"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-fab-out", "/tmp/project"]), 2)

        with (
            mock.patch(
                "pcbforge.cli.check_fab",
                side_effect=FabError("stale packet"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-fab-out", "/tmp/project"]), 1)


if __name__ == "__main__":
    unittest.main()
