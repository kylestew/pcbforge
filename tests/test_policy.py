from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.cli import main
from pcbforge.policy import (
    PolicyInputError,
    check_policy,
    load_policy_profile,
    policy_baseline_fingerprint,
    policy_circuit_fingerprint,
    policy_exception_fingerprints,
    render_default_policy,
)
from pcbforge.status import (
    StatusDocument,
    StatusEvent,
    _approval_fingerprint,
    approve_phase,
    inspect_status,
    mark_policy,
    mark_status,
    review_phase,
    write_status,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN_LOCK_HASH = hashlib.sha256(
    (TOOL_ROOT / "toolchain" / "uv.lock").read_bytes()
).hexdigest()
RULES_2L_HASH = hashlib.sha256(
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

BOARD_0603 = """(kicad_pcb
  (version 20241229)
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (at 110 120)
    (property "Reference" "R1")
    (pad "1" smd rect (at -0.8 0) (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (net 2 "+3V3"))
  )
)
"""

BUILD_TEST = """build_test_schema: 1
build: default
bom:
  - lcsc: C25804
    mpn: 0603WAF1002T5E
    footprint: Resistor_SMD:R_0603_1608Metric
    quantity: 1
board_footprints: 1
assertions: [rail-test]
"""


class PolicyFixture(unittest.TestCase):
    def project(
        self,
        root: Path,
        *,
        schema: int = 1,
        board: str = BOARD_0603,
        complete_evidence: bool = True,
        include_build_test: bool = True,
    ) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(SPEC, encoding="utf-8")
        policy = yaml.safe_load(render_default_policy())
        if complete_evidence:
            for assurance in policy["assurances"].values():
                assurance["evidence"] = ["fixture evidence"]
        if include_build_test:
            policy["sourcing"] = [
                {
                    "lcsc": "C25804",
                    "jlc_class": "basic",
                    "assembly_status": "available",
                    "lifecycle": "active",
                    "checked_on": "2026-07-27",
                    "second_source": "C25803",
                }
            ]
        (project / "policy.yaml").write_text(
            yaml.safe_dump(policy, sort_keys=False),
            encoding="utf-8",
        )
        _, _, policy_hash = load_policy_profile(TOOL_ROOT)
        policy_pin = f"""policy:
  profile: pcbforge-standard-v1
  profile_sha256: {policy_hash}
  baseline_approval: spec
"""
        pcbforge_pin = """pcbforge:
  revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  dirty: false
"""
        guidance = """  agents_schema: 1
  architect_schema: 1
  architecture_diagram_schema: 1
  mcu_schema: 1
  circuit_schema: 1
  circuit_review_schema: 3
  policy_schema: 1
  build_test_schema: 1
  layout_handoff_schema: 1
  approval_schema: 1
  status_schema: 1
"""
        (project / ".pcbforge").write_text(
            f"""schema: {schema}
project: garden-logger
{pcbforge_pin}\
toolchain:
  atopile: "0.15.7"
  kicad: "9.0.9"
  uv_lock_sha256: {TOOLCHAIN_LOCK_HASH}
rules:
  profile: jlc-2layer-conservative-v1
  profile_sha256: {RULES_2L_HASH}
{policy_pin}\
guidance:
{guidance}\
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
            board,
            encoding="utf-8",
        )
        (project / "fab").mkdir()
        if include_build_test:
            (project / "build-test.yaml").write_text(
                BUILD_TEST,
                encoding="utf-8",
            )
        return project


class PolicyCheckerTests(PolicyFixture):
    def test_policy_fingerprints_scope_baseline_circuit_and_sourcing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(
                Path(temporary),
                complete_evidence=False,
                include_build_test=False,
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            baseline_before = policy_baseline_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )
            circuit_before = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["assurances"]["reverse-polarity"]["evidence"] = [
                "Reviewed reverse-polarity implementation.",
                "Checked the protected input net.",
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            baseline_with_evidence = policy_baseline_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )
            circuit_with_evidence = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["assurances"]["reverse-polarity"]["evidence"].reverse()
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            circuit_with_reordered_evidence = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["sourcing"] = [
                {
                    "lcsc": "C25804",
                    "jlc_class": "basic",
                    "assembly_status": "available",
                    "lifecycle": "active",
                    "checked_on": "2026-07-31",
                    "second_source": "C25803",
                }
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            circuit_with_sourcing = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["exceptions"] = [
                {
                    "id": "allow-0402",
                    "rule": "components.commodity-package",
                    "scope": "R1",
                    "rationale": "Required by the approved density constraint.",
                },
                {
                    "id": "allow-castellation",
                    "rule": "manufacturing.edge-clearance",
                    "scope": "J1",
                    "rationale": "Required by the approved module interface.",
                },
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            circuit_with_exception = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["exceptions"].reverse()
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            circuit_with_reordered_exceptions = policy_circuit_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

            policy["assurances"]["reverse-polarity"]["status"] = (
                "not-applicable"
            )
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            baseline_with_status_change = policy_baseline_fingerprint(
                project,
                tool_root=TOOL_ROOT,
            )

        self.assertEqual(baseline_before, baseline_with_evidence)
        self.assertNotEqual(circuit_before, circuit_with_evidence)
        self.assertEqual(circuit_with_evidence, circuit_with_reordered_evidence)
        self.assertEqual(circuit_with_evidence, circuit_with_sourcing)
        self.assertNotEqual(circuit_with_sourcing, circuit_with_exception)
        self.assertEqual(
            circuit_with_exception,
            circuit_with_reordered_exceptions,
        )
        self.assertNotEqual(baseline_with_evidence, baseline_with_status_change)

    def test_standard_policy_passes_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            result = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="circuit",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.violations, ())
        self.assertTrue(result.baseline_fingerprint)

    def test_future_assurance_evidence_does_not_block_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(
                Path(temporary),
                complete_evidence=False,
                include_build_test=False,
            )
            spec_result = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="spec",
            )
            implement_result = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="circuit",
            )

        self.assertTrue(spec_result.ok)
        self.assertFalse(implement_result.ok)
        self.assertIn(
            "assurance.reverse-polarity",
            {violation.rule for violation in implement_result.violations},
        )

    def test_hard_fabricator_constraint_cannot_be_excepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["manufacturing"]["fabricator"] = "other"
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )

            result = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="spec",
            )

        self.assertFalse(result.ok)
        violation = next(
            item for item in result.violations if item.rule == "hard.fabricator"
        )
        self.assertTrue(violation.hard)

    def test_0402_requires_a_current_scoped_exception(self) -> None:
        board = BOARD_0603.replace(
            "R_0603_1608Metric",
            "R_0402_1005Metric",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(
                Path(temporary),
                board=board,
                include_build_test=False,
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["exceptions"] = [
                {
                    "id": "allow-r1-0402",
                    "rule": "components.commodity-package",
                    "scope": "R1",
                    "rationale": "Required for the approved density constraint.",
                }
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )

            blocked = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="circuit",
            )
            fingerprint = policy_exception_fingerprints(
                project,
                tool_root=TOOL_ROOT,
            )["allow-r1-0402"]
            approved = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="circuit",
                exception_approvals={"allow-r1-0402": fingerprint},
            )

        self.assertFalse(blocked.ok)
        self.assertIn("lacks current explicit approval", blocked.violations[0].message)
        self.assertTrue(approved.ok)

    def test_tampered_toolchain_and_rules_pins_are_hard_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            pins_path = project / ".pcbforge"
            pins = yaml.safe_load(pins_path.read_text(encoding="utf-8"))
            pins["toolchain"]["atopile"] = "unreviewed"
            pins["rules"]["profile_sha256"] = "unreviewed"
            pins_path.write_text(
                yaml.safe_dump(pins, sort_keys=False),
                encoding="utf-8",
            )

            result = check_policy(
                project,
                tool_root=TOOL_ROOT,
                through_phase="architect",
            )

        violations = {
            violation.rule: violation for violation in result.violations
        }
        self.assertTrue(violations["hard.toolchain"].hard)
        self.assertTrue(violations["hard.fabricator-rules"].hard)


class PolicyApprovalTests(PolicyFixture):
    def test_preproject_spec_exception_can_be_approved_before_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "garden-logger"
            project.mkdir()
            (project / "spec.md").write_text(SPEC, encoding="utf-8")
            policy = yaml.safe_load(render_default_policy())
            policy["manufacturing"]["thickness_mm"] = 0.8
            policy["exceptions"] = [
                {
                    "id": "allow-0-8-mm-fr4",
                    "rule": "manufacturing.thickness",
                    "scope": "project",
                    "rationale": "Improve through-board light transmission.",
                }
            ]
            (project / "policy.yaml").write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            write_status(project)

            approved = mark_policy(
                project,
                "exception-approved",
                "User approved 0.8 mm FR4 for the optical experiment.",
                subject="allow-0-8-mm-fr4",
                tool_root=TOOL_ROOT,
            )
            review = review_phase(project, "spec", tool_root=TOOL_ROOT)

        self.assertEqual(
            approved.report.document.policy_events[-1].action,
            "exception-approved",
        )
        self.assertEqual(
            approved.report.document.policy_events[-1].subject,
            "allow-0-8-mm-fr4",
        )
        self.assertTrue(review.ready)

    def test_new_project_spec_approval_is_bound_to_policy_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "garden-logger"
            project.mkdir()
            (project / "spec.md").write_text(SPEC, encoding="utf-8")
            (project / "policy.yaml").write_text(
                render_default_policy(),
                encoding="utf-8",
            )
            review = review_phase(
                project,
                "spec",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "spec",
                review.fingerprint,
                "User approved requirements and initial policy",
                tool_root=TOOL_ROOT,
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["manufacturing"]["thickness_mm"] = 2.0
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )

            stale = inspect_status(project)
            reopened = write_status(project)

        self.assertEqual(stale.phases[0].state, "Blocked")
        self.assertIn("approval is stale", stale.phases[0].detail)
        self.assertEqual(reopened.report.document.events[-1].action, "reopened")

    def test_exception_approval_reopens_affected_completed_phase(self) -> None:
        board = BOARD_0603.replace(
            "R_0603_1608Metric",
            "R_0402_1005Metric",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(
                Path(temporary),
                board=board,
                include_build_test=False,
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["exceptions"] = [
                {
                    "id": "allow-r1-0402",
                    "rule": "components.commodity-package",
                    "scope": "R1",
                    "rationale": "Approved density tradeoff.",
                }
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            phases = ("spec", "architect", "circuit")
            events = tuple(
                StatusEvent(
                    f"2026-07-27T10:0{index}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                    _approval_fingerprint(project, phase),
                )
                for index, phase in enumerate(phases)
            )
            with mock.patch(
                "pcbforge.status._static_evidence",
                return_value=(True, "fixture evidence", True),
            ), mock.patch(
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.current_baseline,
            ):
                write_status(project, document=StatusDocument("", events, {}))
                marked = mark_policy(
                    project,
                    "exception-approved",
                    "User approved 0402 for R1",
                    subject="allow-r1-0402",
                    tool_root=TOOL_ROOT,
                    now="2026-07-27T12:00:00+00:00",
                )

        self.assertEqual(marked.report.document.events[-1].phase, "circuit")
        self.assertEqual(marked.report.document.events[-1].action, "reopened")
        self.assertEqual(
            marked.report.document.policy_events[-1].action,
            "exception-approved",
        )

    def test_policy_change_durably_reopens_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), include_build_test=False)
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["exceptions"] = [
                {
                    "id": "unused-for-test",
                    "rule": "manufacturing.thickness",
                    "scope": "project",
                    "rationale": "Fixture exception.",
                }
            ]
            policy["manufacturing"]["thickness_mm"] = 2.0
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            marked = mark_policy(
                project,
                "exception-approved",
                "User approved 2 mm construction",
                subject="unused-for-test",
                tool_root=TOOL_ROOT,
            )
            policy["exceptions"][0]["rationale"] = "Changed rationale and tradeoff."
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            invalidated = write_status(project)

        self.assertEqual(
            marked.report.document.policy_events[-1].action,
            "exception-approved",
        )
        self.assertEqual(
            invalidated.report.document.policy_events[-1].action,
            "reopened",
        )

    def test_post_fab_sourcing_confirmation_gates_order_and_reopens_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "fab" / "board.zip").write_bytes(b"fabrication package")
            manual_phases = (
                "spec",
                "architect",
                "circuit",
                "layout",
                "verify",
            )
            events = tuple(
                StatusEvent(
                    f"2026-07-27T11:{index:02d}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                    _approval_fingerprint(project, phase),
                )
                for index, phase in enumerate(manual_phases)
            )
            from pcbforge import status as status_module

            original_evidence = status_module._static_evidence

            def evidence(project_dir, spec, document, phase):
                if phase == "order":
                    return original_evidence(
                        project_dir,
                        spec,
                        document,
                        phase,
                    )
                return True, "fixture evidence", True

            with mock.patch(
                "pcbforge.status._static_evidence",
                side_effect=evidence,
            ), mock.patch(
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.current_baseline,
            ), mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.current_handoff,
            ), mock.patch(
                "pcbforge.status._current_fab_out",
                return_value=mock.sentinel.current_fab,
            ):
                written = write_status(
                    project,
                    document=StatusDocument("", events, {}),
                )
                blocked = inspect_status(project)
                confirmed = mark_policy(
                    project,
                    "sourcing-confirmed",
                    "User confirmed current JLC availability and lifecycle",
                    tool_root=TOOL_ROOT,
                )
                order_event = StatusEvent(
                    "2026-07-27T12:30:00+00:00",
                    "order",
                    "complete",
                    "User reviewed files and placed order",
                    _approval_fingerprint(
                        project,
                        "order",
                        document=confirmed.report.document,
                    ),
                )
                ordered = write_status(
                    project,
                    tool_root=TOOL_ROOT,
                    document=replace(
                        confirmed.report.document,
                        events=(*confirmed.report.document.events, order_event),
                    ),
                )
                (project / "build-test.yaml").write_text(
                    BUILD_TEST.replace("quantity: 1", "quantity: 2"),
                    encoding="utf-8",
                )
                invalidated = write_status(project)

            restored = inspect_status(project)

        self.assertIn("post-FAB sourcing confirmation", blocked.current.detail)
        self.assertTrue(written.report.transitions[3].complete)
        self.assertTrue(ordered.report.phases[5].complete)
        self.assertEqual(
            invalidated.report.document.policy_events[-1].action,
            "reopened",
        )
        self.assertEqual(
            invalidated.report.document.events[-1].phase,
            "order",
        )
        self.assertEqual(
            invalidated.report.document.events[-1].action,
            "reopened",
        )
        self.assertFalse(restored.phases[5].complete)


class PolicyCliTests(PolicyFixture):
    def test_check_policy_exit_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print"):
                self.assertEqual(main(["check-policy", str(project)]), 0)

            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["manufacturing"]["fabricator"] = "other"
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            with mock.patch("builtins.print"):
                self.assertEqual(main(["check-policy", str(project)]), 1)

            pins = project / ".pcbforge"
            pins.write_text(
                pins.read_text(encoding="utf-8").replace(
                    "schema: 1",
                    "schema: 9",
                    1,
                ),
                encoding="utf-8",
            )
            with mock.patch("builtins.print"):
                self.assertEqual(main(["check-policy", str(project)]), 2)


if __name__ == "__main__":
    unittest.main()
