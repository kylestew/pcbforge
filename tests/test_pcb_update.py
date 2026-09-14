from __future__ import annotations
from dataclasses import replace
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from pcbforge import sexpr as sx
from pcbforge.pcb_update import prepare_pcb_update, check_pcb_update, board_state, _parity, SYNC_PATH, UPDATE_PATH
from pcbforge.schematic import SchematicError, canonical
from pcbforge.schematic_edit import SchematicDocument
from tests.native_fixture import seed_native, seed_evidence

SPEC='''---
spec_schema: 1
name: test
layers: 2
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: []
board_mm: [50, 40]
---
# Native acceptance fixture
'''

def board_for(graph, *, copper=False):
    root=['kicad_pcb',['version','20260206'],['general',['thickness','1.6']],
          ['gr_line',['start','0','0'],['end','50','0'],['stroke',['width','0.05'],['type','default']],['layer',sx.Quoted('Edge.Cuts')]]]
    for i,c in enumerate(graph.components):
        if c.exclude_board:continue
        fp=['footprint',sx.Quoted(c.footprint),['layer',sx.Quoted('F.Cu')],['at',str(10+i*10),'20','0'],
            ['uuid',sx.Quoted('fp-'+c.reference)],['path',sx.Quoted('/' + '/'.join(c.identity.strip('/').split('/')[1:]))]]
        fp.append(['attr','smd',*(['dnp'] if c.dnp else []),*(['exclude_from_bom'] if c.exclude_bom else [])])
        for name,value in {'Reference':c.reference,'Value':c.value,**c.fields}.items():
            if name in {'Footprint','dnp','exclude_from_bom','exclude_from_board'}:continue
            fp.append(['property',sx.Quoted(name),sx.Quoted(value),['at','0','0','0'],['layer',sx.Quoted('F.Fab')]])
        for j,p in enumerate(c.pins):
            fp.append(['pad',sx.Quoted(p.number),'smd','rect',['at',str(j),'0'],['size','1','1'],['layers',sx.Quoted('F.Cu')],
                       *([['net',sx.Quoted(p.net)]] if p.net else [])])
        root.append(fp)
    if copper:
        root += [['segment',['start','10','20'],['end','12','20'],['width','0.25'],['layer',sx.Quoted('F.Cu')],['net',sx.Quoted('+3V3')],['uuid',sx.Quoted('route')]],
                 ['zone',['net',sx.Quoted('GND')],['layer',sx.Quoted('B.Cu')],['uuid',sx.Quoted('zone')],['polygon',['pts',['xy','0','0'],['xy','20','0'],['xy','20','20']]]]]
    return root

class PCBUpdateTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.project=Path(temp.name).resolve()
        (self.project/'spec.md').write_text(SPEC)
        seed_native(self.project);self.graph=seed_evidence(self.project)
        self.board=self.project/'test.kicad_pcb';self.board.write_text(sx.dumps(board_for(type(self.graph)((),{},self.graph.root_uuid))))
        self.approval=SimpleNamespace(approval_fingerprint='a'*64)
        patch=mock.patch('pcbforge.pcb_update._approved',side_effect=lambda p:(None,self.approval));patch.start();self.addCleanup(patch.stop)

    def write(self,root):self.board.write_text(sx.dumps(root))

    def first_update(self,copper=False):
        prepare_pcb_update(self.project);self.write(board_for(self.graph));result=check_pcb_update(self.project)
        (self.project/SYNC_PATH).write_bytes(canonical({'schema':1,'fingerprint':result.fingerprint,'graph':result.graph}))
        if copper:self.write(board_for(self.graph,copper=True))
        return result

    def revision(self):
        self.first_update(copper=True)
        doc=SchematicDocument.load(self.project/'test.kicad_sch');doc.set_field('R1','Value','22k');doc.save()
        self.graph=seed_evidence(self.project);self.approval.approval_fingerprint='b'*64
        prepare_pcb_update(self.project);self.write(board_for(self.graph,copper=True))

    def test_old_board_format_cannot_enter_native_synchronization(self):
        raw=self.board.read_text();self.board.write_text(raw.replace('20260206','20241229'))
        with self.assertRaisesRegex(SchematicError,'KiCad 10.0.3'):prepare_pcb_update(self.project)
        self.assertFalse((self.project/UPDATE_PATH).exists())

    def test_initial_update_uses_native_identity_fields_and_pad_nets(self):
        raw=self.board.read_bytes();path=prepare_pcb_update(self.project);data=json.loads(path.read_text())
        self.assertEqual((self.project/data['backup']).read_bytes(),raw)
        self.write(board_for(self.graph));result=check_pcb_update(self.project)
        self.assertIn('preservation passed',result.summary)

    def test_nested_sheet_paths_keep_instance_identity(self):
        component = self.graph.components[0]
        root = '/' + self.graph.root_uuid
        symbol = component.uuids[0]
        graph = replace(self.graph, components=(
            replace(component, identity=root + '/sheet-a/nested/' + symbol),
            replace(component, reference='R2', identity=root + '/sheet-b/nested/' + symbol),
        ))
        self.write(board_for(graph))
        _parity(graph, board_state(self.board))
        base = sx.parse(self.board.read_text())
        for bad_path in ['/' + symbol, '/wrong/nested/' + symbol,
                         root + '/sheet-a/nested/' + symbol]:
            with self.subTest(path=bad_path):
                changed = copy.deepcopy(base)
                sx.child(sx.child(changed, 'footprint'), 'path')[1] = sx.Quoted(bad_path)
                self.write(changed)
                with self.assertRaisesRegex(SchematicError, 'identities differ'):
                    _parity(graph, board_state(self.board))

    def test_native_save_defaults_preserve_settings_but_real_edits_fail(self):
        self.first_update()
        base = board_for(self.graph)
        base.append(['setup', ['pad_to_mask_clearance', '0'],
                     ['allow_soldermask_bridges_in_footprints', 'no'],
                     ['tenting', 'front', 'back'], ['aux_axis_origin', '100', '100']])
        self.write(base)
        self.approval.approval_fingerprint = 'b'*64
        prepare_pcb_update(self.project)
        saved = copy.deepcopy(base)
        saved.remove(sx.child(saved, 'setup'))
        saved.append(sx.parse((Path(__file__).parent/'fixtures/native-saved-setup.sexpr').read_text()))
        self.write(saved)
        check_pcb_update(self.project)
        for tag in ['tenting', 'covering', 'plugging', 'capping', 'filling',
                    'pad_to_mask_clearance', 'pcbplotparams']:
            with self.subTest(setting=tag):
                changed = copy.deepcopy(saved)
                node = sx.child(sx.child(changed, 'setup'), tag)
                if tag in {'tenting', 'covering', 'plugging'}:
                    sx.child(node, 'front')[1] = 'no' if tag == 'tenting' else 'yes'
                elif tag == 'pcbplotparams':
                    sx.child(node, 'mirror')[1] = 'yes'
                else:
                    node[1] = '0.1' if tag == 'pad_to_mask_clearance' else 'yes'
                self.write(changed)
                with self.assertRaisesRegex(SchematicError, 'board settings'):
                    check_pcb_update(self.project)

    def test_wrong_package_fails_without_a_footprint_property(self):
        root = board_for(self.graph)
        sx.child(root, 'footprint')[1] = sx.Quoted('Wrong:Package')
        self.write(root)
        with self.assertRaisesRegex(SchematicError, 'footprint differs'):
            _parity(self.graph, board_state(self.board))

    def test_electrical_revision_preserves_routed_board(self):
        self.revision();before=self.board.read_bytes();check_pcb_update(self.project);self.assertEqual(self.board.read_bytes(),before)

    def test_route_zone_and_placement_mutations_are_rejected(self):
        self.revision();base=sx.parse(self.board.read_text())
        for kind in ['segment','zone','footprint']:
            with self.subTest(kind=kind):
                root=copy.deepcopy(base);node=sx.child(root,kind)
                if kind=='segment':sx.child(node,'end')[1]='99'
                elif kind=='zone':sx.child(sx.child(node,'polygon'),'pts')[1][1]='99'
                else:sx.child(node,'at')[1]='99'
                self.write(root)
                with self.assertRaisesRegex(SchematicError,'changed'):check_pcb_update(self.project)

    def test_wrong_reference_field_value_net_or_pad_fails(self):
        prepare_pcb_update(self.project);base=board_for(self.graph)
        for kind in ['Reference','MPN','Value','net','pad','path']:
            with self.subTest(kind=kind):
                root=copy.deepcopy(base);fp=sx.child(root,'footprint')
                if kind in {'Reference','MPN','Value'}:
                    next(p for p in sx.children(fp,'property') if sx.atom(p)==kind)[2]=sx.Quoted('wrong')
                elif kind=='net':sx.child(sx.child(fp,'pad'),'net')[1]=sx.Quoted('wrong')
                elif kind=='pad':fp.remove(sx.child(fp,'pad'))
                else:sx.child(fp,'path')[1]=sx.Quoted('/wrong')
                self.write(root)
                with self.assertRaises(SchematicError):check_pcb_update(self.project)

    def test_existing_pad_geometry_cannot_change_during_update(self):
        self.revision();root=sx.parse(self.board.read_text());sx.child(sx.child(sx.child(root,'footprint'),'pad'),'at')[1]='4';self.write(root)
        with self.assertRaisesRegex(SchematicError,'geometry'):check_pcb_update(self.project)

    def test_backup_and_approval_changes_fail_closed(self):
        path=prepare_pcb_update(self.project);data=json.loads(path.read_text());self.write(board_for(self.graph))
        (self.project/data['backup']).write_text('modified')
        with self.assertRaisesRegex(SchematicError,'backup'):check_pcb_update(self.project)
        self.approval.approval_fingerprint='c'*64
        with self.assertRaisesRegex(SchematicError,'stale'):check_pcb_update(self.project)

    def test_preparation_does_not_replace_a_changed_pending_board(self):
        prepare_pcb_update(self.project);before=(self.project/UPDATE_PATH).read_bytes();self.write(board_for(self.graph))
        with self.assertRaisesRegex(SchematicError,'changed after update'):prepare_pcb_update(self.project)
        self.assertEqual((self.project/UPDATE_PATH).read_bytes(),before)

    def test_existing_board_without_sync_baseline_is_refused(self):
        self.write(board_for(self.graph))
        with self.assertRaisesRegex(SchematicError,'fresh projects'):prepare_pcb_update(self.project)

    def test_zone_fill_cache_may_change_but_zone_boundary_may_not(self):
        self.revision();root=sx.parse(self.board.read_text());sx.child(root,'zone').append(['filled_polygon',['layer',sx.Quoted('B.Cu')],['pts',['xy','1','1']]])
        self.write(root);check_pcb_update(self.project)

    def test_approved_net_rename_preserves_authored_copper(self):
        self.first_update(copper=True)
        doc=SchematicDocument.load(self.project/'test.kicad_sch')
        for label in sx.children(doc.root,'global_label'):
            if sx.atom(label)=='+3V3':label[1]=sx.Quoted('VDD')
        doc.save();self.graph=seed_evidence(self.project);self.approval.approval_fingerprint='b'*64;prepare_pcb_update(self.project)
        root=board_for(self.graph,copper=True);sx.child(sx.child(root,'segment'),'net')[1]=sx.Quoted('VDD');self.write(root)
        check_pcb_update(self.project)


