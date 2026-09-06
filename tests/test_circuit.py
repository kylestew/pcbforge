import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import yaml
from pcbforge import sexpr as sx
from pcbforge.circuit import check_circuit, read_evidence, fingerprint_inputs, presentation_fingerprint, check_mcu, check_component_parts, GRAPH_PATH, BOM_PATH, EVIDENCE_PATH
from pcbforge.schematic import SchematicError, CircuitGraph, CircuitComponent, CircuitPin
from pcbforge.schematic_edit import SchematicDocument
from pcbforge.schematic_lint import lint_saved
from tests.native_fixture import seed_native
from tests.test_pcb_update import SPEC

ROOT=Path(__file__).resolve().parents[1]

class CircuitChecks(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.project=Path(temp.name).resolve()
        (self.project/'spec.md').write_text(SPEC)
        self.path=seed_native(self.project,evidence=False)
        doc=SchematicDocument.load(self.path)
        # A second fitted resistor removes isolated-label ERC warnings.
        doc.move_symbol('R1',(50.8,50.8))
        for label in sx.children(doc.root,'global_label'):
            label_at=doc.pin_position('R1','1' if sx.atom(label)=='+3V3' else '2')
            sx.child(label,'at')[1:3]=list(map(str,label_at))
        doc.add_symbol('R2','Device:R',(76.2,50.8),value='10k',footprint='Resistor_SMD:R_0603_1608Metric',
                       fields={'MPN':'TEST-R','LCSC':'C1','Datasheet':'https://example.test/datasheet','pcbforge_purpose':'Second test branch'})
        for number,name in [('1','+3V3'),('2','GND')]:doc.label(name,doc.pin_position('R2',number),kind='global_label')
        self.path.write_text(doc.serialize())
        facts={'electrical_facts_schema':1,'values':{},'parts':{ref:{'mpn':'TEST-R','source':'Unit fixture, datasheet table',
                     'footprint':'Resistor_SMD:R_0603_1608Metric','pins':{'1':'','2':''}} for ref in ['R1','R2']},'mcu':{}}
        (self.project/'electrical-facts.yaml').write_text(yaml.safe_dump(facts))
        patch=mock.patch('pcbforge.circuit.check_mcu');self.mcu=patch.start();self.addCleanup(patch.stop)

    def check(self,write=True):return check_circuit(self.project,write_report=write)

    def test_real_erc_native_parts_tests_and_preview_produce_bound_evidence(self):
        result=self.check();data=read_evidence(self.project,require_presentation=True)
        self.assertEqual(data['fingerprint'],result.fingerprint);self.assertEqual(len(data['tests']),1)
        self.assertTrue(data['previews']);self.assertEqual(json.loads((self.project/BOM_PATH).read_text())['components'][0]['quantity'],2)
        before=self.path.read_bytes();self.check();self.assertEqual(self.path.read_bytes(),before)

    def test_read_only_check_does_not_write_acceptance_artifacts(self):
        self.check(write=False);self.assertFalse((self.project/EVIDENCE_PATH).exists());self.assertFalse((self.project/GRAPH_PATH).exists())

    def test_electrical_edit_stales_evidence_but_drawing_edit_only_stales_presentation(self):
        self.check();electrical=fingerprint_inputs(self.project);presentation=presentation_fingerprint(self.project)
        doc=SchematicDocument.load(self.path);doc.text_note('User note',(25.4,25.4));doc.save()
        self.assertEqual(electrical,fingerprint_inputs(self.project));self.assertNotEqual(presentation,presentation_fingerprint(self.project))
        read_evidence(self.project)
        with self.assertRaisesRegex(SchematicError,'presentation'):read_evidence(self.project,require_presentation=True)
        self.check();doc.set_field('R1','Value','1k');doc.save()
        with self.assertRaisesRegex(SchematicError,'electrically stale'):read_evidence(self.project)

    def test_derived_graph_bom_report_and_preview_tampering_are_detected(self):
        self.check()
        for relative in [GRAPH_PATH,BOM_PATH,Path('docs/circuit-check.md'),Path('review/circuit/preview/test.svg')]:
            path=self.project/relative
            if not path.exists():
                path=next((self.project/'review/circuit/preview').glob('*.svg'))
            old=path.read_bytes();path.write_bytes(b'modified')
            with self.assertRaises(SchematicError):read_evidence(self.project,require_presentation=True)
            path.write_bytes(old)

    def test_failed_electrical_test_does_not_overwrite_passing_evidence(self):
        self.check();before=(self.project/EVIDENCE_PATH).read_bytes()
        (self.project/'circuit_tests.py').write_text('def test_supply(ctx):\n    ctx.rail("R1.1", "WRONG")\n')
        with self.assertRaisesRegex(SchematicError,'electrical acceptance failed'):self.check()
        self.assertEqual(before,(self.project/EVIDENCE_PATH).read_bytes())

    def test_pin_function_or_footprint_evidence_mismatch_blocks(self):
        path=self.project/'electrical-facts.yaml';data=yaml.safe_load(path.read_text());data['parts']['R1']['pins']['1']='wrong';path.write_text(yaml.safe_dump(data))
        with self.assertRaisesRegex(SchematicError,'part-pin-functions'):self.check()

    def test_missing_part_identity_is_rejected(self):
        doc=SchematicDocument.load(self.path);doc.set_field('R1','LCSC','');doc.save()
        with self.assertRaisesRegex(SchematicError,'part-identity'):self.check()

    def test_lint_reports_saved_overlap_and_dangling_label(self):
        doc=SchematicDocument.load(self.path);doc.text_note('BAD DRAWING',(50.8,50.8));doc.label('FLOATING',(150,150));doc.save()
        codes={f.code for f in lint_saved(self.path)}
        self.assertIn('dangling-label',codes);self.assertIn('text-symbol-overlap',codes)
        with self.assertRaises(SchematicError):self.check()

    def test_exclusions_require_exact_current_ids_and_rationale(self):
        path=self.project/'circuit-review.yaml';data=yaml.safe_load(path.read_text());data['erc_exclusions']=[{'id':'nonexistent','rationale':'Test stale exclusion'}];path.write_text(yaml.safe_dump(data))
        with self.assertRaisesRegex(SchematicError,'stale or unknown'):self.check()

    def test_helper_and_declared_data_changes_stale_acceptance(self):
        helper=self.project/'helpers.py';helper.write_text('LIMIT = 1\n')
        datafile=self.project/'limits.csv';datafile.write_text('voltage,3.3\n')
        contract=self.project/'circuit-tests.yaml'
        data=yaml.safe_load(contract.read_text());data['inputs']=['limits.csv'];contract.write_text(yaml.safe_dump(data))
        self.check()
        for path in (helper,datafile):
            original=path.read_bytes();path.write_bytes(original+b'# change\n')
            with self.assertRaisesRegex(SchematicError,'electrically stale'):read_evidence(self.project)
            path.write_bytes(original)

    def test_variants_are_rejected(self):
        (self.project/'test.kicad_pro').write_text('{"variants": [{"name":"alternate"}]}')
        with self.assertRaisesRegex(SchematicError,'variants'):self.check()

class MCUParityTests(unittest.TestCase):
    def test_exact_mcu_pin_signal_and_net_assignments_are_required(self):
        graph=CircuitGraph((CircuitComponent('U1','/u','MCU','STM32','QFP',{'MPN':'STM32G031K8T6'},(CircuitPin('1','PA13','bidirectional','SWDIO'),)),),{'SWDIO':('U1.1',)},'root')
        facts={'mcu':{'reference':'U1','assignments':{'PA13':{'pin':'1','signal':'SYS_SWDIO','net':'SWDIO'}}}}
        checked=SimpleNamespace(part_number='STM32G031K8T6',pins=[SimpleNamespace(pin='PA13',signal='SYS_SWDIO')])
        with mock.patch('pcbforge.ioc.check_ioc',return_value=checked):
            check_mcu(graph,facts,Path('/tmp'))
            for key,value in [('signal','GPIO_Input'),('net','WRONG'),('pin','99')]:
                original=facts['mcu']['assignments']['PA13'][key];facts['mcu']['assignments']['PA13'][key]=value
                with self.assertRaises(SchematicError):check_mcu(graph,facts,Path('/tmp'))
                facts['mcu']['assignments']['PA13'][key]=original
            checked.part_number='STM32G071KBT6'
            with self.assertRaisesRegex(SchematicError,'MPN'):check_mcu(graph,facts,Path('/tmp'))
