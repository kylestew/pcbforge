# pcbforge — Design

AI-assisted PCB development tool. **Schematic capture happens in code, AI-driven;
the human owns layout and routing — the art.** Compiler + deterministic scripts
own everything mechanical around KiCad board design for JLCPCB fab.

## Constraints (fixed for v1)

- **EDA:** KiCad (layout/routing + fab outputs; capture happens upstream in code)
- **Capture:** circuit-as-code, atopile-class compiler (pilot decides; see Pilot)
- **Fab:** JLCPCB only (assembly via LCSC parts)
- **Boards:** hobby, 2 / 4 layer, medium density
- **MCU family:** STM32
- **Volume:** one-offs (no mass production)

## Philosophy

> **Layout is the art. Everything else is toil or verification.**

- **Layout + routing:** user's time concentrates here. Complex, artistic, black
  magic. AI guides and verifies — never places, never routes. Spotter, not painter.
- **Schematic capture:** toil. Expressed as code; AI writes it primarily, user
  influences via review at chosen depth (architecture always, resistor values
  rarely).
- **The machine gate carries the floor:** the lighter the human gate on capture,
  the heavier the machine gate must be — typed interfaces, assertions, compile
  errors. Nothing structural rides on user attention.
- One residual drawing act: prettifying block schematic renders — presentation
  for future AI-proposed reuse, not capture.

### Actors

| Actor | Role |
|---|---|
| **User** | spec intent, architecture approval, **layout + routing (the art)**, order |
| **AI agent** | spec interview, writes/edits all capture code, part selection, layout spotter, review |
| **Compiler/scripts** | netlist + BOM emission, assertions, electrical checks/DRC, briefs, fab-out — deterministic, free (stock queries excepted: live network) |

## Decision record

- **2026-07-24 — atopile pilot blocked on KiCad 10 PCB parsing.** Pinned
  atopile `0.15.7` successfully builds a KiCad 9 fixture but rejects the same
  KiCad `10.0.3` fixture at the first named-net pad (`(net "2")`), before
  synchronization. The input remains byte-identical after failure. Full Roamer
  translation stops at the compatibility gate; resume only with demonstrated
  KiCad 10 reader/writer support or an explicit compiler-fallback decision.
  Reproduction and evidence: `pilots/roamer-rev-a/REPORT.md`.
- **2026-07-24 — capture ruled toil, layout ruled art.** Consequences:
  - **Option B** (pipeline automator: human hand-draws schematics in KiCad, tool
    automates around them) — **rejected**. Its wall around capture was forced by
    the `.kicad_sch` graphics medium, not by values.
  - **Option C** (design compiler: capture in code) — **adopted**. This doc.
  - **Option D** (AI generates `.kicad_sch` directly) — fallback only, if the
    compiler pilot fails. Same actor split, weaker guarantees: graphics medium
    has no types, no assertions, no reviewable diffs — ERC and vibes.
  - Superseded with B: net-naming canon → **typed interfaces**; static copied
    block sheets → **versioned parameterized modules**; `harvest` verb →
    `publish` side effect; hand-drawn MCU core → **generated from CubeMX .ioc**.
- Kept from B (capture-agnostic, still right): spec-is-chat, spec.md two-zone
  format, session-resume doctrine, JLC rules profiles, fab-out, vendor-neutral
  CLI-as-interface, agent manuals shape.

## Capture medium: code

Design source of truth is text: circuit modules in a DSL (atopile-class) or
Python (SKiDL fallback). Compiler emits KiCad netlist, BOM, check results.

Why code wins (over human drawing — and over AI drawing, option D):

- LLMs write/review text natively; they are bad at coordinate graphics.
- Diffs are reviewable — "user influence" gets an ergonomic lever.
- Types + assertions + parameterization exist only in the code medium.
- Reuse becomes import-with-params, not copy-and-mutate.

### Handoff mechanics — how code becomes parts in pcbnew

KiCad already decouples schematic graphics from board: "Update PCB from
Schematic" (F8) sends pure data — components (refdes + footprint ID + fields)
and nets (name → pin list). Graphics never reach the board file. **Code capture
replaces eeschema as producer of that payload; pcbnew never knows.**

**Ownership invariant — the code↔KiCad contract:**

- Circuit source exclusively owns: component identity, footprint assignment,
  fields, electrical connectivity.
- `.kicad_pcb` exclusively owns: placement, routing, vias, zones, board
  geometry — all spatial work, all human.
- Sync modifies compiler-owned data only and must preserve user-owned
  artwork. No exceptions, no silent repair.

Mechanics:

- Each part in code declares footprint ID + LCSC#; pin→net map falls out of
  connections.
