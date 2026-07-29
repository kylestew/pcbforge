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
| `PC-002` | Ready for pilot | High | 2026-07-28 | A generated KiCad schematic is the wrong human-review artifact |
| `PC-003` | Ready for merge | High | 2026-07-28 | Build + Test rejects intentional unfitted PCB features as rogue BOM parts |
| `PC-004` | Ready for merge | High | 2026-07-28 | Required Step-6 assertions invalidate approved MCU and IMPLEMENT gates |
| `PC-005` | Ready for pilot | Medium | 2026-07-28 | IMPLEMENT and Build + Test create two approvals for one unfinished circuit |
| `PC-006` | Ready for pilot | High | 2026-07-29 | Volatile compiler run IDs make approval fingerprints change after every no-op build |
| `PC-007` | Ready for pilot | High | 2026-07-29 | An incompatible dirty tool checkout can rewrite a pinned project's workflow state |

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
| 2026-07-28 | Ready for pilot | The Blinky replay showed that schema 12's native-KiCad remedy was not readable enough. PC-002 supersedes that remedy while retaining PC-001's requirement for circuit comprehension before implementation. |

## PC-002 — Do not generate a KiCad schematic for human review

### Report

**Project context:** Blinky schema-12 replay, during the IMPLEMENT proposal
pilot created to exercise the PC-001 remedy.

**Workflow point:** The proposal had a complete generated native KiCad
schematic, an exported SVG, exact part identities, a canonical net graph, and
a clean pinned KiCad ERC result. The gate reported 25 components, 23 nets, 57
connected pins, and zero ERC violations.

**User feedback:**

> “The Kicad schematic is pretty hard to follow. Would it have been better to
> create a simplified SVG?”
>
> “For example, its unclear how the battery connects to fuse then to reverse
> polarity and then defines the power net. Why is there a button on the reverse
> polarity, etc”
>
> “Yes! This is exactly what I need.”
>
> “Make sure you highlight that we don't want to attempt a kicad schematic.”

The first two quotes describe the generated KiCad schematic and its direct SVG
export. The third quote followed review of a purpose-built simplified SVG that
showed one continuous path:

```text
BT1 positive → F1 → Q1 drain → Q1 source → +VBAT
BT1 negative → GND
Q1 gate → GND
```

The same SVG placed the only pushbutton in a separate PA2 wake circuit, called
out that Q1 is a transistor rather than a button, showed both complete LED
paths, and used human net names instead of compiler-generated labels. The user
could understand it immediately.

### Why this is a process defect

PC-001 correctly identified that prose, tables, and source are insufficient
for topology approval. Its proposed remedy was wrong: a synthetic native KiCad
schematic optimizes for file format validity and ERC, not for explanation.

The pilot's KiCad file was electrically complete and machine-checkable, but
the generated drawing:

- represented many connections as isolated labeled stubs instead of visible
  continuous paths;
- exposed compiler-oriented net names that obscured familiar rails and
  signals;
- scattered related parts across a large page;
- mixed exact identity evidence, implementation labels, and explanatory text
  in one visual layer;
- used symbols without enough visual context for the reviewer to distinguish
  circuit function confidently; and
- made a clean ERC result look like evidence of readability when it was only
  evidence of KiCad electrical-rule consistency.

Creating and maintaining generated KiCad geometry is also expensive and
fragile. Considerable agent effort went into symbol-library compatibility,
property placement, label direction, intentional singleton nets, ERC
suppression, and rendering behavior. None of that effort improved the
authoritative Atopile circuit, and it still produced a worse human-review
artifact than a direct explanatory SVG.

A valid native schematic is not automatically a readable schematic. PCBForge
must not treat “KiCad can parse and ERC this” as a proxy for “the user can
understand and approve this.”

### Required process change

For circuit-as-code projects, **do not generate or require a synthetic KiCad
schematic as the IMPLEMENT human-review artifact**.

Replace the schema-12 native-schematic proposal with two deliberately separate
artifacts:

1. A purpose-built, schematic-like SVG optimized for human comprehension.
2. A structured electrical review contract optimized for machine comparison.

The SVG should:

- show power flow as continuous wires from each external source through
  protection to the named rail it defines;
