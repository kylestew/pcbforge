<!-- pcbforge-electrical-tests-schema: 2 -->
# Independent electrical acceptance

Follow the standard review and approval protocol in [the operating manual](operating-manual.md).

The saved KiCad schematic is the implementation. Python tests express requirements independently of it.
Do not generate tests from a list of the schematic's current connections. Do not maintain a second complete circuit model.

Declare tests in `circuit-tests.yaml`:

```yaml
circuit_tests_schema: 1
requirements:
  - id: power
    description: The MCU uses the regulated 3.3 V supply.
    tests: [mcu-supply]
  # Also cover mcu, interfaces, protection and component-ratings.
tests:
  - id: mcu-supply
    callable: circuit_tests.py:test_mcu_supply
```

```python
def test_mcu_supply(ctx):
    ctx.rail("U1.5", "+3V3")
    ctx.connected("U1.5", "C3.1")
    ctx.isolated("U1.5", "U1.3")
    ctx.decoupling("U1.5", "U1.3", minimum=90e-9, maximum=110e-9)
```

Use the actual package pin numbers from the reviewed datasheet. The example numbers do not identify a particular MCU.
A test must run context checks and return `None`. Exceptions, skips, timeouts, empty tests and unexpected return values fail.
The report records each test ID, measured result and limits.

Use `ctx.value(ref, unit)` for a schematic value and `ctx.fact(name, unit)` for a sourced limit.
Supported units include V, A, ohm, F, H, Hz, s and W. `interval`, `divider` and `rc_time` support worst-case calculations.
Check regulator headroom, load margin, resistor power, capacitor voltage margin and interface loading where applicable.
Keep numerical assumptions and sources in `electrical-facts.yaml`, not hidden in an unexplained test constant.

The required categories are power, mcu, interfaces, protection and component-ratings.
A requirement may use a sourced engineering assessment when a scripted test is unsuitable.
Every listed test must trace to a requirement. Review test coverage separately from the drawing.

Parts facts map reference to exact MPN, source, footprint and numbered pin functions.
MCU facts map physical IOC pin names to schematic pin numbers, signals and nets.
Do not treat ERC or matching pin counts as proof of correct pin functions.

All project Python files bind the acceptance fingerprint. List extra data files under optional `inputs:` in `circuit-tests.yaml`.
Keep inputs inside the project. Changes to these files require a fresh check and review.