- Route into board, two options: classic netlist import (File → Import
  Netlist; SKiDL path) or **direct `.kicad_pcb` sync** (atopile path, better —
  compiler maintains the board file; footprint blocks are s-expression text
  with pads bound to nets).
- In pcbnew: new footprints arrive in a pile, pads pre-bound, ratsnest shows
  all connectivity. User places and routes.
- Footprint geometry: `fp-lib-table` → official KiCad libs first; generate +
  verify against datasheet only when missing (rule kept from B).

**Sync contract** — required behavior; *hypothesis until the pilot proves it*
for whichever compiler wins:

- No-op rebuild: placement, tracks, vias, zones, rules, geometry byte-stable.
- Add/change/remove in code → only the intended deltas on the board.
- Net rename, footprint swap, deletion: defined, safe, documented behavior.
- Failed build leaves the last valid `.kicad_pcb` untouched (atomic write).
- Clean clone + locked deps rebuilds reproducibly.
- Identity stable at component/pad/net level (UUIDs), not merely refdes;
  designator lock file still required on top.

## Workflow

**U** user, **T** tool/compiler, **AI** agent.

```
1. SPEC       chat interview → spec.md (frontmatter contract + prose body).
              Not a CLI verb — chat is the medium (see Spec).
2. init       T: spec.md → compiler project + KiCad shell + JLC rules profile
3. ARCHITECT  AI proposes module graph as code skeleton (power tree, MCU,
              peripherals, typed interfaces), showing block renders for
              proposed reuse. U approves — the main human gate on capture.
4. MCU        U: CubeMX pinmux (judgment) → T: ioc2code parses .ioc →
              generates MCU module. Manual pin transcription eliminated;
              conversion validated against the .ioc, not trusted.
5. IMPLEMENT  AI writes module bodies: parts pinned with LCSC#, values,
              pullups, decoupling per rules. U reviews at chosen depth.
6. build+test T: compile → netlist + BOM; assertions + compiler-native
              electrical checks; fail loud.
7. brief      T: placement brief from module constraints; net classes +
              rules seeded into .kicad_pcb — canvas primed.
8. LAYOUT     U — THE ART. AI spotter on request (see Layout copilot).
9. ROUTE      U — the art continues. AI sanity checks on request.
10. verify    T: DRC vs JLC rules + scripted layout audits; AI render review.
11. fab-out   T: JLC Gerbers + drill + BOM + CPL → fab/board.zip
12. order     U uploads to JLCPCB (tool never touches money/orders)
13. publish   proven modules: version tag + generated schematic render;
              U optional prettify (the surviving drawing act).
```

### Phase 1 — SPEC (unchanged from B)

Spec is a conversation, not a script. U opens empty project dir, starts AI
session, gives trigger ("pcbforge: new board") + brain-dump. Agent reads
`agent/spec-interview.md`, runs Q&A across the dimensions (purpose, power in,
rails, MCU class, peripherals, connectors, I/O count, size, **layers — decided
here**, special, cost, debug), writes `spec.md`.

`spec.md` = two zones: **YAML frontmatter** (machine contract — `init` reads
only this, `yaml.safe_load` + versioned schema, fails loud on missing keys) + **markdown body**
(human intent, for the user and future AI sessions). AI keeps frontmatter in
sync with prose. No exact chip in spec — family + constraints only, unless user
names a part. Schema lives in `agent/spec-interview.md`.

## Layout copilot — serving the art

Tool's job in the art phase: prime, spot, audit. Never move copper.

**Pre-layout — prime the canvas:**
- Modules declare layout constraints as structured fields (`decap_max_mm: 2`,
  crystal keepout, diff-pair markers, thermal notes, "near connector" hints).
- Compiler emits **placement brief** (`brief.md`): per-block constraints, net
  priorities, suggested regions.
- Net classes + design rules pre-seeded into `.kicad_pcb`/`.kicad_pro`.

**During — checkpoints on request** ("review my placement"):
- Scripted exact audits: decap-to-pin distances from `.kicad_pcb`, courtyard/
  edge clearances, connector orientation vs board edge.
- AI eyeball: `kicad-cli pcb export svg` render reviewed against brief —
  orientation, access, routability.

**Post-route — beyond DRC:**
- Scripted: track width vs current per net class, stub/antenna sweep.
- Heuristic (honest about it): 2-layer return-path — flag traces crossing
  GND-pour splits.
- USB FS: keep pair short + together; no length-match theater.
- Final AI render review vs brief.

Hard rule: **spotter, not painter.** Output always words + measurements.

## Modules

