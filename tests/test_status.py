from __future__ import annotations

import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.build_test import fingerprint_inputs
from pcbforge.cli import main
from pcbforge.policy import render_default_policy
from pcbforge.status import (
    CheckRecord,
    StatusCheckError,
    StatusDocument,
    StatusEvent,
    StatusInputError,
    _approval_fingerprint,
    approve_phase,
    import_legacy_architect_approval,
    inspect_status,
    mark_status,
    migrate_approvals,
    read_status_document,
    record_initialization_blocker,
    render_dashboard,
    review_phase,
    run_status_checks,
    write_status,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]


def spec_text(*, legacy_approval: bool = False) -> str:
    decision = (
        "- 2026-07-20: ARCHITECT approved — power, MCU, and I/O graph;\n"
        "  diagram: docs/architecture.md.\n"
        if legacy_approval
        else ""
    )
    return f"""---
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
{decision}"""


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
        if initialized:
            self.add_scaffold(project)
        else:
            (project / "policy.yaml").write_text(
                render_default_policy(),
                encoding="utf-8",
            )
        return project

    def add_scaffold(self, project: Path) -> None:
        (project / ".pcbforge").write_text("schema: 5\n", encoding="utf-8")
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
        (project / "src" / "mcu.ato").write_text(
            "module Mcu:\n    pass\n",
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
    def test_step_six_assertions_preserve_prior_source_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / ".pcbforge").write_text("schema: 13\n", encoding="utf-8")
            self.add_architecture(project)
            before = {
                phase: _approval_fingerprint(project, phase)
                for phase in ("architect", "mcu", "implement")
            }
            for relative in ("src/main.ato", "src/mcu.ato"):
                source = project / relative
                source.write_text(
                    source.read_text(encoding="utf-8")
                    + """    # pcbforge-test: rail-3v3-tolerance
    assert 3.3V within 3.3V +/- 5%
""",
                    encoding="utf-8",
                )

            after = {
                phase: _approval_fingerprint(project, phase)
                for phase in ("architect", "mcu", "implement")
            }
            source = project / "src" / "mcu.ato"
            source.write_text(
                source.read_text(encoding="utf-8") + "    signal = 1\n",
                encoding="utf-8",
            )
            changed = {
                phase: _approval_fingerprint(project, phase)
                for phase in ("mcu", "implement")
            }

        self.assertEqual(before, after)
        self.assertNotEqual(after["mcu"], changed["mcu"])
        self.assertNotEqual(after["implement"], changed["implement"])

    def test_schema_eleven_rejects_direct_completion_for_every_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            (project / ".pcbforge").write_text(
                "schema: 11\n",
                encoding="utf-8",
            )
            for phase in (
                "spec",
                "init",
                "architect",
                "mcu",
                "implement",
                "build",
                "brief",
                "layout",
                "route",
                "verify",
                "fab-out",
                "order",
                "publish",
            ):
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

    def test_schema_ten_migration_preserves_only_sequential_bound_approvals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / "policy.yaml").write_text(
                render_default_policy(),
                encoding="utf-8",
            )
            (project / ".pcbforge").write_text(
                """schema: 10
policy:
  baseline_approval: spec
guidance:
  agents_schema: 10
  approval_schema: 1
""",
                encoding="utf-8",
            )
            (project / "AGENTS.md").write_text(
                "<!-- pcbforge-agents-schema: 10 -->\n# generated\n",
                encoding="utf-8",
            )
            self.add_architecture(project)
            events = (
                StatusEvent(
                    "2026-07-27T10:00:00+00:00",
                    "spec",
                    "complete",
                    "Bound spec approval",
                    _approval_fingerprint(project, "spec"),
                ),
                StatusEvent(
                    "2026-07-27T10:01:00+00:00",
                    "architect",
                    "complete",
                    "Bound architecture approval",
                    _approval_fingerprint(project, "architect"),
                ),
                StatusEvent(
                    "2026-07-27T10:02:00+00:00",
                    "mcu",
                    "complete",
                    "Legacy unbound MCU completion",
                ),
            )
            write_status(
                project,
                document=StatusDocument("", events, {}),
            )

            migration = migrate_approvals(
                project,
                tool_root=TOOL_ROOT,
                now="2026-07-28T11:00:00+00:00",
            )
            pins = yaml.safe_load(
                (project / ".pcbforge").read_text(encoding="utf-8")
            )
            document = read_status_document(project)
            second = migrate_approvals(project, tool_root=TOOL_ROOT)

        self.assertTrue(migration.wrote)
        self.assertEqual(migration.reopened_phases, ("architect", "mcu"))
        self.assertEqual(pins["schema"], 14)
        self.assertEqual(pins["guidance"]["agents_schema"], 15)
        self.assertEqual(pins["guidance"]["approval_schema"], 5)
        self.assertEqual(document.events[0].phase, "spec")
        self.assertEqual(document.events[0].action, "complete")
        self.assertEqual(document.events[-2].action, "reopened")
        self.assertEqual(document.events[-1].action, "reopened")
        self.assertFalse(second.wrote)

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

    def test_dashboard_renders_every_phase_and_core_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            report = inspect_status(project)
            rendered = render_dashboard(report)

        self.assertIn("## Current status", rendered)
        self.assertIn("## What's next", rendered)
        self.assertIn("## Completed", rendered)
        self.assertIn("## Blockers", rendered)
        self.assertIn("## Workflow", rendered)
        self.assertIn("## Recent progress", rendered)
        self.assertEqual(rendered.count("\n| "), 12)
        self.assertIn("0 of 8 required phases complete", rendered)
        self.assertIn("SPEC → ARCHITECT: initialize", rendered)
        self.assertIn("CIRCUIT → LAYOUT: layout handoff", rendered)

    def test_completion_requires_order_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            with self.assertRaisesRegex(StatusInputError, "SPEC is not complete"):
                mark_status(
                    project,
                    "architect",
                    "complete",
                    "Approved",
                    runner=FakeRunner(),
                )

            mark_status(project, "spec", "complete", "Approved")
            with self.assertRaisesRegex(StatusInputError, "tracked architecture"):
                mark_status(
                    project,
                    "architect",
                    "complete",
                    "Approved",
                    runner=FakeRunner(),
                )

    def test_checked_architecture_becomes_stale_after_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            mark_status(project, "spec", "complete", "Approved")
            marked = mark_status(
                project,
                "architect",
                "complete",
                "Graph approved",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
            architect = marked.report.phases[2]
            self.assertTrue(architect.complete)

            (project / "src" / "mcu.ato").write_text(
                "module Mcu:\n    # changed\n    pass\n",
                encoding="utf-8",
            )
            refreshed = inspect_status(project)
            architect = refreshed.phases[2]
            self.assertFalse(architect.complete)
            self.assertEqual(architect.state, "Blocked")
            self.assertIn("stale", architect.detail)

    def test_schema_nine_architect_requires_proposal_and_final_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / ".pcbforge").write_text("schema: 9\n", encoding="utf-8")
            self.add_architecture(project)
            spec_gate = mark_status(project, "spec", "complete", "User approved spec")
            self.assertTrue(
                spec_gate.report.document.events[-1].approval_fingerprint
            )

            with self.assertRaisesRegex(
                StatusInputError,
                "proposal has not received explicit user approval",
            ):
                mark_status(
                    project,
                    "architect",
                    "complete",
                    "Final architecture approved",
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

            proposed = mark_status(
                project,
                "architect",
                "proposal-approved",
                "User approved module graph and interfaces",
            )
            proposal_event = proposed.report.document.events[-1]
            self.assertEqual(proposal_event.action, "proposal-approved")
            self.assertTrue(proposal_event.approval_fingerprint)
            self.assertEqual(proposed.report.phases[2].state, "In progress")

            diagram = project / "docs" / "architecture.md"
            diagram.write_text(
                diagram.read_text(encoding="utf-8").replace(
                    "# architecture",
                    "# revised architecture",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StatusInputError,
                "proposal has not received explicit user approval",
            ):
                mark_status(
                    project,
                    "architect",
                    "complete",
                    "Final architecture approved",
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )
            mark_status(
                project,
                "architect",
                "proposal-approved",
                "User approved revised module graph and interfaces",
            )
            completed = mark_status(
                project,
                "architect",
                "complete",
                "User approved compiled architecture and final audit",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

        self.assertTrue(completed.report.phases[2].complete)
        self.assertTrue(
            completed.report.document.events[-1].approval_fingerprint
        )

    def test_schema_nine_flags_architecture_source_created_before_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / ".pcbforge").write_text("schema: 9\n", encoding="utf-8")
            mark_status(project, "spec", "complete", "User approved spec")
            self.add_architecture(project)

            report = inspect_status(project)

        self.assertEqual(report.phases[2].state, "Blocked")
        self.assertIn(
            "source exists without current proposal approval",
            report.phases[2].detail,
        )

    def test_changed_approved_architecture_reopens_and_cannot_revive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / ".pcbforge").write_text("schema: 9\n", encoding="utf-8")
            self.add_architecture(project)
            mark_status(project, "spec", "complete", "User approved spec")
            mark_status(
                project,
                "architect",
                "proposal-approved",
                "User approved proposed graph",
            )
            completed = mark_status(
                project,
                "architect",
                "complete",
                "User approved compiled graph and audit",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )
            self.assertTrue(completed.report.phases[2].complete)

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
            self.assertEqual(inspected.phases[2].state, "Blocked")
            self.assertIn("approval is stale", inspected.phases[2].detail)

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

            diagram.write_text(approved_contents, encoding="utf-8")
            restored = inspect_status(project)
            reapproved_final = mark_status(
                project,
                "architect",
                "complete",
                "User reapproved the restored final architecture",
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

        self.assertFalse(restored.phases[2].complete)
        self.assertEqual(restored.phases[2].state, "In progress")
        self.assertTrue(reapproved_final.report.phases[2].complete)

    def test_spatial_board_edit_does_not_stale_saved_build_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            mark_status(project, "spec", "complete", "Approved")
            marked = mark_status(
                project,
                "architect",
                "complete",
                "Graph approved",
                tool_root=TOOL_ROOT,
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

        self.assertTrue(marked.report.phases[2].complete)
        self.assertTrue(refreshed.phases[2].complete)

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
                "mcu",
                "implement",
                "brief",
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
            ):
                report = inspect_status(project, document=document)

        self.assertTrue(report.phases[2].complete)
        self.assertEqual(report.phases[3].phase.key, "mcu")
        self.assertEqual(report.phases[3].state, "Blocked")
        self.assertIn("stale", report.phases[3].detail)

    def test_optional_publish_skip_does_not_change_required_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            manual = (
                "spec",
                "architect",
                "mcu",
                "implement",
                "brief",
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
            ):
                report = inspect_status(project, document=document)

        self.assertEqual(report.completed_required, 12)
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
            with self.assertRaisesRegex(StatusInputError, "expected integer 1"):
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

    def test_legacy_architecture_decision_imports_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            (project / "spec.md").write_text(
                spec_text(legacy_approval=True),
                encoding="utf-8",
            )
            empty = StatusDocument(updated_at="", events=(), checks={})
            imported = import_legacy_architect_approval(project, empty)
            imported_again = import_legacy_architect_approval(project, imported)

        self.assertEqual(len(imported.events), 1)
        self.assertEqual(imported.events[0].phase, "architect")
        self.assertEqual(imported.events[0].action, "complete")
        self.assertIn("diagram: docs/architecture.md", imported.events[0].note)
        self.assertEqual(imported_again.events, imported.events)


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

    def test_implement_completion_is_blocked_by_parts_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            (project / "src" / "modules").mkdir()
            (project / "src" / "modules" / "power.ato").write_text(
                "module Power:\n    pass\n",
                encoding="utf-8",
            )
            (project / "firmware" / "garden-logger.ioc").write_text(
                "Mcu.Name=STM32G0\n",
                encoding="utf-8",
            )
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
            events = tuple(
                StatusEvent(
                    f"2026-07-26T10:0{index}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                )
                for index, phase in enumerate(("spec", "architect", "mcu"))
            )
            with mock.patch(
                "pcbforge.status.check_ioc",
                return_value=mock.Mock(part_number="STM32G071KBT6"),
            ):
                checked = run_status_checks(
                    project,
                    StatusDocument(updated_at="", events=events, checks={}),
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )
                write_status(
                    project,
                    document=checked,
                    now="2026-07-26T10:10:00+00:00",
                )
                with self.assertRaisesRegex(
                    StatusCheckError,
                    "parts failed: 1 commodity part uses",
                ):
                    mark_status(
                        project,
                        "implement",
                        "complete",
                        "Physical circuit complete",
                        tool_root=TOOL_ROOT,
                        runner=FakeRunner(),
                    )

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

    def test_step_six_requires_current_build_test_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture(project)
            (project / "src" / "modules").mkdir()
            (project / "src" / "modules" / "power.ato").write_text(
                "module Power:\n    pass\n",
                encoding="utf-8",
            )
            (project / "firmware" / "garden-logger.ioc").write_text(
                "Mcu.Name=STM32G0\n",
                encoding="utf-8",
            )
            (project / "build-test.yaml").write_text(
                """build_test_schema: 1
build: default
bom:
  - lcsc: C25804
    mpn: R10K
    footprint: Resistor_SMD:R_0603_1608Metric
    quantity: 1
board_footprints: 1
assertions: [rail-test]
""",
                encoding="utf-8",
            )
            events = tuple(
                StatusEvent(
                    f"2026-07-26T10:0{index}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                )
                for index, phase in enumerate(("spec", "architect", "mcu", "implement"))
            )
            document = StatusDocument(updated_at="", events=events, checks={})

            def fake_build_test(
                project_dir,
                *,
                write_report=False,
                **_kwargs,
            ):
                fingerprint = fingerprint_inputs(Path(project_dir))
                if write_report:
                    report = Path(project_dir) / "docs" / "build-test.md"
                    report.parent.mkdir(exist_ok=True)
                    report.write_text(
                        f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {fingerprint}
---
# pass
""",
                        encoding="utf-8",
                    )
                return SimpleNamespace(
                    summary="exact build-test evidence passed",
                    fingerprint=fingerprint,
                )

            with (
                mock.patch(
                    "pcbforge.status.check_ioc",
                    return_value=mock.Mock(part_number="STM32G071KBT6"),
                ),
                mock.patch(
                    "pcbforge.status.check_build_test",
                    side_effect=fake_build_test,
                ),
            ):
                checked_only = run_status_checks(
                    project,
                    document,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )
                checked_report = inspect_status(
                    project,
                    document=checked_only,
                )
                written = write_status(
                    project,
                    check=True,
                    document=document,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

        self.assertEqual(checked_only.checks["build-test"].outcome, "pass")
        self.assertEqual(checked_report.current.phase.key, "build")
        self.assertFalse(checked_report.phases[5].complete)
        self.assertIn("missing docs/build-test.md", checked_report.phases[5].detail)
        self.assertTrue(
            written.report.phases[5].complete,
            [
                (phase.phase.key, phase.state, phase.detail)
                for phase in written.report.phases
            ],
        )
        self.assertEqual(written.report.current.phase.key, "brief")

    def test_completed_implement_without_manifest_records_migration_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(
                    StatusEvent(
                        "2026-07-26T10:00:00+00:00",
                        "implement",
                        "complete",
                        "legacy implementation",
                    ),
                ),
                checks={},
            )
            checked = run_status_checks(
                project,
                document,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
            )

        self.assertEqual(checked.checks["build-test"].outcome, "fail")
        self.assertIn(
            "missing build-test.yaml",
            checked.checks["build-test"].summary,
        )


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


if __name__ == "__main__":
    unittest.main()
