<!-- pcbforge-circuit-schema: 1 -->
# CIRCUIT schematic playbook

Use this playbook when Gate A of [`circuit.md`](circuit.md) requires the
review schematic at the contract `schematic` path. The schematic is a real
KiCad 9 sheet, `<project>.kicad_sch`, generated from a Python script and
installed beside `<project>.kicad_pro` so that eeschema and pcbnew
**cross-probe** it during hand layout: click a footprint and its symbol
highlights, highlight a net in either editor and the other follows. Atopile
still owns the board. The sheet is never hand-drawn, never edited or saved
from KiCad, and never used to update the board — the gates refuse both.

## Why a script

The agent writes `review/circuit/circuit_schematic.py` against
`pcbforge.kicad_sch.ReviewSchematic`: semantic placement and wiring, no
S-expressions. The tool copies the exact official symbol from the pinned
KiCad 9 libraries (a generated box only when no official symbol fits the
footprint pads), computes pin positions through rotation and mirroring,
snaps to the 1.27 mm grid, inserts junctions, splits wires so KiCad 9 sees
every connection, writes the group titles and registers, lints readability,
runs `kicad-cli sch erc`, and proves the exported netlist equals the model
pin for pin. Raw S-expression authoring
fails in known ways (inverted Y, off-grid pins, missing `instances`, wrong
library syntax); the script never touches any of that.

## The contract

Author `review/circuit/circuit_schematic.py`. It must:

1. construct one `ReviewSchematic` bound to the project, which reads
   `circuit-review.yaml` for the exact model and output paths:

   ```python
   from pathlib import Path
   from pcbforge.kicad_sch import ReviewSchematic

   PROJECT = Path(__file__).resolve().parents[2]
   sch = ReviewSchematic(PROJECT, title="...", desc="...")
   ```

   `group_boxes=True` adds the dashed rectangle per model group; the
   default draws only the group titles so wires can follow the circuit.

2. place every model component and wire or label every multi-pin net;
3. end with `result = sch.save()`.

Run it with:

```sh
pcbforge render-circuit --svg
```

`save()` writes `<project>.kicad_sch`, records its hash in
`review/circuit/schematic.audit.json`, registers the root sheet in
`<project>.kicad_pro` (`sheets`; every other key untouched), then runs
the same `validate_circuit_schematic` gate as `check-circuit-review` and
raises on any failure: structural binding, ERC errors, and netlist parity.
A script that exits cleanly has already passed the schematic side of the
proposal check. `--svg` exports `review/circuit/preview/circuit.svg` (and
`.png` when `rsvg-convert` or `magick` is installed) so you can look at it.

`result` reports:

- `warnings` — readability lint, each `[code] message`. Drive the list to
  zero; do not ignore a warning without looking at the preview.
- `symbol_choices` — which official symbol (or generated box) each
  reference got, which pads it was checked against, and every candidate
  that was rejected and why. The same record lands in the audit. Override
  with `symbol="Lib:Name"` when the default reads badly
  (`Connector_Generic:Conn_02x05_Odd_Even` for a 2×5 header, for instance);
  the override must still match the footprint pads. `symbol="generic"` is
  refused while an official symbol fits.
- `missing_component_symbols` — connected references with no placed symbol.

Output is deterministic for a given model and script; re-rendering without
changes leaves the approved bytes alone.

## Before you place: probe

Never guess which end pin 1 is. Run

```sh
pcbforge render-circuit --probe all      # or --probe U1,D2,J1
```

It prints, per component, the resolved symbol (official or generated box)
with the pad list it was checked against, every pin with its model net, and
the pin-tip offset and side for rotations 0/90/180/270 (plus `mirror="y"`
for connectors). Copy the conventions you rely on into a comment at the top
of the script. Verified defaults:

| Part family | rotation 0 | 90 | 180 | 270 |
|---|---|---|---|---|
| vertical two-pin (R, C, L, fuse) | pin 1 up | pin 1 left | pin 1 down | pin 1 right |
| horizontal two-pin (diode, LED, crystal, solder jumper, TVS) | pin 1 left | pin 1 down | pin 1 right | pin 1 up |
| connector `Conn_01xN` | pins left, pin 1 top | — | pins right, pin 1 bottom | — |
| connector `Conn_01xN`, `mirror="y"` | pins right, pin 1 top | | | |
| test pad | pad up | pad left | pad down | pad right |

