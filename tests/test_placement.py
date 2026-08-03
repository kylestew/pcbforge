from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.build_test import fingerprint_inputs
from pcbforge.cli import main
from pcbforge.policy import load_policy_profile, render_default_policy
from pcbforge.placement import (
    BRIEF_FILENAME,
    BriefResult,
    PlacementError,
    PlacementInputError,
    brief_status_fingerprint,
    check_brief,
    generate_brief,
    read_placement_contract,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN_LOCK_HASH = hashlib.sha256(
    (TOOL_ROOT / "toolchain" / "uv.lock").read_bytes()
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
  (footprint "Connector:USB_C"
    (layer "F.Cu")
    (at 100 110 0)
    (property "Reference" "J1")
    (pad "A1" smd rect (at 0 0) (net 1 "GND"))
    (pad "A6" smd rect (at 1 0) (net 2 "USB_D+"))
    (pad "A7" smd rect (at 2 0) (net 3 "USB_D-"))
  )
  (footprint "Package_QFP:LQFP-32"
    (layer "F.Cu")
    (at 120 110 0)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0) (net 4 "+3V3"))
    (pad "2" smd rect (at 1 0) (net 2 "USB_D+"))
    (pad "3" smd rect (at 2 0) (net 3 "USB_D-"))
    (pad "4" smd rect (at 3 0))
  )
  (gr_line
    (start 100 100)
    (end 150 100)
    (stroke (width 0.05) (type default))
    (layer "Edge.Cuts")
  )
)
"""

PLACEMENT = """placement_schema: 1
board:
  strategy: Keep the connector at the west edge and the MCU central.
  rules:
    - Preserve a continuous return path under USB.
groups:
  - id: connector
    priority: 1
    region: west edge
    rationale: Cable access.
    references: [J1]
  - id: controller
    priority: 2
    region: center
    rationale: Short fan-out.
    references: [U1]
placement_order: [connector, controller]
constraints:
  - id: usb-short
    type: proximity
    subjects: [J1.A6, U1.2]
    max_mm: 15
    rationale: Keep USB short.
  - id: connector-edge
    type: board-edge
    subjects: [J1]
    edge: west
    max_mm: 1
    rationale: Cable access.
  - id: connector-outward
    type: orientation
    subjects: [J1]
    direction: opening faces west
    rationale: Cable access.
net_classes:
  - name: power
    rationale: Wider board power.
    nets: [+3V3]
    clearance_mm: 0.2
    track_width_mm: 0.5
    via_diameter_mm: 0.7
    via_drill_mm: 0.3
  - name: usb
    rationale: Stable USB geometry.
    nets: [USB_D+, USB_D-]
    clearance_mm: 0.2
    track_width_mm: 0.2
    via_diameter_mm: 0.6
    via_drill_mm: 0.3
    differential_pair:
      width_mm: 0.2
      gap_mm: 0.2
      via_gap_mm: 0.2
checklist:
  - Check connector access and USB return path.
