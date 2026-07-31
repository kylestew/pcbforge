from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.status import (
    PHASES,
    StatusDocument,
    StatusEvent,
    _approval_gate_sequence,
    _approval_payload,
    _content_fingerprint,
    _payload_fingerprint,
    inspect_status,
    prepare_cascade_review,
    read_status_document,
    renew_cascade,
    write_status,
)
from tests.test_status import FakeRunner, StatusFixture, TOOL_ROOT


class WorkflowFrictionRegressionTests(StatusFixture):
    def test_circuit_assurance_evidence_does_not_reopen_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True).resolve()
            self.approve_through_architect(project)
            before = read_status_document(project)
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
            policy["assurances"]["reverse-polarity"]["evidence"] = [
                "Reviewed reverse-polarity implementation.",
                "Checked the protected input net.",
            ]
            policy_path.write_text(
                yaml.safe_dump(policy, sort_keys=False),
                encoding="utf-8",
            )

            refreshed = write_status(
                project,
                now="2026-07-31T17:00:00+00:00",
            )

        new_phase_events = refreshed.report.document.events[len(before.events) :]
        new_transition_events = refreshed.report.document.transition_events[
            len(before.transition_events) :
        ]
        self.assertFalse(
            any(
                event.action == "reopened"
                and event.phase in {"spec", "architect"}
                for event in new_phase_events
            )
        )
        self.assertFalse(
            any(
                event.action == "reopened"
                and event.transition == "architecture-baseline"
                for event in new_transition_events
            )
        )
        self.assertTrue(refreshed.report.phases[0].complete)
        self.assertTrue(refreshed.report.phases[1].complete)
        self.assertTrue(refreshed.report.transitions[1].complete)

    def test_one_cascade_renew_recovers_unchanged_downstream_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            spec_path = project / "spec.md"
            spec_path.write_text(
                spec_path.read_text(encoding="utf-8").replace(
                    "board_mm: [50, 40]",
                    "board_mm: [45, 40]",
                ),
                encoding="utf-8",
            )
            self.refresh_architect_checks(project)

            cascade = prepare_cascade_review(
                project,
                record=True,
                reviewed_at="2026-07-31T17:10:00+00:00",
            )
            before_renew = read_status_document(project)
            renewed = renew_cascade(
                project,
                None,
                "User approved the unchanged architecture cascade",
                last_reviewed=True,
                now="2026-07-31T17:11:00+00:00",
            )

        self.assertTrue(cascade.ready)
        self.assertEqual(
            [gate.key for gate in cascade.gates],
            ["spec", "architect:proposal"],
        )
        renewal_events = renewed.report.document.events[len(before_renew.events) :]
        self.assertEqual(len(renewal_events), 2)
        self.assertTrue(all(event.renewed_from for event in renewal_events))
        self.assertTrue(renewed.report.phases[0].complete)
        self.assertTrue(renewed.report.phases[1].complete)

    def test_post_fab_sourcing_refresh_does_not_reopen_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True).resolve()
            (project / "fab" / "board.zip").write_bytes(b"fabrication packet")
            document = StatusDocument(updated_at="", events=(), checks={})
            payload = _approval_payload(
                project,
                "circuit",
                "complete",
                document,
            )
            circuit_event = StatusEvent(
                "2026-07-31T17:20:00+00:00",
                "circuit",
                "complete",
                "User approved the current circuit",
                _payload_fingerprint(payload),
                _content_fingerprint(payload),
            )
            written = write_status(
                project,
                document=replace(document, events=(circuit_event,)),
                now="2026-07-31T17:20:00+00:00",
            )
            policy_path = project / "policy.yaml"
            policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
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

            refreshed = write_status(
                project,
                now="2026-07-31T17:21:00+00:00",
            )

        new_events = refreshed.report.document.events[
            len(written.report.document.events) :
        ]
        self.assertFalse(
            any(
                event.phase == "circuit" and event.action == "reopened"
                for event in new_events
            )
        )
        latest_circuit = next(
            event
            for event in reversed(refreshed.report.document.events)
            if event.phase == "circuit"
        )
        self.assertEqual(latest_circuit, circuit_event)

    def test_unchanged_cold_start_runs_no_external_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            first = write_status(
                project,
                check=True,
                tool_root=TOOL_ROOT,
                runner=FakeRunner(),
                now="2026-07-31T17:30:00+00:00",
            )
            second_runner = FakeRunner()
            with (
                mock.patch(
                    "pcbforge.status.check_parts",
                    side_effect=AssertionError("parts check should be reused"),
                ),
                mock.patch(
                    "pcbforge.status.check_policy",
                    side_effect=AssertionError("policy check should be reused"),
                ),
            ):
                second = write_status(
                    project,
                    check=True,
                    tool_root=TOOL_ROOT,
                    runner=second_runner,
                    now="2026-07-31T17:31:00+00:00",
                )

        self.assertEqual(second_runner.calls, [])
        self.assertFalse(second.wrote)
        self.assertEqual(first.report.document.checks, second.report.document.checks)

    def test_v1_happy_path_gate_count_remains_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            report = inspect_status(project)

        self.assertEqual(
            [phase.key for phase in PHASES],
            ["spec", "architect", "circuit", "layout", "verify", "order", "publish"],
        )
        self.assertEqual(
            [transition.key for transition in report.transitions],
            ["initialize", "architecture-baseline", "layout-handoff", "fab-out"],
        )
        self.assertEqual(
            [
                gate.key
                for gate in _approval_gate_sequence()
                if gate.phase != "publish"
            ],
            [
                "spec",
                "architect:proposal",
                "circuit:proposal",
                "circuit",
                "layout:handoff",
                "layout",
                "verify",
                "order",
            ],
        )
        self.assertEqual(report.required_total, 6)
