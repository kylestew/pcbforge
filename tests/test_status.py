from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.build_test import fingerprint_inputs
from pcbforge.circuit_review import capture_reopen_baseline
from pcbforge.cli import main
from pcbforge.policy import load_policy_profile, render_default_policy
from pcbforge.initialize import read_spec
from pcbforge.status import (
    ADVISORY_CHECKS,
    APPROVAL_CHECKS,
    CheckRecord,
    PHASE_EVIDENCE_CHECKS,
    PHASES,
    PhaseResult,
    PhaseReview,
    ReviewRecord,
    StatusCheckError,
    StatusDocument,
    StatusEvent,
    StatusInputError,
    TransitionEvent,
    _approval_fingerprint,
    _architecture_baseline_payload,
    _check_inputs,
    _approval_gate_sequence,
    _approval_payload,
    _content_fingerprint,
    _fingerprint,
    _derive_transitions,
    _latest_events,
    _layout_handoff_payload,
    _payload_fingerprint,
    _phase_review_artifact_paths,
    _review_key,
    approve_phase,
    finish_architect,
    inspect_status,
    mark_status,
    read_status_document,
    record_initialization_blocker,
    render_cascade_review,
    render_dashboard,
    render_phase_review,
    review_phase,
    run_status_checks,
    prepare_cascade_review,
    record_fab_out_transition,
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
    def __init__(
        self,
        *,
        build_ok: bool = True,
        drc_ok: bool = True,
        drc_report: dict[str, object] | None = None,
    ) -> None:
        self.build_ok = build_ok
        self.drc_ok = drc_ok
        self.drc_report = drc_report
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
            report = Path(command[command.index("--output") + 1])
            payload = self.drc_report
            if payload is None:
                payload = {
                    "violations": [] if self.drc_ok else [{}],
                    "unconnected_items": [],
                    "schematic_parity": [],
                }
            report.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                "DRC report saved\n",
                "",
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
  circuit_review_schema: 3
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
        if phase == "architect":
            return finish_architect(
                project,
                tool_root=TOOL_ROOT,
                runner=runner,
                now=now,
            )
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

        handoff = report.transitions[2]
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
            )[2]

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
        self.assertTrue(rendered.startswith("# garden-logger project dashboard\n"))
        self.assertGreater(
            rendered.index("<!-- pcbforge-metadata -->"),
            rendered.index("## Recent history"),
        )
        self.assertTrue(rendered.endswith("```\n"))
        self.assertEqual(rendered.count("\n| "), 12)
        self.assertIn("0 of 6 required phases complete", rendered)
        self.assertIn("SPEC → ARCHITECT: initialize", rendered)
        self.assertIn("ARCHITECT → CIRCUIT: architecture baseline", rendered)
        self.assertIn("CIRCUIT → LAYOUT: layout handoff", rendered)
        self.assertIn("VERIFY → ORDER: FAB-OUT", rendered)

    def test_architect_requires_proposal_and_checked_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            spec_gate = self.approve(project, "spec", "User approved spec")
            self.assertTrue(
                spec_gate.report.document.events[-1].approval_fingerprint
            )

            with self.assertRaisesRegex(
                StatusInputError,
                "finish-architect",
            ):
                review_phase(
                    project,
                    "architect",
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
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
            with self.assertRaisesRegex(StatusInputError, "finish-architect"):
                review_phase(
                    project,
                    "architect",
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )

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
        transition = completed.report.document.transition_events[-1]
        self.assertEqual(transition.transition, "architecture-baseline")
        self.assertEqual(transition.action, "complete")
        self.assertTrue(
            transition.content_fingerprint
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
                restored_baseline = self.approve(
                    project,
                    "architect",
                    "Tool re-recorded the restored architecture baseline",
                    runner=FakeRunner(),
                )

        self.assertFalse(restored.phases[1].complete)
        self.assertEqual(restored.phases[1].state, "In progress")
        self.assertTrue(restored_baseline.report.phases[1].complete)

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

    def test_circuit_reopen_captures_only_a_current_approved_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            prior = StatusEvent(
                "2026-07-26T10:00:00+00:00",
                "circuit",
                "complete",
                "Approved CIRCUIT",
                "a" * 64,
            )
            write_status(
                project,
                document=StatusDocument(updated_at="", events=(prior,), checks={}),
            )
            saved = mock.sentinel.status_result
            with (
                mock.patch(
                    "pcbforge.status._approval_is_current",
                    return_value=True,
                ) as approval_current,
                mock.patch(
                    "pcbforge.status.capture_reopen_baseline",
                ) as capture,
                mock.patch(
                    "pcbforge.status.write_status",
                    return_value=saved,
                ) as persist,
            ):
                result = mark_status(
                    project,
                    "circuit",
                    "reopened",
                    "Replace J1",
                )

            self.assertIs(result, saved)
            approval_current.assert_called_once()
            capture.assert_called_once_with(project.resolve(), "a" * 64)
            recorded = persist.call_args.kwargs["document"].events[-1]
            self.assertEqual((recorded.phase, recorded.action), ("circuit", "reopened"))

            with (
                mock.patch(
                    "pcbforge.status._approval_is_current",
                    return_value=False,
                ),
                mock.patch(
                    "pcbforge.status.capture_reopen_baseline",
                ) as rejected_capture,
            ):
                with self.assertRaisesRegex(
                    StatusInputError,
                    "restore the last approved CIRCUIT",
                ):
                    mark_status(
                        project,
                        "circuit",
                        "reopened",
                        "Changed too early",
                    )
            rejected_capture.assert_not_called()

    def test_circuit_reopen_recovers_only_an_unchanged_automatic_baseline_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            baseline_fingerprint = _payload_fingerprint(
                _architecture_baseline_payload(project, StatusDocument("", (), {}))
            )
            prior = StatusEvent(
                "2026-07-26T10:00:00+00:00",
                "circuit",
                "complete",
                "Approved CIRCUIT",
                "a" * 64,
            )
            transitions = (
                TransitionEvent(
                    "2026-07-26T09:00:00+00:00",
                    "architecture-baseline",
                    "complete",
                    "Architecture checked",
                    content_fingerprint=baseline_fingerprint,
                ),
                TransitionEvent(
                    "2026-07-26T10:30:00+00:00",
                    "architecture-baseline",
                    "reopened",
                    "Automatic transition invalidated because ARCHITECT proposal or baseline content changed",
                ),
            )
            write_status(
                project,
                document=StatusDocument(
                    updated_at="",
                    events=(prior,),
                    checks={},
                    transition_events=transitions,
                ),
            )
            saved = mock.sentinel.status_result
            with (
                mock.patch(
                    "pcbforge.status._approval_is_current",
                    return_value=True,
                ),
                mock.patch("pcbforge.status.capture_reopen_baseline"),
                mock.patch(
                    "pcbforge.status.write_status",
                    return_value=saved,
                ) as persist,
            ):
                result = mark_status(
                    project,
                    "circuit",
                    "reopened",
                    "Replace J1",
                    now="2026-07-26T11:00:00+00:00",
                )

            self.assertIs(result, saved)
            recovered = persist.call_args.kwargs["document"].transition_events[-1]
            self.assertEqual(recovered.action, "complete")
            self.assertEqual(recovered.content_fingerprint, baseline_fingerprint)

            changed = StatusDocument(
                updated_at="",
                events=(prior,),
                checks={},
                transition_events=(
                    replace(transitions[0], content_fingerprint="b" * 64),
                    transitions[1],
                ),
            )
            write_status(project, document=changed)
            with (
                mock.patch(
                    "pcbforge.status._approval_is_current",
                    return_value=True,
                ),
                mock.patch("pcbforge.status.capture_reopen_baseline") as capture,
            ):
                with self.assertRaisesRegex(
                    StatusInputError,
                    "cannot recover the ARCHITECT baseline",
                ):
                    mark_status(
                        project,
                        "circuit",
                        "reopened",
                        "Unsafe recovery",
                    )
            capture.assert_not_called()

    def test_circuit_reopen_reuses_a_previously_captured_bound_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True).resolve()
            prior = StatusEvent(
                "2026-07-26T10:00:00+00:00",
                "circuit",
                "complete",
                "Approved CIRCUIT",
                "a" * 64,
            )
            reopened = StatusEvent(
                "2026-07-26T10:30:00+00:00",
                "circuit",
                "reopened",
                "Approved connector replacement requested",
            )
            write_status(
                project,
                document=StatusDocument(
                    updated_at="",
                    events=(prior, reopened),
                    checks={},
                ),
            )
            capture_reopen_baseline(project, "a" * 64)
            saved = mock.sentinel.status_result
            with (
                mock.patch(
                    "pcbforge.status._approval_is_current",
                    return_value=False,
                ),
                mock.patch(
                    "pcbforge.status.capture_reopen_baseline",
                ) as recapture,
                mock.patch(
                    "pcbforge.status.write_status",
                    return_value=saved,
                ),
            ):
                result = mark_status(
                    project,
                    "circuit",
                    "reopened",
                    "Resume an interrupted reopen",
                )

            self.assertIs(result, saved)
            recapture.assert_not_called()

    def test_reopened_phase_invalidates_older_downstream_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            manual = (
                "spec",
                "architect",
                "circuit",
                "layout",
                "verify",
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
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.current_baseline,
            ), mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.current_handoff,
            ), mock.patch(
                "pcbforge.status._current_fab_out",
                return_value=mock.sentinel.current_fab,
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
                "verify",
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
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.current_baseline,
            ), mock.patch(
                "pcbforge.status._current_layout_handoff",
                return_value=mock.sentinel.current_handoff,
            ), mock.patch(
                "pcbforge.status._current_fab_out",
                return_value=mock.sentinel.current_fab,
            ):
                report = inspect_status(project, document=document)

        self.assertEqual(report.completed_required, 6)
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
                ["spec", "architect:proposal"],
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
        self.assertEqual(len(renewal_events), 2)
        self.assertEqual(
            [event.action for event in renewal_events],
            ["complete", "proposal-approved"],
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
                ["eligible", "eligible", "blocked"],
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
        self.assertEqual(renewed.report.phases[1].state, "Ready")

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
            ), mock.patch(
                "pcbforge.status._current_architecture_baseline",
                return_value=mock.sentinel.current_baseline,
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


class V1WorkflowTests(StatusFixture):
    def test_v1_phase_transition_and_human_gate_sequences_are_fixed(self) -> None:
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
            [gate.key for gate in _approval_gate_sequence()],
            [
                "spec",
                "architect:proposal",
                "circuit:proposal",
                "circuit",
                "layout:handoff",
                "layout",
                "verify",
                "order",
                "publish",
            ],
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

    def test_finish_architect_failure_records_blocked_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve(project, "spec", "User approved SPEC")
            self.add_architecture(project)
            with (
                mock.patch(
                    "pcbforge.status.check_ioc",
                    return_value=mock.Mock(part_number="STM32G071KBT6"),
                ),
                self.assertRaisesRegex(
                    StatusCheckError,
                    "current architecture proposal approval is missing",
                ),
            ):
                finish_architect(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                    now="2026-07-31T13:00:00+00:00",
                )
            document = read_status_document(project)

        transition = document.transition_events[-1]
        self.assertEqual(transition.transition, "architecture-baseline")
        self.assertEqual(transition.action, "blocked")

    def test_finish_architect_blocks_spatial_change_and_capture_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            self.approve(project, "spec", "User approved SPEC")
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
                "User approved architecture proposal",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            self.add_architecture(project)
            with (
                mock.patch(
                    "pcbforge.status.check_ioc",
                    return_value=mock.Mock(part_number="STM32G071KBT6"),
                ),
                mock.patch(
                    "pcbforge.status._spatial_errors",
                    return_value=["changed board spatial data"],
                ),
                self.assertRaisesRegex(StatusCheckError, "changed board spatial data"),
            ):
                finish_architect(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )
            with (
                mock.patch(
                    "pcbforge.status.check_ioc",
                    return_value=mock.Mock(part_number="STM32G071KBT6"),
                ),
                mock.patch(
                    "pcbforge.status.capture_implementation_baseline",
                    side_effect=OSError("baseline write failed"),
                ),
                self.assertRaisesRegex(StatusCheckError, "baseline write failed"),
            ):
                finish_architect(
                    project,
                    tool_root=TOOL_ROOT,
                    runner=FakeRunner(),
                )
            self.assertFalse(
                (project / "review/circuit/source-baseline.json").exists()
            )

    def test_finish_architect_records_failed_build_and_missing_board(self) -> None:
        for failure in ("build", "board"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = self.project(Path(temporary), initialized=True)
                self.add_architecture_proposal(project)
                self.approve(project, "spec", "User approved SPEC")
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
                    "User approved architecture proposal",
                    stage="proposal",
                    tool_root=TOOL_ROOT,
                )
                self.add_architecture(project)
                if failure == "board":
                    (project / "garden-logger.kicad_pcb").unlink()
                with (
                    mock.patch(
                        "pcbforge.status.check_ioc",
                        return_value=mock.Mock(part_number="STM32G071KBT6"),
                    ),
                    self.assertRaises(StatusCheckError),
                ):
                    finish_architect(
                        project,
                        tool_root=TOOL_ROOT,
                        runner=FakeRunner(build_ok=failure != "build"),
                    )
                document = read_status_document(project)
                transition = document.transition_events[-1]
                self.assertEqual(transition.transition, "architecture-baseline")
                self.assertEqual(transition.action, "blocked")
                self.assertIn(
                    "build failed" if failure == "build" else "missing",
                    transition.note,
                )

    def test_circuit_source_edits_do_not_recapture_architecture_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.approve_through_architect(project)
            source = project / "src" / "mcu.ato"
            source.write_text(
                source.read_text(encoding="utf-8") + "# circuit work\n",
                encoding="utf-8",
            )

            before_proposal = inspect_status(project)
            with mock.patch(
                "pcbforge.status._current_circuit_proposal",
                return_value=mock.sentinel.current_circuit_proposal,
            ):
                after_proposal = inspect_status(project)

        self.assertFalse(before_proposal.phases[1].complete)
        self.assertEqual(before_proposal.phases[1].state, "In progress")
        self.assertTrue(after_proposal.phases[1].complete)
        self.assertEqual(after_proposal.transitions[1].state, "Complete")

    def test_layout_fingerprint_binds_routing_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = read_status_document(project)
            before = _approval_payload(project, "layout", "complete", document)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    ")\n",
                    "  (segment (start 1 1) (end 2 2) (width 0.2) "
                    "(layer \"F.Cu\") (net 0))\n)\n",
                ),
                encoding="utf-8",
            )
            after = _approval_payload(project, "layout", "complete", document)

        self.assertNotEqual(before["board"], after["board"])
        self.assertNotEqual(_payload_fingerprint(before), _payload_fingerprint(after))

    def test_fab_out_transition_gates_order_and_reopens_after_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            events = tuple(
                StatusEvent(
                    f"2026-07-31T14:0{index}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                    "a" * 64,
                    "c" * 64,
                )
                for index, phase in enumerate(
                    ("spec", "circuit", "layout", "verify")
                )
            )
            document = StatusDocument("", events, {})
            fab = project / "fab" / "board.zip"
            fab.write_bytes(b"packet-v1")
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
                    return_value=mock.sentinel.current_baseline,
                ),
                mock.patch(
                    "pcbforge.status._current_layout_handoff",
                    return_value=mock.sentinel.current_handoff,
                ),
            ):
                initial = write_status(project, document=document)
                self.assertFalse(initial.report.phases[5].complete)
                recorded = record_fab_out_transition(project)
                self.assertTrue(recorded.report.transitions[3].complete)
                fab.write_bytes(b"packet-v2")
                invalidated = write_status(project)

        latest = invalidated.report.document.transition_events[-1]
        self.assertEqual(latest.transition, "fab-out")
        self.assertEqual(latest.action, "reopened")
        self.assertFalse(invalidated.report.phases[5].complete)

    def test_legacy_route_and_fab_out_phase_events_are_rejected(self) -> None:
        for legacy in ("route", "fab-out"):
            with (
                self.subTest(legacy=legacy),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = self.project(Path(temporary))
                (project / "STATUS.md").write_text(
                    "---\n"
                    "pcbforge_status_schema: 1\n"
                    "updated_at: ''\n"
                    "events:\n"
                    f"  - {{at: now, phase: {legacy}, action: complete, note: old}}\n"
                    "policy_events: []\n"
                    "transition_events: []\n"
                    "checks: {}\n"
                    "---\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(StatusInputError, "unknown phase"):
                    read_status_document(project)


class LayoutAssistTests(StatusFixture):
    def test_ai_assist_requires_layout_and_a_current_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            with self.assertRaisesRegex(StatusInputError, "only layout records"):
                mark_status(
                    project,
                    "circuit",
                    "ai-assisted",
                    "User asked for a placement pass",
                )
            with self.assertRaisesRegex(StatusInputError, "handoff is not currently"):
                mark_status(
                    project,
                    "layout",
                    "ai-assisted",
                    "User asked for a placement pass",
                )

    def test_ai_assist_is_history_that_never_changes_phase_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            events = tuple(
                StatusEvent(
                    f"2026-08-03T10:0{index}:00+00:00",
                    phase,
                    "complete",
                    f"{phase} complete",
                    "a" * 64,
                    "c" * 64,
                )
                for index, phase in enumerate(("spec", "architect", "circuit"))
            ) + (
                StatusEvent(
                    "2026-08-03T11:00:00+00:00",
                    "layout",
                    "ai-assisted",
                    "User asked for power-rail routing; 6 segments added",
                ),
            )
            document = StatusDocument("", events, {})
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
                    return_value=mock.sentinel.current_baseline,
                ),
                mock.patch(
                    "pcbforge.status._current_layout_handoff",
                    return_value=mock.sentinel.current_handoff,
                ),
            ):
                report = inspect_status(project, document=document)
                recorded = mark_status(
                    project,
                    "layout",
                    "ai-assisted",
                    "User asked for a first placement pass",
                    runner=FakeRunner(),
                )

        layout = report.phases[3]
        self.assertFalse(layout.complete)
        self.assertNotEqual(layout.state, "Blocked")
        self.assertEqual(
            [event.action for event in recorded.report.document.events],
            ["ai-assisted"],
        )
        self.assertFalse(recorded.report.phases[3].complete)

    def test_ai_assist_never_masks_an_approval_for_invalidation(self) -> None:
        approval = StatusEvent(
            "2026-08-03T10:00:00+00:00",
            "layout",
            "complete",
            "Layout approved",
            "a" * 64,
            "c" * 64,
        )
        annotation = StatusEvent(
            "2026-08-03T12:00:00+00:00",
            "layout",
            "ai-assisted",
            "User asked for a routing touch-up",
        )
        latest, _ = _latest_events((approval, annotation))

        self.assertEqual(latest["layout"][1], approval)

    def test_layout_review_packet_lists_requested_ai_work(self) -> None:
        review = PhaseReview(
            Path("/tmp/project"),
            next(phase for phase in PHASES if phase.key == "layout"),
            True,
            "technical evidence passed; explicit user approval is required",
            "f" * 64,
            ("garden-logger.kicad_pcb",),
            (),
            notes=("2026-08-03T11:00:00+00:00: routed the power rails on request",),
        )
        rendered = render_phase_review(review)

        self.assertIn("user-requested AI spatial work in this phase:", rendered)
        self.assertIn("routed the power rails on request", rendered)


