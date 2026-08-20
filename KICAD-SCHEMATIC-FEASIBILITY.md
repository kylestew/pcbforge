# Feasibility: CIRCUIT review as a KiCad schematic instead of SVG

Date: 2026-08-20. Question: replace the schemdraw SVG review diagram with a
generated `.kicad_sch` the user can open in eeschema while hand-laying the
board. Can a mid/high AI produce it inside the process, and what does it cost?

**Verdict: yes, feasible, same authoring model as today — AI writes a short
placement/wiring script, a deterministic tool does geometry, serialization,
symbol embedding, ERC, and netlist proof. Raw S-expression authoring by the
AI: no.** Proven locally with a 112-line spike (`pilots/kicad-sch-spike/`):
8-part LDO+LED circuit, ERC 0/0, readable, 3 iterations, KiCad 10.0.3.

---

## 1. What exists today (relevant facts)

- Model before the diagram: `review/circuit/circuit.yaml` — components
  (`reference, kind, value, footprint, mpn, lcsc, purpose`), nets (`id,
  display_name, compiler_name, nodes=REF.PIN`), groups, paths.
  `pcbforge/circuit_review.py:390-576`. Pin-to-net is already exact and
  unique. This is a netlist plus grouping — everything a schematic needs
  except geometry.
- SVG contract: schemdraw script → `ReviewDiagram.save()` → validate
  (`data-*` coverage, model SHA, review-only marker, collision lint)
  `circuit_review.py:736-849`, `diagram.py:273-901`. Consumers: humans + the
  checker. Nothing downstream reads it.
