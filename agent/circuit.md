<!-- pcbforge-circuit-schema: 1 -->
# CIRCUIT playbook

Use this playbook for the CIRCUIT phase defined in
[`WORKFLOW.md`](../WORKFLOW.md), after the checked ARCHITECT baseline
transition and its MCU workstream.
The goal is an understandable, explicitly approved circuit proposal followed
by a physical circuit definition whose compiled connectivity and exact parts
are proven to match that proposal.

The project's `policy.yaml` is part of the circuit contract. JLCPCB,
STM32, 2/4 layers, SWD, pinned tools, exact part identity, official commodity
libraries, spatial ownership, and human ordering authority are hard rules.
Standard construction, 0603-or-larger ordinary R/C/LED packages, conventional
vias, and avoidance of BGA/WLCSP/sub-0.5-mm QFN are defaults with explicit
exception gates.

## Gate A — explanatory circuit proposal before source edits

`pcbforge finish-architect` captures
`review/circuit/source-baseline.json`. Do not change physical Atopile module
bodies, part definitions, values, footprints, connectivity, or the product PCB
until the topology proposal below is approved.

Create `circuit-review.yaml`:

```yaml
circuit_review_schema: 3
build: default
model: review/circuit/circuit.yaml
schematic: <project>.kicad_sch
proposal_narrative: docs/circuit-proposal.md
final_narrative: docs/circuit-review.md
```

Create the exact proposal model at
`review/circuit/circuit.yaml` using `circuit_model_schema: 1`. It contains:

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
a second design source after CIRCUIT; compiled Atopile output remains the
electrical authority.

Deliberately author the review schematic for human comprehension: generate
`<project>.kicad_sch` (beside the KiCad project, so eeschema and pcbnew
cross-probe it) from a `review/circuit/circuit_schematic.py` script
following [`circuit-kicad.md`](circuit-kicad.md) and
`pcbforge render-circuit`. Never hand-edit or save the sheet from KiCad and
never update the board from it — Atopile owns the PCB; the gates refuse a
modified sheet or a board carrying schematic links. The finished schematic
must:

1. Show external power through protection to the named rail as continuous
   wires.
2. Show all user-control, load, MCU-support, programming, and test paths at pin
   level.
3. Name nets as the board does (model `compiler_name`) so KiCad net
   highlight crosses over; the generated net register maps each to the
   model display name.
4. Group related parts — one region per model group, drawn by the tool — and
   label ambiguous functions such as MOSFETs, switches, connectors, and
   protection devices directly.
5. Draw each local support part at its device pin with a real symbol and wire.
6. Keep all readability warnings at zero before review.
7. Give every passive a visible purpose note. The generated component
   register satisfies this requirement.
8. Carry the prominent `PCBForge review-only — not PCB input` marker and the
   canonical model fingerprint in the title block and on the sheet.
9. Bind every symbol to its model component (`Reference`, `Value`,
   `Footprint`, hidden `pcbforge_group`/`pcbforge_purpose`), colour every
   model path, and pass `kicad-cli sch erc` with zero errors.
10. Export a netlist whose pin-exact endpoint sets equal the model nets; the
    schematic net name of every multi-pin net is the model compiler name.
11. Include no hierarchy, images, embedded files, or PCB spatial information;
    its symbol instances belong to the project and its root sheet is
    registered in `<project>.kicad_pro`.

`pcbforge.kicad_sch.ReviewSchematic.save()` produces items 7–11 mechanically
and runs the same validation as `check-circuit-review`. Items 1–6 are the
authored drawing itself. The script is review support, not a design source;
the model stays the approval contract.

If the sheet's model fingerprint is absent or stale, or the sheet bytes no
longer match `review/circuit/schematic.audit.json`, `check-circuit-review`
says so; re-run `pcbforge render-circuit`.

Write `docs/circuit-proposal.md`. It must contain the exact phrase
`PCBForge review-only`, identify itself as a proposal, explain every circuit
block and passive purpose, and list material alternatives and decisions.

Run:

```sh
pcbforge check-circuit-review --stage proposal --write
```

