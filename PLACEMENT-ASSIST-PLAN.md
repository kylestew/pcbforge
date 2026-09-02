# Implementation plan — placement assistance (PA1–PA7)

Adds tool and playbook support for PCB placement so the user no longer does
all the datasheet research, closeness tradeoffs, and mechanical reasoning by
hand. Written for a capable but literal implementer: every design decision is
already made here. Do not re-litigate them. When something is genuinely
ambiguous, stop and ask rather than inventing.

Read [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md) "Ground rules" first.
Everything there still applies: no migrations, no feature gates, no
version-keyed branches, stdlib `unittest`, docs move with code.

## Ground rules specific to this plan

- **The tool never moves footprints or copper on its own.** Only the two
  explicitly user-requested commands in PA5 and PA7 write to `.kicad_pcb`, and
  only inside an open LAYOUT with a current handoff approval. Everything else
  reads the board and writes reports, YAML, or Markdown.
- **`placement_schema` stays `1`.** Every new key in `placement.yaml` is
  optional. An existing valid file stays valid. This is an additive change to
  one schema, not a second schema. Kinetic-tile (mid-LAYOUT) is the pilot and
  must keep working without restart.
- **Byte-level board edits only.** When a command must move a footprint, it
  edits that footprint block's text in place. It never re-serializes the file
  through `sexpr.dumps`. See PA5 step 6 for the exact edit rule.
- **Measurements, not opinions.** Every check reports the measured value next
  to the limit. Constraints that cannot be measured (orientation,
  accessibility, airflow) are reported as `manual` with the current
  position, rotation, and side so a reviewer can judge them.
- **Advisory, never blocking.** The new `placement` status check is listed
  and displayed for LAYOUT but never appears in `PHASE_EVIDENCE_CHECKS`, so
  it never blocks a phase or an approval. The user decides.
- **KiCad 9 only.** `(version 20241229)` boards. Anything else fails loud with
  `unsupported board version`. Do not add KiCad 10 handling.
- **No new runtime dependencies.** `yaml` and `numpy` already exist in the
  toolchain venv; use stdlib otherwise.
- Test command (verify it works before starting):

  ```sh
  uv run --project toolchain python -m unittest discover -s tests -t .
  ```

- Key file map: placement contract and brief `pcbforge/placement.py`; board
  reader used for topology `pcbforge/build_test.py` (`read_board_evidence`);
  s-expression parser `pcbforge/sexpr.py`; CLI `pcbforge/cli.py`; status
  checks `pcbforge/status.py` (`CHECK_PHASES`, `_check_inputs`,
  `_check_fingerprint`, `run_status_checks`); scaffold text that becomes each
  project's `AGENTS.md` `pcbforge/initialize.py` (~lines 885–920).
- Pilot project: `/Users/kylestewart/Projects/ai-pcb-attempts/kinetic-tile`.
  U2 is a TI DRV8316CT (VQFN-40, exposed pad 41). It is in LAYOUT with the
  handoff approved. Read its `placement.yaml` before starting; it is the
  realistic example every schema decision below was made against.

## Decisions (do not reopen)

| Question | Decision |
|---|---|
| Scope | All seven workstreams, in order PA1→PA7. PA1–PA4 are the priority; PA6–PA7 may ship later but are specified now. |
| Board format | KiCad 9 only. |
| check-placement gating | Advisory. Shown in LAYOUT status and in the LAYOUT and VERIFY review packets. Never blocks. |
| Pattern fidelity | Two fidelities: `exact` (mm offsets, from EVM files or dimensioned drawings) and `sketch` (side/order/max distance, transcribed from a datasheet figure). Both are checked. Only `exact` can be applied. |
| Pattern home | Catalog in this repo at `patterns/<id>.yaml`. A project may carry `patterns/<id>.yaml` too; the project copy wins by id. Handoff fingerprint binds the bytes of every pattern the project uses. |
| apply-pattern scope | Moves satellite footprints only. Thermal vias and pours are checked, never drawn. |
| Layer mismatch | Pattern declares `source.layers`. When it differs from `spec.layers`, the brief and the check print a warning. Never blocks. |
| Kinetic-tile | Pilot for each workstream as it lands (PA4 step 9). The DRV8316CT pattern is captured at `sketch` fidelity from the datasheet layout example first; upgrade to `exact` only if TI EVM design files are obtained and imported. |
| Sketcher solver | Hand-rolled simulated annealing on group rectangles, pure Python plus `random` with a fixed seed. No OR-tools. |
| Research ledger form | Agent-written `docs/layout-research.md` during CIRCUIT, plus an optional `source:` string on every constraint and an optional `guidance:` list on every group in `placement.yaml`. The handoff check warns (does not fail) on a `proximity`, `keepout`, or `loop` constraint without a `source`. |
| Distance semantics | Pad endpoint → pad centre. Reference endpoint → footprint bounding box. Proximity = centre-to-centre for two pads, nearest-edge for boxes. Separation = nearest-edge box-to-box. Board edge = nearest distance from box edge to the outline bounding box side. |
| Footprint bounding box | **CORRECTED in PA1.** Courtyard graphics when present; otherwise the union of fab, silk, and pad extents grown 0.25 mm per side. `box_source` is `"courtyard"` or `"fallback"`. A courtyard-then-fab precedence is wrong: kinetic-tile's U2 and L1 have no courtyard and only a pin-1 dot on `F.Fab`, so fab-first reports a 5x7 mm package as a 0.06 mm box. |

