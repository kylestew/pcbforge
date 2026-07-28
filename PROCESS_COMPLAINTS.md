# PCBForge process complaints

This document records user-reported failures in the PCBForge development
process. A complaint describes where the workflow made informed review harder,
even if the electrical work or automated checks were technically correct.

Complaints remain open until the workflow, playbooks, and review evidence have
been changed and exercised on a real board project.

## Complaint registry

| ID | Status | Severity | Reported | Summary |
|---|---|---|---|---|
| `PC-001` | Ready for pilot | High | 2026-07-28 | IMPLEMENT asks for topology approval without a native, auditable schematic |

## PC-001 — A topology approval needs a schematic

### Report

**Project context:** Blinky, a small CR2032-powered STM32 board with two fading
LEDs and one button.

**Workflow point:** After MCU approval and during the material-topology proposal
that precedes IMPLEMENT source edits.

**User feedback:**

> “At this point I can't fully visualize the schematic. Should I be able to?”
>
> “I am feeling the need for a schematic. For example, what are the capacitors
> for?”
>
> “I just want something approaching a schematic view so I can understand parts
> and their relationships.”
>
> “Maybe we should actually add schematic creation in KiCad to our work.”

The user had received:

- the approved functional architecture diagram;
- the selected MCU and pin table;
- the CubeMX configuration;
- prose describing the proposed physical topology;
- a signal-flow Mermaid diagram;
- exact parts, values, calculations, sourcing classes, and alternatives.

That evidence was still insufficient for the user to see the circuit as one
electrical system. The workflow was asking for approval while making the user
mentally reconstruct a conventional schematic from prose, tables, source
snippets, and prior conversations.

The follow-up build exposed a second failure. Atopile compiled the circuit, but
the Atopile VS Code views did not present a conventional schematic. The 3D,
Layout, and Pinout panels were either empty or represented different concerns;
none showed component symbols and their electrical relationships. An ad hoc
SVG could illustrate the design, but it was not a native electrical document,
could not run ERC, and had no built-in guarantee that it still matched the
Atopile circuit. Calling such an illustration “the schematic” would give the
reviewer more confidence than the artifact deserved.

### Why this is a process defect

PCBForge now requires explicit user acceptance of every phase and requires
proposal approval before material implementation choices are written. An
approval cannot be meaningfully informed if the review artifact does not show
the thing being approved in a form the reviewer can comprehend.

The current process provides:

- a block-level architecture diagram before ARCHITECT source;
- MCU configuration and pin evidence during MCU;
- exact physical parts and connectivity during IMPLEMENT;
- the first explicit “schematic presentation is adequate” gate during BRIEF,
  after IMPLEMENT and build + test have already been approved.

That timing is backwards for topology review. BRIEF correctly prevents the user
from investing in placement without an adequate circuit presentation, but it is
too late to be the first point at which the user can see the complete circuit.
By then, the user has already been asked to approve the topology, exact parts,
and compiled implementation.

Atopile not providing a schematic view does not justify omitting one. PCBForge
should create a native KiCad schematic as a tracked review derivative. KiCad
provides the familiar electrical notation the user is asking for, supports
ERC, and can export stable SVG/PDF review copies. Atopile circuit code can
remain authoritative if PCBForge also performs an explicit connectivity and
identity parity check between the compiled design and the KiCad schematic.

### Concrete example: the Blinky capacitors

The user saw several capacitors in the part list but could not tell why each one
existed. A conventional schematic with purpose annotations would have made the
roles immediately visible:

| Proposed capacitor | Connection | Purpose the review view should explain |
|---|---|---|
| 100 nF digital decoupling | MCU `VDD` to `GND` | Supplies local high-frequency switching current and keeps digital supply noise out of the longer battery path. |
| 100 nF analog decoupling | MCU `VDDA` to `GND` | Stabilizes the MCU analog supply locally, even though `VDDA` and `VDD` share the same board rail. |
| 4.7 µF bulk capacitor | Protected `+VBAT` to `GND` | Provides a local energy reservoir for slower load changes and reduces coin-cell rail movement during MCU and LED activity. |
| 100 nF reset filter | `NRST` to `GND` | Filters short reset-line disturbances and follows ST's reset-network guidance. |
| 100 nF button filter | `BUTTON/PA2` to `GND` | Works with the MCU pull-up to suppress contact bounce while preserving an immediate falling-edge wake event. |

A BOM proves that five capacitors exist. It does not communicate that two
decouple different MCU supplies, one is bulk storage, one filters reset, and
one filters the user input. Asking what a passive is for is not evidence that
the reviewer lacks required electronics knowledge; it is evidence that the
review artifact omitted design intent.

### Required process change

Add an explicit **IMPLEMENT topology-proposal gate** before physical circuit
source is edited, and carry its schematic forward as a checked implementation
artifact.

The gate should include a tracked native KiCad schematic (`.kicad_sch`) in a
clearly named review-only KiCad project. It may be a proposal artifact rather
than compiler output, but it must show the complete proposed circuit at once.
An informal SVG, Mermaid diagram, or prose connectivity table may supplement
the schematic but should not substitute for it.

Minimum contents:

1. Every proposed electrical component, reference role, and value.
2. All power rails and ground connections.
3. MCU power, reset, boot, clock, and programming connections.
4. Every peripheral and external boundary connection.
5. Net labels matching the proposed source contract.
6. Protection direction and polarized-part orientation.
7. Passive-purpose annotations, especially decoupling, filtering, pull,
   termination, current limiting, biasing, and bulk-energy roles.
8. Clear marking that the artifact is a **proposal**, not yet compiled
   authority.
9. The material alternatives and exact decisions covered by approval.
10. An artifact fingerprint binding the proposal text, schematic view, and
    applicable approved upstream artifacts.
11. A rendered SVG or PDF so review does not depend on opening KiCad.

After proposal approval, the agent writes the circuit source. Before final
IMPLEMENT approval, the review packet must then include:

1. the implemented or regenerated schematic presentation;
2. a one-to-one source-to-schematic connectivity audit;
3. the exact compiled BOM and part identities;
4. compiler, parts-policy, manufacturing-policy, and spatial-preservation
   results;
5. a visual diff or explicit list of every difference from the approved
   proposal;
6. a clean KiCad ERC report, with any intentional exceptions documented;
7. a new final-review fingerprint.

If the implementation differs materially from the approved schematic proposal,
the proposal gate becomes stale and must be repeated before final IMPLEMENT
approval.

### Artifact ownership and safety

The KiCad schematic is a review and verification derivative, not a second
electrical authority:

- Atopile source remains authoritative for compiled connectivity and parts.
- The review schematic lives under a distinct name and directory, separate
  from Atopile's generated PCB project.
- The review project does not own or update the PCB; PCBForge must never run
  **Update PCB from Schematic** from this derivative.
- References, values, physical pin numbers, net names, and fitted-part
  identities are compared against the compiled Atopile result.
- Any parity mismatch blocks IMPLEMENT approval.
- KiCad ERC and SVG/PDF export are rerun whenever the schematic changes.

This separation gives the user a real schematic without silently introducing
two competing sources of truth.

### Expected review artifacts by phase

| Phase | Human-readable electrical view |
|---|---|
| ARCHITECT proposal | Functional blocks and typed interfaces; no physical parts. |
| MCU review | KiCad schematic fragment, or an explicitly provisional equivalent, showing supplies, decoupling, reset, boot, clocks, SWD, and assigned application pins. |
| IMPLEMENT proposal | Complete native KiCad proposal schematic with values, net labels, part roles, and material choices, plus an SVG/PDF export. |
| IMPLEMENT final | Complete native KiCad implementation schematic, clean ERC, source-to-schematic parity audit, rendered export, and compiled BOM. |
| build + test | Frozen connectivity/BOM evidence and assertion results; not the first introduction to the circuit. |
| BRIEF | The already reviewed circuit alongside the placement contract; schematic adequacy remains a layout-readiness check. |

### Acceptance criteria for resolving this complaint

This complaint can close only when all of the following are true:

- `agent/implement.md` requires the pre-source schematic proposal and its
  explicit approval.
- `agent/mcu.md` requires an MCU support-circuit review view or clearly hands
  that requirement to the IMPLEMENT proposal before any support topology is
  approved.
- PCBForge defines a review-only KiCad schematic artifact and prevents it from
  being used to update the Atopile-owned PCB.
- A pinned KiCad CLI check runs schematic ERC and exports an SVG or PDF.
- A schematic parity check compares references, values, physical pins, nets,
  and fitted-part identities with the compiled Atopile design.
- `agent/operating-manual.md` and `DESIGN.md` describe schematic comprehension
  as an early approval requirement, not only a BRIEF kill switch.
- Project instructions generated by `pcbforge init` carry the same rule.
- Phase review fingerprints include the relevant schematic artifact.
- Staleness logic invalidates proposal approval when the bound schematic or
  upstream electrical contract changes.
- A pilot user can explain every major block and the purpose of each passive
  from the review packet without reading atopile source.
- The final implementation audit demonstrates that the review schematic and
  circuit source describe the same connectivity.

### Non-goals

- Do not make a hand-drawn KiCad schematic the electrical source of truth.
- Do not require the user to validate compiler pin mappings or replace machine
  checks with visual inspection.
- Do not treat an informal vector illustration as an auditable final
  schematic.
- Do not add component placement, routing, or other PCB spatial decisions to
  the schematic artifact.
- Do not require a polished publication render at proposal time; readability
  and completeness matter more than aesthetics.

### Likely affected areas

- `DESIGN.md`
- `agent/operating-manual.md`
- `agent/mcu.md`
- `agent/implement.md`
- `agent/brief.md`
- generated project `AGENTS.md`
- `pcbforge status review` artifact selection and fingerprinting
- phase staleness and migration tests
- pinned KiCad schematic ERC and export tooling
- compiled-design-to-schematic parity tooling

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-28 | Open | Complaint recorded from the Blinky IMPLEMENT proposal review. No workflow fix has been implemented yet. |
| 2026-07-28 | Open | Complaint strengthened after a successful Atopile build still provided no conventional schematic view. Native review-only KiCad schematic, ERC, rendered export, and compiled-design parity are now the proposed remedy. |
| 2026-07-28 | Ready for pilot | Schema-12 tooling and playbooks add the pre-source native KiCad proposal, pinned ERC/SVG evidence, final compiled parity, staged approvals, and migration. Resolution still requires the user to exercise the Blinky replay and confirm circuit/passive comprehension. |
