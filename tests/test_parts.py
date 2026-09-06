import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import yaml
from pcbforge.circuit import load_graph, check_component_parts
from pcbforge.parts import check_parts, render_parts_audit
from pcbforge.kicad_fp import footprint_pads
from tests.native_fixture import seed_native
from tests.test_pcb_update import SPEC

ROOT=Path(__file__).resolve().parents[1]

class NativePartsTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.project=Path(t.name).resolve();(self.project/'spec.md').write_text(SPEC)
        seed_native(self.project,evidence=False);self.graph=load_graph(self.project)
        self.facts={'electrical_facts_schema':1,'values':{},'mcu':{},'parts':{'R1':{'mpn':'TEST-R','source':'test source',
                    'footprint':'Resistor_SMD:R_0603_1608Metric','pins':{'1':'','2':''}}}}
        (self.project/'electrical-facts.yaml').write_text(yaml.safe_dump(self.facts))

    def test_official_commodity_assets_pass(self):
        self.assertEqual(check_component_parts(self.graph,self.facts,self.project,ROOT),())
        result=check_parts(self.project);self.assertTrue(result.ok);self.assertIn('1 schematic parts',render_parts_audit(result))

    def test_custom_commodity_symbol_fails_even_with_a_rationale(self):
        component=dataclasses.replace(self.graph.components[0],symbol='Vendor:Custom0603')
        graph=dataclasses.replace(self.graph,components=(component,));self.facts['parts']['R1']['library_rationale']='Test search'
        self.assertIn('part-commodity',{f.code for f in check_component_parts(graph,self.facts,self.project,ROOT)})

    def test_project_table_precedence_cannot_hide_custom_commodity_footprint(self):
        library=self.project/'parts/Resistor_SMD.pretty';library.mkdir(parents=True)
        (library/'R_0603_1608Metric.kicad_mod').write_text('(footprint "R" (pad "1" smd rect) (pad "2" smd rect))')
        (self.project/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Resistor_SMD") (type "KiCad") (uri "${KIPRJMOD}/parts/Resistor_SMD.pretty")))')
        self.assertIn('part-commodity',{f.code for f in check_component_parts(self.graph,self.facts,self.project,ROOT)})

    def test_local_pad_discovery_updates_after_file_edit(self):
        library=self.project/'parts/Test.pretty';library.mkdir(parents=True);path=library/'Part.kicad_mod'
        (self.project/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Test") (type "KiCad") (uri "${KIPRJMOD}/parts/Test.pretty")))')
        path.write_text('(footprint "Part" (pad "1" smd rect))');self.assertEqual(footprint_pads('Test:Part',None,self.project)[0],{'1'})
        path.write_text('(footprint "Part" (pad "2" smd rect))');self.assertEqual(footprint_pads('Test:Part',None,self.project)[0],{'2'})

    def test_sourced_package_pin_map_is_required(self):
        self.facts['parts']['R1']['pins']['1']='VCC'
        self.assertIn('part-pin-functions',{f.code for f in check_component_parts(self.graph,self.facts,self.project,ROOT)})
