<!-- pcbforge-layout-handoff-schema: 1 -->
# CIRCUIT-to-LAYOUT handoff playbook

Use this playbook for the CIRCUIT-to-LAYOUT transition defined in
[`WORKFLOW.md`](../WORKFLOW.md), only after CIRCUIT is complete. The transition
turns the known circuit topology into a complete, reviewable placement
contract. It does not place footprints, create keepout geometry, route copper,
or modify the KiCad PCB.

The transition opens the single LAYOUT phase. Placement and routing remain
different physical tasks, but they share one lightweight done-declaration;
VERIFY performs the detailed scrutiny after both are finished. Inside that open
phase the user may explicitly ask the agent to attempt placement or routing;
see the LAYOUT-assist rules in
[`operating-manual.md`](operating-manual.md#layout-assist-only-when-the-user-asks).
This transition itself never edits the board, whoever later does.

## Outputs and ownership

- `placement.yaml` is the authoritative, human-reviewable placement contract.
- `docs/placement-brief.md` is generated from that contract. Do not edit it by
  hand.
- `<project>.kicad_pro` receives only classes named `pcbforge:<name>` and exact
  net-to-class patterns. The Default class, user classes, assignments, and
  unknown project settings are preserved.
- `<project>.kicad_pcb` remains byte-for-byte unchanged across this transition.
  The user owns every position, side, track, via, zone, outline, graphic, and
  other spatial object; the agent edits them only after the handoff is
  approved, and only for spatial work the user explicitly requested.

## `placement.yaml` schema 1

Unknown keys, duplicate YAML keys, ambiguous endpoints, wildcard net names,
incomplete footprint coverage, and unsafe manufacturing dimensions fail loud.

```yaml
placement_schema: 1

board:
  strategy: >-
    One or more sentences describing the qualitative floorplan and the
    dominant signal, power, thermal, RF, accessibility, and mechanical goals.
  rules:
    - Keep all user-accessible connectors reachable from a board edge.
    - Preserve an uninterrupted return path beneath sensitive signals.

groups:
  - id: power-entry
    priority: 1
    region: west edge
    rationale: Short input-current path and direct connector access.
    references: [J1, F1, D1, U1, C1, C2]
  - id: controller
    priority: 2
    region: center
    rationale: Central fan-out with short local support paths.
    references: [U2, C3, C4, C5]

placement_order: [power-entry, controller]

constraints:
  - id: controller-decoupling
    type: proximity
    subjects: [U2.4, C3.1]
    max_mm: 2
    rationale: Minimize the high-frequency supply loop.
  - id: noisy-power-separation
    type: separation
    subjects: [U1, U2]
    min_mm: 8
    rationale: Keep switch-node fields away from the controller.
  - id: usb-at-edge
    type: board-edge
    subjects: [J1]
    edge: west
    max_mm: 1
    rationale: The connector must be physically accessible.
  - id: antenna-clearance
    type: keepout
    subjects: [U3]
    keepout: copper, components, and ground fill in the antenna keepout
    min_mm: 5
    rationale: Preserve the antenna radiation region.
  - id: connector-outward
    type: orientation
    subjects: [J1]
    direction: connector opening faces west
    rationale: Cable access.
  - id: swd-access
    type: accessibility
    subjects: [J2]
    edge: north
    rationale: Probe access after assembly.
  - id: thermal-flow
    type: airflow
    subjects: [U1, U4]
    direction: south to north
    rationale: Do not place heat-sensitive parts downstream of the regulator.

net_classes:
  - name: power
    rationale: Wider copper for the board power path.
    nets: [+5V, +3V3]
    clearance_mm: 0.2
    track_width_mm: 0.5
    via_diameter_mm: 0.7
    via_drill_mm: 0.3
  - name: usb
    rationale: Consistent geometry for the USB differential pair.
    nets: [USB_D+, USB_D-]
    clearance_mm: 0.2
    track_width_mm: 0.2
    via_diameter_mm: 0.6
    via_drill_mm: 0.3
    differential_pair:
      width_mm: 0.2
      gap_mm: 0.2
      via_gap_mm: 0.2

checklist:
  - Every connector orientation and enclosure interface is correct.
  - Every proximity, separation, edge, keepout, and access constraint is met.
  - Sensitive routes have a plausible continuous return path.
```

The schema rules are:

1. Every PCB reference appears in exactly one group. No missing, extra, or
   duplicate assignment is allowed.
2. Group IDs, constraint IDs, and net-class names use kebab case. Group
   priorities are positive and unique. `placement_order` lists every group
   exactly once.
3. Constraint endpoints are exact `REF` or `REF.PAD` values from the current
   PCB. `proximity` and `separation` take two endpoints. `board-edge`,
   `orientation`, and `accessibility` take one. `airflow` takes at least two.
4. `proximity` and `board-edge` require `max_mm`; `separation` and `keepout`
   require `min_mm`. A keepout also names what is excluded. Board-edge and
   accessibility edges are `north`, `east`, `south`, `west`, or `any`.
5. Orientation, accessibility, and airflow subjects are whole references,
   never pads.
6. Every net is an exact existing resolved PCB net. Wildcards are not
   interpreted. A net may appear in only one PCBForge class.
7. Track, clearance, via, drill, annular-ring, and differential-pair values
   must meet the project's pinned conservative JLC profile. These classes are
   routing defaults, not an impedance claim.
8. At least one PCBForge net class, one board rule, and one checklist item are
   required. Constraints may be empty only when the reviewed circuit genuinely
   has no typed spatial relationship.
9. Keepouts remain written instructions in the LAYOUT handoff; the tool never draws
   geometry or changes the board.

## Procedure

1. Read `spec.md`, `docs/build-test.md`, the exact BOM/designators, resolved
   PCB nets and pads, datasheets, connector mechanics, and all thermal/RF/power
   requirements already recorded in project documentation.
2. Write `placement.yaml` from intent. Do not infer it by merely copying the
   current PCB arrangement.
3. Run:

   ```sh
   pcbforge prepare-layout
   pcbforge check-layout-handoff
   ```

4. Confirm the command reports that the PCB is unchanged. Review the generated
   `docs/placement-brief.md`, placement coverage, class dimensions, exact net
   assignments, and the `.kicad_pro` diff. PCBForge may replace its own
   `pcbforge:` entries; it must preserve every non-PCBForge entry.
5. Present `docs/placement-brief.md` beside the current, approved CIRCUIT
   explanatory SVG. PCBForge has already compared its exact approved proposal model with
   the compiled BOM and PCB topology; the handoff checks that this evidence is
   still current before layout.
   Confirm the `polarity-marking` and `pin1-marking` policy assurances have
   current evidence or an explicitly approved exception.
6. If the approved circuit overview is missing, stale, or no longer adequate for
   placement decisions, record a concrete blocker and stop before layout:

   ```sh
   pcbforge status mark layout blocked \
     --note "Circuit presentation is inadequate: <reason>"
   ```

7. Follow the
   [standard review and approval protocol](operating-manual.md#review-and-approval-protocol)
   using its layout-handoff variant. Present the exact packet and fingerprint
   alongside both visual artifacts, then stop for explicit user approval.

Passing generation alone never records human approval. The approval event is
bound to the current CIRCUIT review and deterministic acceptance evidence,
`placement.yaml`, generated brief, rules profile, topology, and PCBForge-owned
net-class fingerprint. Regeneration after a material change requires renewed
user review and approval; rerunning the checker cannot revive the old event.

## Staleness contract

- Circuit identity, footprint, pad, or connectivity changes stale CIRCUIT
  evidence and the LAYOUT handoff and require regeneration/review.
- Changes to `placement.yaml`, generated `docs/placement-brief.md`, or
  PCBForge-owned net classes stale the LAYOUT handoff.
- Footprint positions/sides, tracks, vias, zones, outline, graphics, and other
  spatial edits do not stale CIRCUIT or the LAYOUT handoff, whether the user or
  a requested agent pass made them.
- User-created KiCad net classes and unrelated project settings do not stale
  the LAYOUT handoff.