- use logical names such as `+VBAT`, `GND`, `LED_A_PWM`, and `BUTTON`, keeping
  compiler-generated net names out of the primary drawing;
- group related components into clear functional regions;
- show complete current or signal paths instead of requiring label matching;
- distinguish transistors, switches, connectors, and other potentially
  ambiguous functions with direct labels and short purpose annotations;
- place each passive beside the connection that explains its role;
- include reference designators and values needed for informed review;
- identify external boundaries, programming access, and test access;
- state explicitly that it is explanatory, review-only, and owns no PCB
  spatial data; and
- open directly in a browser without requiring KiCad.

The structured review contract should carry the exact references, MPNs,
supplier identities, footprints, physical pins, and proposed connectivity. It
must be fingerprinted with the explanatory SVG, narrative, and approved
upstream artifacts. After proposal approval, PCBForge should compare this
contract directly with the compiled Atopile BOM, net graph, and PCB topology.

The SVG and structured contract should be generated from, or validated
against, one common proposal model so the explanatory view cannot silently
diverge from the exact approval contract.

### KiCad policy

PCBForge should not attempt to synthesize a native KiCad schematic merely to
satisfy a review gate.

If a project already has a human-authored native schematic with established
ownership, PCBForge may present it as additional evidence. That is not a reason
to require one for Atopile projects, create a parallel electrical authority,
or make generated KiCad schematic geometry part of the critical workflow.

The product `.kicad_pcb` remains the spatial authority and must never be
updated from a review derivative.

### Acceptance criteria for resolving this complaint

This complaint can close only when all of the following are true:

- `agent/implement.md` requires a browser-readable schematic-like SVG before
  proposal approval and does not require PCBForge to create a KiCad schematic.
- Generated project `AGENTS.md` instructions carry the same rule.
- The proposal review contract stores exact component identity, physical pins,
  and connectivity without depending on `.kicad_sch`.
- `pcbforge status review implement --stage proposal` fingerprints the SVG,
  narrative, structured contract, source baseline, and approved upstream
  artifacts.
- Final IMPLEMENT checks compare the approved structured contract directly
  with compiled Atopile BOM, connectivity, and PCB topology.
- The workflow does not require generated KiCad ERC or schematic-render parity
  for an Atopile project.
- Human-facing SVGs use continuous paths and logical net names while preserving
  exact compiler names in machine evidence.
- Every protection device and user control is visually unambiguous.
- Every passive's purpose can be understood from its placement and annotation.
- A pilot user can explain the battery-to-rail path, LED current paths, button
  wake path, MCU support, and service boundaries from the SVG alone.
- The final packet retains machine proof that the approved proposal and
  implemented circuit are electrically identical.

### Non-goals

- Do not make the SVG the electrical source of truth.
- Do not replace exact connectivity, BOM, pin, policy, or PCB-topology checks
  with visual inspection.
- Do not put placement, routing, layers, or copper geometry into the
  explanatory SVG.
- Do not ban presentation of a native schematic that already exists and has a
  clear project owner.
- Do not preserve synthetic KiCad generation merely because schema-12 already
  implemented it.

### Likely affected areas

- `pcbforge/circuit_review.py`
- `pcbforge/status.py`
- `pcbforge/initialize.py`
- `agent/implement.md`
- `agent/brief.md`
- generated project `AGENTS.md`
- `README.md`, `WORKFLOW.md`, and `DESIGN.md`
- schema migration and review/status tests

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-28 | Open | Complaint recorded from the Blinky schema-12 proposal review. The purpose-built overview SVG demonstrated the preferred human artifact. |
| 2026-07-28 | Ready for pilot | Schema 13 replaces generated KiCad review with an intentionally authored SVG bound to a strict pre-source circuit model. Proposal checks validate semantic SVG coverage; final checks compare the frozen model directly with compiled Atopile BOM identity and PCB endpoint topology. Schema-12 projects have an explicit, non-destructive migration. Resolution still requires a real project pilot confirming comprehension. |

## PC-003 — Build + Test must distinguish fitted BOM parts from PCB features

### Report

**Project context:** Blinky schema-13 replay, during the first Build + Test run
after final IMPLEMENT approval.

**Workflow point:** `build-test.yaml` correctly declared the ten exact fitted
BOM lines and the board's total of 25 physical footprints. The board also
contained two required M3 mounting holes and eight required rear service pads,
all already represented in the approved circuit model as intentionally
unfitted PCB features with `LCSC: N/A`.

