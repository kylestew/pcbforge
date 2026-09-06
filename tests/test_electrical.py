import dataclasses
import math
import tempfile
import unittest
from pathlib import Path
import yaml
from pcbforge.electrical import CircuitTestContext, ElectricalError, quantity, interval, divider, rc_time, read_contract, read_facts, run_tests
from pcbforge.schematic import CircuitGraph, CircuitComponent, CircuitPin, SchematicError


def graph():
    return CircuitGraph((
        CircuitComponent('R1','/r','Device:R','4k7','R',{},(CircuitPin('1','~','passive','VIN'),CircuitPin('2','~','passive','OUT'))),
        CircuitComponent('C1','/c','Device:C','100nF','C',{},(CircuitPin('1','~','passive','OUT'),CircuitPin('2','~','passive','GND'))),
        CircuitComponent('U1','/u','IC','IC','IC',{},(CircuitPin('1','VCC','power_in','OUT'),CircuitPin('2','GND','power_in','GND')))),
        {'VIN':('R1.1',),'OUT':('R1.2','C1.1','U1.1'),'GND':('C1.2','U1.2')},'root')

class ElectricalPrimitiveTests(unittest.TestCase):
    def test_units_and_tolerance_bounds(self):
        for text,unit,value in [('4k7','ohm',4700),('4.7 kΩ','ohm',4700),('100nF','F',1e-7),('3.3V','V',3.3),('20mA','A',.02)]:
            self.assertAlmostEqual(quantity(text,unit),value)
        for text,unit in [('NaN','V'),('inf','F'),('3V','A'),('1e999','V'),('foo','ohm')]:
            with self.assertRaises(ElectricalError):quantity(text,unit)
        self.assertEqual(interval(100,.1),(90,110.00000000000001))
        lo,hi=divider((4.75,5.25),interval(10000,.01),interval(20000,.01));self.assertLess(lo,3.333);self.assertGreater(hi,3.333)
        self.assertEqual(rc_time((1000,2000),(1e-6,2e-6)),(.001,.004))

    def test_connectivity_value_and_decoupling_checks_record_measured_results(self):
        ctx=CircuitTestContext(graph(),{})
        ctx.connected('R1.2','U1.1');ctx.isolated('U1.1','U1.2');ctx.rail('U1.1','OUT')
        ctx.resistor_between('R1.1','U1.1',minimum=4000,maximum=5000)
        ctx.decoupling('U1.1','U1.2',minimum=90e-9,maximum=110e-9)
        self.assertTrue(ctx.measurements);self.assertTrue(all(m['passed'] for m in ctx.measurements))

    def test_deliberate_electrical_faults_fail(self):
        for check in [lambda c:c.connected('U1.1','U1.2'),lambda c:c.within('rail',5,3,3.6,'V'),
                      lambda c:c.rail('U1.1','GND'),lambda c:c.resistor_between('R1.1','U1.1',minimum=100,maximum=200),
                      lambda c:c.decoupling('U1.1','U1.2',minimum=1e-6,maximum=10e-6),
                      lambda c:c.regulator(input_min=3,output_max=3.3,dropout_max=.3,load_max=.1,rated_current=.2)]:
            with self.subTest(check=check), self.assertRaises(AssertionError):check(CircuitTestContext(graph(),{}))

    def test_unknown_pins_and_unsourced_facts_fail(self):
        ctx=CircuitTestContext(graph(),{})
        with self.assertRaises(SchematicError):ctx.connected('U1.99','R1.1')
        with self.assertRaises(ElectricalError):ctx.fact('vdd','V')
        with self.assertRaises(ElectricalError):ctx.within('bad',math.nan,0,1,'V')
        with self.assertRaises(ElectricalError):ctx.require('true','invalid boolean')

    def test_short_supply_cannot_count_as_decoupling(self):
        ctx=CircuitTestContext(graph(),{})
        with self.assertRaises(AssertionError):ctx.decoupling('U1.1','R1.2',minimum=0,maximum=1)

class ElectricalContractTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.source=self.root/'checks.py';self.source.write_text('def test_supply(ctx):\n    ctx.rail("U1.1", "OUT")\n')
        self.contract={'circuit_tests_schema':1,'requirements':[{'id':k,'description':k,'tests':['supply']} for k in ['power','mcu','interfaces','protection','component-ratings']],
                       'tests':[{'id':'supply','callable':'checks.py:test_supply'}]}
        self.write()
    def write(self):
        (self.root/'circuit-tests.yaml').write_text(yaml.safe_dump(self.contract))

    def test_subprocess_executes_declared_test_and_records_measurements(self):
        result=run_tests(self.root,graph(),{})
        self.assertEqual(result[0]['outcome'],'pass');self.assertTrue(result[0]['measurements'])

    def test_noop_return_skip_exception_and_process_exit_all_fail(self):
        for body in ['pass','return True','raise AssertionError("fault")','import unittest; raise unittest.SkipTest("skip")','import sys; sys.exit(0)']:
            with self.subTest(body=body):
                self.source.write_text('def test_supply(ctx):\n    '+body+'\n')
                self.assertEqual(run_tests(self.root,graph(),{})[0]['outcome'],'fail')

    def test_timeout_fails(self):
        self.source.write_text('def test_supply(ctx):\n    while True: pass\n')
        result=run_tests(self.root,graph(),{},timeout=.1)
        self.assertEqual(result[0]['outcome'],'fail');self.assertIn('exceeded',result[0]['error'])

    def test_missing_coverage_and_duplicate_ids_are_rejected(self):
        self.contract['requirements'].pop();self.write()
        with self.assertRaisesRegex(ElectricalError,'missing acceptance'):read_contract(self.root)
        self.contract['tests'].append(self.contract['tests'][0]);self.write()
        with self.assertRaisesRegex(ElectricalError,'duplicate'):read_contract(self.root)

    def test_missing_callable_and_escaping_source_are_rejected(self):
        for value in ['missing.py:test_missing','../checks.py:test_supply','checks.py:arbitrary']:
            self.contract['tests'][0]['callable']=value;self.write()
            with self.assertRaises(ElectricalError):read_contract(self.root)

    def test_duplicate_yaml_keys_and_empty_tests_are_rejected(self):
        path=self.root/'circuit-tests.yaml';path.write_text('circuit_tests_schema: 1\ncircuit_tests_schema: 1\n')
        with self.assertRaises(ElectricalError):read_contract(self.root)
        self.contract['tests']=[];self.write()
        with self.assertRaises(ElectricalError):read_contract(self.root)

    def test_engineering_assessment_needs_a_source(self):
        self.contract['requirements'][0].pop('tests');self.write()
        with self.assertRaisesRegex(ElectricalError,'sourced'):read_contract(self.root)
        self.contract['requirements'][0]['assessment']={'rationale':'Scoped exclusion','sources':['datasheet section 1']};self.write();read_contract(self.root)
