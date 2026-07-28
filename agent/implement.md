<!-- pcbforge-implement-schema: 3 -->
# IMPLEMENT playbook

Use this playbook after MCU is complete and before requesting IMPLEMENT
approval.
The goal is an understandable, explicitly approved circuit proposal followed
by a physical circuit definition whose compiled connectivity and exact parts
are proven to match that proposal.

The schema-13 workflow's `policy.yaml` is part of the implementation contract. JLCPCB,
STM32, 2/4 layers, SWD, pinned tools, exact part identity, official commodity
libraries, spatial ownership, and human ordering authority are hard rules.
Standard construction, 0603-or-larger ordinary R/C/LED packages, conventional
vias, and avoidance of BGA/WLCSP/sub-0.5-mm QFN are defaults with explicit
exception gates.

## Gate A — explanatory circuit proposal before source edits

MCU approval captures `review/implement/source-baseline.json`. Do not change
physical Atopile module bodies, part definitions, values, footprints,
connectivity, or the product PCB until the topology proposal below is approved.

Create `circuit-review.yaml`:

```yaml
circuit_review_schema: 1
build: default
model: review/implement/circuit.yaml
diagram: review/implement/circuit.svg
proposal_narrative: docs/implementation-proposal.md
final_narrative: docs/implementation-review.md
```

Do **not** generate a KiCad schematic. Create the exact proposal model at
`review/implement/circuit.yaml` using `circuit_model_schema: 1`. It contains:

1. `components`: every proposed reference with `kind`, value, official
   footprint, `mpn`, `lcsc`, and a plain-language `purpose`;
2. `nets`: a stable kebab-case `id`, human `display_name`, optional exact
   `compiler_name`, and every connected physical `REF.PIN` endpoint;
3. `groups`: complete one-to-one component grouping with title and purpose;
4. `paths`: important power/current/signal flows as ordered endpoints, where
   adjacent endpoints either share a net or cross the same component.

Example shape:

```yaml
circuit_model_schema: 1
components:
  - reference: F1
    kind: fuse
    value: 10mA hold
    footprint: Fuse:Fuse_0603_1608Metric
    mpn: SMD0603-001-60
    lcsc: C46640946
    purpose: Limits abnormal battery current.
nets:
  - id: battery-positive
    display_name: BAT+
    nodes: [BT1.1, F1.1]
  - id: fused-battery
    display_name: FUSED_BAT
    nodes: [F1.2, Q1.1]
groups:
  - id: power-entry
    title: Power input and protection
    purpose: Defines the protected board supply.
    references: [BT1, F1, Q1]
paths:
  - id: protected-power
    title: Battery to protected rail
    purpose: Shows current through the fuse and reverse-polarity MOSFET.
    nodes: [BT1.1, F1.1, F1.2, Q1.1, Q1.2]
```

The complete file must define every referenced component and every connected
endpoint; the shortened example only illustrates the field shapes.

The model is the frozen pre-source approval contract. It is not maintained as
a second design source after IMPLEMENT; compiled Atopile output remains the
electrical authority.

Deliberately author `review/implement/circuit.svg` for human comprehension. It
must open directly in a browser and:

1. show external power through protection to the named rail as continuous
   wires, and show complete user-control, LED/load, MCU-support, programming,
   and test paths;
2. use logical names rather than compiler-generated labels in the drawing;
3. group related parts and label ambiguous functions such as MOSFETs, switches,
   connectors, and protection devices directly;
4. place every passive beside a visible purpose note;
5. include an accessible `<title>` and `<desc>` plus a prominent
   `PCBForge review-only — not PCB input` marker;
6. put the canonical model fingerprint on the root as
   `data-pcbforge-model-sha256`;
7. tag visible explanatory elements with `data-component-ref`, `data-net-id`,
   `data-group-id`, `data-path-id`, and `data-purpose-for` for complete model
   coverage; every tagged path contains a visible SVG wire shape; and
8. contain no scripts, external images/resources, or PCB spatial information.

If the root model fingerprint is absent or stale, `check-circuit-review`
reports the exact expected SHA-256 to place on the SVG before review.

Write `docs/implementation-proposal.md`. It must contain the exact phrase
`PCBForge review-only`, identify itself as a proposal, explain every circuit
block and passive purpose, and list material alternatives and decisions.

Run:

```sh
pcbforge check-circuit-review --stage proposal --write
pcbforge status review implement --stage proposal
```

PCBForge parses the exact model, validates the SVG's semantic coverage and
model binding, checks that no review PCB exists, proves the product topology
is unchanged, and binds the packet to the MCU handoff baseline. Present the
SVG, narrative, exact model summary, and fingerprint. Stop. Only after explicit
user approval may you run:

```sh
pcbforge status approve implement --stage proposal \
  --fingerprint <sha256> --note "<approved topology and material choices>"
```

Any proposal model, SVG, narrative, upstream contract, or baseline change
stales this approval. Physical source changes made before approval are a
blocker; return to the captured baseline instead of recapturing it.

## Gate B — implement and prove parity

Only after the current proposal approval may you write physical Atopile source,
select/finalize parts, or update compiler-owned PCB identity/connectivity.

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

Before requesting final IMPLEMENT completion:

1. Implement the approved topology in Atopile and run the pinned frozen build.
2. Audit every local `src/parts` definition and every `fp-lib-table` entry.
3. Run `pcbforge check-parts`.
4. Complete `policy.yaml`, run `pcbforge check-policy`, and resolve or obtain
   explicit approval for every exception.
5. Write `docs/implementation-review.md`, marked `PCBForge review-only`. Keep
   the approved model and explanatory SVG unchanged.
6. Run `pcbforge check-circuit-review --stage final --write`. It requires exact
   parity for references, values, footprints, MPN/LCSC identity, and every
   connected physical-pin endpoint set against the approved model and compiled
   Atopile BOM/PCB. Declared `compiler_name` values must also match; generated
   compiler names are otherwise recorded only in machine evidence.
7. If an electrical or part-identity difference exists, update the proposal
   and repeat Gate A. Cosmetic drawing layout may change without changing the
   canonical graph.
8. Run `pcbforge status --check --write`.
9. Confirm `build`, `parts`, `policy`, and `circuit-final` are current and
   passing.
10. Present the selected parts, values, footprints, LCSC identifiers,
   constraints, generated-asset justifications, and check results.
11. Run `pcbforge status review implement`, present the exact artifacts, checks,
   and fingerprint, and stop.
12. Only after explicit user approval, run
   `pcbforge status approve implement --fingerprint <sha256> --note "<approval>"`.