import pcbforge.pcb_update as mod

class SupersededUpdateTests(PCBUpdateTests):
 def next_revision(self):
  self.revision()
  pending=(self.project/mod.UPDATE_PATH).read_bytes()
  doc=SchematicDocument.load(self.project/'test.kicad_sch');doc.set_field('R1','Value','33k');doc.save()
  self.graph=seed_evidence(self.project);self.approval.approval_fingerprint='c'*64
  event=SimpleNamespace(phase='circuit',action='complete',approval_fingerprint='b'*64)
  document=SimpleNamespace(events=[event])
  patch=mock.patch('pcbforge.pcb_update._approved',return_value=(document,self.approval));patch.start();self.addCleanup(patch.stop)
  return pending,document
 def test_accepts_prior_approved_circuit_and_keeps_current_layout(self):
  pending,document=self.next_revision();sync=(self.project/mod.SYNC_PATH).read_bytes()
  root=sx.parse(self.board.read_text());sx.child(sx.child(root,'footprint'),'at')[1]='99';self.write(root);raw=self.board.read_bytes()
  data=json.loads(mod.prepare_pcb_update(self.project).read_text())
  self.assertEqual((self.project/data['backup']).read_bytes(),raw)
  self.assertEqual((self.project/data['superseded_update']['archive']).read_bytes(),pending)
  self.assertEqual((self.project/mod.SYNC_PATH).read_bytes(),sync)
  self.assertEqual(self.board.read_bytes(),raw)
  root=board_for(self.graph,copper=True);sx.child(sx.child(root,'footprint'),'at')[1]='99';self.write(root)
  mod.check_pcb_update(self.project)
 def test_rejects_unapproved_predecessor(self):
  pending,doc=self.next_revision();doc.events=[]
  with self.assertRaises(SchematicError):mod.prepare_pcb_update(self.project)
  self.assertEqual((self.project/mod.UPDATE_PATH).read_bytes(),pending)
 def test_rejects_corrupt_prior_backup(self):
  pending,doc=self.next_revision();p=json.loads(pending);(self.project/p['backup']).write_text('corrupt')
  with self.assertRaisesRegex(SchematicError,'backup'):mod.prepare_pcb_update(self.project)
 def test_rejects_board_with_unapproved_electrical_change(self):
  pending,doc=self.next_revision();root=sx.parse(self.board.read_text());fp=sx.child(root,'footprint');next(p for p in sx.children(fp,'property') if sx.atom(p)=='Value')[2]=sx.Quoted('wrong');self.write(root)
  with self.assertRaises(SchematicError):mod.prepare_pcb_update(self.project)