The frozen build compiled successfully and all 13 source assertions executed,
but the gate failed with:

```text
PCB has non-BOM references: H1, H2, TP1, TP2, TP3, TP4, TP5, TP6, TP7, TP8
```

The checker required every PCB footprint reference to appear in the fitted
compiler BOM. The contract could not express an unfitted feature because its
strict BOM schema accepts only real `C...` LCSC identifiers.

### Why this is a process defect

PCBForge requires mounting holes and test access when applicable, and the
IMPLEMENT review model explicitly distinguishes them from fitted assembly
parts. Build + Test then discarded that distinction and treated the same
approved features as unexpected components.

This creates three bad choices:

- remove required test and mounting features from the PCB;
- invent supplier identities for non-purchased PCB geometry; or
- weaken exact BOM parity by treating every physical footprint as a fitted
  component.

All three corrupt the design contract. A physical footprint count and a fitted
BOM answer different questions and must remain independently exact.

The bug also made the documented Build + Test gate impossible for any board
that uses canonical test pads, mounting holes, fiducials, or similar PCB-only
features unless the checker has an explicit representation for them.

### Required process change

Build + Test must validate two disjoint physical sets:

1. **Fitted BOM components:** exact LCSC, MPN, footprint, quantity, and
   designator parity with compiler output and the PCB.
2. **Intentionally unfitted PCB features:** exact allowed references and
   canonical feature footprints, included in `board_footprints` but excluded
   from the procurement BOM.

The immediate schema-1 repair is deliberately narrow:

- `H[1-9][0-9]*` is allowed only with a `MountingHole:` or
  `MountingHole.pretty:` footprint;
- `TP[1-9][0-9]*` is allowed only with a `TestPoint:` or
  `TestPoint.pretty:` footprint;
- every other non-BOM reference still fails; and
- all allowed features still count toward the exact board footprint total.

A future contract schema may list unfitted features explicitly or import them
from the approved circuit model. It must not broadly waive non-BOM parity.

### Acceptance criteria for resolving this complaint

- A board with exact fitted BOM parity and canonical `H*`/`TP*` PCB features
  passes Build + Test.
- The same `H*` or `TP*` reference attached to an unrelated connector or
  component footprint fails.
- Missing, duplicate, unexpected, or footprint-mismatched fitted BOM
  designators still fail.
- `board_footprints` counts both fitted components and intentional PCB
  features.
- `docs/build-test.md` reports fitted BOM evidence separately from unfitted
  mounting-hole/test-point evidence.
- The playbook explains the fitted/unfitted distinction and does not instruct
  agents to invent LCSC identities.
- Regression tests cover both the accepted canonical feature case and the
  disguised unrelated-footprint case.
- The real Blinky gate passes with ten BOM lines, 15 fitted components, ten
  unfitted PCB features, and 25 total footprints.

### Non-goals

- Do not permit arbitrary non-BOM footprints.
- Do not put mounting holes or bare test pads into the procurement BOM.
- Do not infer that a reference is safe from its prefix alone.
- Do not weaken exact BOM identity, quantity, designator, footprint, or PCB
  connectivity checks.

### Likely affected areas

- `pcbforge/build_test.py`
- `tests/test_build_test.py`
- `agent/build-test.md`
- future `build-test.yaml` schema evolution
- circuit-review/build-test feature-parity integration

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-28 | Open | Blinky reproduced the defect after a successful frozen build: H1, H2, and TP1–TP8 were rejected as non-BOM references despite approved IMPLEMENT evidence. |
| 2026-07-28 | Ready for merge | The checker now admits only canonical mounting-hole and test-point footprints, reports them separately, and retains exact fitted-BOM validation. Seventeen Build + Test regression tests pass, including a negative disguised-footprint case. |
| 2026-07-28 | Ready for merge | The repaired real Blinky gate passed with 10 BOM lines, 25 footprints, 23 nets, 59 pad-to-net assignments, 13 assertions, and unchanged spatial data. |

## PC-004 — Step-6 assertions must not reopen approved circuit phases

### Report

**Project context:** Blinky schema-13 replay, immediately after IMPLEMENT
fingerprint `d4f4c2dc…53be5` was explicitly approved.

