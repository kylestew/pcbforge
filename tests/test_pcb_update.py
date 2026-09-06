from __future__ import annotations
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
            ['uuid',sx.Quoted('fp-'+c.reference)],['path',sx.Quoted(c.identity)]]
        fp.append(['attr','smd',*(['dnp'] if c.dnp else []),*(['exclude_from_bom'] if c.exclude_bom else [])])
        for name,value in {'Reference':c.reference,'Value':c.value,**c.fields}.items():
            if name in {'dnp','exclude_from_bom','exclude_from_board'}:continue
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

    def test_initial_update_uses_native_identity_fields_and_pad_nets(self):
        raw=self.board.read_bytes();path=prepare_pcb_update(self.project);data=json.loads(path.read_text())
        self.assertEqual((self.project/data['backup']).read_bytes(),raw)
        self.write(board_for(self.graph));result=check_pcb_update(self.project)
        self.assertIn('preservation passed',result.summary)

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
