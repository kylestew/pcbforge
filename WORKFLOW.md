# PCBForge workflow

This is the normative process map: seven numbered phases, six required.
There are seven required human decisions at phase and handoff gates. Sourcing confirmation is a separate policy decision.

PCBForge uses KiCad 10.0.3. The saved schematic owns the circuit. The PCB owns placement and routing.
Atopile is not part of this workflow. Existing projects must use their historical pinned PCBForge checkout.
Start a fresh project to use this workflow.

| Phase | Work | Completion |
|---|---|---|
| SPEC | Requirements and manufacturing policy | User approves the requirements |
| ARCHITECT | Functional diagram, exact MCU and pin plan | User approves the proposal; the tool validates the IOC and records the baseline |
| CIRCUIT | Saved schematic, sourced part facts and independent electrical tests | User approves the checked schematic; the user updates the PCB; the tool verifies synchronization |
| LAYOUT | Placement and routing | User approves the placement brief, then declares layout complete |
| VERIFY | DRC, schematic parity, engineering audit and render review | User approves the verification packet |
| ORDER | Fabrication packet and current sourcing | Tool generates the packet; user confirms sourcing and places the order |
| PUBLISH | Reusable circuits with evidence from hardware | User approves publication or skips this optional phase |

The checked transitions are initialization, architecture baseline, PCB synchronization, layout handoff and fabrication output.
CIRCUIT has one schematic approval. A successful PCB check completes the phase without a second human approval.
Passing checks do not grant a user approval.

## Circuit source and acceptance

Edit `<project>.kicad_sch` and its child sheets directly in KiCad or with `SchematicDocument`.
Keep native UUIDs, sheet instance paths, wires, labels and symbol fields.
Use official symbols and footprints first. Record exact MPN, LCSC, Datasheet and `pcbforge_purpose` fields on fitted parts.
Put custom libraries inside `parts/` and register them in the native library tables.
Record the official-library search and package justification for custom parts. Use official assets for ordinary passives.

`circuit-tests.yaml` maps requirements to Python test functions. It does not duplicate the circuit.
`electrical-facts.yaml` contains sourced limits, package pin functions and the MCU-to-IOC mapping.
Tests inspect the graph that KiCad extracts from the saved schematic. They must check requirements independently of the drawing.
Cover power, MCU support, interfaces, protection and component ratings. Use sourced engineering assessments where a scripted check is unsuitable.

Run `pcbforge check-circuit --write-report` to check:

- Native connectivity, hierarchical instances, multi-unit symbols, no-connects and fitted-part flags.
- KiCad ERC and explicit finding exclusions.
- Symbol pins, package pads, exact part identity and sourced package facts.
- MCU part, physical pins, signals and nets against the checked IOC.
- Independent electrical tests with recorded measurements and limits.
- Readability measured from saved drawing geometry.

The command writes the acceptance report, extracted graph, BOM and SVG previews.
Review every changed sheet, the semantic circuit changes and the electrical test coverage before requesting approval.
ERC and readability checks cannot prove engineering correctness or drawing usability on their own.
The user reviews the actual schematic previews as part of the circuit approval.

Only exact finding IDs with a rationale can be excluded in `circuit-review.yaml`.
Stale exclusions fail. Electrical test failures cannot be excluded there.
Warnings about fragmented paths or ambiguous crossings require visual review.

### Human-readable schematic capture

Apply these rules during capture, not only during final cleanup:

- Use direct wires for the main signal path and local connections within each functional block.
- Arrange each block with a clear input-to-output flow.
- Place supporting components beside the circuit they serve.
- Show local supply and decoupling connections together, with clear ground returns.
- Draw pull-ups, protection, filters and termination as visible branches of their associated paths.
- Use net labels primarily between functional blocks and sheets.
- Keep power symbols and labels where direct wires create clutter or obscure the circuit.
- Do not replace useful local connections with isolated components that share net labels.
- Preserve net names, pin connections and component identity during drawing-only changes.

For example, draw an RS-485 transceiver with its decoupling capacitor beside the supply pin.
Show A/B as continuous paths, protection as branches, and switchable termination across the pair.
Keep encoder supply bypassing in the encoder block, distinct from transceiver decoupling.

Before requesting CIRCUIT approval, inspect every changed rendered sheet:

- Trace each main signal path through its functional block.
- Check that each supporting component has a visible relationship to the circuit it serves.
- Check that local operation is understandable without a search for matching labels elsewhere on the sheet.
- Check junctions and unconnected crossings for visual ambiguity.
- Explain necessary label-only connections in the review when their purpose is not clear.

ERC establishes connectivity checks, not human comprehension. Readability lint provides advisory evidence, not acceptance by label or wire count.
A passing automated check does not replace this visual review. This review belongs to the existing CIRCUIT approval, not another gate.

## Native PCB update

After the user approves the schematic:

1. Run `pcbforge prepare-pcb-update`. Read the circuit delta and retain the recorded PCB backup.
2. Open the project in KiCad. Use **Update PCB from Schematic**.
3. Match footprints through their native schematic links. Enable field updates and review additions, removals and footprint replacements.
4. Save the PCB. Run `pcbforge check-pcb-update`.
5. Resolve any discrepancy, then run `pcbforge finish-circuit`.

The verifier checks component identity, fields, fitted flags, pad numbers and exact nets.
It also checks retained placement, pad geometry, tracks, vias, zones, outline and graphics against the backup.
An approved net rename may change the net name on retained copper. Zone fill caches may change.
The verifier never applies the PCB update or restores a backup automatically.
A populated PCB without a synchronization baseline is rejected.

## Layout and fabrication

Write `placement.yaml`, then run `pcbforge prepare-layout` and `pcbforge check-layout-handoff`.
Present the placement brief for the user's handoff approval.
The user owns placement and routing. Perform a spatial assist only after an explicit request during open LAYOUT.
Record requested assists in the workflow history. Preserve the approved board rules and user net classes.

After layout, run the applicable status checks and inspect the board renders.
KiCad DRC includes schematic parity. VERIFY requires its own user approval.
Run `pcbforge fab-out`, then `pcbforge check-fab-out` to validate the packet.
Fabrication uses the fitted BOM extracted from the schematic. Confirm sourcing against that packet before ordering.

## Changes and approvals

Approvals bind fingerprints and remain in append-only history.
Changed electrical intent invalidates CIRCUIT and affected downstream approvals.
A drawing-only edit preserves the electrical approval, but invalidates the saved presentation checks and previews.
Recheck and review changed sheets before continuing. A drawing edit between review and approval invalidates that review.
PCB placement changes do not invalidate circuit acceptance. They can invalidate LAYOUT and VERIFY.
Changes to requirements, tests, facts or exact parts invalidate their dependent evidence.

Use `pcbforge status --next` to identify the next action.
Use `pcbforge status --check --write` to refresh checks and record stale approvals durably.
Unchanged passing checks are reused. `--force-checks` forces a fresh validation.
After an upstream change, review the cascade packet. Renew only the unchanged gates that the user explicitly approves.
The approval procedure is in [the operating manual](agent/operating-manual.md).
