# KiCad schematic review pilot — Blinky

Date: 2026-08-20. Decision record: `DESIGN.md` (2026-08-20 entry).
Feasibility study that preceded this: `KICAD-SCHEMATIC-FEASIBILITY.md`.

## What was built

`pcbforge/sexpr.py`, `kicad_sym.py`, `kicad_sch.py`, `sch_lint.py`; the
`validate_circuit_schematic` gate in `circuit_review.py`; contract schema 2;
`render-circuit --svg`; playbook `agent/circuit-kicad.md`. The schemdraw SVG
path (`diagram.py`, `circuit-svg.md`, `schemdraw` dep) is removed.

## Spike (this directory)

`ksch.py` + `spike2.py` are the first 112 lines that proved the format:
hand-authored writer, 8-part LDO circuit, ERC 0 on KiCad 10 after three
iterations. Two of its lessons became tool rules:

- KiCad **9** refuses symbols copied from KiCad **10** libraries
  (`duplicate_pin_numbers_are_jumpers`, `in_pos_files`, `show_name`,
  `do_not_autoplace`). The resolver reads the pinned KiCad 9 bundle via the
  `scripts/kicad-cli` shim.
- KiCad 9 does not treat a junction in the middle of a wire run as a
  connection; KiCad 10 does. The writer splits every run at junctions, pin
  tips and label points.

## Blinky acceptance run

Project: `~/Projects/ai-pcb-attempts/blinky` — 17 components, 24 nets (12
single-pin `NC_*`), 6 groups, 12 reviewed paths. Migration: `.pcbforge`
guidance `circuit_review_schema: 2`, `circuit-review.yaml` schema 2 with
`schematic:`, `circuit.svg` removed, `review/circuit/circuit_schematic.py`
authored (≈120 lines).

| Iteration | Result | Cause |
|---|---|---|
| 1 | ERC: two PWR_FLAGs on one net | my C2 placement arithmetic put its body on the VDD wire |
| 2 | netlist parity: BUTTON merged into GND; `/NAME` net names | C3's GND symbol landed exactly on the BUTTON label point (tool now lints `label-meets-power-symbol` and reports lint warnings with gate failures); gate now strips the root-sheet `/` prefix |
| 3 | ERC 0, parity OK, 14 lint warnings | text collisions from tight placement |
| 4–5 | 2 → 0 warnings | spacing |
| 6 | 9 warnings | tool: text width recalibrated 0.62 → 0.85 × size (measured from the render), property text now counts toward group extents |
| 7–8 | 1 → 0 warnings | tool: property anchors no longer grid-snapped; script: mid-wire labels for the two LED feed nets |

Final: ERC 0 errors (KiCad 9.0.9 and 10.0.3), exported netlist equals the
model pin for pin, 0 readability warnings, byte-identical re-render,
`check-circuit-review --stage proposal|final --write` both pass (final
includes compiled BOM/board parity). Symbol policy: 16 stock symbols, 1
generated box (J1: the ARM10 footprint omits pad 7 as the key, so no stock
connector matches; the box carries named, two-row pins).

## Friction worth knowing

- The readability loop is where the time goes, as with the SVG. Five of
  eight iterations were spacing; the lint caught every one before a human
  looked. Zero-warning doctrine held.
- Symbol-pin sides matter for wiring decisions; `Placed.pin()` and
  `pin_outward()` keep the agent out of coordinate arithmetic, but the agent
  still needs the pin layout (the resolver's `LibSymbol.pins`) to choose
  rotations. The playbook should point at `sch.symbol_for(ref).symbol.pins`.
- KiCad labels connect anywhere along a wire, not only at ends; mid-run
  labels are the cleanest way to name a two-pin local net.
- Rasterized previews: KiCad's SVG embeds searchable `<text>` elements that
  rasterizers draw over the stroke glyphs; the preview strips them.
- `pcbforge status` reopens CIRCUIT after migration (contract, pin and
  evidence bytes changed). Re-approval is the expected cost.