- **Parameterized:** `Ldo3V3(input_max_v=6)` — one module, many variants.
- **Versioned + imported**, not copied. Version pin keeps fab-reproducibility
  (vendoring mechanism per compiler — pilot decides).
- **Publish is cheap, not free:** the artifact is already library-shaped —
  no extract/sanitize/redraw labor — but curation still gates: provenance
  (which board proved it), interface freeze, docs, version. Flywheel
  advantage is zero format conversion, not zero judgment.
- **Imported layout is discarded by default.** Compiler packages may carry
  layout data; applying it requires an explicit user-approved action. Sync
  never silently touches human artwork (see ownership invariant).
- **No MCU module** — MCU is per-project, generated from `.ioc`.

### Typed interfaces (replaces net-naming canon)

```
ldo.power_in ~ usb.power_out    # ElectricPower ↔ ElectricPower: ok
mcu.i2c      ~ sensor.i2c       # SDA/SCL move as one; pullup rule attached
mcu.swd      ~ header.swd       # wrong interface = compile error
```

Port mismatch = build failure, not a convention violation you hope to catch.
Human-facing net names still emitted for pcbnew readability.

### Presentation layer

At `publish`, generate schematic render of the module; user optionally
prettifies. Derived + non-authoritative (banner says so), regenerated on
version bump. Library browse (`index.md`) embeds renders; ARCHITECT phase
shows them when AI proposes blocks.

### Footprint rule (kept from B)

Official KiCad footprints first. Generate only when none exists; always verify
measured dimensions against datasheet. Generated ones live project/tool-local
`.pretty`.

## Checks become tests

Review items encoded as assertions, run every build — deterministic, free:

```
assert mcu.free_gpio >= 4
assert rail_3v3.load_ma <= ldo.rated_ma * 0.7
rule: every VDD pin has 100n within its group
rule: every I2C bus has exactly one pullup pair
```

Every board's postmortem adds a rule; all future boards inherit it. The
assertion suite is the floor under a hands-off human — but it proves only
what's encoded. AI netlist review stays as second layer; catches what nobody
encoded yet.

Electrical validation is **compiler-native** (connectivity/drive checks on
the resolved graph). KiCad ERC exists only if a real `.kicad_sch` is emitted —
this flow doesn't promise one. DRC runs for real, on the `.kicad_pcb`.