Pin 1 of a diode is the cathode, of an LED the cathode, of a polarised
capacitor the positive plate — but the *model nets* in the probe table are
what you wire to, so read the table, not the datasheet.

## Helper API

| Call | Meaning |
|---|---|
| `sch.place(ref, (x, y), rotation=0, mirror=None, symbol=None, unit=1)` | place a model component; returns a `Placed` with `.pin(n)`, `.pins`, `.bbox`, `.left/.right/.top/.bottom/.center` |
| `sch.place(ref, right_of=p, gap=7.62)` / `left_of` / `below` / `above`, `align=(dx, dy)` | place relative to another part's bounding box, origins aligned on the other axis (stacked two-pin parts share a pin axis) |
| `sch.pin(ref, n)` | absolute pin tip, grid-snapped |
| `sch.wire(p1, p2, ..., path=None)` | orthogonal wire through points; junctions and splits are automatic |
| `sch.connect(a, b, route="hv"\|"vh", path=None)` | one-bend Manhattan wire |
| `sch.drop(ref, n, y)` | vertical wire from a pin tip to a rail at `y`; returns the rail point |
| `sch.rail(y, x1, x2)` / `sch.stub(point, direction, length)` | horizontal run / short wire |
| `sch.label(ref, n, net_id, direction=None, length=2.54)` | net label (model `compiler_name`, the board's net name) on a stub from a pin; `length=0` puts it on the tip |
| `sch.label_at(point, net_id, direction)` | net label on an existing wire end |
| `sch.power(ref, n, net_id, direction=None, flag=False)` | rail arrow or ground symbol on a stub from a pin |
| `sch.power_at(point, net_id, direction="up", flag=True)` | power symbol on a rail point; `flag=True` adds `PWR_FLAG` for externally driven rails |
| `sch.no_connect(ref, n)` | no-connect marker; single-node model nets get one automatically |
| `sch.note((x, y), text)` | free text |
| `sch.group_title(group_id, (x, y))` | put the group title here instead of above its placed members |
| `sch.group_box(group_id, (x1, y1, x2, y2))` | draw this group's rectangle at an explicit place (with `group_boxes=True`, overrides the automatic one) |
| `sch.pin_names(ref, {"A": "anode"}, sides={"A": "left"}, pitch=5.08)` | name and side pins of a generated box (call before `place`); `sides` insertion order is the pin order along each side; `pitch=5.08` leaves room for parts hanging from pin rows |
| `sch.symbol_for(ref).symbol.pins` | the resolved symbol's pin table (number, name, lib position, direction) — read it before choosing a rotation |

All identifiers are model IDs; a typo fails immediately. `Reference`,
`Value`, `Footprint`, group and purpose fields come from `circuit.yaml` and
cannot be overridden.

## Layout doctrine

- Sheet coordinates are millimetres, y down, 1.27 mm grid. Put the first
  part at roughly `(40, 60)` and let relative placement do the rest.
- **Connected paths first.** Arrange parts so a reader can follow the
  longest useful functional paths as continuous wires: USB power through
  protection and regulation to the MCU supply, MCU to encoder contacts
  with their pull-up and filter branches, MCU to LED to current-limiting
  resistor, debug header to MCU with the reset network. A wire that a
  reader can trace beats a pair of labels they must search for.
- This is a readability goal, not a wire-length contest. Keep pull-ups,
  filters and decoupling in their real topology (a branch hangs off the
  run; never redraw a branch as a series link). Keep normal power and
  ground symbols.
- Use a net label jump only when continuous wires would cause excessive
  crossings, obscure a relationship, or cross sheets (the USB data pair
  next to a dense MCU is the usual case). Labels may also name a
  continuous run: every multi-pin net needs its compiler name somewhere on
  the sheet (label or power symbol), and `label_at` on a long run does it
  without crowding the symbols.
- Group titles are placed by the tool above each group's parts (move one
  with `group_title`); the group register lists title and purpose. Decorative
  boxes are opt-in (`group_boxes=True`). With boxes, leave ≥ 10 mm between
  regions and expect `wire-crosses-group-box` wherever a connected path
  crosses a boundary.