---

## PA1 — Board geometry reader — DONE

Shipped as `pcbforge/board_geometry.py` with `tests/test_board_geometry.py` and
`pilots/kicad9-multichannel/scripts/check_board_geometry.py`. Four corrections to
the spec below were found while implementing it and are recorded inline. The
fixture is the in-repo multichannel pilot board, not kinetic-tile, and step 8's
KiCad GUI session was replaced by a scriptable IPC-D-356 oracle.

**Goal:** one module that turns a KiCad 9 `.kicad_pcb` into absolute
footprint, pad, outline, via, and zone geometry in millimetres. Every later
workstream consumes it. Nothing writes.

New file `pcbforge/board_geometry.py`. Do not extend `BoardEvidence`; it is a
topology fingerprint object and must stay position-free.

### Data model

```python
@dataclass(frozen=True)
class Box:            # mm, KiCad coordinates, y grows downward
    min_x: float; min_y: float; max_x: float; max_y: float
    # helpers: width, height, centre, area, contains(point), distance_to(Box), overlaps(Box)

@dataclass(frozen=True)
class PadGeometry:
    number: str; x: float; y: float          # absolute centre
    size_x: float; size_y: float; net: str    # net "" when unconnected
    through_hole: bool

@dataclass(frozen=True)
class FootprintGeometry:
    reference: str; footprint: str            # e.g. "Capacitor_SMD:C_0603_1608Metric"
    x: float; y: float; rotation: float       # footprint `(at x y r)`; r in degrees, 0 when absent
    side: str                                 # "front" for F.Cu, "back" for B.Cu
    box: Box                                  # absolute
    box_source: str                           # "courtyard" | "fab" | "pads"
    pads: tuple[PadGeometry, ...]
    properties: Mapping[str, str]             # every (property "Name" "Value") pair

@dataclass(frozen=True)
class ViaGeometry:
    x: float; y: float; diameter: float; drill: float; net: str

@dataclass(frozen=True)
class ZoneGeometry:
    layer: str; net: str; box: Box            # bounding box of the first (polygon (pts ...))

@dataclass(frozen=True)
class BoardGeometry:
    version: int
    layer_count: int                          # count of copper layers in (layers ...)
    footprints: tuple[FootprintGeometry, ...] # sorted by reference
    outline: Box | None                       # bbox of all Edge.Cuts graphics; None if none
    outline_segments: int
    vias: tuple[ViaGeometry, ...]
    zones: tuple[ZoneGeometry, ...]

    def footprint(self, reference) -> FootprintGeometry   # raises KeyError
    def pad(self, reference, number) -> PadGeometry       # raises KeyError
```

Public entry point: `read_board_geometry(path: Path) -> BoardGeometry`.
Raise `BoardGeometryError(RuntimeError)` with a one-line reason for a missing
file, a parse failure, or a `(version ...)` other than `20241229`.

### Steps

1. Parse with `sexpr.parse`. Walk top-level children with `sexpr.head`,
   `children`, `child`, `atom`, `number`.
2. Footprint transform. For a footprint at `(fx, fy, fr)` in degrees and a
   child point `(dx, dy)`:

   ```
   r = radians(fr)
   abs_x = fx + dx*cos(r) + dy*sin(r)
   abs_y = fy - dx*sin(r) + dy*cos(r)
   ```

   KiCad rotation is counter-clockwise on screen with y downward, which is
   what this formula encodes. Apply the same transform on both sides: KiCad
   stores back-side child offsets already mirrored, so do not add a mirror.
   **Verify both claims empirically before writing tests** (step 8).
3. Pad absolute position uses the pad's own `(at dx dy [a])`; ignore `a` for
   position. **CORRECTION:** `a` is the pad's ABSOLUTE board-space angle, not an
   offset from the footprint rotation, and it is omitted when zero. Use it as
   stored when orienting a pad's box; never add the footprint rotation to it. `size` is `(size sx sy)`. `through_hole` when the pad type atom
   is `thru_hole` or `np_thru_hole`. Net name is the third atom of
   `(net N "NAME")`, `""` when absent.
