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
S-expressions. The tool copies the exact stock symbol from the pinned KiCad 9
libraries (or generates a box when the footprint pads differ), computes pin
positions through rotation and mirroring, snaps to the 1.27 mm grid, inserts
junctions, splits wires so KiCad 9 sees every connection, draws the group
boxes and registers, lints readability, runs `kicad-cli sch erc`, and proves
the exported netlist equals the model pin for pin. Raw S-expression authoring
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
- `symbol_choices` — which stock symbol (or generated box) each reference
  got and why. Override with `symbol="Lib:Name"` when the default reads
  badly (`Connector_Generic:Conn_02x05_Odd_Even` for a 2×5 header, for
  instance); the override must still match the footprint pads.
- `missing_component_symbols` — connected references with no placed symbol.

Output is deterministic for a given model and script; re-rendering without
changes leaves the approved bytes alone.

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
| `sch.group_box(group_id, (x1, y1, x2, y2))` | override the automatic group rectangle |
| `sch.pin_names(ref, {"A": "anode"}, sides={"A": "left"})` | name and side pins of a generated box (call before `place`) |
| `sch.symbol_for(ref).symbol.pins` | the resolved symbol's pin table (number, name, lib position, direction) — read it before choosing a rotation |

All identifiers are model IDs; a typo fails immediately. `Reference`,
`Value`, `Footprint`, group and purpose fields come from `circuit.yaml` and
cannot be overridden.

## Layout doctrine

- Sheet coordinates are millimetres, y down, 1.27 mm grid. Put the first
  part at roughly `(40, 60)` and let relative placement do the rest.
- One rectangular region per model group; the tool draws the box and title
  from the placed members. Leave ≥ 10 mm between regions so boxes never
  touch. Wires must not cross a group box: connect groups with net labels.
- Power flows left to right: entry connector at the left, protection, then
  regulation; rails leave as power symbols. MCU in the middle, peripherals
  right, debug and mechanical parts at the bottom.
- Rails: one `rail`/`drop` per supply inside a group, `power_at(...)` once
  per rail with `flag=True` where the rail is driven from outside the sheet
  (battery, USB, connector). Ground symbols point down, rail arrows up.
- Draw local support parts (bypass, pull-ups, filters, current limits) with
  wires at the device pins they serve. A label is not a substitute.
- Labels for everything that leaves a group, using the model net id; the
  sheet shows the compiler name (what pcbnew's ratsnest and net highlight
  use) and the net register lists the model display name beside it. A
  label may sit anywhere along a wire
  (`label_at` at a run's midpoint names a local two-pin net without
  crowding the symbols).
- Use `path="<path-id>"` on the wires that realise each model path; the
  legend colours them. Every model path needs at least one such wire.

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
   (one per unit for multi-unit symbols).
5. Externally driven rails need `flag=True` once, or ERC reports
   `power_pin_not_driven`.
6. A wire that passes over a pin tip connects to it in KiCad. The lint
   reports `wire-passes-pin`; reroute.
7. Mechanical parts (mounting holes) still get a symbol; the stock
   `Mechanical:MountingHole` has no pins and no wires.

## Iterate until proven

1. Run `pcbforge render-circuit --svg`. Fix every `SchematicError`
   (unknown IDs, unplaced parts, undrawn paths).
2. Read the ERC list when the gate fails: `pin_not_connected` means a wire
   end is off the pin tip — use `sch.pin()` instead of arithmetic;
   `power_pin_not_driven` means a missing `flag=True`; `label_dangling`
   means a label off a wire end.
3. Drive `warnings` to zero, then look at the preview PNG/SVG — the lint
   estimates text extents and cannot judge comprehension.
4. Continue with Gate A of [`circuit.md`](circuit.md):
   `pcbforge check-circuit-review --stage proposal --write`.