**Workflow point:** The Build + Test playbook requires agents to create
`build-test.yaml`, then add a stable `pcbforge-test` marker immediately before
every required Atopile assertion. Blinky added 13 such marker/assert pairs for
LED current limiting, BOOT0 bias, capacitance, and capacitor voltage headroom.

The assertions compiled and the standalone Build + Test diagnostic passed.
However, the required full command:

```text
pcbforge status --check --write
```

automatically changed the dashboard from five completed phases to three. It
reopened MCU and IMPLEMENT and failed the IMPLEMENT proposal check with:

```text
Approval invalidated automatically because the approved artifact fingerprint changed
physical source or board topology changed before proposal approval
```

No component, pin, footprint, net, or board topology had changed. Raw file
hashes were reacting to the Step-6 test declarations that the workflow itself
had instructed the agent to add.

### Why this is a process defect

The documented order was self-contradictory:

1. final IMPLEMENT approval must complete before Build + Test begins;
2. Build + Test must add executable assertions to physical source; and
3. any source-byte change invalidates MCU, the IMPLEMENT proposal baseline,
   final circuit evidence, and IMPLEMENT approval.

Following Step 6 exactly therefore destroyed the approvals required to enter
Step 6. The only apparent recovery was to ask the user to reapprove unchanged
MCU and circuit artifacts, which would make fingerprint-bound approval noisy
and untrustworthy.

This was not one isolated hash. Raw source bytes appeared in:

- the MCU approval fingerprint;
- final IMPLEMENT source semantics;
- the pre-IMPLEMENT proposal baseline;
- circuit-review check fingerprints;
- final circuit-review evidence; and
- automatic staleness evaluation.

Repairing only one location would leave the workflow unstable.

### Required process change

Earlier electrical approvals must use source semantics that exclude only a
valid Step-6 traceability pair:

```ato
# pcbforge-test: stable-kebab-case-id
assert deterministic_expression
```

The marker and assertion must be adjacent. The normalization must preserve
every other source byte. In particular:

- an unmarked assertion remains approval-significant;
- a malformed marker remains approval-significant;
- a marker not immediately followed by `assert` remains
  approval-significant;
- component creation, assignments, connections, pin mappings, imports, and
  all other real source changes remain approval-significant; and
- the Build + Test fingerprint itself continues to bind the raw source,
  contract, and generated report, so changing a test invalidates Step 6.

The same normalization must be reused by MCU/IMPLEMENT approval hashing,
proposal-baseline comparison, circuit-review fingerprints, and final evidence
generation. Separate near-duplicate filters are too likely to drift.

### Acceptance criteria for resolving this complaint

- Adding only valid adjacent `pcbforge-test` marker/assert pairs does not change
  current ARCHITECT, MCU, IMPLEMENT proposal, or final IMPLEMENT approval
  fingerprints.
- It does make Build + Test evidence stale until the compiler and report are
  rerun.
- Unmarked, malformed, or non-adjacent assertions remain visible to earlier
  approval fingerprints.
- Any real source statement added beside the tests changes the applicable MCU
  or IMPLEMENT fingerprint.
- Proposal source-baseline checks remain current after Step-6 assertions but
  still fail for real physical source changes.
- Final circuit-review evidence remains byte-stable for assertion-only changes.
- Automatic status refresh does not append erroneous MCU or IMPLEMENT reopen
  events.
- The real Blinky project reproduces the exact approved MCU fingerprint
  `efdb10cc…c879` and IMPLEMENT fingerprint `d4f4c2dc…53be5` after adding the
  assertions.
- The full gate returns Blinky to five completed phases with Build + Test
  `Awaiting approval`.
- Regression tests cover semantic stripping, baseline stability, circuit
  evidence stability, earlier approval stability, and real-source-change
  invalidation.

### Non-goals

- Do not ignore all comments or all assertions.
- Do not weaken exact compiled BOM, connectivity, IOC, parts, policy, or board
  topology checks.
- Do not automatically revive a phase that was genuinely reopened.
- Do not treat test-only semantic normalization as user approval of Step 6.

### Likely affected areas

