from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.build_test import fingerprint_inputs
from pcbforge.cli import main
from pcbforge.policy import load_policy_profile, render_default_policy
from pcbforge.status import (
    CheckRecord,
    PHASES,
    PhaseResult,
    StatusCheckError,
    StatusDocument,
    StatusEvent,
    StatusInputError,
    TransitionEvent,
    _approval_fingerprint,
    _approval_payload,
    _content_fingerprint,
    _derive_transitions,
    _layout_handoff_payload,
    _payload_fingerprint,
    _phase_review_artifact_paths,
    approve_phase,
    inspect_status,
    mark_status,
    read_status_document,
    record_initialization_blocker,
    render_cascade_review,
    render_dashboard,
    review_phase,
    run_status_checks,
    prepare_cascade_review,
    renew_cascade,
    spec_contract_digest,
    write_status,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]


def spec_text() -> str:
    return """---
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

## Decisions log
"""


class FakeRunner:
    def __init__(self, *, build_ok: bool = True, drc_ok: bool = True) -> None:
        self.build_ok = build_ok
        self.drc_ok = drc_ok
        self.calls: list[list[str]] = []

    def __call__(self, command, *, cwd, **kwargs):
        command = list(command)
        self.calls.append(command)
        if "build" in command:
            return subprocess.CompletedProcess(
                command,
                0 if self.build_ok else 1,
                "frozen build passed\n" if self.build_ok else "",
                "" if self.build_ok else "build failed\n",
            )
        if "drc" in command:
            return subprocess.CompletedProcess(
                command,
                0 if self.drc_ok else 5,
                "DRC passed\n" if self.drc_ok else "",
                "" if self.drc_ok else "violations found\n",
            )
        raise AssertionError(f"unexpected command: {command}")


