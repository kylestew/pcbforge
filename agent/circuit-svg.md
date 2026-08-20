<!-- pcbforge-circuit-schema: 1 -->
# CIRCUIT diagram playbook

Use this playbook when Gate A of [`circuit.md`](circuit.md) requires the
explanatory diagram at the contract `diagram` path. The diagram is a real
schematic authored as a Python script on top of schemdraw — never hand-written
SVG, never a KiCad schematic, never a block diagram with prose inside boxes.

## Why a script

Hand-laid SVG fails predictably: free-form coordinates produce text
collisions, block-level wires hide the actual topology, and half the file
becomes tables that belong in the narrative. A schemdraw script keeps the
author working in semantic elements and relative placement — code, not
coordinate arithmetic — while `pcbforge.diagram` generates everything
mechanical: model binding, `data-*` coverage, the review-only marker, the
component/net registers, and the reviewed-path legend.

## The contract

Author `review/circuit/circuit_diagram.py`. It must:

1. construct one `pcbforge.diagram.ReviewDiagram` bound to the project,
   which reads `circuit-review.yaml` for the exact model and output paths:

   ```python
   from pathlib import Path
   from pcbforge.diagram import ReviewDiagram

   PROJECT = Path(__file__).resolve().parents[2]
   diagram = ReviewDiagram(PROJECT, title="...", desc="...")
   ```

2. draw the schematic through `diagram.drawing` and `diagram.elm`;
3. end with `result = diagram.save()` and print the result summary.

Run it with:

```sh
pcbforge render-circuit
```

`save()` validates the output
with the same `validate_circuit_svg` gate `check-circuit-review` uses and
raises on any failure, so a script that exits cleanly has already passed the
SVG side of the proposal check. It also returns:

- `collision_warnings` — estimated label-overlap pairs. Drive this to zero
  by moving labels or symbols; do not ignore warnings without looking at a
  rendering.
- `missing_component_labels` — references never visible in the drawing
  itself. The generated register keeps machine coverage complete either way;
  treat entries here as a prompt to ask whether the part should be drawn.
- `missing_component_symbols` — connected components that have no bound
  schematic symbol. Text notes do not satisfy this audit.
- `warnings` — structured readability warnings for symbols, text, wires,
  crossings, and overlapping wire runs.

The command writes these warnings to the SVG audit record. The CIRCUIT
evidence includes the same warnings. Warnings do not stop the command, but a
review packet must have zero warnings.

The helper appends the review-only marker, the component and purpose
register, the net register, and the reviewed-path legend below the drawing.
Do not draw any of those yourself, and do not place the model fingerprint or
`<title>`/`<desc>` — `save()` stamps them.

## Helper API

- `diagram.section(group_id, (x, y))` — draws the group's title as a section
  header. **Every model group needs exactly one**; `save()` refuses
  otherwise.
- `diagram.component(ref, element)` — adds a real schematic symbol and binds
  it to a model component. Use the same reference for each unit of a multi-unit
  component.
- `diagram.netflag(at, net_id, direction, length, note=...)` — net stub with
  the model display name; `note="TP9"` renders `NAME · TP9`. Use flags for
  every long-distance connection instead of routing wires across the sheet.
- `diagram.testpoint(at, ref, direction, length, note=...)` — test-point
  flag; the reference must exist in the model.
- `diagram.nc(at, net_id, direction)` — intentionally-unconnected stub
  labeled with the NC net name.
- `diagram.note((x, y), text)` — small gray annotation.

All helper identifiers are model IDs; a typo fails immediately instead of
producing an untagged label.

## Layout doctrine

- One section per model group, laid out on a coarse grid of absolute section
  origins. **Section origins are the only absolute coordinates in the
  script**; everything inside a section places relative to pins and element
  ends.
- Power flows left to right: entry connector at top left, regulation top
  middle, rails exit as net flags. MCU center left, peripherals to its
  right, debug and repeated-part arrays at the bottom.
- Connect local support parts with drawn wires (`elm.Line`, `elm.Wire`).
  This rule applies to bypass capacitors, straps, filters, and inductors.
- Use net flags for connections between sections. Do not replace a local
  support circuit with text or disconnected net flags.
- ICs are `elm.Ic` with named `IcPin`s carrying real pin numbers; passives
  are standard symbols labeled `REF\nvalue`.
- Give each repeated signal row a separate wire corridor. Add enough space
  for the symbol, value, net name, and test-point name.

## Hard rules — each one is a defect class observed in practice

1. **Give every `Ic` an explicit `.right()`.** schemdraw elements inherit
   the current drawing direction; without it an IC placed after a downward
   stub renders rotated.
2. **Verticals must not cross a horizontal run of the same section.** When
   two stacked pins each need a hanging resistor, the *upper* pin's resistor
   goes beyond the end of the lower pin's run. Place a pull-up's vertical
   past the span of any run above it.
3. **Join long runs with `elm.Wire('|-')`, not `'-|'`,** so the vertical
   drop lands in the corridor left of the target instead of on its pin
   leads.
4. **Move wire labels off line centers with `ofst`.** The default label
   position is the line's midpoint — exactly where pull-ups and junctions
   get placed.
5. **Repeated small parts get blank pin names.** Use
   `IcPin(name='', pin='4', anchorname='DIN')` and put the reference inside
   the body; visible pin names do not fit repeated 4-pin packages.
6. **Fold test-point names into the adjacent net flag** (`note="TP9"`)
   instead of adding a second stub that collides with the next pin row.
7. **Dot-free crossings are no-connects; junctions always get `elm.Dot()`.**
8. **Add connected components with `diagram.component()`.** A note that
   contains a reference is not a component symbol.
9. **Draw local support circuits at the applicable device pins.** Do not list
   their references in a note below the device.

## Iterate until proven

1. Run the script. Fix every `DiagramError` (missing sections, unknown IDs).
2. Drive `warnings` and `missing_component_symbols` to zero.
3. Rasterize the SVG once (any renderer or a browser) and look at it —
   the lint estimates text extents and cannot judge comprehension.
4. Confirm the printed fingerprint, then continue with Gate A of
   [`circuit.md`](circuit.md): `pcbforge check-circuit-review
   --stage proposal --write`.

The machine checks prove coverage, binding, and safety. Whether the drawing
actually explains the circuit — flow direction, grouping, emphasis — remains
your judgment and the user's review.