4. Bounding box: collect `fp_line`, `fp_rect`, `fp_arc`, `fp_circle`,
   `fp_poly` on `F.CrtYd` or `B.CrtYd` (whichever side the footprint is on);
   if none, the same on `F.Fab`/`B.Fab`; if none, pad boxes (centre ± size/2,
   rotated by the pad angle only when the angle is a multiple of 90; otherwise
   use the larger of the two sizes as a square) grown by 0.25 mm. For arcs and
   circles use the endpoints and centre ± radius; exactness is not required
   for a bounding box. Transform each point with step 2 then take the extremes.
   Record `box_source`.
5. Outline: bounding box of every `gr_line`, `gr_rect`, `gr_arc`, `gr_circle`,
   `gr_poly` on `Edge.Cuts`. Count segments.
6. Vias: `(via (at x y) (size d) (drill h) ... (net N))`. **CORRECTION:** a via
   carries only the numeric net index, with no name, so the name must be resolved
   through the top-level `(net N "NAME")` table. Reading it locally yields `""`
   for every via on every real board. Zones differ: they carry `(net_name "X")`.
   Zones are `(zone (net N) (net_name "X") (layer "L") ... (polygon (pts ...)))`;
   take the first `polygon` and never `filled_polygon`, which is derived, repeats
   per layer, and holds thousands of points. **CORRECTION:** `(layers ...)` plural
   is the common zone form, not the rare one, at five of six zones in the fixture;
   record the first named layer. Skip a zone with no polygon.
7. Layer count: count entries under `(layers ...)` whose type atom is
   `signal`, `power`, `mixed`, or `jumper`.
8. **REPLACED.** The planned KiCad GUI session was unnecessary. Verify against
   `kicad-cli pcb export ipcd356`, which emits absolute pad coordinates in
   0.0001-inch units with y up. Result: worst error 0.0017 mm across 265 pads,
   the IPC quantum. The back-side mirror hypothesis was tested and rejected at
   5.4 mm. The golden fixture is
   `pilots/kicad9-multichannel/baseline/source/multichannel_mixer.kicad_pcb`,
   which is git-tracked, KiCad 9, and has the 81 back-side footprints, 29 vias,
   and 6 zones that kinetic-tile lacks. Its `-unrouted` sibling is version
   20241030 and serves as the version-rejection fixture. Do not create
   `tests/fixtures/geometry/`.
9. **CORRECTION:** a rect and a circle must contribute all four corners of their
   local bounding square, and local points must be transformed individually
   before being bounded. Bounding first and transforming that box's corners
   circumscribes any non-rectangular shape and over-estimates it.

### Tests (`tests/test_board_geometry.py`)

- Rotation 0, 90, 180, 270, and 45 for a two-pad part; back-side fixture.
- Bounding-box source precedence: courtyard, fab, pads.
- Outline bbox from four `gr_line`s and from one `gr_rect`.
- Via and zone extraction.
- Version rejection.
- The real kinetic-tile fixtures from step 8.

---

## PA2 — `pcbforge check-placement`

**Goal:** measure the live board against `placement.yaml`, write a report,
surface the result in status as an advisory LAYOUT check.

New file `pcbforge/placement_check.py`. CLI `pcbforge check-placement
[PROJECT_DIR] [--write-report]`. Report path `docs/placement-check.md`.

### Preconditions

`placement.yaml` parses via `read_placement_contract` and the board reads via
PA1. Do **not** require a current handoff approval or CIRCUIT acceptance; the
user may run this at any point in LAYOUT, including after a reopen. Do require
that every reference in the contract exists on the board (the contract parser
already enforces this).

### Findings model

```python
@dataclass(frozen=True)
class Finding:
    kind: str          # "constraint" | "overlap" | "outline" | "pattern" | "floorplan"
    identifier: str    # constraint id, "REF/REF", "REF", pattern role id, group id
    status: str        # "pass" | "fail" | "manual" | "unmeasured"
    measured: str      # e.g. "1.62 mm", "rotation 90°, front, (147.25, 119.46)"
    limit: str         # e.g. "≤ 2 mm"
    detail: str        # one line; empty when obvious
```

`PlacementCheckResult` holds the findings, counts by status, the board
fingerprint used (sha256 of the `.kicad_pcb` bytes), and `summary` in the
style of `BriefResult.summary`: `"31 pass, 3 fail, 7 manual, 0 unmeasured"`.

### Evaluators (one function per constraint type)