- Layout: atopile 0.15.7 writes `.kicad_pcb` directly with nets → ratsnest
  already exists for hand layout. No netlist file anywhere. KiCad pinned 9.x
  (atopile can't read KiCad-10 boards, `DESIGN.md`). Local machine has
  KiCad 10.0.3; format version `20250114` (KiCad 9) loads in both.
- History: `DESIGN.md:201-210` Option D "AI generates `.kicad_sch`" was
  parked as fallback — "graphics medium has no types, no assertions, no
  reviewable diffs — ERC and vibes." That objection was about *capture*. This
  proposal keeps capture in atopile + `circuit.yaml`; the schematic is a
  derived review artifact, same role the SVG has now. The objection no longer
  applies.
- Deleted `pcbforge/schematic.py` (946 lines, commit `b1db6d2`) parsed
  human-drawn KiCad netlists; it never generated. Nothing to resurrect.
- Sizes: pilots Blinky/Temper ≈10–40 parts; mixer board 114 footprints / 81
  nets; Roamer 70 footprints.

## 2. Format facts that matter (verified against KiCad 10 + dev-docs)

Spec: https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/

| Item | Rule | Failure if wrong |
|---|---|---|
| Header | `(kicad_sch (version 20250114) (generator ..) (uuid ROOT) (paper ..))` | "Failed to load" |
| `lib_symbols` | Copy full `(symbol "R" ..)` subtree from stock `.kicad_sym`, rename top-level to `"Device:R"`; **sub-units `R_0_1`/`R_1_1` stay unqualified**; `(extends "X")` symbols must be flattened | "Failed to load" / symbol not found |
| Instances | `(instances (project "NAME" (path "/ROOT_UUID" (reference "R1") (unit 1))))` — path = root sheet UUID | `R?` references |
| Grid | Everything on 1.27 mm; pin tip = lib `(at x y)` rotated, **Y inverted** (lib is Y-up, sheet Y-down) | ERC pin not connected |
| Wires | `(wire (pts (xy..)(xy..)))`, orthogonal; `(junction (at ..))` at T's | ERC / visual ambiguity |
| Labels | `label` / `global_label` attach by exact coordinate on wire end or pin tip | ERC label dangling |
| Power | `power:GND`, `power:+3V3`; needs `power:PWR_FLAG` on externally-driven rails | ERC power_pin_not_driven |
| `sheet_instances` | `(sheet_instances (path "/" (page "1")))` | page numbering |

Stock libs: `/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols/`
(223 files). CC-BY-SA 4.0 with exception for generated designs — embedding
is fine.

Headless loop, all via `kicad-cli` (already a pinned dep in `fab.py`):

```
kicad-cli sch erc --format report|json --exit-code-violations
kicad-cli sch export svg|pdf            # AI can look at it (rsvg/magick → PNG)
kicad-cli sch export netlist --format kicadsexpr   # round-trip proof
```

## 3. The spike (`pilots/kicad-sch-spike/`)

- `ksch.py` (74 lines): `place(ref, lib, sym, value, x, y, rot)`, `wire(*pts)`,
  `junction`, `label`, `power`, `text`, `save`. Extracts symbols from stock
  libs, flattens `extends`, parses pin tips, rotates/mirrors, snaps grid.
- `spike2.py` (38 lines): the "agent" script — J1 → C1 → AMS1117 → C2 → R1 →
  LED, VBUS/+3V3/GND rails, PWR_FLAGs.
- Iterations: (1) "Failed to load" — I qualified sub-unit names and left a
  bogus instances path; (2) ERC 4 errors — hard-coded LED pin offsets wrong,
  fixed by parsing pins from the lib; (3) ERC 2 errors — missing PWR_FLAG.
  Then 0/0. Every mistake was caught mechanically and the message pointed at
  the fix. This is the key evidence for "an AI can do this in-process".
- Render (`render.png`): readable at first placement. Residual warts: D1
  ref/value overlap after 90° rotation; J1 value sits on pin 2 — exactly what
  the current SVG collision lint catches, so that lint must be ported.
- Netlist export reproduced the three nets with correct `REF.PIN` nodes —
  i.e. `circuit.yaml` nets ↔ exported netlist equality is a *stronger* gate
  than today's `data-net-id` coverage.

## 4. What an AI actually has to do per project

Identical in shape to the schemdraw playbook (`agent/circuit-svg.md`):
author `review/circuit/circuit_schematic.py` that places symbols and draws
wires by semantic helpers, run `pcbforge render-circuit`, read ERC + lint +
PNG, iterate. Differences:

| Task | SVG today | KiCad proposal | AI difficulty |
|---|---|---|---|
| Pick symbol | schemdraw element | `Device:R`, `Regulator_Linear:AMS1117-3.3`, … (tool suggests from `kind`/`mpn`) | low; fallback needed when MPN absent from stock libs |
| Place | relative (`.right()`, `.at()`) | absolute mm on 1.27 grid, or a relative helper layered on top | same once helper exists |
| Wire | implicit from element chaining | explicit orthogonal point lists; junctions inferred by tool | slightly higher; tool can auto-stub 2-pin neighbours |
| Labels | free text | net labels on pin tips for non-local nets | low |
| Bind to model | `data-*` attrs | tool fills `Reference/Value/Footprint` + custom `pcbforge_group`/`pcbforge_purpose` properties from `circuit.yaml` | zero (mechanical) |
| Prove coverage | attr set equality | `kicad-cli` netlist ≟ `circuit.yaml` nets, per pin | zero (mechanical), stronger |
| Readability | collision lint | port lint to sch geometry + ERC | same |
| Reviewed paths | `data-path-id` + legend | coloured wire strokes `(stroke (color r g b a))` + text legend | low |

Scale judgement:
- ≤40 parts, flat sheet: mid-tier model fine with helper + feedback loop
  (spike-grade difficulty ×5; 3–6 iterations expected).
- 70–120 parts: one sheet per `circuit.yaml` group (groups already
  mandatory, every part in exactly one). Hierarchical sheets with
  hierarchical labels at group boundaries; each sheet is again ≤40 parts.
  Tool generates the root sheet + sheet symbols mechanically. Mid-tier OK
  per sheet; high-tier for sensible inter-group label naming.
- Raw S-expr by the model with no helper: not recommended at any tier —
  published failure modes are exactly the ones I hit (Y inversion, off-grid,
  missing instances, bad lib_symbols).

## 5. Cost to build the tool (`pcbforge/kisch.py` or similar)

| Piece | LOC est. | Notes |
|---|---|---|
| S-expr writer (spike ×3: multi-unit, mirror, properties, text, sheets) | 300–400 | done 25% in spike |
| Symbol resolver: stock-lib lookup, `extends` flattening, power symbols, cache | 150 | |
| **Generic symbol generator** for parts not in stock libs (box with pins from `circuit.yaml` nodes / atopile pin map) | 150–250 | unavoidable; JLC-basic parts often missing from stock libs |
| Model binding: properties from `circuit.yaml`; SHA as sheet text/property | 80 | |
| Netlist round-trip check: run `kicad-cli sch export netlist`, parse, compare to model nets (reuse `_compare_to_compiled` shape, `circuit_review.py:898-1010`) | 150 | replaces `data-*` coverage gate |
| ERC gate: `kicad-cli sch erc --format json`, fail on errors, allowlist | 60 | |
| Readability lint on sch geometry (text bbox vs wires/symbols, overlapping wire runs, ambiguous crossings) — port of `diagram.py:673-901` | 250 | symbol bboxes from lib graphics; text metrics ≈ 1.27 mm font |
| Hierarchical sheets per group (root sheet, sheet pins, `sheet_instances`) | 200 | phase 2 |
| Relative-placement helper (`right_of(ref, gap)`, `rail(y)`, `stub_label`) to keep agent out of coordinate arithmetic | 150 | strongly recommended |
| Playbook rewrite `agent/circuit-svg.md` → `circuit-kicad.md`; tests | — | |
| **Total** | **~1.5–1.8k LOC** | vs ~900 in `diagram.py` today |

Roughly 2–3× the current diagram module. No new Python deps: stock libs +
`kicad-cli` subprocess. Do **not** take kiutils (unmaintained, KiCad ≤7),
kicad-skip (clone-only symbols). `kicad-sch-api` (circuit-synth, Nov 2025)
does most of the writer half but is unverified on KiCad 9/10 and pulls its
own conventions; a 400-line owned writer is the lower-risk path given
PCBForge already owns `.kicad_pcb` parsing.

## 6. Risks / design constraints

1. **Two sources of truth in one project dir.** If the `.kicad_sch` sits next
   to atopile's `.kicad_pcb` under the same `.kicad_pro`, F8 "Update PCB from
   Schematic" will clobber the compiler-owned board. Keep the schematic in
   `review/circuit/` with no `.kicad_pro`, or a separate throwaway `.kicad_pro`.
   Cost: no cross-probing sch↔pcb. Cross-probe would need matching symbol
   UUID paths to atopile's footprint `path` fields — possible later, not v1.
2. **Pin numbering parity.** `circuit.yaml` nodes use pin *numbers*; stock
   symbols use the same numbers as their footprint pads, but pad names on
   atopile-generated footprints (e.g. `A`/`K`, `1`..`n` vs `G/D/S`) may not
   match the stock symbol. Resolver must map via the compiled footprint pad
   list (`build_test.read_board_evidence`) and fail loudly.
3. **KiCad version skew.** Write `20250114`; loads in 9 and 10. Don't use
   10-only features. Note the repo pins 9.x while this machine has 10.0.3.
4. **Readability is still the art.** Serialization is solved; a good drawing
   still comes from the agent's placement choices. The lint + PNG loop is
   what made the schemdraw pilot work and must be ported, not dropped.
5. **Stock-lib gaps** for JLC-basic MPNs force generic box symbols; fine for
   review, uglier than SVG.
6. **Loss of `data-*` semantic bindings.** KiCad has no DOM; replace with
   custom symbol properties (hidden) + netlist equality. Anything that wanted
   to build a UI review packet from the SVG (`PROCESS-REVIEW.md:266-268`)
   loses that hook, gains a real EDA file.

## 7. What the user gains vs SVG

- Open in eeschema next to pcbnew; zoom, net highlight, find-by-ref, print
  to PDF; familiar symbols.
- Stronger proof (netlist equality per pin) than attribute coverage.
- Diffable text file; ERC as an extra reviewer.
- Optional later: cross-probe, BOM export, SPICE.

What it does not gain: ratsnest (already there from atopile); footprint
assignment (atopile owns it).

## 8. Alternatives considered

| Option | Verdict |
|---|---|
| **A. Owned writer + agent placement script** (spike) | Recommended. Same authoring model as today; ~1.5k LOC. |
| B. SKiDL 2.3.0 `generate_schematic()` (auto place+route, KiCad 6–10, Jul 2026) | Worth a 1-day trial: feed `circuit.yaml` → SKiDL → sheet. Historic readability poor; 2.2–2.3 claim fixes. If good, removes agent placement work entirely. Adds a 1.6k-star dep + its own part/lib model. |
| C. Label-only schematic (every pin stub + net label, zero routing, as circuit-synth does) | Trivial, ERC-clean, unreadable as topology. Acceptable as automatic fallback when lint can't be satisfied, not as the review artifact. |
| D. Keep SVG, additionally export netlist-only `.kicad_sch` | Halves the benefit; user still can't read the schematic in KiCad. |
| E. Generate both SVG and KiCad from one placement script | Doubles lint/geometry surface; no. |

## 9. Recommended path

1. Phase 0 (½ day): try SKiDL on Blinky-sized `circuit.yaml`. If readable
   with ≤ few manual hints → Option B, skip the writer.
2. Phase 1 (3–5 days): owned writer + resolver + generic symbol + ERC +
   netlist equality + lint port. Flat sheet only. Rewrite playbook. Re-run
   the Blinky readability pilot.
3. Phase 2 (2–3 days): group → hierarchical sheets; coloured reviewed paths.
4. Keep SVG code until one pilot passes with KiCad, then delete
   `diagram.py` per the v1 clean-break habit.

## Unresolved questions

- Accept no cross-probe in v1 (sch outside project dir)?
- Pin-name mismatch policy: generic box symbol always, or stock symbol when
  pads match?
- Spend ½ day on SKiDL trial first, or go straight to owned writer?
- KiCad pin stays 9.x — test gate on 9 in CI, or move to 10?
- Keep `data-*`-style bindings as hidden symbol properties, or netlist
  equality only?
