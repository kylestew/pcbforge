<!-- pcbforge-implement-schema: 1 -->
# IMPLEMENT playbook

Use this playbook after MCU is complete and before marking IMPLEMENT complete.
The goal is a physical circuit definition with exact sourcing and no redundant
project-local KiCad libraries.

The schema-10 `policy.yaml` is part of the implementation contract. JLCPCB,
STM32, 2/4 layers, SWD, pinned tools, exact part identity, official commodity
libraries, spatial ownership, and human ordering authority are hard rules.
Standard construction, 0603-or-larger ordinary R/C/LED packages, conventional
vias, and avoidance of BGA/WLCSP/sub-0.5-mm QFN are defaults with explicit
exception gates.

## Library precedence

For every physical part, decide in this order:

1. Use the compiler's standard primitive and an official KiCad symbol and
   footprint when the electrical behavior, pin mapping, and verified package
   match.
2. Keep the exact manufacturer part number and LCSC selection in supplier/BOM
   metadata. Exact sourcing does not require a unique KiCad symbol or footprint.
3. Generate a project-local symbol or footprint only when the exact required
   pin mapping or mechanical package is missing from the official libraries.
4. For a generated asset, record why the official library is insufficient and
   verify pad count, pitch, body, courtyard, polarity/pin 1, and exposed-pad
   geometry against the datasheet.

Standard two-terminal chip resistors in 01005, 0201, 0402, 0603, 0805, 1206,
1210, 1812, 2010, and 2512 packages must use `Device:R` and the matching
`Resistor_SMD` footprint. Standard capacitors through 1812 and standard chip
LEDs in the recognized official package set must likewise use `Device:C` /
`Capacitor_SMD` or `Device:LED` / `LED_SMD`. Do not run `easyeda2kicad` for
these parts.

Examples for 0603:

```text
Device:R   + Resistor_SMD:R_0603_1608Metric
Device:C   + Capacitor_SMD:C_0603_1608Metric
Device:LED + LED_SMD:LED_0603_1608Metric
```

Use an atopile `Resistor`, `Capacitor`, or `LED` primitive, constrain its value
and package, and retain the selected MPN/LCSC identity as part-selection
metadata. A small source wrapper for shared constraints is acceptable; local
`.kicad_sym`, `.kicad_mod`, `.step`, or `.wrl` assets are not.

## Required audit

Run this repeatedly while selecting parts:

```sh
pcbforge check-parts
```

The check scans project-local atomic parts and fails when a recognized
commodity resistor, capacitor, or LED references local KiCad assets. Each
failure reports the canonical official symbol and footprint to use.

For every selected LCSC item, add a `policy.yaml` sourcing entry with JLC
class, assembly availability, lifecycle, check date, and optional second
source. Complete each protection/testability assurance with evidence, a
reasoned `not-applicable` disposition, or an `exception` disposition backed by
one declared exception. Normal checks are offline; do the live research before
recording the evidence.

Run:

```sh
pcbforge check-policy
```

If an approval-required violation is intentional, present the alternatives,
recommendation, scope, manufacturing/cost risk, and exception diff. Stop. Only
after the user explicitly approves it may you run:

```sh
pcbforge policy approve-exception <exception-id> --note "<approved tradeoff>"
```

Before requesting IMPLEMENT completion:

1. Audit every local `src/parts` definition and every `fp-lib-table` entry.
2. Run `pcbforge check-parts`.
3. Complete `policy.yaml`, run `pcbforge check-policy`, and resolve or obtain
   explicit approval for every exception.
4. Run `pcbforge status --check --write`.
5. Confirm `build`, `parts`, and `policy` are current and passing.
6. Present the selected parts, values, footprints, LCSC identifiers,
   constraints, generated-asset justifications, and check results.
7. Mark IMPLEMENT complete only after the dashboard accepts its required
   evidence.