| Type | Measurement | Pass when |
|---|---|---|
| `proximity` | two pads: centre distance. One or two references: nearest-edge distance between boxes (pad→box uses the pad's own box). | `≤ max_mm` |
| `separation` | nearest-edge box-to-box, same for pads | `≥ min_mm` |
| `board-edge` | distance from the subject box's nearest side to the outline bbox side named by `edge`; for `any`, the minimum over four sides. `north` = outline `min_y`, `south` = `max_y`, `west` = `min_x`, `east` = `max_x`. | `≤ max_mm` |
| `keepout` | for each subject: the nearest other footprint box and the nearest via to the subject box; report the closest offender | both `≥ min_mm` |
| `orientation`, `accessibility`, `airflow` | none | `manual`, with position/rotation/side of every subject |
| `order` (new, PA3) | subject centres projected on the axis named by `direction` | strictly monotonic in listed order |
| `loop` (new, PA3) | perimeter of the polygon through the listed pad centres in listed order, closed | `≤ max_mm` |

Always-on findings, independent of constraints:

- **overlap**: every pair of footprints on the same side whose boxes overlap
  by more than 0.05 mm in both axes → `fail`. Mounting holes
  (`footprint` name starting with `MountingHole`) are excluded.
- **outline**: `unmeasured` when the outline is missing; else `fail` for any
  footprint box not fully inside the outline bbox, and one finding comparing
  outline width × height to `spec.board_mm` (`fail` when larger in either
  axis).
- **stacked at origin**: if more than three footprints share the same `(x, y)`
  to 0.01 mm, one `fail` finding "N footprints unplaced at (x, y)" so an
  unplaced board is obvious.

Pattern findings come from PA4; floorplan findings from PA6. Leave the two
`kind` values in the model now.

### Report

`docs/placement-check.md`, deterministic, generated header
`<!-- generated by pcbforge check-placement; board sha256 ... -->`, then one
table per `kind` with columns `id | status | measured | limit | detail`, fails
first. Advisory warnings (layer mismatch, uncited constraints) go in a final
"Warnings" list. The report is **not** part of any fingerprint and is
git-tracked like `docs/placement-brief.md`.

### Status integration (`pcbforge/status.py`)

1. Add `"layout": ("placement",)` to `CHECK_PHASES`. Do **not** touch
   `PHASE_EVIDENCE_CHECKS`. Grep every consumer of `CHECK_PHASES` and confirm
   a failing check in it only displays; `_failed_checks_for_phase` reads
   `PHASE_EVIDENCE_CHECKS` so it stays advisory. Write a test that proves a
   failing `placement` check leaves the LAYOUT phase status and health
   unchanged.
2. `_check_inputs("placement")` → `placement.yaml`, `<name>.kicad_pcb`, and
   every pattern file the contract references (PA4). `_check_fingerprint`
   falls through to `_fingerprint(project_dir, inputs)`; the board's full
   bytes are correct here because positions matter for this check.
3. In `run_status_checks`, after the `layout-handoff` block: when
   `placement.yaml` and the board exist and the current phase is LAYOUT or
   later, run `check_placement(project_dir, write_report=write_reports)`.
   Status `pass` only when no `fail` findings; summary is the result summary.
   Follow the existing `_reusable_check_record` pattern so unchanged boards
   skip the run.
4. Dashboard: render `placement` in the Workflow table's LAYOUT row evidence
   column as `advisory: <summary>`; the LAYOUT and VERIFY review packets list
   the report path and the fail count. Advisory checks render with `⚠️` not
   `❌` and never change `Health`.

### CLI output

```
pcbforge: placement check — 31 pass, 3 fail, 7 manual, 0 unmeasured
  FAIL driver-vm-bypass-a  measured 4.10 mm  limit ≤ 2 mm  (U2.9 ↔ C3.1)
  ...
pcbforge: wrote docs/placement-check.md          # with --write-report
```

Exit 0 on pass, 1 when any finding fails, 2 on input errors. Always print
every `fail` line; print `manual` lines only with `--verbose`.

### Tests (`tests/test_placement_check.py`)

Build boards as inline text like `tests/test_placement.py` does. Cover each
evaluator's pass and fail, `any` edge, keepout against a via, overlap
exclusion of mounting holes, the stacked-at-origin finding, the report being
byte-stable across two runs, CLI exit codes, and the status advisory test.

---

## PA3 — Research ledger and schema additions

**Goal:** the research happens once, in CIRCUIT, by the agent, with sources;
`placement.yaml` can carry it.

### Schema additions (`pcbforge/placement.py`)

All optional. Add to the `allowed` sets and dataclasses.

- Constraint `source: <string>` — free text citing a document section,
  figure, EVM revision, spec line, or `docs/layout-research.md` heading.
- Group `guidance: [<string>, ...]` — short imperative layout notes for the
  group, each ending with a `(source)` parenthetical by convention. Render in
  the brief under the group.
- New constraint type `order`: `subjects` ≥ 2 references, `direction` one of
  `west-to-east`, `east-to-west`, `north-to-south`, `south-to-north`, no
  `min_mm`/`max_mm`. Rationale required as today.
- New constraint type `loop`: `subjects` ≥ 3 `REF.PAD` endpoints, `max_mm`
  required, meaning the closed perimeter through the pads in order.
- `read_placement_contract` gains a `warnings: tuple[str, ...]` field on
  `PlacementContract`: one warning per `proximity`, `keepout`, or `loop`
  constraint without `source`. `prepare-layout` and `check-layout-handoff`
  print warnings after the pass line; they never fail on them.

Update the `_constraint_instruction` renderer and `layout-handoff.md` schema
docs and rules list for the two new types.

### `docs/layout-research.md` (agent-written, CIRCUIT phase)

Not tool-generated, not fingerprinted. One `## REF — MPN` section per IC,
regulator, connector, crystal, antenna, power inductor, and any part whose
datasheet has a layout section. Each section has exactly these subsections:

```
### Sources
- <document id, revision, section/figure, URL if any>
### Guidance
- <one imperative sentence per rule, each with (source) at the end>
### Pattern
- none | sketch: patterns/<id>.yaml | exact: patterns/<id>.yaml
### Mechanical
- height, mating direction, keepout, tool access, or "none"
```

The agent writes this in CIRCUIT after part selection (Gate B), before the
final CIRCUIT review. The handoff playbook step 1 then reads it instead of
re-reading datasheets.

### Playbook edits

- `agent/circuit.md`: new section "Layout research" after "Library
  precedence" describing the file above and its timing.
- `agent/spec-interview.md` dimensions table: replace the `Size / form` row
  with three rows: `Size / form` (dims, holes), `Mechanical` (enclosure,
  which edges connectors must reach, max component height, keepouts, which
  side is user-facing), `Cabling` (what plugs in, from which direction, strain
  relief). Defaults: none stated → record "unconstrained" explicitly in
  `spec.md`.
- `agent/layout-handoff.md`: schema docs for `source`, `guidance`, `order`,
  `loop`; step 1 reads `docs/layout-research.md`.
- `pcbforge/initialize.py` `AGENTS.md` text: add the research file to the
  CIRCUIT steps and the two new commands (`check-placement` now,
  `apply-pattern` in PA5) to the LAYOUT gate section.

### Tests

Parser tests for each new key and type, including every rejection (`order`
with pads, `loop` with two subjects, unknown direction), warning emission,
and brief rendering of `guidance`.

---

## PA4 — Reference layout patterns (capture, bind, check)

**Goal:** a vendor reference layout becomes a reusable file; the tool binds it
to real references through the netlist and measures the board against it.

New file `pcbforge/patterns.py`. Catalog directory `patterns/` in this repo
with a `patterns/index.md` table (id, part match, fidelity, source, captured
date), maintained by hand like `modules/index.md`.

### Pattern file schema (`pattern_schema: 1`)

```yaml
pattern_schema: 1
id: drv8316ct                      # kebab-case, equals the file stem
part:
  partnumber_match: "^DRV8316[CR]T"   # regex, full match against footprint property "Partnumber"
  footprint_match: "VQFN-40"          # regex, search in the footprint name; sanity check only
fidelity: sketch                    # exact | sketch
source:
  document: "DRV8316 datasheet, Layout Example figure"   # exact citation
  layers: 4
  captured: 2026-09-02
  notes:
    - "EVM is four-layer; on two layers keep the GND pour on the back."
frame: >-
  Offsets are in the anchor footprint's local frame with anchor rotation 0,
  x to the right, y downward, origin at the footprint (at x y).
roles:
  - id: vm-bypass-1
    anchor_pads: ["9"]              # satellite must share a net with every listed anchor pad
    satellite_pads: 1               # default 1: how many satellite pads must land on those nets
    footprint_match: "^Capacitor_SMD"   # optional regex, search in footprint name
    side: same                      # same | opposite (relative to the anchor)
    # exact fidelity only:
    offset_mm: [-4.2, 1.25]
    rotation_deg: 90
    tolerance_mm: 0.5               # default 0.5
    # sketch fidelity only:
    near_side: west                 # west | east | north | south, in the anchor frame
    max_mm: 2.0                     # centre-to-centre from the first anchor pad to the nearest satellite pad on that net
    rationale: First high-frequency VM bypass directly at pin 9.
rules:
  - id: ep-thermal-vias
    type: vias-under-pad
    anchor_pad: "41"
    min_count: 9
    rationale: Exposed-pad heat path to the back copper.
  - id: gnd-pour-back
    type: note
    text: Solid GND pour under the package on the opposite layer.
```

Validation: unknown keys fail; `exact` roles need `offset_mm` and
`rotation_deg` and forbid `near_side`/`max_mm`; `sketch` the reverse; ids
kebab-case and unique; `id` equals the file stem.

### Binding into `placement.yaml`

Group gains an optional block:

```yaml
  - id: motor-power-stage
    pattern:
      id: drv8316ct
      anchor: U2
      bind:                 # optional explicit overrides, role → reference
        vm-bypass-1: C3
        vm-bypass-2: C4
```

Lookup order for `id`: `<project>/patterns/<id>.yaml`, then
`<tool_root>/patterns/<id>.yaml`. Missing → input error.

Resolution algorithm, deterministic, in `patterns.bind(pattern, anchor_ref,
board_evidence, overrides)`:

1. The anchor must be in the group, must match `partnumber_match` against
   its `Partnumber` property (read via PA1 properties), and its footprint name
   must search-match `footprint_match`. Either miss → input error.
2. For each role, `nets = {net of anchor pad p for p in anchor_pads}`. A
   candidate is any non-anchor footprint in the **same group** with at least
   `satellite_pads` pads on nets in `nets`, that also matches
   `footprint_match` when given, and is not yet bound.
3. Exactly one candidate → bound. Zero → role `unbound` (warning). More than
   one → input error naming the candidates and telling the author to add
   `bind:`. Overrides in `bind:` are applied first and must name a reference
   in the group that satisfies the net test.
4. Roles are processed in file order so an override for one VM bypass leaves
   exactly one candidate for the other.

Binding results (`role → ref | unbound`) are rendered in the brief as a table
under the group, and the pattern file's bytes plus the resolved binding JSON
join `_contract_fingerprint` under a `\0patterns\0` label. `brief_inputs`
lists the pattern file(s) so the dashboard shows them.

`check-layout-handoff` warns on: unbound roles, `source.layers` ≠
`spec.layers`, a `sketch` pattern (reminder that it cannot be applied).

### Check (extends PA2)

For each bound role, one `pattern` finding:

- `exact`: expected absolute position = anchor transform (PA1 formula) of
  `offset_mm`; expected rotation = anchor rotation + `rotation_deg` mod 360;
  expected side per `side`. Measured = distance between expected and actual
  footprint origin, rotation delta, side. Pass when distance ≤
  `tolerance_mm`, rotation delta ≤ 1°, side matches.
- `sketch`: measured = centre distance from the first anchor pad to the
  nearest satellite pad on the shared net, and which side of the anchor the
  satellite centre lies on in the anchor frame. Pass when distance ≤ `max_mm`
  and side equals `near_side`.
- `vias-under-pad` rule: count vias whose centre lies inside the anchor pad's
  box and whose net equals the pad's net. Pass when ≥ `min_count`.
- `note` rules: `manual`.

### Kinetic-tile pilot (step 9)

1. Write `docs/layout-research.md` for U2 (DRV8316CT), U1 (STM32G4), U4
   (THVD1450), U5 (TLV75533), J1/J2 (JST VH), J5 (Cortex Debug). Cite the
   actual datasheet sections you read; do not invent section numbers.
2. Capture `patterns/drv8316ct.yaml` at `sketch` fidelity from the DRV8316
   datasheet layout example: roles for VM bypass ×2 (pads 9, 10), CP
   reservoir (8), flying cap (6, 7), AVDD (25), VREF (37), buck inductor (5),
   buck output cap (via L1 net — note this role's anchor is L1, not U2, so
   express it as a `proximity` constraint instead; patterns anchor on one
   part only). Rules: `vias-under-pad` on pad 41, pour note.
3. Add the `pattern:` block to the `motor-power-stage` group with explicit
   `bind` for the two VM bypasses. Add `source:` to every existing
   proximity constraint (they all cite TI guidance today).
4. Run `prepare-layout`, `check-layout-handoff`, then
   `pcbforge status review --cascade` and renew per `WORKFLOW.md`; the
   handoff fingerprint changes because `placement.yaml` changed. Present the
   packet to the user; do not approve on their behalf.
5. Run `check-placement --write-report`; commit the report.

### Tests (`tests/test_patterns.py`)

Pattern parsing and every rejection; binding: unique, unbound, ambiguous,
override, wrong anchor part; fingerprint changes when the pattern file
changes; check findings for exact and sketch roles with rotated anchors on
both sides; `vias-under-pad` counting.

---

## PA5 — `pcbforge apply-pattern` (requested spatial edit)

**Goal:** the user places the anchor; the agent, on explicit request, stamps
the bound satellites of an `exact` pattern around it.

CLI: `pcbforge apply-pattern --group <id> [--dry-run] [PROJECT_DIR]`.

### Preconditions (all fail loud, exit 2)

1. LAYOUT is the current phase and the `layout:handoff` approval is current
   (reuse the check used by `status mark layout ai-assisted`, `status.py`
   ~line 5325).
2. Pattern fidelity is `exact`. Every role to be moved is bound.
3. The anchor is not at the stacked-at-origin position from PA2.
4. For every satellite, the required side equals its current side. Flipping
   is out of scope: print `flip <REF> to the back side in KiCad, then rerun`.
5. The board file's sha256 equals the one in the latest `placement` check
   record when one exists — otherwise print a warning that the board changed
   since the last check and continue.

### Steps

1. Read geometry (PA1) and binding (PA4). Compute each satellite's target
   `(x, y, rotation)` with the PA1 transform.
2. `--dry-run`: print a table `ref | from (x, y, r) | to (x, y, r) | Δ mm` and
   exit 0 without touching anything.
3. Copy `<name>.kicad_pcb` to `layout-backups/<name>-<UTC timestamp>.kicad_pcb`
   (create the directory; it is git-ignored). Print the path.
4. Edit the board text in place per step 6. Write atomically via
   `fsutil`/`_commit_outputs` semantics.
5. Re-read with PA1 and `read_board_evidence`; assert
   `board_topology_bytes` is unchanged and each moved footprint is within
   0.001 mm and 0.01° of its target; else restore the backup and fail with
   exit 1.
6. Byte-level edit rule for one footprint block: locate the block by its
   `(property "Reference" "REF"` line, bounded by the `\t(footprint ` that
   precedes it and the next `\n\t)\n`. Within that block:
   - replace the first `(at X Y[ R])` that appears **before** the first
     `(property` line with the new `(at x y r)`, omitting `r` when it is 0
     and formatting numbers with up to 6 decimals, trailing zeros stripped
     (KiCad's own style);
   - for every other `(at dx dy A)` in the block that has a third number
     (pad, property, and `fp_text` entries), replace `A` with
     `(A + Δrotation) mod 360`, same formatting. Entries with only two
     numbers stay as they are.
   - change nothing else. **Before relying on this**, rotate one part by 90°
     in KiCad 9, save, and diff: confirm that child `(at ...)` angles shift by
     the same delta. Record the confirmation in the test docstring.
7. Print the concrete delta and remind the agent to run
   `pcbforge status mark layout ai-assisted --note "<request; changes>"`. The
   command does not record the event itself; the note is the agent's.

### Tests (`tests/test_apply_pattern.py`)

Dry-run output; backup creation; moved coordinates and angles re-read via
PA1; topology unchanged; the untouched remainder of the file is byte-identical
(compare with the block ranges removed); refusal cases 1–4; restore on
verification failure.

---

## PA6 — `pcbforge sketch-placement` (floorplan variants)

**Goal:** before detailed placement, show two or three coarse floorplans with
visible tradeoffs; the user picks one and it becomes a checkable part of the
contract.

CLI: `pcbforge sketch-placement [--variants 3] [--seed 1] [PROJECT_DIR]`.
Writes `docs/placement-sketch.md` and `docs/placement-sketch-<A|B|C>.svg`.
Never touches the board or `placement.yaml`.

### Inputs

- Groups and constraints from `placement.yaml`; board size from the outline
  bbox when present and non-degenerate, else `spec.board_mm`.
- Group area: `sum(box.area) × 1.8` (routing margin, fixed). Group rectangle
  aspect = clamp(aspect of the group's largest footprint box, 0.5, 2.0).
- Fixed items: any group whose every reference is a `MountingHole*`
  footprint at a non-origin position is fixed at its current centroid. Nothing
  else is fixed.
- Netlist: for each pair of groups, the number of nets with pads in both
  groups (from `BoardEvidence.pad_nets`).

### Cost, evaluated on group rectangles

| Term | Weight | Definition |
|---|---:|---|
| overlap | 100 | total overlapped area between rectangles, mm² |
| out of bounds | 100 | area outside the board rectangle, mm² |
| proximity | 10 | for each `proximity` constraint whose endpoints are in different groups: max(0, rect distance − max_mm) |
| separation | 10 | analogous, max(0, min_mm − rect distance) |
| board-edge | 10 | for each `board-edge`: distance of the subject's group rectangle side to the named board side (min over sides for `any`), minus max_mm, clamped at 0 |
| order | 10 | 1 per violated pair in a group-level `order` constraint (subjects mapped to groups) |
| wirelength | 1 | Σ shared_nets(g1,g2) × centre distance, mm |
| compactness | 0.1 | area of the bounding box of all rectangles, mm² |

### Solver

Simulated annealing, pure Python, `random.Random(seed + variant_index)`.
State = centre of every non-fixed rectangle on a 1 mm grid inside the board.
Moves: shift one rectangle by ≤ 5 mm, swap two rectangles, rotate one
rectangle 90° (swap w/h). 20 000 iterations, geometric cooling from T=50 to
T=0.1. Run `--variants` times with different seeds. Discard a variant whose
rectangle centres are all within 3 mm of an earlier variant's; rerun with the
next seed up to 3 extra times.

### Output

`docs/placement-sketch.md`: for each variant, the SVG (inline `<img>` link),
the cost breakdown table, and a "tight constraints" list: every constraint
whose term is nonzero or whose slack is under 1 mm, with the number. Then a
ready-to-paste YAML block:

```yaml
floorplan:
  variant: B
  seed: 2
  board_mm: [60, 60]
  groups:
    - id: motor-power-stage
      rect_mm: [x, y, w, h]      # top-left corner and size, board-relative, y down
```

The agent pastes the chosen block into `placement.yaml` (top-level `floorplan`
key, optional). Parser: every group listed exactly once, rectangles inside
`board_mm`. It joins the handoff fingerprint by virtue of being in the file.

SVG: board rectangle, one labelled rectangle per group, fixed groups hatched,
constraint lines between rectangle centres for proximity (green) and
separation (red). Plain hand-written SVG, no library; keep it under 20 lines
of generator code per element type.

### Check (extends PA2)

When `floorplan` is present: one `floorplan` finding per group. Measured =
share of the group's footprints whose box centre lies inside its rectangle
and the centroid's distance outside the rectangle (0 when inside). Pass when
every footprint centre is inside. Also one finding comparing the outline bbox
to `floorplan.board_mm` (pass within 0.5 mm).

### Tests (`tests/test_sketch_placement.py`)

Cost terms individually on hand-built states; the solver reaches zero overlap
and zero out-of-bounds for a four-group toy board within the iteration budget
(seeded, deterministic); variant de-duplication; YAML block parses back; SVG
is well-formed XML; floorplan findings.

---

## PA7 — `pcbforge apply-floorplan` (requested spatial edit)

**Goal:** first-pass placement from the adopted floorplan, on request.

CLI: `pcbforge apply-floorplan --groups <id>[,<id>...] [--dry-run] [PROJECT_DIR]`.
Requires `floorplan` in `placement.yaml` and the same preconditions 1, 3
(anchor check not applicable), and 5 as PA5. Moves only the listed groups'
footprints, all of them, including ones the user already positioned; say so
in the help text.

Placement inside a rectangle: sort references by box area descending; place
the largest at the rectangle centre; place the rest on a greedy grid outward
from the centre with 0.5 mm gaps, keeping each footprint's current rotation
and side; if the rectangle is full, continue outside it and report which
references spilled. Reuse the PA5 byte-level edit, backup, verification, and
reporting steps exactly.

Tests mirror PA5 plus the spill report.

---

## Docs (with each workstream, not at the end)

- `WORKFLOW.md`: LAYOUT row gains "advisory placement check"; a sentence
  under "4–7" that `apply-pattern` and `apply-floorplan` are requested
  spatial edits under the existing assist rules; CIRCUIT completion contract
  mentions `docs/layout-research.md`.
- `README.md` repo map: `patterns/` row.
- `agent/operating-manual.md` LAYOUT assist: the two commands are the
  preferred way to do requested placement; they perform the backup step
  themselves; the `ai-assisted` mark is still the agent's job.
- `agent/layout-handoff.md`: schema additions (PA3, PA4, PA6), sketcher step
  between writing `placement.yaml` and `prepare-layout` (optional, recommended
  for boards over 20 footprints), procedure step 1 reads the research file.
- `agent/circuit.md`, `agent/spec-interview.md`: PA3.
- `pcbforge/initialize.py` `AGENTS.md` text: every new command and file.
- `DESIGN.md`: one short "Placement assistance" decision entry dated
  2026-09-02 summarising the Decisions table above.

## Order and definition of done

PA1 → PA2 → PA3 → PA4 (incl. kinetic-tile pilot) → PA5 → PA6 → PA7. One PR
each. A workstream is done when its tests pass, the full suite is green, the
docs above are updated, and for PA2/PA4/PA5 the command has been run against
the kinetic-tile board with the output pasted into the PR description.
