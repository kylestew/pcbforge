from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.cli import main
from pcbforge.status import (
    CheckRecord,
    StatusCheckError,
    StatusDocument,
    StatusEvent,
    StatusInputError,
    import_legacy_architect_approval,
    inspect_status,
    mark_status,
    read_status_document,
    render_dashboard,
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


class DashboardTests(StatusFixture):
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

            marked = mark_status(
                project,
                "spec",
                "complete",
                "Requirements baseline approved",
                now="2026-07-26T12:00:00+00:00",
            )
            self.assertEqual(marked.report.completed_required, 1)
            self.assertEqual(marked.report.current.phase.key, "init")
            document = read_status_document(project)
            self.assertEqual(document.events[-1].phase, "spec")
            self.assertEqual(document.events[-1].action, "complete")

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
        self.assertEqual(rendered.count("\n| "), 14)
        self.assertIn("0 of 12 required phases complete", rendered)

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

    def test_reopen_is_append_only_and_reactivates_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            mark_status(project, "spec", "complete", "Approved")
            reopened = mark_status(project, "spec", "reopened", "Requirements changed")

            self.assertEqual(reopened.report.current.phase.key, "spec")
            self.assertEqual(reopened.report.current.state, "In progress")
            self.assertEqual(
                [event.action for event in reopened.report.document.events],
                ["complete", "reopened"],
            )

    def test_reopened_phase_invalidates_older_downstream_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            manual = (
                "spec",
                "architect",
                "mcu",
                "implement",
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
            project = self.project(Path(temporary))
            manual = (
                "spec",
                "architect",
                "mcu",
                "implement",
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

    def test_cli_write_and_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with mock.patch("builtins.print"):
                self.assertEqual(
                    main(["status", "--write", str(project)]),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "status",
                            "mark",
                            "spec",
                            "complete",
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
