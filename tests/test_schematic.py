from __future__ import annotations
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from pcbforge import sexpr as sx
from pcbforge.schematic import SchematicError, extract, hierarchy, semantic_diff, parse_netlist
from pcbforge.schematic_edit import SchematicDocument

class SavedSchematicTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.kicad_sch'

    def document(self):
        doc=SchematicDocument.create(self.path)
        doc.add_symbol('R1','Device:R',(50.8,50.8),value='10k',footprint='Resistor_SMD:R_0603_1608Metric')
        doc.label('VIN',doc.pin_position('R1','1'),kind='global_label')
        doc.label('GND',doc.pin_position('R1','2'),kind='global_label')
        return doc

    def test_native_extraction_and_noop_save_preserve_bytes_and_identity(self):
        doc=self.document();doc.save();raw=self.path.read_bytes();a=extract(self.path)
        self.assertEqual(a.pin('R1.1').net,'VIN');self.assertEqual(a.pin('R1.2').net,'GND')
        loaded=SchematicDocument.load(self.path);self.assertIsNone(loaded.save());self.assertEqual(raw,self.path.read_bytes())
        loaded.set_field('R1','Value','22k');backup=loaded.save();b=extract(self.path)
        self.assertEqual(backup.read_bytes(),raw);self.assertEqual(a.component('R1').identity,b.component('R1').identity)
        self.assertEqual(b.component('R1').value,'22k');self.assertTrue(semantic_diff(a,b))

    def test_cosmetic_edits_preserve_electrical_semantics(self):
        doc=self.document();doc.save();a=extract(self.path)
        doc.text_note('Reviewed supply',(30,20));doc.save();b=extract(self.path)
        self.assertEqual(a.fingerprint,b.fingerprint)

    def test_failed_validation_and_transaction_leave_source_intact(self):
        doc=self.document();doc.save();raw=self.path.read_bytes()
        with self.assertRaises(SchematicError):
            with doc.transaction():
                doc.set_field('R1','Value','47k')
                raise SchematicError('stop')
        self.assertEqual(doc.serialize().encode(),raw)
        doc.set_field('R1','Value','47k')
        def fail(cmd,**kwargs):return subprocess.CompletedProcess(cmd,1,'','deliberate failure')
        with self.assertRaises(SchematicError):doc.save(runner=fail)
        self.assertEqual(self.path.read_bytes(),raw)

    def test_concurrent_save_is_refused(self):
        doc=self.document();doc.save();doc.set_field('R1','Value','47k')
        self.path.write_text(self.path.read_text()+'\n')
        with self.assertRaisesRegex(SchematicError,'changed since load'):doc.save()

    def test_intentional_no_connect_is_extracted(self):
        doc=SchematicDocument.create(self.path);doc.add_symbol('R1','Device:R',(50.8,50.8))
        doc.no_connect('R1','1');doc.save();graph=extract(self.path)
        self.assertTrue(graph.pin('R1.1').no_connect)
        self.assertFalse(graph.pin('R1.2').no_connect)
        self.assertFalse(graph.connected('R1.1','R1.2'))

    def test_fitted_flags_are_extracted(self):
        for flag,value,attribute in [('dnp',True,'dnp'),('in_bom',False,'exclude_bom'),('on_board',False,'exclude_board')]:
            with self.subTest(flag=flag):
                if self.path.exists():self.path.unlink()
                doc=self.document();doc.set_flag('R1',flag,value);doc.save();graph=extract(self.path)
                self.assertTrue(getattr(graph.component('R1'),attribute));self.assertFalse(graph.bom())

    def test_sheet_interface_connects_parent_and_child_with_real_kicad(self):
        from pcbforge.schematic import properties
        parent=self.document()
        child=SchematicDocument.create(self.path.parent/'channel.kicad_sch')
        child.add_symbol('R2','Device:R',(50.8,50.8),value='10k',footprint='Resistor_SMD:R_0603_1608Metric')
        child.label('INPUT',child.pin_position('R2','1'),kind='hierarchical_label')
        sheet=parent.add_sheet('Channel','channel.kicad_sch',(100,40),(40,30))
        parent.sheet_pin(sheet,'INPUT',(100,50),direction='input')
        start=parent.pin_position('R1','1')
        parent.wire(start,(90,start[1]),(90,50),(100,50))
        root=sx.atom(sx.child(parent.root,'uuid'))
        child.bind_instance(self.path.stem,'/'+root+'/'+sheet,{'R2':'R2'})
        child.path.write_text(child.serialize());parent.save()
        graph=extract(self.path)
        self.assertTrue(graph.connected('R1.1','R2.1'))
        with self.assertRaisesRegex(SchematicError,'boundary'):
            parent.sheet_pin(sheet,'WRONG',(110,50))

    def test_multiunit_aggregation_has_every_pin_and_stable_identity(self):
        doc=SchematicDocument.create(self.path)
        for unit in (1,2,3):doc.add_symbol('U1','Amplifier_Operational:LM358',(50.8+unit*25.4,50.8),unit=unit)
        doc.save();a=extract(self.path)
        self.assertEqual(len(a.components),1);self.assertEqual(len(a.component('U1').pins),8)
        self.assertEqual(len(a.component('U1').uuids),3)
        doc.set_field('U1','Value','LM358B');doc.save();
        self.assertEqual({sx.atom(p,2) for n in sx.children(doc.root,'symbol') for p in sx.children(n,'property') if sx.atom(p)=='Value'}, {'LM358B'})
        self.assertEqual(extract(self.path).component('U1').identity,a.component('U1').identity)

    def test_repeated_hierarchy_has_distinct_instance_identities(self):
        root=SchematicDocument.create(self.path);child=SchematicDocument.create(self.path.parent/'channel.kicad_sch')
        child.add_symbol('R1','Device:R',(50.8,50.8))
        root_uid=sx.atom(sx.child(root.root,'uuid'))
        for n in (1,2):
            sheet=root.add_sheet(f'Channel {n}','channel.kicad_sch',(n*50.8,100))
            child.bind_instance('test',f'/{root_uid}/{sheet}',{'R1':f'R{n}'})
        child.path.write_text(child.serialize());root.save();graph=extract(self.path)
        self.assertEqual({c.reference for c in graph.components},{'R1','R2'})
        self.assertEqual(len({c.identity for c in graph.components}),2)
        self.assertEqual(len(hierarchy(self.path)),3)

    def test_hierarchy_cycles_and_external_sheets_are_refused(self):
        doc=SchematicDocument.create(self.path);doc.add_sheet('Cycle','test.kicad_sch',(50,50));self.path.write_text(doc.serialize())
        with self.assertRaisesRegex(SchematicError,'cyclic'):hierarchy(self.path)
        with self.assertRaisesRegex(SchematicError,'relative'):doc.add_sheet('Outside','../outside.kicad_sch',(0,0))

    def test_untouched_records_survive_formatting_and_edit(self):
        doc=self.document();doc.text_note('User drawing',(20,20));self.path.write_text(doc.serialize())
        loaded=SchematicDocument.load(self.path);raw=loaded.text
        note=next(n for n in sx.children(loaded.root,'text'))
        from pcbforge.schematic_edit import record_spans
        original_records=[raw[a:b] for a,b in record_spans(raw)]
        loaded.set_field('R1','Value','5k1');new=loaded.serialize()
        for record in original_records:
            if sx.head(sx.parse(record))!='symbol':self.assertIn(record,new)
