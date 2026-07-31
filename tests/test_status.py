from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
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
    _derive_transitions,
    approve_phase,
    inspect_status,
    mark_status,
    read_status_document,
    record_initialization_blocker,
    render_dashboard,
    review_phase,
    run_status_checks,
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


class DashboardTests(StatusFixture):
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
                spec_text() + "\nChanged requirement.\n",
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
                approved_contents + "\nMaterial requirement changed.\n",
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

class CheckTests(StatusFixture):
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