- Power flows left to right: entry connector at the left, protection, then
  regulation; rails continue as wires where the path is the point and
  leave as power symbols where it is not. MCU in the middle, peripherals
  right, debug and mechanical parts at the bottom.
- Rails: `power_at(...)` once per rail with `flag=True` where the rail is
  driven from outside the sheet or through a passive part (battery, USB,
  connector, the far side of a fuse). Ground symbols point down, rail
  arrows up.
- Draw local support parts (bypass, pull-ups, filters, current limits) with
  wires at the device pins they serve. A label is not a substitute.
- Use `path="<path-id>"` on the wires that realise each model path; the
  legend colours them. Every model path needs at least one such wire.

`tests/connected_fixture.py` is the reference drawing in this style: supply
chain, encoder branches, LED series path, and debug/reset path as wires,
only the USB pair labelled, zero warnings under real KiCad.

## Using it during layout

Open `<project>.kicad_pro` in KiCad 9 (project manager), then both
editors. Selecting a footprint in pcbnew highlights the symbol; `Highlight
Net` in either editor highlights the other; clicking a pin highlights the
pad. Cross-probe is by reference designator, pin number and net name, all of
which the gate proves equal to the board.

## Hard rules

1. Never hand-edit `<project>.kicad_sch` or `schematic.audit.json`; re-run
   the script. Never save the sheet from KiCad — even an untouched save
   rewrites it and the gate reports "modified outside render-circuit".
2. Never run **Update PCB from Schematic** (F8) or **Update Schematic from
   PCB**. Atopile owns the board; both gates and fab-out refuse a board whose
   footprints carry schematic links (`path`/`Sheetfile`). Recover from
   `<project>-backups/` or rebuild with atopile.
3. Open the sheet with KiCad 9, not 10 (format pin).
4. Every group needs ≥ 1 placed part; every component exactly one placement
   (one per unit for multi-unit symbols). Every group title must appear on
   the sheet; the tool places it whether or not boxes are drawn.
5. Externally driven rails need `flag=True` once, or ERC reports
   `power_pin_not_driven`.
6. A wire that passes over a pin tip connects to it in KiCad. The lint
   reports `wire-passes-pin`; reroute. Stubs that go *through* a
   connector's neighbouring pins (a ground stub dropped past pins 3 and 4)
   are the classic case — use a label along the pin row instead.
7. Dense boxes: parts hanging from a pin row cross the rows below them.
   Fan rows out as a staircase (top rows reach furthest) or give each pin a
   column and build the RC chain below the box; keep a ≥ 20 mm column pitch
   when values are shown.
8. Mechanical parts (mounting holes) still get a symbol; the stock
   `Mechanical:MountingHole` has no pins and no wires.
9. Official symbols carry every physical pin. Pins with no model net get a
   no-connect marker automatically; the model still names each unused pad
   as a single-node `NC_<REF>_<PIN>` net, and the gate checks both.

## When the tool says nets are joined

Three messages, in the order you will meet them:

1. `wire … runs through pin X.n` / `pin X.n lands on the wire …` — raised
   at the offending call with its script line. A stub dropped past a
   connector's neighbouring pins, or a part rotated so its far pin sits on
   the wire meant for its near pin. Fix: end the wire at the pin, use a
   label along the pin row, or fix the rotation (check the probe table).
2. `short: model nets A, B are joined — first joined by <element>` — the
   pre-flight connectivity check, before KiCad runs. The named element is
   the bug: a label or power symbol placed on a point that belongs to
   another net, two labels with different nets at one point, or a power
   symbol whose name equals a local label of another net.
3. `schematic has unproposed endpoint sets: NAME {…} — merges model nets:
   …` — KiCad's own netlist disagrees with the model (the authoritative
   check). Rare once 1 and 2 pass; if it happens, export the netlist
   (`kicad-cli sch export netlist --format kicadsexpr`) and bisect by
   deleting one element class at a time.