class ReviewErgonomicsTests(StatusFixture):
    def test_review_keys_cover_final_proposal_and_handoff_packets(self) -> None:
        self.assertEqual(_review_key("spec", "final"), "spec")
        self.assertEqual(
            _review_key("architect", "proposal"),
            "architect:proposal",
        )
        self.assertEqual(
            _review_key("circuit", "proposal"),
            "circuit:proposal",
        )
        self.assertEqual(_review_key("layout", "handoff"), "layout:handoff")

    def test_review_records_round_trip_and_validate_strictly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            fingerprint = "a" * 64
            written = write_status(
                project,
                now="2026-07-31T16:00:00+00:00",
                document=StatusDocument(
                    "",
                    (),
                    {},
                    reviews={
                        "spec": ReviewRecord(
                            "2026-07-31T15:59:00+00:00",
                            fingerprint,
                        )
                    },
                ),
            )
            loaded = read_status_document(project)

            self.assertTrue(written.wrote)
            self.assertEqual(loaded.reviews["spec"].fingerprint, fingerprint)
            self.assertIn("reviews:", (project / "STATUS.md").read_text())

            for reviews, expected in (
                (
                    "  unknown: {at: now, fingerprint: " + fingerprint + "}\n",
                    "unknown review key",
                ),
                (
                    "  spec: {at: now, fingerprint: nope}\n",
                    "expected a lowercase SHA-256",
                ),
                (
                    "  spec: {at: now, fingerprint: "
                    + fingerprint
                    + ", extra: nope}\n",
                    "contains unknown keys",
                ),
            ):
                with self.subTest(expected=expected):
                    (project / "STATUS.md").write_text(
                        "---\n"
                        "pcbforge_status_schema: 1\n"
                        "updated_at: now\n"
                        "events: []\n"
                        "policy_events: []\n"
                        "transition_events: []\n"
                        "reviews:\n"
                        + reviews
                        + "checks: {}\n"
                        "---\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(StatusInputError, expected):
                        read_status_document(project)

    def test_phase_review_then_last_reviewed_approval_needs_no_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            review = review_phase(
                project,
                "spec",
                tool_root=TOOL_ROOT,
                checked_at="2026-07-31T16:10:00+00:00",
            )
            saved = read_status_document(project)
            approved = approve_phase(
                project,
                "spec",
                None,
                "User approved the saved SPEC review",
                last_reviewed=True,
                tool_root=TOOL_ROOT,
                now="2026-07-31T16:11:00+00:00",
            )

        self.assertEqual(saved.reviews["spec"].fingerprint, review.fingerprint)
        self.assertEqual(saved.checks["policy"].outcome, "pass")
        self.assertEqual(
            approved.report.document.events[-1].approval_fingerprint,
            review.fingerprint,
        )
        self.assertEqual(
            approved.report.document.reviews["spec"].fingerprint,
            review.fingerprint,
        )

    def test_proposal_review_uses_canonical_key_and_retains_prior_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            review_phase(project, "spec", tool_root=TOOL_ROOT)
            approve_phase(
                project,
                "spec",
                None,
                "User approved SPEC",
                last_reviewed=True,
                tool_root=TOOL_ROOT,
            )
            self.add_architecture_proposal(project)
            proposal = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            document = read_status_document(project)

        self.assertEqual(
            set(document.reviews),
            {"spec", "architect:proposal"},
        )
        self.assertEqual(
            document.reviews["architect:proposal"].fingerprint,
            proposal.fingerprint,
        )

    def test_blocked_review_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            self.add_architecture_proposal(project)
            review = review_phase(
                project,
                "architect",
                stage="proposal",
                tool_root=TOOL_ROOT,
            )
            document = read_status_document(project)

        self.assertFalse(review.ready)
        self.assertNotIn("architect:proposal", document.reviews)

    def test_last_reviewed_fails_closed_for_changes_missing_and_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            with self.assertRaisesRegex(StatusInputError, "no saved review for spec"):
                approve_phase(
                    project,
                    "spec",
                    None,
                    "User approval",
                    last_reviewed=True,
                    tool_root=TOOL_ROOT,
                )

            review = review_phase(project, "spec", tool_root=TOOL_ROOT)
            with self.assertRaisesRegex(StatusInputError, "mutually exclusive"):
                approve_phase(
                    project,
                    "spec",
                    review.fingerprint,
                    "User approval",
                    last_reviewed=True,
                    tool_root=TOOL_ROOT,
                )
            with self.assertRaisesRegex(StatusInputError, "one of --fingerprint"):
                approve_phase(
                    project,
                    "spec",
                    None,
                    "User approval",
                    tool_root=TOOL_ROOT,
                )

            spec = project / "spec.md"
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "# Garden logger",
                    "# Changed garden logger",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                StatusInputError,
                "artifacts changed since review; rerun status review",
            ):
                approve_phase(
                    project,
                    "spec",
                    None,
                    "User approved only the earlier packet",
                    last_reviewed=True,
                    tool_root=TOOL_ROOT,
                )

            document = read_status_document(project)

        self.assertFalse(document.events)

    def test_cascade_review_and_last_reviewed_renewal(self) -> None:
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
            review = prepare_cascade_review(
                project,
                record=True,
                reviewed_at="2026-07-31T16:20:00+00:00",
            )
            saved = read_status_document(project)
            renewed = renew_cascade(
                project,
                None,
                "User approved the saved cascade",
                last_reviewed=True,
                now="2026-07-31T16:21:00+00:00",
            )

        self.assertTrue(review.ready)
        self.assertEqual(
            saved.reviews["cascade"].fingerprint,
            review.fingerprint,
        )
        self.assertTrue(
            any(event.renewed_from for event in renewed.report.document.events)
        )

    def test_changed_cascade_rejects_last_reviewed(self) -> None:
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
            prepare_cascade_review(project, record=True)
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    "board_mm: [45, 40]",
                    "board_mm: [44, 40]",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                StatusInputError,
                "artifacts changed since review; rerun status review",
            ):
                renew_cascade(
                    project,
                    None,
                    "User approved only the earlier cascade",
                    last_reviewed=True,
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

    def _document_with_failing_placement(self, project: Path) -> StatusDocument:
        return StatusDocument(
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
            checks={
                "placement": CheckRecord(
                    "2026-07-26T10:00:00+00:00",
                    _fingerprint(project, _check_inputs(project, read_spec(project / "spec.md"), "placement")),
                    "fail",
                    "8 pass, 23 fail, 6 manual, 0 unmeasured",
                )
            },
        )

    def test_status_does_not_crash_with_a_placement_record(self) -> None:
        """`_check_inputs` is called unguarded, and raises for an unknown name.

        Without its "placement" branch every `pcbforge status` invocation dies
        with AssertionError the moment this record exists.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_failing_placement(project)
            report = inspect_status(project, document=document)
        self.assertIsNotNone(report)

    def test_failing_placement_check_leaves_health_green(self) -> None:
        """Advisory means advisory: it must not redden Health or the exit code.

        Fails if the ADVISORY_CHECKS guard in `inspect_status` is removed.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_failing_placement(project)
            report = inspect_status(project, document=document)
            rendered = render_dashboard(report)

        self.assertEqual(document.checks["placement"].outcome, "fail")
        self.assertFalse(report.checks_failed)
        self.assertIn("🟢 On track", rendered)
        self.assertNotIn("🔴 Blocked", rendered)

    def _document_with_spent_proposal_check(
        self,
        project: Path,
        *,
        proposal_approved: bool,
        fingerprint: str = "0" * 64,
    ) -> StatusDocument:
        """A failing `circuit-proposal` record, with and without its approval.

        `circuit-proposal` proves physical source did not change before the
        proposal was approved. Implementing the circuit necessarily changes that
        fingerprint, so after Gate B the record fails permanently.
        """
        events = [
            StatusEvent(
                "2026-07-26T10:00:00+00:00",
                "spec",
                "complete",
                "Approved",
                _approval_fingerprint(project, "spec"),
            ),
        ]
        if proposal_approved:
            events.append(
                StatusEvent(
                    "2026-07-26T11:00:00+00:00",
                    "circuit",
                    "proposal-approved",
                    "Approved",
                    _approval_fingerprint(project, "circuit", "proposal-approved"),
                )
            )
        return StatusDocument(
            updated_at="",
            events=tuple(events),
            checks={
                "circuit-proposal": CheckRecord(
                    "2026-07-26T12:00:00+00:00",
                    fingerprint,
                    "fail",
                    "physical source or board topology changed before proposal "
                    "approval",
                )
            },
        )

    def test_a_spent_proposal_check_leaves_health_green(self) -> None:
        """Once the proposal is approved the check has done its job.

        It gates nothing at that point: it is in neither PHASE_EVIDENCE_CHECKS
        nor APPROVAL_CHECKS, and `_gate_check_names` scopes it to the proposal
        stage. Without the STAGE_SCOPED_CHECKS guard it would hold Health red
        and print a blocker for the rest of the project's life. The guard runs
        before `_check_inputs`, so no circuit-review artifacts are needed here.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_spent_proposal_check(
                project, proposal_approved=True
            )
            report = inspect_status(project, document=document)
            rendered = render_dashboard(report)

        self.assertEqual(document.checks["circuit-proposal"].outcome, "fail")
        self.assertFalse(report.checks_failed)
        self.assertNotIn("circuit-proposal check", rendered)

    def test_the_proposal_check_still_counts_before_its_approval(self) -> None:
        """The guard is scoped, not a blanket exemption.

        While the proposal is unapproved -- or has been reopened, which makes
        `_current_circuit_proposal` None again -- a changed source baseline is
        exactly what the check exists to catch, and it must still bite. The
        fingerprint machinery is mocked so this tests the guard rather than
        `circuit_review_inputs`.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_spent_proposal_check(
                project, proposal_approved=False, fingerprint="deadbeef"
            )
            with mock.patch(
                "pcbforge.status._check_inputs",
                return_value=(project / "spec.md",),
            ), mock.patch(
                "pcbforge.status._check_fingerprint",
                return_value="deadbeef",
            ):
                report = inspect_status(project, document=document)

        self.assertTrue(report.checks_failed)

    def test_failing_placement_check_is_not_a_dashboard_blocker(self) -> None:
        """Fails if the ADVISORY_CHECKS guard in `render_dashboard` is removed."""
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_failing_placement(project)
            rendered = render_dashboard(inspect_status(project, document=document))

        blockers = rendered.split("## Blockers", 1)[1].split("##", 1)[0]
        self.assertNotIn("placement", blockers)
        self.assertIn("- None.", blockers)

    def test_placement_advisory_row_shows_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = self._document_with_failing_placement(project)
            rendered = render_dashboard(inspect_status(project, document=document))

        self.assertIn("placement check", rendered)
        self.assertIn("⚠️ Advisory", rendered)
        self.assertIn("8 pass, 23 fail", rendered)

    def test_placement_never_gates_a_phase_or_an_approval(self) -> None:
        """The two registries that block and gate must never list it."""
        for phase, names in PHASE_EVIDENCE_CHECKS.items():
            self.assertNotIn("placement", names, phase)
        for phase, names in APPROVAL_CHECKS.items():
            self.assertNotIn("placement", names, phase)
        self.assertIn("placement", ADVISORY_CHECKS)

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

    def test_layout_gate_enables_drc_check(self) -> None:
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
            # run_status_checks dispatches DRC from the latest LAYOUT declaration;
            # earlier phase validity is handled separately by the status model.
            document = StatusDocument(
                updated_at=document.updated_at,
                events=(
                    # Deliberately direct fixture construction to test dispatch only.
                    StatusEvent(
                        "2026-07-26T10:00:00+00:00",
                        "layout",
                        "complete",
                        "Placement and routing done",
                    ),
                ),
                checks=document.checks,
            )
            runner = FakeRunner()
            with mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ):
                checked = run_status_checks(
                    project,
                    document,
                    tool_root=TOOL_ROOT,
                    runner=runner,
                )

        self.assertIn("drc", checked.checks)
        self.assertEqual(checked.checks["drc"].outcome, "pass")
        self.assertEqual(len(runner.calls), 2)

    def test_drc_check_allows_only_excluded_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(
                    StatusEvent(
                        "2026-07-26T10:00:00+00:00",
                        "layout",
                        "complete",
                        "Placement and routing done",
                    ),
                ),
                checks={},
            )
            runner = FakeRunner(
                drc_report={
                    "violations": [
                        {"severity": "warning", "excluded": True},
                        {"severity": "warning", "excluded": True},
                        {"severity": "warning", "excluded": True},
                    ],
                    "unconnected_items": [],
                    "schematic_parity": [],
                }
            )
            with mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ):
                checked = run_status_checks(
                    project,
                    document,
                    tool_root=TOOL_ROOT,
                    runner=runner,
                )

        self.assertEqual(checked.checks["drc"].outcome, "pass")
        self.assertEqual(
            checked.checks["drc"].summary,
            "0 active DRC findings (0 violations, 0 unconnected, 0 parity), "
            "3 exclusions",
        )
        drc_command = next(call for call in runner.calls if "drc" in call)
        self.assertNotIn("--exit-code-violations", drc_command)

    def test_drc_check_rejects_active_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            document = StatusDocument(
                updated_at="",
                events=(
                    StatusEvent(
                        "2026-07-26T10:00:00+00:00",
                        "layout",
                        "complete",
                        "Placement and routing done",
                    ),
                ),
                checks={},
            )
            runner = FakeRunner(drc_ok=False)
            with mock.patch(
                "pcbforge.status._approval_is_current",
                return_value=True,
            ):
                checked = run_status_checks(
                    project,
                    document,
                    tool_root=TOOL_ROOT,
                    runner=runner,
                )

        self.assertEqual(checked.checks["drc"].outcome, "fail")
        self.assertEqual(
            checked.checks["drc"].summary,
            "1 active DRC findings (1 violations, 0 unconnected, 0 parity), "
            "0 exclusions",
        )