"""


def project_json() -> dict:
    return {
        "board": {"unknown_user_setting": {"keep": True}},
        "meta": {"filename": "garden-logger.kicad_pro", "version": 1},
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.2,
                    "track_width": 0.25,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "diff_pair_width": 0.2,
                    "diff_pair_gap": 0.2,
                    "diff_pair_via_gap": 0.2,
                    "priority": 2147483647,
                    "wire_width": 6,
                },
                {
                    "name": "User RF",
                    "clearance": 0.25,
                    "track_width": 0.3,
                    "priority": 0,
                    "custom": "preserve",
                },
                {
                    "name": "pcbforge:old",
                    "clearance": 9,
                    "track_width": 9,
                    "priority": 1,
                },
            ],
            "netclass_assignments": {"manual": "User RF"},
            "netclass_patterns": [
                {"netclass": "User RF", "pattern": "RF_USER"},
                {"netclass": "pcbforge:old", "pattern": "OLD"},
            ],
            "unknown": {"keep": "yes"},
        },
    }


class PlacementFixture(unittest.TestCase):
    def project(self, root: Path, *, current_step6: bool = True) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        policy_data = yaml.safe_load(render_default_policy())
        for assurance in policy_data["assurances"].values():
            assurance["evidence"] = ["test fixture evidence"]
        policy_data["sourcing"] = [
            {
                "lcsc": "C1",
                "jlc_class": "basic",
                "assembly_status": "available",
                "lifecycle": "active",
                "checked_on": "2026-07-27",
                "second_source": "C2",
            }
        ]
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
  profile_sha256: c1435709810dfff76e2b1b727a15ae575449331d7888cc4dc9c13252aece3784
""",
            encoding="utf-8",
        )
        (project / "ato.yaml").write_text(
            "builds:\n  default:\n    entry: src/main.ato:App\n",
            encoding="utf-8",
        )
        (project / "build-test.yaml").write_text(
            """build_test_schema: 1
build: default
bom:
  - lcsc: C1
    mpn: TEST
    footprint: Package_QFP:LQFP-32
    quantity: 1
board_footprints: 2
assertions: [test]
""",
            encoding="utf-8",
        )
        (project / "src").mkdir()
        (project / "src" / "main.ato").write_text(
            "module App:\n    pass\n",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_pcb").write_text(BOARD, encoding="utf-8")
        (project / "garden-logger.kicad_pro").write_text(
            json.dumps(project_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_dru").write_text(
            "(version 1)\n# user rules\n",
            encoding="utf-8",
        )
        (project / "placement.yaml").write_text(PLACEMENT, encoding="utf-8")
        if current_step6:
            fingerprint = fingerprint_inputs(project)
            (project / "docs").mkdir()
            (project / "docs" / "build-test.md").write_text(
                f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {fingerprint}
---
# Pass
""",
                encoding="utf-8",
            )
        return project


class SchemaTests(PlacementFixture):
    def test_reads_complete_contract_and_exact_board_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = read_placement_contract(
                self.project(Path(temporary)),
                tool_root=TOOL_ROOT,
            )

        self.assertEqual(contract.placement_order, ("connector", "controller"))
        self.assertEqual(contract.groups[0].references, ("J1",))
        self.assertEqual(contract.net_classes[1].nets, ("USB_D+", "USB_D-"))
        self.assertEqual(contract.constraints[0].subjects, ("J1.A6", "U1.2"))

    def test_rejects_unknown_keys_missing_coverage_and_bad_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            placement = project / "placement.yaml"
            placement.write_text(
                PLACEMENT.replace("    references: [U1]", "    references: [J1]")
                .replace("subjects: [J1.A6, U1.2]", "subjects: [J1.NOPE, U9.2]")
                .replace("placement_schema: 1", "placement_schema: 1\nunknown: no"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError,
                "(?s)unknown keys.*assigned more than once.*missing.*unknown pad",
            ):
                read_placement_contract(project, tool_root=TOOL_ROOT)

    def test_rejects_unknown_net_conflict_and_profile_violations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            placement = project / "placement.yaml"
            placement.write_text(
                PLACEMENT.replace(
                    "nets: [+3V3]",
                    "nets: [+3V3, USB_D+, DOES_NOT_EXIST]",
                )
                .replace("clearance_mm: 0.2", "clearance_mm: 0.1", 1)
                .replace("via_diameter_mm: 0.7", "via_diameter_mm: 0.5", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError,
                "(?s)multiple classes.*unknown exact nets.*below profile minimum",
            ):
                read_placement_contract(project, tool_root=TOOL_ROOT)

    def test_rejects_non_finite_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            placement = project / "placement.yaml"
            placement.write_text(
                PLACEMENT.replace("track_width_mm: 0.5", "track_width_mm: .nan"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError,
                "expected a positive number",
            ):
                read_placement_contract(project, tool_root=TOOL_ROOT)

    def test_requires_supported_layout_handoff_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pins = project / ".pcbforge"
            pins.write_text(
                pins.read_text(encoding="utf-8").replace("schema: 1", "schema: 9", 1),
                encoding="utf-8",
            )
            report = project / "docs" / "build-test.md"
            report.write_text(
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
            with self.assertRaisesRegex(
                PlacementInputError,
                "unsupported version — restart the project",
            ):
                generate_brief(project, tool_root=TOOL_ROOT)

    def test_rejects_unpinned_rules_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pins = project / ".pcbforge"
            pins.write_text(
                pins.read_text(encoding="utf-8").replace(
                    "c1435709810dfff76e2b1b727a15ae575449331d7888cc4dc9c13252aece3784",
                    "stale",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError,
                "pinned JLC rules profile does not match",
            ):
                read_placement_contract(project, tool_root=TOOL_ROOT)

    def test_rejects_conflicting_user_net_class_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pro = project / "garden-logger.kicad_pro"
            data = json.loads(pro.read_text(encoding="utf-8"))
            data["net_settings"]["netclass_patterns"].append(
                {"netclass": "User RF", "pattern": "USB_D+"}
            )
            pro.write_text(json.dumps(data) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                PlacementInputError,
                "user net-class assignments conflict",
            ):
                generate_brief(project, tool_root=TOOL_ROOT)


class GeneratorTests(PlacementFixture):
    def test_current_guidance_generates_placement_brief_under_docs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            fingerprint = fingerprint_inputs(project)
            (project / "docs" / "build-test.md").write_text(
                f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {fingerprint}
---
# Pass
""",
                encoding="utf-8",
            )

            result = generate_brief(project, tool_root=TOOL_ROOT)
            brief_exists = (project / result.brief_path).is_file()
            root_alias_exists = (project / "brief.md").exists()

        self.assertEqual(result.brief_path, Path("docs/placement-brief.md"))
        self.assertTrue(brief_exists)
        self.assertFalse(root_alias_exists)

    def test_generation_is_safe_preserving_idempotent_and_checkable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            board_before = (project / "garden-logger.kicad_pcb").read_bytes()
            rules_before = (project / "garden-logger.kicad_dru").read_bytes()
            first = generate_brief(project, tool_root=TOOL_ROOT)
            project_after = json.loads(
                (project / "garden-logger.kicad_pro").read_text(encoding="utf-8")
            )
            brief_path = project / first.brief_path
            brief_text = brief_path.read_text(encoding="utf-8")
            brief_after = brief_path.read_bytes()
            pro_after = (project / "garden-logger.kicad_pro").read_bytes()
            second = generate_brief(project, tool_root=TOOL_ROOT)
            checked = check_brief(project, tool_root=TOOL_ROOT)
            board_unchanged = (
                board_before == (project / "garden-logger.kicad_pcb").read_bytes()
            )
            brief_stable = brief_after == brief_path.read_bytes()
            project_stable = (
                pro_after == (project / "garden-logger.kicad_pro").read_bytes()
            )
            rules_unchanged = (
                rules_before == (project / "garden-logger.kicad_dru").read_bytes()
            )

        self.assertTrue(first.wrote_brief)
        self.assertTrue(first.wrote_project)
        self.assertFalse(second.wrote_brief)
        self.assertFalse(second.wrote_project)
        self.assertEqual(checked.reference_count, 2)
        self.assertTrue(board_unchanged)
        self.assertTrue(brief_stable)
        self.assertTrue(project_stable)
        self.assertTrue(rules_unchanged)
        self.assertIn("pcbforge_brief_schema: 1", brief_text)
        self.assertIn("## Typed constraints", brief_text)
        self.assertIn("pcbforge:usb", brief_text)
        classes = project_after["net_settings"]["classes"]
        self.assertEqual(
            [item["name"] for item in classes],
            ["Default", "User RF", "pcbforge:power", "pcbforge:usb"],
        )
        self.assertEqual(classes[1]["custom"], "preserve")
        self.assertEqual(
            [item["priority"] for item in classes],
            [2147483647, 0, 1, 2],
        )
        self.assertEqual(
            project_after["net_settings"]["netclass_assignments"],
            {"manual": "User RF"},
        )
        self.assertEqual(
            project_after["net_settings"]["netclass_patterns"][0],
            {"netclass": "User RF", "pattern": "RF_USER"},
        )
        self.assertEqual(
            project_after["board"]["unknown_user_setting"],
            {"keep": True},
        )

    def test_spatial_and_user_class_edits_do_not_stale_brief_or_step_six(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            generate_brief(project, tool_root=TOOL_ROOT)
            step6_before = fingerprint_inputs(project)
            brief_before = brief_status_fingerprint(project)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    "(at 120 110 0)",
                    "(at 125 115 90)",
                )
                + "\n",
                encoding="utf-8",
            )
            pro = project / "garden-logger.kicad_pro"
            data = json.loads(pro.read_text(encoding="utf-8"))
            data["net_settings"]["classes"][0]["wire_width"] = 8
            data["net_settings"]["classes"][1]["track_width"] = 0.35
            data["board"]["another_user_setting"] = "keep"
            pro.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            checked = check_brief(project, tool_root=TOOL_ROOT)
            step6_after = fingerprint_inputs(project)
            brief_after = brief_status_fingerprint(project)

        self.assertEqual(step6_after, step6_before)
        self.assertEqual(brief_after, brief_before)
        self.assertEqual(checked.reference_count, 2)

    def test_topology_and_contract_changes_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            generate_brief(project, tool_root=TOOL_ROOT)
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8").replace(
                    "Cable access.",
                    "Direct cable access.",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PlacementError, "stale"):
                check_brief(project, tool_root=TOOL_ROOT)

            generate_brief(project, tool_root=TOOL_ROOT)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    '(net 4 "+3V3")',
                    '(net 5 "+3V3_A")',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError,
                "CIRCUIT acceptance is not current",
            ):
                check_brief(project, tool_root=TOOL_ROOT)

    def test_circuit_source_change_stales_step_seven_after_step_six_refresh(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            generate_brief(project, tool_root=TOOL_ROOT)
            before = brief_status_fingerprint(project)
            source = project / "src" / "main.ato"
            source.write_text(
                source.read_text(encoding="utf-8") + "\n# circuit review changed\n",
                encoding="utf-8",
            )
            step6 = fingerprint_inputs(project)
            report = project / "docs" / "build-test.md"
            report.write_text(
                f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {step6}
---
# Pass
""",
                encoding="utf-8",
            )
            after = brief_status_fingerprint(project)
            with self.assertRaisesRegex(PlacementError, "stale"):
                check_brief(project, tool_root=TOOL_ROOT)

        self.assertNotEqual(after, before)

    def test_generation_rolls_back_if_second_output_cannot_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pro = project / "garden-logger.kicad_pro"
            pro_before = pro.read_bytes()
            board_before = (project / "garden-logger.kicad_pcb").read_bytes()
            real_replace = os.replace
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated commit failure")
                return real_replace(source, destination)

            with (
                mock.patch("pcbforge.fsutil.os.replace", side_effect=fail_second),
                self.assertRaisesRegex(PlacementError, "atomically write"),
            ):
                generate_brief(project, tool_root=TOOL_ROOT)
            pro_restored = pro.read_bytes() == pro_before
            brief_absent = not (project / BRIEF_FILENAME).exists()
            board_restored = (
                project / "garden-logger.kicad_pcb"
            ).read_bytes() == board_before

        self.assertTrue(pro_restored)
        self.assertTrue(brief_absent)
        self.assertTrue(board_restored)

    def test_generation_requires_current_step_six_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), current_step6=False)
            with self.assertRaisesRegex(
                PlacementInputError,
                "CIRCUIT acceptance is not current",
            ):
                generate_brief(project, tool_root=TOOL_ROOT)


class StatusAndCliTests(PlacementFixture):
    def test_cli_commands_report_results_and_error_categories(self) -> None:
        result = BriefResult(
            Path("/tmp/project"),
            "abc",
            2,
            3,
            2,
            2,
            Path("docs/placement-brief.md"),
            Path("board.kicad_pro"),
            True,
            False,
        )
        with (
            mock.patch("pcbforge.cli.generate_brief", return_value=result),
            mock.patch("builtins.print") as output,
        ):
            exit_code = main(["prepare-layout", "/tmp/project"])
        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertEqual(exit_code, 0)
        self.assertIn("PCB unchanged", rendered)

        with (
            mock.patch(
                "pcbforge.cli.check_brief",
                side_effect=PlacementInputError("bad placement"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-layout-handoff", "/tmp/project"]), 2)

        with (
            mock.patch(
                "pcbforge.cli.check_brief",
                side_effect=PlacementError("stale brief"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-layout-handoff", "/tmp/project"]), 1)


class ContractFormattingTests(unittest.TestCase):
    def test_example_is_valid_yaml(self) -> None:
        data = yaml.safe_load(PLACEMENT)
        self.assertEqual(data["placement_schema"], 1)