**BOM by construction:** BOM and connectivity derive from the same resolved
component graph — they cannot diverge from each other. (Wrong part choice
still possible; that's review + assertions.) `verify-stock` is a live JLC
query — time-dependent, not deterministic; it **reports** availability and
proposes alternates, never silently changes locked parts.

## Reproducibility artifacts

"Fab-reproducible years later" is a claim about files, so pin everything:

- `.pcbforge`: exact compiler + KiCad versions; dependency lockfile w/ hashes.
- Resolved selections recorded per refdes (MPN/LCSC#); generated-footprint
  hashes.
- `build` emits a manifest: input hashes + tool versions.
- `fab-out` archives the set: final `.kicad_pcb`, BOM, CPL, Gerbers, DRC
  report.

## Architecture — Tool vs Projects

- **The Tool** = one repo (`~/Projects/pcbforge`): module library, assertion/
  rule library, JLC rules profiles, scripts, agent manuals. Versioned, grows.
- **A Project** = one board. Self-contained, consumes the tool, pins versions.

`pcbforge` installed globally, on PATH. Projects call `pcbforge <verb>`.

```
pcbforge/                    ← THE TOOL
  rules/jlc-2layer.json  jlc-4layer.json
  modules/                   versioned circuit modules + renders + index.md
  asserts/                   shared rule/assertion library
  scripts/                   init, ioc2code, brief, verify, verify_stock,
                             fab_out, publish (+ compiler wrappers build/test)
  agent/
    operating-manual.md      what pcbforge is, phases, actor split, verbs
    spec-interview.md        step-one playbook + spec.md schema
    layout-copilot.md        spotter playbook: audits, render review, limits
  README.md                  quickstart (how to start a session)

my-stm32-thing/              ← A PROJECT
  spec.md                    living design doc (the spine)
  src/                       circuit code (modules, board top)
  my-stm32-thing.kicad_pcb   layout/routing (the art) + kicad_pro
  .pcbforge                  tool/compiler version pins
  fab/  bom/
```

## Session resume (CRITICAL requirement)

A board spans days and many chat sessions. Any new session must recover full
context by `cd`-ing in — zero dependence on prior conversation.

| Layer | Answers | Form |
|---|---|---|
| Project-local agent instructions | "what is this + how to operate" | dropped by `init` |
| `spec.md` | "what are we building + why" | contract + prose |
| Source tree + board file | "where are we right now" | **`build && test` output = status report** |

Orient routine: read instructions → read spec.md → run `build && test` +
inspect `.kicad_pcb` → report step + propose next. Progress re-derived from
files, never a stored status field. Code capture strengthens this: compile +
test output is machine-precise progress.

## Agent tool access

Verbs must be plain CLI, exit-code + stdout/stderr clean — any agent with
shell + file read/write drives them. No plugin binding. Vendor-neutral.

## Command set

`init`, `build`, `test`, `brief`, `verify`, `verify-stock`, `fab-out`,
`publish`, `ioc2code`, `migrate`. Spec is not a verb — chat. **`init` is
create-only** — refuses to touch an initialized project. Layer changes go
through `migrate` (backup, rules swap, revalidation) — never re-`init`.

## Docs & bootstrap (kept from B, verbs updated)

- **User-facing** `README.md`: quickstart, the copy-paste line pointing agent
  at `agent/operating-manual.md`. Trigger phrase: **"pcbforge: new board"**.
- **AI-facing** `agent/*.md`: vendor-neutral operating manuals.
- Bootstrap: first board relies on README line; after `init`, project-local
  instructions file grounds every future session.

## Scope boundary

**IS:** spec interview, code-capture toolchain (compiler wrapper, ioc2code,
refdes lock), module library + publish + renders, assertion/check suite,
layout copilot (brief + audits + render review), sourcing verify, fab output
gen.

**IS NOT (v1):** placement/routing by tool or AI, pinmux judgment (CubeMX
owns), simulation, ordering/payments, hand-drawn schematic capture (moot —
capture is code).

## Known costs (accepted 2026-07-24)

| Cost | Mitigation |
|---|---|
| Schematic is generated, not drawn — and user reviews it visually **in-loop**, not just at bench | hierarchy makes it tractable: block-diagram top + small per-module sheets (flat full-board render is a non-goal); atopile viewer first, own `.kicad_sch`-emit render path as fallback; pilot criterion 3 |
| Compiler dependency (atopile young) | SKiDL fallback, then option D; ejection: netlist + `.kicad_pcb` are plain KiCad — boards outlive tool |
| Board-1 cost (DSL + ioc2code + rules port) | crossover ~board 3–5 — hypothesis; pilot + early boards test it |
| Pin swap during routing re-coded by hand | KiCad is forward-annotation-dominant anyway |
| Weird analog corner easier drawn | raw-netlist island module; ugly but contained |
| Refdes churn breaks placement mapping | designator lock file (pilot criterion) |

## Pilot (gate before full build)

Port one small already-fabbed board to atopile. Pass/fail:

1. ioc2code feasibility (parse `.ioc` → MCU module).
2. Typed-interface + assertion expressiveness covers the JLC rule set;
   compiler-native electrical checks adequate (no KiCad ERC in flow).
3. **In-loop visual schematic review** (elevated to make-or-break
   2026-07-24): viewer/renders good enough to eyeball every build — top
   block diagram + per-module sheets; bench + block presentation included.
   Miss → build own render path: emit per-module `.kicad_sch` (kiutils +
   autolayout) → `kicad-cli sch export svg`; side effect: real KiCad ERC
   returns to the flow.
4. **Sync contract holds** (see Handoff): no-op idempotence, intended deltas
   only, atomic failure, component/pad/net identity stability.
5. Registry/versioning health, breaking-change cadence tolerable.

Failure stops the pilot and produces a decision report. A SKiDL retry or option
D requires an explicit follow-up choice; the pilot does not silently change
capture medium.

## Build order

1. **Pilot** (above) — decides compiler; everything else waits on it.
2. `agent/spec-interview.md` (reuse B's design — unchanged).
3. `README.md` + `agent/operating-manual.md`.
4. `init` + `ioc2code`.
5. Assertion library + `build`/`test` wrappers.
6. Layout copilot: `brief` + `verify` scripts + `agent/layout-copilot.md`.
7. `fab-out`, `verify-stock`.
8. Module library grows per board; `publish` when first board proves modules.

## Open questions

- Compiler path after the Roamer atopile block — wait for KiCad 10 support,
  maintain a pinned parser patch, or authorize the SKiDL fallback.
- Module layout-constraint schema — field list (`decap_max_mm`, keepout,
  `pair_with`…): define during pilot.
- Layout copilot: file audits + `kicad-cli` renders are the headless default;
  supported KiCad IPC API for live in-editor checkpoints later (optional).
- Review-depth default: which diffs demand user eyes?
- Module distribution: atopile registry vs tool-repo local imports.
- LCSC/JLC stock API access (carried over from B — research).
