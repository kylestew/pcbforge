"""Approval and synchronization integration; the PCB update here is simulated.

Native extraction runs through KiCad. Seeded acceptance isolates workflow state;
real electrical acceptance is covered by test_circuit and test_electrical.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import yaml
from pcbforge import sexpr as sx
from pcbforge.circuit import read_evidence
from pcbforge.pcb_update import prepare_pcb_update, finish_circuit, check_pcb_update
from pcbforge.schematic import SchematicError
from pcbforge.schematic_edit import SchematicDocument
from pcbforge.status import (review_phase, approve_phase, inspect_status, read_status_document,
                             mark_status, StatusInputError, run_status_checks)
from tests.native_fixture import seed_evidence
from tests.test_status import StatusFixture, FakeRunner
from tests.test_pcb_update import board_for


class NativeWorkflowTests(StatusFixture):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = self.project(Path(temp.name), initialized=True)
        self.approve_through_architect(self.directory)
        policy = self.directory / 'policy.yaml'
        data = yaml.safe_load(policy.read_text())
        for assurance in data['assurances'].values():
            assurance['evidence'] = ['Workflow unit fixture']
        data['sourcing'] = [{'lcsc':'C1', 'jlc_class':'basic', 'assembly_status':'available',
                            'lifecycle':'active', 'checked_on':'2026-09-06', 'second_source':'C2'}]
        policy.write_text(yaml.safe_dump(data))
        self.board = self.directory / 'garden-logger.kicad_pcb'
        self.graph = seed_evidence(self.directory)
        self.board.write_text(sx.dumps(board_for(type(self.graph)((), {}, self.graph.root_uuid))))
        def checked(*args, **kwargs):
            self.graph = seed_evidence(self.directory)
            return SimpleNamespace(ok=True, summary='Seeded workflow fixture')
        patch = mock.patch('pcbforge.circuit.check_circuit', side_effect=checked)
        patch.start(); self.addCleanup(patch.stop)

    def review(self):
        return review_phase(self.directory, 'circuit', runner=FakeRunner())

    def approve_circuit(self):
        review = self.review()
        self.assertTrue(review.ready, review.detail)
        return approve_phase(self.directory, 'circuit', review.fingerprint,
                             'Synthetic approval for workflow test', runner=FakeRunner())

    def simulate_update(self):
        prepare_pcb_update(self.directory)
        self.board.write_text(sx.dumps(board_for(self.graph)))
        check_pcb_update(self.directory)
        return finish_circuit(self.directory)

    def test_single_circuit_approval_completes_only_after_checked_update(self):
        approved = self.approve_circuit()
        self.assertFalse(approved.report.phases[2].complete)
        finished = self.simulate_update()
        self.assertTrue(finished.report.phases[2].complete)
        approvals = [e for e in finished.report.document.events if e.phase == 'circuit' and e.action == 'complete']
        self.assertEqual(len(approvals), 1)
        self.assertEqual(finished.report.document.transition_events[-1].transition, 'circuit-sync')

    def test_drawing_change_between_review_and_approval_rejects_packet(self):
        review = self.review()
        doc = SchematicDocument.load(self.directory/'garden-logger.kicad_sch')
        doc.text_note('Changed after review', (25.4,25.4)); doc.save()
        with self.assertRaisesRegex(StatusInputError, 'stale|changed'):
            approve_phase(self.directory, 'circuit', review.fingerprint, 'Synthetic stale approval', runner=FakeRunner())

    def test_drawing_change_after_approval_preserves_electrical_decision(self):
        self.approve_circuit(); self.simulate_update()
        doc = SchematicDocument.load(self.directory/'garden-logger.kicad_sch')
        doc.text_note('Cosmetic revision', (25.4,25.4)); doc.save()
        report = inspect_status(self.directory)
        self.assertTrue(report.phases[2].complete)
        read_evidence(self.directory)
        with self.assertRaisesRegex(SchematicError, 'presentation'):
            read_evidence(self.directory, require_presentation=True)

    def test_upstream_reopen_prevents_update_with_old_circuit_approval(self):
        self.approve_circuit()
        mark_status(self.directory, 'architect', 'reopened', 'Synthetic MCU revision')
        with self.assertRaises(SchematicError):
            prepare_pcb_update(self.directory)

    def test_pad_net_edit_breaks_sync_and_blocks_layout(self):
        self.approve_circuit(); self.simulate_update()
        root = sx.parse(self.board.read_text())
        sx.child(sx.child(sx.child(root, 'footprint'), 'pad'), 'net')[1] = sx.Quoted('WRONG')
        self.board.write_text(sx.dumps(root))
        report = inspect_status(self.directory)
        self.assertFalse(report.phases[2].complete)
        self.assertFalse(report.phases[3].complete)

    def test_policy_check_from_spec_is_rerun_for_circuit(self):
        # Restore a passing SPEC-only check with the current input fingerprint.
        from dataclasses import replace
        from pcbforge.status import _check_inputs, _check_fingerprint
        document = read_status_document(self.directory)
        spec = inspect_status(self.directory).spec
        prior = replace(document.checks['policy'], scope='spec',
                        fingerprint=_check_fingerprint(self.directory,'policy',_check_inputs(self.directory,spec,'policy')))
        document = replace(document, checks={**document.checks, 'policy':prior})
        from pcbforge.policy import check_policy
        with mock.patch('pcbforge.status.check_policy', wraps=check_policy) as checked:
            result = run_status_checks(self.directory, document, runner=FakeRunner())
        self.assertTrue(checked.called)
        self.assertEqual(result.checks['policy'].scope, 'circuit')