- `pcbforge/build_test.py`
- `pcbforge/schematic.py`
- `pcbforge/circuit_review.py`
- `pcbforge/status.py`
- `tests/test_build_test.py`
- `tests/test_schematic.py`
- `tests/test_circuit_review.py`
- `tests/test_status.py`

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-28 | Open | Blinky reproduced the contradiction: 13 required passing assertions reopened MCU and IMPLEMENT and made the approved proposal baseline fail. |
| 2026-07-28 | Ready for merge | A shared semantic-source helper now removes only valid adjacent Step-6 marker/assert pairs from earlier approval and review hashes. Real source changes remain significant. |
| 2026-07-28 | Ready for merge | Sixty-nine focused Build Test, schematic, circuit-review, and status regression tests pass. Blinky reproduced the original MCU and IMPLEMENT fingerprints and the repaired full gate reached Build + Test Awaiting approval without reopening prior phases. |
| 2026-07-28 | Ready for merge | Schema 14 keeps assertions inside CIRCUIT, after proposal approval but before its single final approval. The semantic normalization still protects the approved proposal and MCU boundary. |

## PC-005 — Implementation and acceptance are one circuit phase

### Report

The schema-13 dashboard treated physical IMPLEMENT and Build + Test as
separate required phases with separate final approvals. In practice, the
circuit was not ready for downstream placement work until exact compiled
parity, assertions, BOM/PCB agreement, and spatial-preservation evidence had
all passed. The first approval therefore certified an intermediate state that
the next phase was required to modify and validate.

### Why this is a process defect

The split created avoidable user ceremony and an ambiguous claim: IMPLEMENT
could display `Complete` while the implemented circuit had not yet passed its
board-specific acceptance contract. It also made ownership look inconsistent,
because the AI-led implementation and tool-led checks were separate dashboard
phases even though both produced one reviewable outcome.

### Required process change

Schema 14 defines one CIRCUIT phase:

1. The AI creates `review/circuit/circuit.yaml`,
   `review/circuit/circuit.svg`, and `docs/circuit-proposal.md`.
2. The user approves that exact proposal before physical source changes.
3. The AI implements the circuit and resolves parts, policy, IOC, and compiled
   parity.
4. The internal `build-test.yaml` gate runs assertions, exact BOM/PCB and
   connectivity checks, and the no-op spatial-preservation audit, producing
   `docs/build-test.md`.
5. The user gives one final CIRCUIT approval covering the implemented and
   tested result.

The internal `check-build-test` command and files remain independently useful,
but `build` is no longer a workflow phase. BRIEF becomes Step 6 and all later
phase numbers move up by one.

### Migration contract

- New projects use `.pcbforge` schema 14 and the `circuit` phase key.
- Active review files use `review/circuit` and `docs/circuit-*`.
- `pcbforge migrate-circuit-phase` upgrades generated schema-13 projects.
- Migration preserves CIRCUIT completion only when both legacy IMPLEMENT and
  Build + Test approvals are current.
- If either approval is missing or stale, CIRCUIT reopens; migration never
  invents user approval.
- The legacy playbooks and phase reader remain only for unmigrated projects.

### Acceptance criteria

- A schema-14 dashboard contains CIRCUIT and no standalone build phase.
- CIRCUIT cannot reach `Awaiting approval` without current proposal, build,
  IOC, parts, policy, final parity, and build-test evidence.
- There is one final CIRCUIT fingerprint and approval after all those checks.
- BRIEF is Step 6 and requires the current CIRCUIT evidence.
- Schema-13 migration is atomic, idempotent, renames active artifacts, and
  follows the two-old-approvals preservation rule.
- Generated `AGENTS.md`, the playbooks, public workflow, and CLI help agree.

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-28 | Open | The phase split was challenged because both phases describe one circuit implementation lifecycle. |
| 2026-07-28 | Ready for pilot | Schema 14 introduces the combined CIRCUIT phase, Step-6 BRIEF, current artifact names, an explicit schema-13 migration, and compatibility handling for legacy dashboards. |

## PC-006 — Approval evidence must exclude volatile compiler run IDs

### Report

**Project context:** Blinky schema-13 replay, after the Build + Test contract,
compiled circuit, assertions, BOM/PCB parity, and spatial-preservation check
had passed.

**Workflow point:** The user explicitly approved a presented Build + Test
fingerprint. A later required `pcbforge status --check --write` performed
another no-op frozen build and regenerated `docs/build-test.md`.

