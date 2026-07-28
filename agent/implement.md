<!-- pcbforge-implement-schema: 2 -->
# IMPLEMENT playbook

Use this playbook after MCU is complete and before requesting IMPLEMENT
approval.
The goal is an understandable, explicitly approved circuit proposal followed
by a physical circuit definition whose compiled connectivity and exact parts
are proven to match that proposal.

The schema-12 workflow's `policy.yaml` is part of the implementation contract. JLCPCB,
STM32, 2/4 layers, SWD, pinned tools, exact part identity, official commodity
libraries, spatial ownership, and human ordering authority are hard rules.
Standard construction, 0603-or-larger ordinary R/C/LED packages, conventional
vias, and avoidance of BGA/WLCSP/sub-0.5-mm QFN are defaults with explicit
exception gates.

## Gate A — native topology proposal before source edits

MCU approval captures `review/implement/source-baseline.json`. Do not change
physical Atopile module bodies, part definitions, values, footprints,
connectivity, or the product PCB until the topology proposal below is approved.

Create `schematic-review.yaml`:

```yaml
schematic_review_schema: 1
build: default
proposal_root: review/implement/proposal/main.kicad_sch
final_root: review/implement/final/main.kicad_sch
proposal_narrative: docs/implementation-proposal.md
final_narrative: docs/implementation-review.md
```

Create a distinct, review-only KiCad project under
`review/implement/proposal/`. It may use hierarchical sheets, but
`main.kicad_sch` and its rendered pages must let the reviewer follow the
complete circuit. Do not create a `.kicad_pcb` in this directory. Never run
Update PCB from Schematic from this derivative; Atopile remains authoritative.

The proposal must show:

1. every electrical component with stable proposed reference, value,
   footprint, `MPN`, and `LCSC` fields;
2. every rail, ground, external boundary, MCU support connection, programming
   connection, polarized direction, and protection direction;
3. physical pin numbers and net labels matching the proposed source contract;
4. visible purpose notes for decoupling, bulk storage, filtering, pull
   resistors, current limiting, biasing, and termination;
5. a prominent `PCBForge review-only — proposal` marker.

Write `docs/implementation-proposal.md`. It must contain the exact phrase
`PCBForge review-only`, identify itself as a proposal, explain every circuit
block and passive purpose, and list material alternatives and decisions.

Run:

```sh
pcbforge check-schematic --stage proposal --write
pcbforge status review implement --stage proposal
```

PCBForge runs pinned KiCad 9 ERC, exports tracked SVG pages, checks that no
review PCB exists, proves the product PCB is byte-identical, and binds the
proposal to the MCU handoff baseline. Present the native schematic, SVG pages,
narrative, ERC result, and exact fingerprint. Stop. Only after explicit user
approval may you run:

```sh
pcbforge status approve implement --stage proposal \
  --fingerprint <sha256> --note "<approved topology and material choices>"
```

Any proposal schematic, narrative, upstream contract, or baseline change
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
5. Create `review/implement/final/main.kicad_sch` and
   `docs/implementation-review.md`, both marked `PCBForge review-only`.
6. Run `pcbforge check-schematic --stage final --write`. It requires clean ERC
   and exact parity for references, values, footprints, MPN/LCSC identity, and
   every connected physical pin/net against both the approved proposal and the
   compiled Atopile BOM/PCB.
7. If an electrical or part-identity difference exists, update the proposal
   and repeat Gate A. Cosmetic drawing layout may change without changing the
   canonical graph.
8. Run `pcbforge status --check --write`.
9. Confirm `build`, `parts`, `policy`, and `schematic-final` are current and
   passing.
10. Present the selected parts, values, footprints, LCSC identifiers,
   constraints, generated-asset justifications, and check results.
11. Run `pcbforge status review implement`, present the exact artifacts, checks,
   and fingerprint, and stop.
12. Only after explicit user approval, run
   `pcbforge status approve implement --fingerprint <sha256> --note "<approval>"`.