Fix order: pin orientation first, then stubs through pins, then label /
power-symbol placement. An `open-net` warning means a model net is drawn
as disconnected pieces with no shared label — add the label or the wire.

## When lint lists collisions

Work through them in this order; each step removes whole families:

1. **Group separation** (only with `group_boxes=True`) —
   `group-boxes-overlap`, `symbol-outside-group`, `wire-crosses-group-box`:
   move whole groups apart (≥ 10 mm) before touching parts, or drop the
   boxes when the connected paths matter more. Boxes include the group's
   wires, labels and text.
2. **Big-part text** — set `ref_pos`/`value_pos` for ICs and connectors
   (above the body when the top has no pins, bottom-right otherwise); the
   default right-of-body position collides with right-side pin labels.
3. **Fan-out around dense boxes** — parts hanging from a pin row cross the
   rows below. Use a staircase (top rows reach furthest out, parts hang
   down past rows that never reach their column) or give each pin its own
   column and build the RC chain below the box. Use `pitch=5.08` on
   generated boxes. Keep ≥ 20 mm between hanging parts when values show.
4. **Decoupling rows** — caps along a rail need ≥ 20 mm pitch with values
   (≈ 0.9 mm per character); values like `100nF 50V X7R` are 15 mm wide.
5. **Labels** — a label's text sits above its anchor and extends in its
   direction; place labels on long runs, at run ends, or mid-wire, never at
   a pin tip pointing into the body. Long net names (`MOTOR_CURRENT_U_RAW`)
   need ~20 mm of clear run.
6. **Last resort** — `hide_value=True` on flying/charge-pump caps and test
   pads; the component register keeps the value visible.

A realistic budget is 5–8 renders for 20 parts and 10–15 for 80+; that is
the normal loop, not failure.

## Official symbols and the generated-box fallback

The tool searches the pinned official libraries before drawing a box, for
every kind of part: by exact MPN, by KiCad's `x`-suffixed package wildcards
(`STM32G0B1KBT6` draws as `MCU_ST_STM32G0:STM32G0B1KBTx` while the value,
MPN and LCSC fields keep the exact part), by the footprint's library family
(a `Rotary_Encoder` footprint looks at `Device:RotaryEncoder*`, a
`Connector_USB` one at `Connector:USB_*`), then by kind. A candidate is
accepted only when its pin numbers equal the complete pad list — unused
pins included — taken from the compiled board when it exists, else from the
official footprint the model names (or the project's `src/parts` copy). Pin
count alone is never enough. `symbol_choices` and the audit record the
pads used, every rejected candidate and its mismatch.

Ask for a box (`symbol="generic"`) only when no official symbol fits; the
tool refuses an avoidable box and names the symbol to place instead. Do not
force a similar-but-wrong official symbol to satisfy the preference: a
keyed header without pad 7 is a generated box, and its reason says why.

```python
sch.pin_names("U2", NAMES, sides={**{n: "left" for n in LEFT}, **{n: "right" for n in RIGHT},
                                  **{n: "bottom" for n in GND_PINS}}, pitch=5.08)
```

Name pins from the model nets, order each side deliberately (`sides`
insertion order is the pin order), put all ground pins on the bottom and
drop them to one rail with a single ground symbol, supplies and straps on
the left, signals and outputs on the right.

## Iterate until proven

1. Run `pcbforge render-circuit --svg`. Fix every `SchematicError`
   (unknown IDs, unplaced parts, undrawn paths, wires through pins, joined
   nets) — each names its script line.
2. Read the ERC list when the gate fails: `pin_not_connected` means a wire
   end is off the pin tip — use `sch.pin()` instead of arithmetic;
   `power_pin_not_driven` means a missing `flag=True`; `label_dangling`
   means a label off a wire end.
3. Drive `warnings` to zero, then look at the preview PNG/SVG — the lint
   estimates text extents and cannot judge comprehension.
4. Continue with Gate A of [`circuit.md`](circuit.md):
   `pcbforge check-circuit-review --stage proposal --write`.