class StatusCliTests(StatusFixture):
    def test_last_reviewed_cli_records_approval_without_hash_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            with (
                mock.patch("pcbforge.cli.validate_project_compatibility"),
                mock.patch("builtins.print") as output,
            ):
                reviewed = main(
                    ["status", "review", "spec", str(project)]
                )
                approved = main(
                    [
                        "status",
                        "approve",
                        "spec",
                        "--last-reviewed",
                        "--note",
                        "User approved the saved review",
                        str(project),
                    ]
                )
            rendered = "\n".join(
                str(call.args[0]) for call in output.call_args_list
            )
            document = read_status_document(project)

        self.assertEqual(reviewed, 0)
        self.assertEqual(approved, 0)
        self.assertIn("--last-reviewed", rendered)
        self.assertEqual(
            document.events[-1].approval_fingerprint,
            document.reviews["spec"].fingerprint,
        )

    def test_approval_clis_require_exactly_one_review_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary))
            commands = (
                [
                    "status",
                    "approve",
                    "spec",
                    "--note",
                    "Approved",
                    str(project),
                ],
                [
                    "status",
                    "approve",
                    "spec",
                    "--fingerprint",
                    "a" * 64,
                    "--last-reviewed",
                    "--note",
                    "Approved",
                    str(project),
                ],
                [
                    "status",
                    "renew",
                    "--note",
                    "Approved",
                    str(project),
                ],
                [
                    "status",
                    "renew",
                    "--fingerprint",
                    "a" * 64,
                    "--last-reviewed",
                    "--note",
                    "Approved",
                    str(project),
                ],
            )
            for command in commands:
                with (
                    self.subTest(command=command),
                    mock.patch("sys.stderr"),
                    self.assertRaisesRegex(SystemExit, "2"),
                ):
                    main(command)

    def test_finish_architect_cli_success_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.project(Path(temporary), initialized=True)
            report = inspect_status(project)
            with (
                mock.patch("pcbforge.cli.validate_project_compatibility"),
                mock.patch(
                    "pcbforge.cli.finish_architect",
                    return_value=mock.Mock(report=report),
                ) as finish,
                mock.patch("builtins.print") as output,
            ):
                result = main(["finish-architect", str(project)])
            rendered = "\n".join(
                str(call.args[0]) for call in output.call_args_list
            )

            self.assertEqual(result, 0)
            finish.assert_called_once_with(project)
            self.assertIn("recorded ARCHITECT → CIRCUIT", rendered)

            for error, expected in (
                (StatusInputError("bad input"), 2),
                (StatusCheckError("build failed"), 1),
            ):
                with (
                    self.subTest(error=error),
                    mock.patch("pcbforge.cli.validate_project_compatibility"),
                    mock.patch(
                        "pcbforge.cli.finish_architect",
                        side_effect=error,
                    ),
                    mock.patch("builtins.print"),
                ):
                    self.assertEqual(
                        main(["finish-architect", str(project)]),
                        expected,
                    )

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