PCBForge parses the exact model, validates the schematic's model binding,
ERC, and netlist parity, refuses a board whose footprints were linked to a
schematic by KiCad, checks that no review PCB or KiCad project exists under
`review/`, proves the product topology is unchanged, and binds the packet to
the ARCHITECT handoff baseline. Then
follow the
[standard review and approval protocol](operating-manual.md#review-and-approval-protocol)
using its proposal variant for CIRCUIT. Present the schematic (or its
preview), narrative, exact model summary, and fingerprint, then stop.

Any proposal model, schematic, narrative, upstream contract, or baseline change
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

## Routing-ready net names

Before final CIRCUIT review, give every resolved PCB net a concise,
human-readable name owned by Atopile source. Record the same exact value as
`compiler_name` on every net in the approved circuit model so final parity
checks it against the compiled PCB.

Use short functional upper-snake-case names such as `VBAT`, `GND`, `SWDIO`,
`BUTTON_N`, or `LED_A_PWM`. Do not leave routable nets with compiler fallbacks
such as `hv`, `lv`, `line`, numeric-only names, or hierarchy-generated labels.
Name intentional single-pad unused nets `NC_<REF>_<PIN>`, zero-padding the pin
when useful for sorting, for example `NC_U1_02`.

Implement names with explicit Atopile net-name traits and synchronize them
through the pinned compiler. Never rename nets only in `.kicad_pcb`: circuit
source owns connectivity metadata, and a later build must reproduce the same
names. Prove a name-only synchronization preserves endpoint membership,
footprint placement/side, tracks, vias, zones, outline, graphics, and user
artwork. Update `placement.yaml` net classes to reference the resulting exact
names.

Changing the approved name map changes the CIRCUIT proposal contract even
when endpoint topology is unchanged. Repeat Gate A before changing source.

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

These shared-file edits have scoped approval effects: assurance evidence and
declared exceptions join the final CIRCUIT fingerprint without reopening SPEC
or ARCHITECT, while sourcing is excluded from CIRCUIT approval and bound by
ORDER's dedicated sourcing fingerprint. A changed assurance status/rationale
is still a SPEC baseline change.

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

Before requesting final CIRCUIT completion:

1. Implement the approved topology in Atopile and run the pinned frozen build.
2. Audit every local `src/parts` definition and every `fp-lib-table` entry.
3. Run `pcbforge check-parts`.
4. Complete `policy.yaml`, run `pcbforge check-policy`, and resolve or obtain
   explicit approval for every exception.
5. Write `docs/circuit-review.md`, marked `PCBForge review-only`. Keep
   the approved model and review schematic unchanged.
6. Run `pcbforge check-circuit-review --stage final --write`. It requires exact
   parity for references, values, footprints, MPN/LCSC identity, and every
   connected physical-pin endpoint set against the approved model and compiled
   Atopile BOM/PCB. Declared `compiler_name` values must also match; generated
   compiler names are otherwise recorded only in machine evidence.
7. If an electrical or part-identity difference exists, update the proposal
   and repeat Gate A. Cosmetic drawing layout may change without changing the
   canonical graph.
8. Write the exact `build-test.yaml` acceptance contract and add stable,
   adjacent `pcbforge-test` markers for every required assertion.
9. Run `pcbforge status --check --write`.
10. Confirm `build`, `parts`, `policy`, `ioc`, `circuit-final`, and
    `build-test` are current and passing, including the saved
    `docs/build-test.md` report.
11. Present the selected parts, values, footprints, LCSC identifiers,
   constraints, generated-asset justifications, and check results.
12. Follow the
    [standard review and approval protocol](operating-manual.md#review-and-approval-protocol)
    for the final CIRCUIT gate. Present the exact artifacts and checks, then
    stop and wait for explicit user approval.

## Deterministic acceptance contract

Create `build-test.yaml` schema 1 from the approved circuit intent, not by
blindly copying compiler output. It declares the build target, exact aggregated
LCSC/MPN/footprint/quantity BOM, total PCB footprint count, and stable IDs for
every required Atopile assertion.

Canonical `H*` mounting-hole and `TP*` test-point footprints are intentionally
unfitted PCB features. They count toward the total footprint count but never
receive invented procurement identities. Every other non-BOM footprint fails.

Put a unique adjacent marker before every required assertion:

```ato
# pcbforge-test: rail-3v3-tolerance
assert power_3v3.voltage within 3.3V +/- 5%
```

The IDs in source and `build-test.yaml` must match exactly. The pinned compiler
evaluates the assertions. PCBForge verifies exact BOM and PCB parity, resolved
connectivity, and an unchanged no-op spatial fingerprint covering placement,
copper, zones, outline, graphics, and user artwork.

Use `pcbforge check-build-test` for a non-writing diagnostic or
`pcbforge check-build-test --write-report` to save the report directly. A
failed run never overwrites the last passing report.
