import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from pcbforge.circuit import check_mcu
from pcbforge.schematic import CircuitGraph, CircuitComponent, CircuitPin, SchematicError


class McuIocSemanticsTests(unittest.TestCase):
    def fixture(self, *, name='PA10', signal='GPIO_Analog', label='', net='', nc=True):
        pin=CircuitPin('32','PA10/UCPD1_DBCC2','bidirectional','unconnected-(U1-Pad32)' if nc else net,nc)
        part=CircuitComponent('U1','/u','MCU','MCU','QFP',{'MPN':'STM32G0B1CCT6'},(pin,))
        graph=CircuitGraph((part,),{pin.net:('U1.32',)},'root')
        facts={'mcu':{'reference':'U1','assignments':{name:{'pin':'32','signal':signal,'net':net}}}}
        checked=SimpleNamespace(part_number='STM32G0B1CCT6',pins=[SimpleNamespace(pin=name,signal=signal,label=label)])
        return graph,facts,checked

    def check(self, graph, facts, checked):
        with mock.patch('pcbforge.ioc.check_ioc',return_value=checked):check_mcu(graph,facts,Path('/tmp'))

    def test_accepts_explicitly_unused_analog_gpio(self):
        self.check(*self.fixture())

    def test_rejects_unconnected_active_or_labeled_signals(self):
        for changes in ({'signal':'GPIO_Output'},{'signal':'SYS_SWDIO'}, {'signal':'ADC1_IN0'}, {'label':'EXTERNAL_ANALOG'}, {'net':'EXPECTED_NET'}):
            with self.subTest(changes=changes),self.assertRaises(SchematicError):self.check(*self.fixture(**changes))

    def test_unused_pin_cannot_share_its_net(self):
        graph,facts,checked=self.fixture()
        graph=dataclasses.replace(graph,nets={graph.components[0].pins[0].net:('U1.32','J1.1')})
        with self.assertRaises(SchematicError):self.check(graph,facts,checked)

    def test_connected_analog_gpio_needs_exact_net(self):
        self.check(*self.fixture(net='ANALOG',nc=False))
        graph,facts,checked=self.fixture(net='ANALOG',nc=False)
        facts['mcu']['assignments']['PA10']['net']='WRONG'
        with self.assertRaises(SchematicError):self.check(graph,facts,checked)

    def test_accepts_cubemx_bracketed_alias_but_not_another_pad(self):
        graph,facts,checked=self.fixture(name='PA12 [PA10]',signal='USB_DP',label='USB_DP',net='USB_DP',nc=False)
        pin=dataclasses.replace(graph.components[0].pins[0],name='PA10/PA12')
        graph=dataclasses.replace(graph,components=(dataclasses.replace(graph.components[0],pins=(pin,)),))
        self.check(graph,facts,checked)
        pin=dataclasses.replace(pin,name='PA10')
        graph=dataclasses.replace(graph,components=(dataclasses.replace(graph.components[0],pins=(pin,)),))
        with self.assertRaises(SchematicError):self.check(graph,facts,checked)

    def test_unused_gpio_still_requires_matching_pin_and_signal(self):
        for value in ('GPIO_Input','ADC1_IN0'):
            graph,facts,checked=self.fixture()
            facts['mcu']['assignments']['PA10']['signal']=value
            with self.assertRaises(SchematicError):self.check(graph,facts,checked)