The semantic Build + Test fingerprint remained:

```text
fcf80d0135fa6aca59a73afe45264eaa2b68399ae55166ae9652836de2129cf2
```

The product PCB also remained byte-identical:

```text
b48612520bd39f208da050f5c3c67e4c2222db5d54abaa758a8d01f2e755eec5
```

However, the compiler emits a new random `build_id` in
`default.bom.json` on every run. The generated report hashed the raw BOM JSON,
so otherwise identical builds produced different artifact hashes, different
report bytes, and different approval fingerprints. During the reproduced
failure, successive no-op review packets included approval fingerprints
`9b126733…34dcc` and `74c14c74…fc6a1`.

The user was consequently asked to approve the same unchanged electrical
result repeatedly. A local diagnostic that removed only top-level `build_id`
before hashing produced byte-identical reports and the same approval
fingerprint, `2fae31e4…c502a`, across two complete no-op builds.

### Why this is a process defect

The approval protocol requires the user to review one exact fingerprint and
then requires `status approve` to recompute that packet before recording it.
Any volatile byte in the packet creates a time-of-check/time-of-use failure:
the act of confirming the evidence can make the reviewed fingerprint stale.

This is not useful reproducibility evidence. `build_id` identifies a compiler
invocation, not a BOM, part selection, connection, assertion, or product PCB.
Binding approval to it adds ceremony while detecting no design change.

### Required process change

- Define one canonical semantic hash for compiler BOM JSON that removes only
  the documented volatile run identifier before canonical JSON encoding.
- Use that semantic hash everywhere tracked evidence or approval fingerprints
  refer to the BOM JSON.
- Clearly label the report value as a semantic BOM hash. A raw artifact hash
  may be logged separately for diagnostics, but must not make tracked approval
  evidence unstable.
- Audit every compiler artifact included in a tracked report for timestamps,
  random IDs, absolute temporary paths, or other invocation-specific fields.
- Ensure `status review` followed immediately by `status approve` cannot
  invalidate itself when the circuit inputs and semantic outputs are
  unchanged.

### Acceptance criteria

- Two frozen no-op builds with different compiler `build_id` values produce
  byte-identical `docs/build-test.md`.
- The Build + Test/CIRCUIT check fingerprint and approval fingerprint remain
  identical across those builds.
- Changing a BOM component, quantity, MPN, LCSC identifier, footprint, value,
  or usage/designator changes the semantic BOM hash and approval fingerprint.
- Malformed BOM JSON still fails rather than falling back to an approval-stable
  but incomplete representation.
- `status review` followed by `status approve` succeeds without asking the
  user to approve a newly generated fingerprint.
- A real Blinky no-op rebuild preserves the PCB hash and the exact reviewed
  approval fingerprint.
- Regression tests inject two different `build_id` values and prove stable
  report bytes, status review, and approval recording.

### Non-goals

- Do not ignore any electrically meaningful BOM field.
- Do not weaken exact BOM, PCB, connectivity, assertion, or spatial
  preservation checks.
- Do not claim the raw compiler artifact is byte-reproducible when it is not.

### Likely affected areas

- `pcbforge/build_test.py`
- `pcbforge/status.py`
- `pcbforge/circuit_review.py`
- `tests/test_build_test.py`
- `tests/test_status.py`
- tracked-report reproducibility documentation

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-29 | Open | Blinky reproduced changing approval fingerprints across no-op builds while the semantic Build + Test fingerprint and PCB hash stayed unchanged. |
| 2026-07-29 | Open | A local semantic-BOM diagnostic excluding only `build_id` produced identical report bytes and approval fingerprints across two complete builds. |
| 2026-07-29 | Ready for pilot | One strict shared semantic BOM hash now removes only top-level `build_id`; build-test reports label it explicitly, CIRCUIT evidence uses it, malformed JSON fails, and injected changing run IDs produce byte-identical reports. |
| 2026-07-29 | Ready for pilot | The full 151-test suite passes. Closure still requires the documented real Blinky no-op rebuild and review/approval replay. |

## PC-007 — Pinned projects must not execute incompatible dirty tool code

### Report

**Project context:** The active Blinky project was generated as schema 13 and
records its PCBForge revision and guidance schemas in `.pcbforge`.