class StatusFixture(unittest.TestCase):
    def project(self, root: Path, *, initialized: bool = False) -> Path:
        project = root / "garden-logger"
        project.mkdir()
        (project / "spec.md").write_text(spec_text(), encoding="utf-8")
        (project / "policy.yaml").write_text(
            render_default_policy(),
            encoding="utf-8",
        )
        if initialized:
            self.add_scaffold(project)
        return project

    def add_scaffold(self, project: Path) -> None:
        _, _, policy_hash = load_policy_profile(TOOL_ROOT)
        lock_hash = hashlib.sha256(
            (TOOL_ROOT / "toolchain" / "uv.lock").read_bytes()
        ).hexdigest()
        rules_hash = hashlib.sha256(
            (TOOL_ROOT / "rules" / "jlc-2layer.json").read_bytes()
        ).hexdigest()
        (project / ".pcbforge").write_text(
            f"""schema: 1
toolchain:
  atopile: "0.15.7"
  kicad: "9.0.9"
  uv_lock_sha256: {lock_hash}
rules:
  profile: jlc-2layer-conservative-v1
  profile_sha256: {rules_hash}
policy:
  profile: pcbforge-standard-v1
  profile_sha256: {policy_hash}
  baseline_approval: spec
guidance:
  agents_schema: 1
  architect_schema: 1
  architecture_diagram_schema: 1
  mcu_schema: 1
  circuit_schema: 1
  circuit_review_schema: 1
  build_test_schema: 1
  layout_handoff_schema: 1
  approval_schema: 1
  policy_schema: 1
  status_schema: 1
""",
            encoding="utf-8",
        )
        (project / "ato.yaml").write_text(
            "builds:\n  default:\n    entry: src/main.ato:App\n",
            encoding="utf-8",
        )
        (project / "src").mkdir(exist_ok=True)
        (project / "src" / "main.ato").write_text(
            "module App:\n    pass\n",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_pcb").write_text(
            "(kicad_pcb\n)\n",
            encoding="utf-8",
        )
        (project / "garden-logger.kicad_pro").write_text("{}\n", encoding="utf-8")
        (project / "garden-logger.kicad_dru").write_text(
            "(version 1)\n", encoding="utf-8"
        )
        (project / "fab").mkdir(exist_ok=True)
        (project / "firmware").mkdir(exist_ok=True)

    def add_architecture(self, project: Path) -> None:
        self.add_architecture_proposal(project)
        (project / "src" / "mcu.ato").write_text(
            "module Mcu:\n    pass\n",
            encoding="utf-8",
        )
        (project / "firmware" / "garden-logger.ioc").write_text(
            "Mcu.Name=STM32G0\n",
            encoding="utf-8",
        )

    def add_architecture_proposal(self, project: Path) -> None:
        (project / "docs").mkdir(exist_ok=True)
        (project / "docs" / "architecture.md").write_text(
            """<!-- pcbforge-architecture-diagram-schema: 1 -->
# architecture
```mermaid
flowchart LR
```
""",
            encoding="utf-8",
        )
        (project / "docs" / "mcu.md").write_text(
            "# MCU plan\n",
            encoding="utf-8",
        )

    def approve(
        self,
        project: Path,
        phase: str,
        note: str = "User explicitly approved the exact phase review",
        *,
        runner=None,
        now: str | None = None,
    ):
        runner = runner or FakeRunner()
        review = review_phase(
            project,
            phase,
            tool_root=TOOL_ROOT,
            runner=runner,
            checked_at=now,
        )
        return approve_phase(
            project,
            phase,
            review.fingerprint,
            note,
            tool_root=TOOL_ROOT,
            runner=runner,
            now=now,
        )

    def approve_through_architect(self, project: Path) -> None:
        self.add_architecture_proposal(project)
        self.approve(project, "spec", "User approved the SPEC contract")
        proposal = review_phase(
            project,
            "architect",
            stage="proposal",
            tool_root=TOOL_ROOT,
        )
        approve_phase(
            project,
            "architect",
            proposal.fingerprint,
            "User approved the architecture proposal",
            stage="proposal",
            tool_root=TOOL_ROOT,
        )
        self.add_architecture(project)
        with mock.patch(
            "pcbforge.status.check_ioc",
            return_value=mock.Mock(part_number="STM32G071KBT6"),
        ):
            self.approve(
                project,
                "architect",
                "User approved the compiled architecture",
                runner=FakeRunner(),
            )

    def refresh_architect_checks(self, project: Path) -> None:
        with mock.patch(
            "pcbforge.status.check_ioc",
            return_value=mock.Mock(part_number="STM32G071KBT6"),
        ):
            checked = run_status_checks(
                project,
                read_status_document(project),
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
        write_status(project, document=checked)


class DashboardTests(StatusFixture):
    def test_scoped_fingerprints_keep_shared_files_in_review_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True).resolve()
            spec = inspect_status(project).spec

            architect = {
                path.relative_to(project).as_posix()
                for path in _phase_review_artifact_paths(
                    project,
                    spec,
                    "architect",
                )
            }
            circuit = {
                path.relative_to(project).as_posix()
                for path in _phase_review_artifact_paths(
                    project,
                    spec,
                    "circuit",
                )
            }

        self.assertIn("spec.md", architect)
        self.assertIn("spec.md", circuit)
        self.assertIn("policy.yaml", circuit)

    def test_spec_contract_digest_is_semantic_and_excludes_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            path = project / "spec.md"
            original = spec_text().replace(
                "## Decisions log\n",
                (
                    "## Purpose\n\nRecord garden conditions.\n\n"
                    "## Decisions log\n\n- Initial decision.\n\n"
                    "## Constraints\n\nKeep the board compact.\n"
                ),
            )
            path.write_text(original, encoding="utf-8")
            initial = spec_contract_digest(project)

            with_decision = original.replace(
                "- Initial decision.\n",
                "- Initial decision.\n- Later implementation note.\n",
            )
            path.write_text(with_decision, encoding="utf-8")
            self.assertEqual(initial, spec_contract_digest(project))

            frontmatter = yaml.safe_load(original.split("---", 2)[1])
            body = original.split("---", 2)[2]
            path.write_text(
                "---\n"
                + yaml.safe_dump(frontmatter, sort_keys=True)
                + "---"
                + body,
                encoding="utf-8",
            )
            self.assertEqual(initial, spec_contract_digest(project))

            path.write_text(
                original.replace(
                    "Record garden conditions.",
                    "Record garden conditions and soil temperature.",
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(initial, spec_contract_digest(project))

            path.write_text(
                original.replace(
                    "Keep the board compact.",
                    "Keep the board under 45 mm wide.",
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(initial, spec_contract_digest(project))

            path.write_text(
                "---\nspec_schema: 1\nspec_schema: 1\n---\n# Invalid\n",
                encoding="utf-8",
            )
            invalid = spec_contract_digest(project)
            self.assertEqual(invalid, spec_contract_digest(project))

        self.assertRegex(invalid, r"^[0-9a-f]{64}$")

    def test_phase_fingerprints_use_scoped_spec_and_policy_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            spec_path = project / "spec.md"
            policy_path = project / "policy.yaml"
            fingerprints = {
                "spec": _approval_fingerprint(project, "spec"),
                "architect-proposal": _approval_fingerprint(
                    project,
                    "architect",
                    "proposal-approved",
                ),
                "architect": _approval_fingerprint(project, "architect"),
                "circuit-proposal": _approval_fingerprint(
                    project,
                    "circuit",
                    "proposal-approved",
                ),
                "circuit": _approval_fingerprint(project, "circuit"),
            }

            spec_path.write_text(
                spec_path.read_text(encoding="utf-8")
                + "- CIRCUIT evidence was populated.\n",
                encoding="utf-8",
            )
            after_decision = {
                key: _approval_fingerprint(
                    project,
                    key.removesuffix("-proposal"),
                    "proposal-approved" if key.endswith("-proposal") else "complete",
                )
                for key in fingerprints
            }

            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["assurances"]["reverse-polarity"]["evidence"] = [
                "Reviewed reverse-polarity implementation."
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            after_evidence = {
                "spec": _approval_fingerprint(project, "spec"),
                "architect": _approval_fingerprint(project, "architect"),
                "circuit": _approval_fingerprint(project, "circuit"),
            }

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
            circuit_with_sourcing = _approval_fingerprint(project, "circuit")

            policy["exceptions"] = [
                {
                    "id": "allow-0402",
                    "rule": "components.commodity-package",
                    "scope": "R1",
                    "rationale": "Approved density tradeoff.",
                }
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            circuit_with_exception = _approval_fingerprint(project, "circuit")

            policy["assurances"]["reverse-polarity"]["status"] = (
                "not-applicable"
            )
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            spec_with_status_change = _approval_fingerprint(project, "spec")

        self.assertEqual(fingerprints, after_decision)
        self.assertEqual(fingerprints["spec"], after_evidence["spec"])
        self.assertEqual(fingerprints["architect"], after_evidence["architect"])
        self.assertNotEqual(fingerprints["circuit"], after_evidence["circuit"])
        self.assertEqual(after_evidence["circuit"], circuit_with_sourcing)
        self.assertNotEqual(circuit_with_sourcing, circuit_with_exception)
        self.assertNotEqual(fingerprints["spec"], spec_with_status_change)

    def test_circuit_work_does_not_reopen_spec_or_architect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            self.approve(project, "spec", "User approved the SPEC contract")

            proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "architect",
                proposal.fingerprint,
                "User approved the architecture and MCU proposal",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            self.add_architecture(project)
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                approved = self.approve(
                    project,
                    "architect",
                    "User approved the compiled architecture and MCU audit",
                    runner=FakeRunner(),
                )
            self.assertTrue(approved.report.phases[0].complete)
            self.assertTrue(approved.report.phases[1].complete)

            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["assurances"]["reverse-polarity"]["evidence"] = [
                "CIRCUIT review confirmed reverse-polarity protection."
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )
            spec_path = project / "spec.md"
            spec_path.write_text(
                spec_path.read_text(encoding="utf-8")
                + "- CIRCUIT evidence recorded without changing requirements.\n",
                encoding="utf-8",
            )

            report = inspect_status(project)

        self.assertTrue(report.phases[0].complete)
        self.assertTrue(report.phases[1].complete)
        self.assertEqual(report.current.phase.key, "circuit")

    def test_current_workflow_rejects_direct_completion_for_every_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            for phase in (item.key for item in PHASES):
                with (
                    self.subTest(phase=phase),
                    self.assertRaisesRegex(
                        StatusInputError,
                        f"status approve {phase}",
                    ),
                ):
                    mark_status(
                        project,
                        phase,
                        "complete",
                        "Agent cannot self-complete this phase",
                    )

    def test_review_packet_requires_exact_current_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            review = review_phase(
                project,
                "spec",
                tool_root=TOOL_ROOT,
                checked_at="2026-07-28T10:00:00+00:00",
            )
            self.assertTrue(review.ready)
            self.assertRegex(review.fingerprint, r"^[0-9a-f]{64}$")
            self.assertEqual(
                [(check.name, check.outcome) for check in review.checks],
                [("policy", "pass")],
            )

            (project / "spec.md").write_text(
                spec_text().replace(
                    "## Decisions log",
                    "## Purpose\n\nChanged requirement.\n\n## Decisions log",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StatusInputError,
                "reviewed fingerprint is stale",
            ):
                approve_phase(
                    project,
                    "spec",
                    review.fingerprint,
                    "User approved only the earlier review",
                    tool_root=TOOL_ROOT,
                )

    def test_pre_init_dashboard_is_deterministic_and_records_spec_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            first = write_status(project, now="2026-07-26T10:00:00+00:00")
            before = (project / "STATUS.md").read_bytes()
            second = write_status(project, now="2026-07-26T11:00:00+00:00")

            self.assertTrue(first.wrote)
            self.assertFalse(second.wrote)
            self.assertEqual((project / "STATUS.md").read_bytes(), before)
            self.assertEqual(first.report.current.phase.key, "spec")
            self.assertEqual(first.report.current.state, "In progress")

            marked = self.approve(
                project,
                "spec",
                "Requirements baseline approved",
                now="2026-07-26T12:00:00+00:00",
            )
            self.assertEqual(marked.report.completed_required, 1)
            self.assertEqual(marked.report.current.phase.key, "architect")
            self.assertEqual(marked.report.current_transition.key, "initialize")
            self.assertEqual(marked.report.current_transition.state, "Ready")
            self.assertEqual(marked.report.primary_action.owner, "Tool")
            self.assertEqual(marked.report.primary_action.command, "pcbforge init")
            document = read_status_document(project)
            self.assertEqual(document.events[-1].phase, "spec")
            self.assertEqual(document.events[-1].action, "complete")

            blocked = record_initialization_blocker(
                project,
                "compiler smoke test failed",
                now="2026-07-26T12:30:00+00:00",
            )
            self.assertIsNotNone(blocked)
            self.assertEqual(
                blocked.report.current_transition.state,
                "Blocked",
            )
            self.assertIn(
                "compiler smoke test failed",
                blocked.report.current_transition.detail,
            )

    def test_changed_approved_spec_is_durably_reopened_before_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            spec = project / "spec.md"
            approved_contents = spec.read_text(encoding="utf-8")
            marked = self.approve(
                project,
                "spec",
                "User approved requirements",
            )
            self.assertTrue(
                marked.report.document.events[-1].approval_fingerprint
            )

            spec.write_text(
                approved_contents.replace(
                    "## Decisions log",
                    (
                        "## Purpose\n\nMaterial requirement changed.\n\n"
                        "## Decisions log"
                    ),
                ),
                encoding="utf-8",
            )
            inspected = inspect_status(project)
            self.assertEqual(inspected.phases[0].state, "Blocked")
            self.assertIn("approval is stale", inspected.phases[0].detail)

            invalidated = write_status(
                project,
                now="2026-07-27T09:00:00+00:00",
            )
            self.assertEqual(
                invalidated.report.document.events[-1].action,
                "reopened",
            )
            spec.write_text(approved_contents, encoding="utf-8")
            restored = inspect_status(project)

        self.assertFalse(restored.phases[0].complete)
        self.assertEqual(restored.phases[0].state, "In progress")

    def test_performed_initialization_becomes_inactive_when_spec_reopens(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="2026-07-29T10:00:00+00:00",
                events=(
                    StatusEvent(
                        "2026-07-29T09:00:00+00:00",
                        "spec",
                        "reopened",
                        "Requirements changed",
                    ),
                ),
                checks={},
                transition_events=(
                    TransitionEvent(
                        "2026-07-28T10:00:00+00:00",
                        "initialize",
                        "complete",
                        "Validated scaffold created",
                    ),
                ),
            )
            report = inspect_status(project, document=document)
            rendered = render_dashboard(report)

        initialize = report.transitions[0]
        self.assertTrue(initialize.performed)
        self.assertFalse(initialize.complete)
        self.assertEqual(initialize.state, "Inactive")
        self.assertEqual(report.current.phase.key, "spec")
        self.assertIsNone(report.current_transition)
        self.assertIn("**Previously performed:**", rendered)
        self.assertIn("inactive while SPEC is reopened", rendered)
        self.assertIn("Forward progress currently begins at SPEC", rendered)

    def test_prior_layout_handoff_is_inactive_while_circuit_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(),
                checks={},
                transition_events=(
                    TransitionEvent(
                        "2026-07-28T10:00:00+00:00",
                        "layout-handoff",
                        "approved",
                        "Prior handoff approved",
                        "abc123",
                    ),
                ),
            )
            report = inspect_status(project, document=document)

        handoff = report.transitions[1]
        self.assertTrue(handoff.performed)
        self.assertFalse(handoff.complete)
        self.assertEqual(handoff.state, "Inactive")

    def test_prior_layout_handoff_is_stale_when_current_inputs_changed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(),
                checks={},
                transition_events=(
                    TransitionEvent(
                        "2026-07-28T10:00:00+00:00",
                        "layout-handoff",
                        "approved",
                        "Prior handoff approved",
                        "obsolete-fingerprint",
                    ),
                ),
            )
            spec = inspect_status(project, document=document).spec
            phases = tuple(
                PhaseResult(
                    phase,
                    "Complete" if phase.key in {"spec", "architect", "circuit"} else "Ready",
                    "test state",
                    phase.key in {"spec", "architect", "circuit"},
                )
                for phase in PHASES
            )
            handoff = _derive_transitions(
                project,
                spec,
                document,
                phases,
            )[1]

        self.assertTrue(handoff.performed)
        self.assertFalse(handoff.complete)
        self.assertEqual(handoff.state, "Stale")

    def test_dashboard_renders_every_phase_and_core_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            report = inspect_status(project)
            rendered = render_dashboard(report)

        self.assertIn("## Handoff", rendered)
        self.assertIn("**Just completed:**", rendered)
        self.assertIn("**Next owner:** AI + user", rendered)
        self.assertIn(
            "**Command when ready:** `pcbforge status review spec`",
            rendered,
        )
        self.assertIn("## Completed", rendered)
        self.assertIn("## Blockers", rendered)
        self.assertIn("## Workflow", rendered)
        self.assertIn("## Recent history", rendered)
        self.assertEqual(rendered.count("\n| "), 12)
        self.assertIn("0 of 8 required phases complete", rendered)
        self.assertIn("SPEC → ARCHITECT: initialize", rendered)
        self.assertIn("CIRCUIT → LAYOUT: layout handoff", rendered)

    def test_architect_requires_proposal_and_final_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            spec_gate = self.approve(project, "spec", "User approved spec")
            self.assertTrue(
                spec_gate.report.document.events[-1].approval_fingerprint
            )

            final_before_proposal = review_phase(
                project,
                "architect",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
            self.assertFalse(final_before_proposal.ready)
            self.assertIn(
                "current architecture proposal approval is missing",
                final_before_proposal.detail,
            )

            proposal_review = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            proposed = approve_phase(
                project,
                "architect",
                proposal_review.fingerprint,
                "User approved module graph and interfaces",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            proposal_event = proposed.report.document.events[-1]
            self.assertEqual(proposal_event.action, "proposal-approved")
            self.assertTrue(proposal_event.approval_fingerprint)
            self.assertEqual(proposed.report.phases[1].state, "In progress")

            self.add_architecture(project)

            diagram = project / "docs" / "architecture.md"
            diagram.write_text(
                diagram.read_text(encoding="utf-8").replace(
                    "# architecture",
                    "# revised architecture",
                ),
                encoding="utf-8",
            )
            stale_final = review_phase(
                project,
                "architect",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
            self.assertFalse(stale_final.ready)

            revised_proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "architect",
                revised_proposal.fingerprint,
                "User approved revised module graph and interfaces",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                completed = self.approve(
                    project,
                    "architect",
                    "User approved compiled architecture and final audit",
                    runner=FakeRunner(),
                )

        self.assertTrue(completed.report.phases[1].complete)
        self.assertTrue(
            completed.report.document.events[-1].approval_fingerprint
        )

    def test_architecture_source_created_before_proposal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve(project, "spec", "User approved spec")
            self.add_architecture(project)

            report = inspect_status(project)
            review = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )

        self.assertEqual(report.phases[1].state, "Blocked")
        self.assertIn(
            "source exists without current proposal approval",
            report.phases[1].detail,
        )
        self.assertFalse(review.ready)
        self.assertIn("source exists before current proposal approval", review.detail)

    def test_changed_approved_architecture_reopens_and_cannot_revive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            self.approve(project, "spec", "User approved spec")
            proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "architect",
                proposal.fingerprint,
                "User approved proposed graph",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            self.add_architecture(project)
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                completed = self.approve(
                    project,
                    "architect",
                    "User approved compiled graph and audit",
                    runner=FakeRunner(),
                )
            self.assertTrue(completed.report.phases[1].complete)

            diagram = project / "docs" / "architecture.md"
            approved_contents = diagram.read_text(encoding="utf-8")
            diagram.write_text(
                approved_contents.replace(
                    "# architecture",
                    "# changed architecture",
                ),
                encoding="utf-8",
            )
            inspected = inspect_status(project)
            self.assertEqual(inspected.phases[1].state, "Blocked")
            self.assertIn("approval is stale", inspected.phases[1].detail)

            invalidated = write_status(
                project,
                now="2026-07-27T12:00:00+00:00",
            )
            self.assertEqual(
                invalidated.report.document.events[-1].action,
                "reopened",
            )
            self.assertIn(
                "fingerprint changed",
                invalidated.report.document.events[-1].note,
            )

            renewal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            self.assertTrue(renewal.ready)
            self.assertEqual(renewal.stage, "proposal")

            diagram.write_text(approved_contents, encoding="utf-8")
            restored = inspect_status(project)
            restored_proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "architect",
                restored_proposal.fingerprint,
                "User reapproved the restored architecture proposal",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                reapproved_final = self.approve(
                    project,
                    "architect",
                    "User reapproved the restored final architecture",
                    runner=FakeRunner(),
                )

        self.assertFalse(restored.phases[1].complete)
        self.assertEqual(restored.phases[1].state, "In progress")
        self.assertTrue(reapproved_final.report.phases[1].complete)

    def test_spatial_board_edit_does_not_stale_saved_build_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            self.approve(project, "spec", "Approved")
            proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            approve_phase(
                project,
                "architect",
                proposal.fingerprint,
                "Graph proposal approved",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            self.add_architecture(project)
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                marked = self.approve(
                    project,
                    "architect",
                    "Graph approved",
                    runner=FakeRunner(),
                )
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                """(kicad_pcb
  (gr_line
    (start 100 100)
    (end 150 100)
    (stroke (width 0.05) (type default))
    (layer "Edge.Cuts")
  )
)
""",
                encoding="utf-8",
            )
            project_file = project / "garden-logger.kicad_pro"
            project_file.write_text(
                '{"user_layout_setting": true}\n',
                encoding="utf-8",
            )
            refreshed = inspect_status(project)

        self.assertTrue(marked.report.phases[1].complete)
        self.assertTrue(refreshed.phases[1].complete)

    def test_reopen_is_append_only_and_reactivates_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.approve(project, "spec", "Approved")
            reopened = mark_status(project, "spec", "reopened", "Requirements changed")

            self.assertEqual(reopened.report.current.phase.key, "spec")
            self.assertEqual(reopened.report.current.state, "In progress")
            self.assertEqual(
                [event.action for event in reopened.report.document.events],
                ["complete", "reopened"],
            )

    def test_reopened_phase_invalidates_older_downstream_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            manual = (
                "spec",
                "architect",
                "circuit",
                "layout",
                "route",
                "verify",
                "fab-out",
                "order",
            )
            events = tuple(
                StatusEvent(
                    f"2026-07-26T10:{index:02d}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} done",
                )
                for index, phase in enumerate(manual)
            )
            events += (
                StatusEvent(
                    "2026-07-26T11:00:00+00:00",
                    "architect",
                    "reopened",
                    "Interface changed",
                ),
                StatusEvent(
                    "2026-07-26T11:01:00+00:00",
                    "architect",
                    "complete",
                    "Reapproved",
                ),
            )
            document = StatusDocument(updated_at="", events=events, checks={})
            with mock.patch(
                "pcbforge.status._static_evidence",
                return_value=(True, "current evidence", True),
            ), mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ), mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.current_handoff,
            ):
                report = inspect_status(project, document=document)

        self.assertTrue(report.phases[1].complete)
        self.assertEqual(report.phases[2].phase.key, "circuit")
        self.assertEqual(report.phases[2].state, "Blocked")
        self.assertIn("stale", report.phases[2].detail)

    def test_optional_publish_skip_does_not_change_required_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            manual = (
                "spec",
                "architect",
                "circuit",
                "layout",
                "route",
                "verify",
                "fab-out",
                "order",
            )
            events = tuple(
                StatusEvent(
                    f"2026-07-26T10:{index:02d}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} done",
                )
                for index, phase in enumerate(manual)
            ) + (
                StatusEvent(
                    "2026-07-26T11:00:00+00:00",
                    "publish",
                    "skipped",
                    "No reusable modules",
                ),
            )
            document = StatusDocument(updated_at="", events=events, checks={})
            with mock.patch(
                "pcbforge.status._static_evidence",
                return_value=(True, "current evidence", True),
            ), mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ), mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.current_handoff,
            ):
                report = inspect_status(project, document=document)

        self.assertEqual(report.completed_required, 8)
        self.assertEqual(report.phases[-1].state, "Skipped")
        self.assertIsNone(report.current)

    def test_malformed_status_schema_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "STATUS.md").write_text(
                """---
pcbforge_status_schema: 99
events: []
checks: {}
---
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StatusInputError,
                "unsupported version — restart the project",
            ):
                read_status_document(project)

    def test_duplicate_frontmatter_keys_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "STATUS.md").write_text(
                """---
pcbforge_status_schema: 1
updated_at: first
updated_at: second
events: []
checks: {}
---
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(StatusInputError, "duplicate key"):
                read_status_document(project)

class CascadeRenewalTests(StatusFixture):
    def test_invalid_content_fingerprint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / "STATUS.md").write_text(
                """---
pcbforge_status_schema: 1
updated_at: "2026-07-31T10:00:00+00:00"
events:
  - at: "2026-07-31T10:00:00+00:00"
    phase: spec
    action: complete
    note: Approved
    approval_fingerprint: old
    content_fingerprint: invalid
checks: {}
---
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                StatusInputError,
                "content_fingerprint: expected a lowercase SHA-256",
            ):
                read_status_document(project)

    def test_approvals_store_and_round_trip_content_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            approved = self.approve(project, "spec", "User approved SPEC")
            event = approved.report.document.events[-1]
            reread = read_status_document(project).events[-1]

        self.assertRegex(event.content_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(event, reread)
        self.assertEqual(event.renewed_from, "")

    def test_spec_edit_renews_unchanged_architect_chain_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)

            cascade = prepare_cascade_review(project)
            rendered = render_cascade_review(cascade)

            self.assertTrue(cascade.ready)
            self.assertEqual(cascade.root_gate, "spec")
            self.assertEqual(
                [item.key for item in cascade.gates],
                ["spec", "architect:proposal", "architect"],
            )
            self.assertEqual(
                {item.classification for item in cascade.gates},
                {"eligible"},
            )
            self.assertIn("upstream approval scope changed", rendered)
            renewed = renew_cascade(
                project,
                cascade.fingerprint,
                "User approved the unchanged architecture cascade",
                now="2026-07-31T12:00:00+00:00",
            )

        renewal_events = [
            event
            for event in renewed.report.document.events
            if event.renewed_from
        ]
        self.assertEqual(len(renewal_events), 3)
        self.assertEqual(
            [event.action for event in renewal_events],
            ["complete", "proposal-approved", "complete"],
        )
        self.assertEqual(
            {event.note for event in renewal_events},
            {"User approved the unchanged architecture cascade"},
        )
        self.assertTrue(renewed.report.phases[0].complete)
        self.assertTrue(renewed.report.phases[1].complete)

    def test_downstream_delta_renews_only_eligible_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            mcu = project / "src" / "mcu.ato"
            mcu.write_text(
                mcu.read_text(encoding="utf-8") + "# changed implementation\n",
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)

            document = read_status_document(project)
            layout_payload = _approval_payload(
                project,
                "layout",
                "complete",
                document,
            )
            later_layout = StatusEvent(
                "2026-07-31T11:00:00+00:00",
                "layout",
                "complete",
                "Earlier layout approval fixture",
                _payload_fingerprint(layout_payload),
                _content_fingerprint(layout_payload),
            )
            cascade = prepare_cascade_review(
                project,
                replace(document, events=(*document.events, later_layout)),
            )

            self.assertEqual(
                [item.classification for item in cascade.gates],
                ["eligible", "eligible", "delta", "deferred"],
            )
            renewed = renew_cascade(
                project,
                cascade.fingerprint,
                "User renewed the unchanged prefix",
            )

        renewal_events = [
            event
            for event in renewed.report.document.events
            if event.renewed_from
        ]
        self.assertEqual(len(renewal_events), 2)
        self.assertTrue(renewed.report.phases[0].complete)
        self.assertFalse(renewed.report.phases[1].complete)
        self.assertEqual(renewed.report.phases[1].state, "Awaiting approval")

    def test_cascade_requires_current_saved_checks_without_running_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "pcbforge.status.run_status_checks",
                side_effect=AssertionError("cascade review must not run checks"),
            ):
                cascade = prepare_cascade_review(project)

        self.assertFalse(cascade.ready)
        self.assertEqual(cascade.gates[0].classification, "blocked")
        self.assertIn("saved checks", cascade.gates[0].detail)

    def test_stale_combined_fingerprint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)
            cascade = prepare_cascade_review(project)
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [45, 40]",
                    "board_mm: [44, 40]",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StatusInputError, "fingerprint is stale"):
                renew_cascade(
                    project,
                    cascade.fingerprint,
                    "User approved the earlier cascade",
                )

    def test_missing_content_fingerprint_requires_full_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)
            document = read_status_document(project)
            events = list(document.events)
            spec_index = next(
                index
                for index, event in enumerate(events)
                if event.phase == "spec" and event.action == "complete"
            )
            events[spec_index] = replace(
                events[spec_index],
                content_fingerprint="",
            )
            cascade = prepare_cascade_review(
                project,
                replace(document, events=tuple(events)),
            )

        self.assertFalse(cascade.ready)
        self.assertEqual(cascade.gates[0].classification, "delta")
        self.assertIn("no content fingerprint", cascade.gates[0].detail)

    def test_explicit_reopen_cannot_be_overridden_by_cascade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            self.approve(project, "spec", "User approved SPEC")
            mark_status(
                project,
                "spec",
                "reopened",
                "User intentionally reopened requirements",
            )

            cascade = prepare_cascade_review(project)

        self.assertFalse(cascade.ready)
        self.assertEqual(cascade.gates[0].classification, "blocked")
        self.assertIn("explicitly reopened", cascade.gates[0].detail)

    def test_handoff_fingerprint_uses_simulated_circuit_renewal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True).resolve()
            document = StatusDocument(updated_at="", events=(), checks={})
            circuit_payload = _approval_payload(
                project,
                "circuit",
                "complete",
                document,
            )
            circuit_event = StatusEvent(
                "2026-07-31T10:00:00+00:00",
                "circuit",
                "complete",
                "Approved circuit",
                _payload_fingerprint(circuit_payload),
                _content_fingerprint(circuit_payload),
            )
            document = replace(document, events=(circuit_event,))
            handoff_payload = _layout_handoff_payload(project, document)
            handoff_event = TransitionEvent(
                "2026-07-31T10:05:00+00:00",
                "layout-handoff",
                "approved",
                "Approved handoff",
                _payload_fingerprint(handoff_payload),
                _content_fingerprint(handoff_payload),
            )
            document = replace(
                document,
                transition_events=(handoff_event,),
            )
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "pcbforge.status._gate_check_reviews",
                return_value=((), ()),
            ):
                cascade = prepare_cascade_review(project, document)

        self.assertEqual(
            [item.classification for item in cascade.gates],
            ["eligible", "eligible"],
        )
        renewed_circuit, renewed_handoff = cascade.gates
        self.assertNotEqual(
            handoff_event.approval_fingerprint,
            renewed_handoff.approval_fingerprint,
        )
        self.assertNotEqual(
            circuit_event.approval_fingerprint,
            renewed_circuit.approval_fingerprint,
        )


class CheckTests(StatusFixture):
    def test_unchanged_checked_write_reuses_passes_without_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            first_runner = FakeRunner()
            first = write_status(
                project,
                check=True,
                tool_root=TOOL_ROOT,
                runner=first_runner,
                now="2026-07-26T10:00:00+00:00",
            )
            second_runner = FakeRunner()
            second = write_status(
                project,
                check=True,
                tool_root=TOOL_ROOT,
                runner=second_runner,
                now="2026-07-26T11:00:00+00:00",
            )

        self.assertTrue(first.wrote)
        self.assertFalse(second.wrote)
        self.assertEqual(len(first_runner.calls), 1)
        self.assertEqual(second_runner.calls, [])
        self.assertEqual(
            first.report.document.checks,
            second.report.document.checks,
        )

    def test_force_checks_reruns_current_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            first = run_status_checks(
                project,
                StatusDocument(updated_at="", events=(), checks={}),
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                checked_at="2026-07-26T10:00:00+00:00",
            )
            runner = FakeRunner()
            forced = run_status_checks(
                project,
                first,
                tool_root=TOOL_ROOT,
                runner=runner,
                checked_at="2026-07-26T11:00:00+00:00",
                force_checks=True,
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            forced.checks["build"].at,
            "2026-07-26T11:00:00+00:00",
        )
        self.assertEqual(
            forced.checks["parts"].at,
            "2026-07-26T11:00:00+00:00",
        )

    def test_failed_record_always_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            failed = run_status_checks(
                project,
                StatusDocument(updated_at="", events=(), checks={}),
                tool_root=TOOL_ROOT,
                runner=FakeRunner(build_ok=False),
                checked_at="2026-07-26T10:00:00+00:00",
            )
            runner = FakeRunner()
            checked = run_status_checks(
                project,
                failed,
                tool_root=TOOL_ROOT,
                runner=runner,
                checked_at="2026-07-26T11:00:00+00:00",
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(checked.checks["build"].outcome, "pass")
        self.assertEqual(
            checked.checks["build"].at,
            "2026-07-26T11:00:00+00:00",
        )

    def test_ioc_change_reruns_only_dependent_external_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                first = run_status_checks(
                    project,
                    StatusDocument(updated_at="", events=(), checks={}),
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                    checked_at="2026-07-26T10:00:00+00:00",
                )
            ioc = project / "firmware" / "garden-logger.ioc"
            ioc.write_text("Mcu.Name=STM32G071KBT6\n", encoding="utf-8")
            runner = FakeRunner()
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ) as check_ioc_mock:
                second = run_status_checks(
                    project,
                    first,
                    tool_root=TOOL_ROOT,
                    runner=runner,
                    checked_at="2026-07-26T11:00:00+00:00",
                )

        self.assertEqual(runner.calls, [])
        check_ioc_mock.assert_called_once()
        self.assertEqual(second.checks["build"], first.checks["build"])
        self.assertEqual(second.checks["parts"], first.checks["parts"])
        self.assertEqual(
            second.checks["ioc"].at,
            "2026-07-26T11:00:00+00:00",
        )

    def test_build_test_reuses_cycle_build_and_requires_saved_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / "build-test.yaml").write_text(
                "build_test_schema: 1\nbuild: default\n",
                encoding="utf-8",
            )
            fingerprint = fingerprint_inputs(project)
            result = mock.Mock(
                summary="build-test passed",
                fingerprint=fingerprint,
            )
            first_runner = FakeRunner()
            with mock.patch(
                "pcbforge.status.check_build_test",
                return_value=result,
            ) as first_check:
                first = run_status_checks(
                    project,
                    StatusDocument(updated_at="", events=(), checks={}),
                    tool_root=TOOL_ROOT,
                    runner=first_runner,
                    checked_at="2026-07-26T10:00:00+00:00",
                )
            report = project / "docs" / "build-test.md"
            report.parent.mkdir(exist_ok=True)
            report.write_text(
                "---\n"
                "pcbforge_build_test_report_schema: 1\n"
                "result: pass\n"
                f"fingerprint: {fingerprint}\n"
                "---\n",
                encoding="utf-8",
            )
            second_runner = FakeRunner()
            with mock.patch("pcbforge.status.check_build_test") as second_check:
                second = run_status_checks(
                    project,
                    first,
                    tool_root=TOOL_ROOT,
                    runner=second_runner,
                    checked_at="2026-07-26T11:00:00+00:00",
                )
            report.unlink()
            third_runner = FakeRunner()
            with mock.patch(
                "pcbforge.status.check_build_test",
                return_value=result,
            ) as third_check:
                run_status_checks(
                    project,
                    second,
                    tool_root=TOOL_ROOT,
                    runner=third_runner,
                    checked_at="2026-07-26T12:00:00+00:00",
                )

        self.assertEqual(len(first_runner.calls), 1)
        self.assertTrue(first_check.call_args.kwargs["skip_build"])
        self.assertEqual(second_runner.calls, [])
        second_check.assert_not_called()
        self.assertEqual(
            second.checks["build-test"],
            first.checks["build-test"],
        )
        self.assertEqual(third_runner.calls, [])
        self.assertTrue(third_check.call_args.kwargs["skip_build"])

    def test_static_status_runs_no_tools_and_checked_status_saves_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            runner = FakeRunner()
            inspect_status(project)
            self.assertEqual(runner.calls, [])

            checked = run_status_checks(
                project,
                StatusDocument(updated_at="", events=(), checks={}),
                tool_root=TOOL_ROOT,
                runner=runner,
                checked_at="2026-07-26T10:00:00+00:00",
            )

        self.assertEqual(len(runner.calls), 1)
        self.assertIn("--frozen", runner.calls[0])
        self.assertEqual(checked.checks["build"].outcome, "pass")

    def test_failed_build_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            checked = run_status_checks(
                project,
                StatusDocument(
                    updated_at="",
                    events=(
                        StatusEvent(
                            "2026-07-26T10:00:00+00:00",
                            "spec",
                            "complete",
                            "Approved",
                            _approval_fingerprint(project, "spec"),
                        ),
                    ),
                    checks={},
                ),
                tool_root=TOOL_ROOT,
                runner=FakeRunner(build_ok=False),
            )
            report = inspect_status(project, document=checked)

        self.assertEqual(checked.checks["build"].outcome, "fail")
        self.assertTrue(report.checks_failed)
        self.assertEqual(report.current.phase.key, "architect")
        self.assertEqual(report.current.state, "Blocked")

    def test_commodity_part_policy_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            part_dir = project / "src" / "parts" / "R_10K_0603"
            part_dir.mkdir(parents=True)
            (part_dir / "R_10K_0603.ato").write_text(
                """#pragma experiment("TRAITS")
import has_designator_prefix
import is_atomic_part

component R_10K_0603:
    trait is_atomic_part<manufacturer="Example", partnumber="R10K", footprint="R0603.kicad_mod", symbol="R_10K_0603.kicad_sym">
    trait has_designator_prefix<prefix="R">
    pin 1
    pin 2
""",
                encoding="utf-8",
            )
            checked = run_status_checks(
                project,
                StatusDocument(updated_at="", events=(), checks={}),
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

        self.assertEqual(checked.checks["parts"].outcome, "fail")
        self.assertIn("commodity part", checked.checks["parts"].summary)

    def test_route_gate_enables_drc_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(),
                checks={
                    "build": CheckRecord(
                        "2026-07-26T10:00:00+00:00",
                        "stale",
                        "pass",
                        "old",
                    )
                },
            )
            # run_status_checks dispatches DRC from the latest explicit route event;
            # earlier phase validity is handled separately by the status model.
            document = StatusDocument(
                updated_at=document.updated_at,
                events=(
                    # Deliberately direct fixture construction to test dispatch only.
                    StatusEvent(
                        "2026-07-26T10:00:00+00:00",
                        "route",
                        "complete",
                        "Routing done",
                    ),
                ),
                checks=document.checks,
            )
            runner = FakeRunner()
            checked = run_status_checks(
                project,
                document,
                tool_root=TOOL_ROOT,
                runner=runner,
            )

        self.assertIn("drc", checked.checks)
        self.assertEqual(checked.checks["drc"].outcome, "pass")
        self.assertEqual(len(runner.calls), 2)

class StatusCliTests(StatusFixture):
    def test_force_checks_requires_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print"):
                result = main(["status", "--force-checks", str(project)])

        self.assertEqual(result, 2)

    def test_force_checks_reaches_read_only_and_write_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            document = read_status_document(project)
            report = inspect_status(project, document=document)
            with (
                mock.patch(
                    "pcbforge.cli.run_status_checks",
                    return_value=document,
                ) as run_checks,
                mock.patch("builtins.print"),
            ):
                read_result = main(
                    ["status", "--check", "--force-checks", str(project)]
                )
            with (
                mock.patch(
                    "pcbforge.cli.write_status",
                    return_value=mock.Mock(report=report, wrote=False),
                ) as write_status_mock,
                mock.patch("builtins.print"),
            ):
                write_result = main(
                    [
                        "status",
                        "--check",
                        "--force-checks",
                        "--write",
                        str(project),
                    ]
                )

        self.assertEqual(read_result, 0)
        self.assertEqual(write_result, 0)
        self.assertTrue(run_checks.call_args.kwargs["force_checks"])
        self.assertTrue(write_status_mock.call_args.kwargs["force_checks"])

    def test_cascade_review_and_renew_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)
            cascade = prepare_cascade_review(project)

            with (
                mock.patch("pcbforge.cli.validate_project_compatibility"),
                mock.patch("builtins.print") as output,
            ):
                reviewed = main(
                    ["status", "review", "--cascade", str(project)]
                )
            rendered = "\n".join(
                str(call.args[0]) for call in output.call_args_list
            )
            with (
                mock.patch("pcbforge.cli.validate_project_compatibility"),
                mock.patch("builtins.print"),
            ):
                renewed = main(
                    [
                        "status",
                        "renew",
                        str(project),
                        "--fingerprint",
                        cascade.fingerprint,
                        "--note",
                        "User approved the cascade packet",
                    ]
                )

        self.assertEqual(reviewed, 0)
        self.assertIn("pcbforge cascade review", rendered)
        self.assertIn("cascade fingerprint:", rendered)
        self.assertEqual(renewed, 0)

    def test_cascade_review_rejects_phase_or_stage_combinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            with mock.patch("builtins.print"):
                phase_result = main(
                    [
                        "status",
                        "review",
                        "spec",
                        "--cascade",
                        str(project),
                    ]
                )
                stage_result = main(
                    [
                        "status",
                        "review",
                        "--cascade",
                        "--stage",
                        "proposal",
                        str(project),
                    ]
                )

        self.assertEqual(phase_result, 2)
        self.assertEqual(stage_result, 2)

    def test_static_cli_does_not_create_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print") as output:
                result = main(["status", str(project)])

            self.assertEqual(result, 0)
            self.assertFalse((project / "STATUS.md").exists())
            rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
            self.assertIn("current: 1. SPEC", rendered)
            self.assertIn("next owner: AI + user", rendered)
            self.assertIn(
                "command when ready: pcbforge status review spec",
                rendered,
            )

    def test_next_cli_shows_only_the_compact_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print") as output:
                result = main(["status", "--next", str(project)])

            rendered = "\n".join(
                str(call.args[0]) for call in output.call_args_list
            )

        self.assertEqual(result, 0)
        self.assertNotIn("required phases complete", rendered)
        self.assertIn("last valid:", rendered)
        self.assertIn("current: 1. SPEC", rendered)
        self.assertIn("next action:", rendered)
        self.assertIn(
            "command when ready: pcbforge status review spec",
            rendered,
        )

    def test_next_cli_can_refresh_the_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print") as output:
                result = main(
                    ["status", "--next", "--write", str(project)]
                )

            rendered = "\n".join(
                str(call.args[0]) for call in output.call_args_list
            )
            dashboard_exists = (project / "STATUS.md").is_file()

        self.assertEqual(result, 0)
        self.assertTrue(dashboard_exists)
        self.assertNotIn("required phases complete", rendered)
        self.assertIn("current: 1. SPEC", rendered)
        self.assertIn("updated STATUS.md", rendered)

    def test_cli_write_and_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print"):
                self.assertEqual(
                    main(["status", "--write", str(project)]),
                    0,
                )
                review = review_phase(project, "spec", tool_root=TOOL_ROOT)
                self.assertEqual(
                    main(
                        [
                            "status",
                            "approve",
                            "spec",
                            "--fingerprint",
                            review.fingerprint,
                            "--note",
                            "Approved",
                            str(project),
                        ]
                    ),
                    0,
                )

            self.assertTrue((project / "STATUS.md").is_file())
            self.assertEqual(read_status_document(project).events[-1].phase, "spec")

    def test_cli_reports_input_errors_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print"):
                result = main(
                    [
                        "status",
                        "mark",
                        "unknown",
                        "complete",
                        "--note",
                        "No",
                        str(project),
                    ]
                )
            self.assertEqual(result, 2)

    def test_cli_reports_check_failures_with_exit_one(self) -> None:
        with (
            mock.patch(
                "pcbforge.cli.mark_status",
                side_effect=StatusCheckError("build failed"),
            ),
            mock.patch("builtins.print"),
        ):
            result = main(
                [
                    "status",
                    "mark",
                    "architect",
                    "complete",
                    "--note",
                    "Approved",
                ]
            )
        self.assertEqual(result, 1)

    def test_next_cli_preserves_check_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with (
                mock.patch(
                    "pcbforge.cli.run_status_checks",
                    side_effect=StatusCheckError("validation failed"),
                ),
                mock.patch("builtins.print"),
            ):
                result = main(
                    ["status", "--next", "--check", str(project)]
                )

        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