**Workflow point:** Between `status review build` and `status approve build`,
the live PCBForge working tree changed to an in-progress schema-14
implementation. The public `scripts/pcbforge` dispatcher continued importing
the live checkout instead of enforcing the project's recorded workflow
version.

The approval command then tried to read the schema-13
`schematic-review.yaml` as the new schema-14 circuit contract and failed:

```text
pcbforge.circuit_review.CircuitReviewInputError:
circuit_review_schema: expected integer 2
```

A subsequent `pcbforge status --write` using the same incompatible checkout
rewrote the dashboard schema and appended a SPEC reopen event. The displayed
project state fell from six completed phases to zero even though the circuit
source, approved review model, acceptance contract, and product PCB had not
changed.

The project pin recorded `dirty: true`, which described the risk but did not
make the exact code reproducible or prevent a later dirty checkout from being
treated as the pinned tool.

### Why this is a process defect

A project-level tool pin is meaningful only if public commands either execute
that exact implementation or refuse to run. Merely recording a commit while
importing arbitrary current working-tree code lets unrelated tool development
reinterpret old artifacts, invalidate valid approvals, and persist false
workflow history.

This is especially dangerous for `status --write`: a schema parse failure
should be a read-only compatibility error. It must never be converted into
evidence that the user changed or rejected an approved artifact.

### Required process change

- New projects must pin a reproducible tool state. Initialization from a dirty
  checkout must either fail or capture an immutable content identity that can
  actually be executed later.
- The public dispatcher must compare the active implementation with the
  project's pin before importing project-aware workflow code.
- On a mismatch, run the pinned implementation from an isolated checkout or
  stop with an actionable migration/version error before touching project
  files.
- Workflow schema upgrades must occur only through an explicit, atomic,
  versioned migration command.
- `status --write`, `status --check --write`, and `status approve` must validate
  every required schema before writing `STATUS.md`.
- Unsupported or partially migrated inputs must leave all project files and
  append-only workflow events byte-for-byte unchanged.

### Acceptance criteria

- Invoking a schema-14 checkout against an unmigrated schema-13 project either
  runs the exact pinned schema-13 implementation or exits without modifying
  any project file.
- A dirty checkout cannot be recorded as a reproducible project pin unless an
  immutable executable snapshot/content digest is retained.
- An unsupported `circuit_review_schema`, status schema, or guidance schema
  causes a fail-closed diagnostic and no new reopen event.
- A failed approval command cannot partially refresh checks, reports, status
  schema, or workflow events.
- Explicit migration is atomic, idempotent, and preserves approvals only under
  its documented migration contract.
- Regression tests compare complete project-directory hashes before and after
  incompatible invocations and prove there are no writes.
- The Blinky schema-13 project remains at its last valid completed phase when
  inspected from a schema-14 development checkout.

### Non-goals

- Do not silently treat old schemas as current.
- Do not suppress legitimate reopening after a real approved-artifact change.
- Do not automatically migrate a project merely because a newer checkout is
  available.

### Likely affected areas

- `scripts/pcbforge`
- `pcbforge/initialize.py`
- `pcbforge/cli.py`
- `pcbforge/status.py`
- `.pcbforge` pin format and migration commands
- `tests/test_initialize.py`
- `tests/test_status.py`
- end-to-end version-skew tests

### Resolution log

| Date | State | Note |
|---|---|---|
| 2026-07-29 | Open | Blinky reproduced a schema-13 review/approval interruption when the live tool checkout changed underneath the project. |
| 2026-07-29 | Open | An incompatible status write appended a false SPEC reopen and rewrote the dashboard despite unchanged project artifacts. |
| 2026-07-29 | Ready for pilot | The public launcher now resolves clean exact-revision Git worktrees before importing workflow code, rejects dirty pins and initialization, and reserves version skew for explicit migrations. Current-schema CLI preflight rejects incompatible pins, guidance, status, and structured artifacts before checks or writes. |
| 2026-07-29 | Ready for pilot | Worktree routing and complete-tree no-write regressions pass. A real `status --write` attempt against Blinky's unreproducible dirty schema-13 pin failed closed while `.pcbforge`, `STATUS.md`, and `docs/build-test.md` remained byte-identical. |
