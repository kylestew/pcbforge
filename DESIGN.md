# pcbforge — Design

AI-assisted PCB development tool. **Schematic capture happens in code, AI-driven;
the human owns layout and routing — the art.** Compiler + deterministic scripts
own everything mechanical around KiCad board design for JLCPCB fab.

[WORKFLOW.md](WORKFLOW.md) is the normative phase and approval contract. This
document records the reasoning, invariants, architecture, and decision history
behind it.

## Constraints (fixed for v1)

- **EDA:** KiCad **9.x, pinned** (layout/routing + fab outputs; capture happens
  upstream in code). KiCad 10 excluded until atopile reads its boards
  (atopile#1822) — see decision record for trigger.
- **Capture:** circuit-as-code with pinned atopile 0.15.7
- **Fab:** JLCPCB only (assembly via LCSC parts)
- **Boards:** hobby, 2 / 4 layer, medium density
- **MCU family:** STM32
- **Volume:** one-offs (no mass production)

## Philosophy

> **Layout is the art. Everything else is toil or verification.**

- **Layout + routing:** user's time concentrates here. Complex, artistic, black
  magic. AI guides and verifies by default — spotter, not painter. It picks up
  the brush only when the user explicitly asks for that specific piece of
  placement or routing, and puts it down again after. The art stays the user's:
  they judge, keep, or discard every AI-produced millimetre.
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
| **User** | material decision gates, spec intent, optional CubeMX review, **layout + routing (the art)**, order |
| **AI agent** | spec interview, writes/edits all capture code, exact MCU/pin selection, part selection, layout spotter, requested layout/routing attempts, review |
| **Compiler/scripts** | netlist + BOM emission, assertions, electrical checks/DRC, briefs, fab-out — deterministic, free (stock queries excepted: live network) |

### Decision-authority invariant

The AI may derive consequences of approved requirements, but may not choose
between materially different reasonable designs. Alternatives affecting
topology, public interfaces, connectors, resource allocation, cost, risk,
reversibility, or user experience are user decisions. The AI presents options,
recommendation, tradeoffs, and consequences, then stops before changing the
affected artifact.

Silence, general permission to continue, and broad implementation requests are
not approval. The AI may record approval already expressed by the user but may
not originate, infer, self-approve, or reuse it. Proposal approval precedes
implementation and required final approval follows presentation plus
validation.
Approval events are bound to artifact fingerprints. A changed approved
artifact becomes stale; a dashboard write durably reopens its phase so tool
reruns or later content restoration cannot silently revive the approval.
The current user gates and checked transitions are defined only in
[WORKFLOW.md](WORKFLOW.md). Passing checks at a user-owned gate produce
`Awaiting approval`, not completion. The agent presents the exact
`status review` packet and may record it only after the user unambiguously
accepts it.

### Manufacturing-policy invariant

Projects consume the hash-pinned
`policies/pcbforge-standard-v1.yaml` profile and track their declarations,
evidence, sourcing, and exception requests in `policy.yaml`. The profile—not
the editable project contract—defines hard rules, exception-capable defaults,
and each exception's earliest affected workflow phase.

JLCPCB fabrication/assembly, STM32, 2/4 layers, SWD, pinned tools, exact part
identity, canonical commodity libraries, spatial ownership, and human ordering
authority are hard and cannot be excepted. FR4 1.6 mm / 1 oz, conventional
vias, no controlled impedance, 0603 minimum ordinary R/C/LED, and avoiding
BGA/WLCSP/sub-0.5-mm QFN are defaults. Protection, testability, marking, and
sourcing require explicit applicability/evidence.

The normal policy checker is deterministic and offline. `policy.yaml` can
request but cannot grant an exception. STATUS keeps a separate append-only
policy approval stream whose events fingerprint the baseline, individual
exception, or post-FAB sourcing package. Changed approvals durably reopen;
exception approval reopens the profile-mapped completed phase.

## Decision record

- **2026-08-03 — the pinned tool revision no longer binds human approvals.**
  `.pcbforge` is an input to the circuit-review, build-test, and policy
  evidence sets, so hashing its raw bytes meant that bumping
  `pcbforge.revision` — which only selects the checkout allowed to execute —
  reopened SPEC, ARCHITECT, and CIRCUIT. Observed on the first board: two tool
  upgrades cost two full cascade renewals. Tracked evidence now binds
  `semantic_pin_bytes`: the `pcbforge` block (revision, dirty) is dropped and
  every other pin — schema, toolchain, rules, policy, guidance — still
  invalidates, because those genuinely change what was built. A malformed pin
  falls back to raw bytes so corruption still fails closed.
- **2026-08-03 — FAB-OUT generates a JLC-shaped packet and records itself.**
  `pcbforge fab-out` plots the pinned stackup, exports placements and DRC
  evidence, derives `jlc-bom.csv` from the approved compiler BOM and
  `jlc-cpl.csv` from the exported placements, writes a manifest, packs a
  deterministic archive including the final board, and calls the existing
  transition recorder. `check-fab-out` re-proves a packet read-only and runs
  as the ORDER-stage `fab` check. Decisions: DRC is not re-gated (VERIFY
  already binds a passing check) but is re-exported and must still be clean,
  since disagreement means the board moved under an approval; JLC column
  shapes are generated rather than hand-mapped at upload; `fab/` keeps loose
  files beside the zip and stays git-ignored. **KiCad 9.0.9 ignores
  `SOURCE_DATE_EPOCH`** and stamps wall-clock time into every plot, drill
  header, and job file, so packets cannot be byte-reproducible; the manifest
  therefore carries both raw and timestamp-normalized hashes, and each
  regeneration legitimately records a new transition. No `.pcbforge` guidance
  key was added — `EXPECTED_GUIDANCE` demands exact presence, so a new key
  would force every existing project to restart.
- **2026-08-03 — AI may attempt layout and routing when explicitly asked.**
  The prohibition was doctrine, not enforcement, and it blocked a legitimate
  user request. Now: default stays spotter-not-painter, and an explicit
  per-request ask authorizes spatial edits for that task only, inside an open
  LAYOUT with a current handoff. The grant never persists across tasks and is
  never inferred. Guardrails are a pre-edit `layout-backups/` copy, a stated
  edit plan, a reported delta, and a `layout ai-assisted` annotation event that
  carries no state so the LAYOUT packet shows AI-produced geometry. Rejected:
  standing permission (would erode the art boundary silently) and allowing
  spatial edits before the handoff (would break CIRCUIT spatial-preservation
  evidence). Not built: a dedicated `layout-assist` CLI gate — the annotation
  plus existing fingerprints covers v1.
- **2026-07-31 — ready reviews are saved for hash-free approval commands.**
  Phase, proposal, layout-handoff, and cascade review commands persist their
  latest ready fingerprint in the schema-1 STATUS `reviews` map. Approval and
  renewal prefer `--last-reviewed`, recompute the current packet, and fail
  closed if any artifact changed; the append-only approval event still stores
  the full SHA-256. Explicit `--fingerprint` remains available. Bundling policy
  exceptions into the same review is deferred because it requires richer
  saved-review metadata and a separate atomic policy-event refactor.
- **2026-07-31 — v1 trims the happy path to eight user decisions.** The sole
  numbered workflow is SPEC, ARCHITECT, CIRCUIT, LAYOUT, VERIFY, ORDER, and
  optional PUBLISH. Placement and routing remain distinct user tasks inside
  one LAYOUT phase and share a lightweight done-declaration before VERIFY.
  ARCHITECT completion is the checked `architecture-baseline` transition:
  `finish-architect` requires the current proposal approval, passing build and
  IOC checks, spatial preservation, and successful source-baseline capture.
  FAB-OUT is likewise a checked VERIFY-to-ORDER transition whose fingerprint
  binds the generated packet. Initialize and layout handoff remain visible
  transitions; the latter retains explicit user approval because it transfers
  the exact circuit contract into user-owned physical work.
- **2026-07-31 — current passing checks are reused incrementally.** Saved pass
  records remain current while their input fingerprints match, so an unchanged
  cold start launches no external validators and does not rewrite STATUS.
  Failed or stale checks rerun, while `--force-checks` deliberately reruns all
  applicable checks. One status cycle shares a single successful frozen build
  across build-dependent checks.
- **2026-07-31 — unchanged approval chains renew as one explicit decision.**
  Approval events now retain a phase-owned content fingerprint in addition to
  the full upstream-bound fingerprint. `status review --cascade` uses current
  saved checks and those content hashes to prove an unchanged prefix, including
  proposal gates and the layout handoff; it stops at the first delta or
  blocker. One user-approved `status renew` appends ordinary approval actions
  linked to their prior fingerprints. Existing events without content hashes
  remain valid but are not renewal-eligible. STATUS stays schema 1 and no
  migration or inferred approval is introduced.
- **2026-07-31 — approval fingerprints follow contract ownership.** Raw shared
  files no longer make downstream evidence reopen earlier phases. The SPEC
  digest canonicalizes YAML frontmatter and binds every body byte except the
  exact `## Decisions log` section. SPEC policy approval binds the profile,
  manufacturing/components, and assurance status/rationale. Final CIRCUIT
  adds assurance evidence and declared exceptions; sourcing remains outside
  that approval and is bound at ORDER by the existing sourcing fingerprint.
  ARCHITECT and CIRCUIT consume the semantic SPEC digest while review packets
  continue to show the full files. This changes fingerprint composition only;
  no artifact schema or migration is introduced.
- **2026-07-31 — v1 clean break.** PCBForge supports freshly initialized
  projects only. All artifact and guidance schemas reset to integer `1` with
  one accepted value. Unsupported versions fail with “unsupported version —
  restart the project.” Schema migrations, feature gates, compatibility
  branches, old numbered phases, deprecated CLI aliases, and native generated
  KiCad schematic review were removed. The sole current process is defined in
  [WORKFLOW.md](WORKFLOW.md).
- **2026-07-25 — MCU pinmux is AI-led; CubeMX review is optional.** After
  ARCHITECT approval, the agent chooses the exact STM32/package, resolves the
  pin mapping, and creates the canonical `firmware/<project>.ioc`. Pinned
  STM32CubeMX 6.18 validates the file in non-UI mode through
  `pcbforge check-ioc`; the user may open and edit it in CubeMX but does not
  have to author it. A saved user edit is an explicit override and must be
  revalidated and reconciled. Until `ioc2code` exists, the agent derives
  `src/mcu.ato` manually and performs a one-to-one audit against the checked
  `.ioc`.
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
- **2026-07-24 — KiCad pinned to 9.x** (user call, post-pilot). atopile 0.15.7
  (latest release) cannot read any KiCad-10-saved board (upstream
  atopile#1822, open); KiCad 9 round-trip verified clean on a substantial
  fixture. Consequences:
  - New boards start in KiCad 9 format; per-project version pins already
    doctrine.
  - Existing KiCad 10 boards (Roamer rev-a) = reference oracles via
    kicad-cli 10 exports only — never live projects on this track; no
    downgrade (9 can't read 10; conversion risks human artwork).
  - "Support" caveat stays open until pilot stage 2: serializer known lossy on
    corner data (teardrops, board setup, embedded fonts) on non-chosen
    fixtures; managed no-op sync untested.
  - **Upgrade trigger:** atopile ships KiCad 10 board support → rerun
    roamer fixture acceptance (`pilots/roamer-rev-a/REPORT.md`) → reconsider
    the pin.
    Netlist-route probe (compiler emits netlist, never touches board file)
    stays the hedge if the trigger never fires.
- **2026-07-24 — future boards only.** Legacy-board adoption (Roamer or any
  pre-pcbforge board) is a non-goal; every board is born inside the flow
  with compiler ownership metadata from day one. Consequence: the
  114-component mixer port is descoped; pilot stage 2 folds into the first
  fresh board (see Pilot).

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
  geometry — all spatial work, all user-authorized. The AI writes here only
  inside LAYOUT, only for work the user explicitly requested.
- Sync modifies compiler-owned data only and must preserve user-owned
  artwork. No exceptions, no silent repair. This holds regardless of who
  authored the artwork.

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

## Workflow contract

[WORKFLOW.md](WORKFLOW.md) is the single normative process map. It defines the
current phases, transitions, approval protocol, phase-owned fingerprint scopes,
and resume behavior. The playbooks under `agent/` provide execution detail
without redefining that sequence.

This design requires the workflow to preserve the actor split, decision
authority, append-only approval history, fail-closed staleness, scoped contract
ownership, reproducible evidence, and human authority over every spatial edit.

## Layout copilot — serving the art

Tool's job in the art phase: prime, spot, audit. Move copper only when asked.

**Pre-layout — prime the canvas:**
- The agent records exact board-specific constraints in `placement.yaml`
  (`max_mm`, keepouts, differential-pair settings, thermal and access notes).
- `pcbforge prepare-layout` emits the **placement brief**
  (`docs/placement-brief.md`):
  per-block
  constraints, net priorities, suggested regions.
- PCBForge-owned net classes are pre-seeded into `.kicad_pro`; the PCB and
  design-rules file remain untouched.

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

Hard rule: **spotter by default, painter only on request.** Unasked, output is
always words + measurements.

**On request — bounded painting:**
- Trigger is an explicit user ask for that placement or routing work. Silence,
  a general "continue", or "implement the plan" is not a trigger.
- Only inside an open LAYOUT with a current handoff. ARCHITECT and CIRCUIT keep
  the hard board-unchanged rule; their evidence depends on it.
- Board copied to `layout-backups/` (git-ignored) before the first edit, so the
  user can discard the attempt wholesale.
- Agent states intended edits first, edits only spatial objects, then reports
  the concrete delta (refs moved, tracks/vias added, DRC state).
- Recorded with `pcbforge status mark layout ai-assisted --note "..."` — an
  append-only annotation that never changes phase state, so the LAYOUT review
  packet shows which geometry the AI produced.
- Authority unchanged: the LAYOUT done-declaration is still the user's, and AI
  geometry gets the same review as their own.

## Modules

- **Parameterized:** `Ldo3V3(input_max_v=6)` — one module, many variants.
- **Versioned + imported**, not copied. Compiler locks and the `.pcbforge`
  tool revision keep builds reproducible.
- **Publish is cheap, not free:** the artifact is already library-shaped —
  no extract/sanitize/redraw labor — but curation still gates: provenance
  (which board proved it), interface freeze, docs, version. Flywheel
  advantage is zero format conversion, not zero judgment.
- **Imported layout is discarded by default.** Compiler packages may carry
  layout data; applying it requires an explicit user-approved action. Sync
  never silently touches human artwork (see ownership invariant).
- **No shared MCU module** — MCU is per-project and derived from its checked
  `.ioc`.

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

`pcbforge check-parts` makes the common case executable: recognized
two-terminal chip resistors, capacitors, and LEDs may keep exact MPN/LCSC
supplier metadata but may not reference project-local KiCad symbol, footprint,
or model files. Its fingerprint is part of the CIRCUIT evidence gate.

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

Electrical validation is **compiler-native** (connectivity/drive checks on the
resolved graph) and independently reviewable. CIRCUIT binds a deliberately
authored explanatory SVG to an exact pre-source proposal model, then compares
that model directly with the compiled BOM/PCB. DRC runs later on the product
`.kicad_pcb`; no review derivative owns or updates that PCB.

**BOM by construction:** BOM and connectivity derive from the same resolved
component graph — they cannot diverge from each other. (Wrong part choice
still possible; that's review + assertions.) `verify-stock` is a live JLC
query — time-dependent, not deterministic; it **reports** availability and
proposes alternates, never silently changes locked parts.

The CIRCUIT build-test gate makes those principles executable.
`build-test.yaml` schema 1 is the board-specific acceptance oracle, written
from reviewed intent rather than copied blindly from build output. It declares
the exact aggregated
LCSC/MPN/footprint/quantity BOM, expected PCB footprint count, selected atopile
build, and stable IDs for every required assertion. A
`# pcbforge-test: <id>` source marker must immediately precede each declared
assertion; the manifest and source ID sets match exactly.

`pcbforge check-build-test` then requires the pinned frozen build, compiler
manifest and BOM JSON/CSV, exact BOM agreement, unique designators,
BOM-to-PCB reference parity, resolved pad-to-net connectivity, and an unchanged
no-op spatial fingerprint. The preservation fingerprint covers footprint
position/side/membership, tracks, vias, zones, board outline, graphics, and
user artwork. Compiler-owned identity/connectivity may change while CIRCUIT is
being implemented, but its final review begins only once another identical
build is a spatial no-op.

CIRCUIT also owns every compiled KiCad net name. Before LAYOUT, each resolved
net receives a concise, human-readable name from explicit circuit source and
the approved proposal model records that exact `compiler_name`. Routable nets
use functional names rather than compiler fallbacks such as `hv`, `lv`,
`line`, numeric-only labels, or hierarchy-generated names. Intentional
single-pad unused nets use `NC_<REF>_<PIN>`. The final parity check proves the
approved names match the compiled PCB, and the LAYOUT handoff references only
those exact names. Net names are never repaired directly in the KiCad PCB.

A full passing checked write atomically produces tracked
`docs/build-test.md`: input/tool versions and hashes, resolved BOM, assertion
locations, connectivity totals/hash, artifact hashes, and preservation
results. `STATUS.md` stores the same input fingerprint. Either source becoming
stale reopens CIRCUIT. Failure never overwrites the previous passing report.

This deterministic gate does not pretend to prove live JLC stock or price,
placement/routing quality, KiCad PCB DRC, or fab output. Circuit comprehension
and exact compiled parity are already mandatory in CIRCUIT.

`pcbforge check-policy` cross-checks the exact CIRCUIT LCSC set against tracked
offline sourcing evidence without making normal status network-dependent.
After FAB-OUT, the user confirms a newly researched sourcing snapshot. That
approval fingerprints the sourcing entries, exact build-test contract, and current
fabrication outputs; ORDER cannot complete while it is absent or stale.

The CIRCUIT-to-LAYOUT transition makes the pre-layout handoff equally explicit.
`placement.yaml` schema 1 is authoritative and board-specific. It records a qualitative board
strategy, board-wide rules, ordered placement groups, typed spatial
constraints, exact-net routing classes, and a review checklist. Every resolved
PCB reference belongs to exactly one group. Constraint endpoints must resolve
to current `REF` or `REF.PAD` identities, and every class net must be an exact
current net; wildcards are not inferred. Manufacturing dimensions must meet
the pinned conservative JLC profile, including annular width and optional
differential-pair width/gaps.

`pcbforge prepare-layout` requires current CIRCUIT evidence, then stages
`docs/placement-brief.md` and a
merged KiCad project before committing either. It owns only net classes named
`pcbforge:<name>` and their exact patterns in `.kicad_pro`; it preserves the
Default class, user classes/assignments, unknown JSON fields, `.kicad_dru`, and
all non-owned settings. Conflicting user assignments fail rather than being
overridden. `.kicad_pcb` is read-only and confirmed byte-identical.
`pcbforge check-layout-handoff` validates those outputs without mutation.

The generated brief contains no coordinates and creates no geometric keepouts.
It is guidance for the human placer, not spatial source. The handoff is a combined
machine/human gate: the checker must pass, then the user approves
`docs/placement-brief.md`
beside the current CIRCUIT overview. Missing, stale, or inadequate
circuit-review evidence blocks the phase before layout.

Saved CIRCUIT and layout-handoff evidence fingerprint circuit-owned PCB semantics
(references, selected footprints, pads, and connectivity), not placement,
side, tracks, vias, zones, outline, graphics, or artwork. Circuit/topology
changes stale both gates. Spatial edits do not. The handoff additionally
fingerprints `placement.yaml`, generated `docs/placement-brief.md`, and PCBForge-owned
net-class semantics; unrelated user net classes remain outside its ownership
and staleness boundary.

## Reproducibility artifacts

"Fab-reproducible years later" is a claim about files, so pin everything:

- `.pcbforge`: exact compiler + KiCad versions; dependency lockfile w/ hashes.
- `.pcbforge`: exact clean PCBForge Git revision. The public dispatcher
  executes that revision from a registered worktree or fails closed; dirty
  revisions cannot initialize projects.
- `.pcbforge`: exact policy profile and hash; baseline approval mode.
- `policy.yaml`: project declarations, assurance/sourcing evidence, and
  exception requests; approval stays in STATUS.
- Scoped approvals bind policy declarations and assurance dispositions at
  SPEC, add assurance evidence and exceptions at final CIRCUIT, and defer
  sourcing currency to ORDER's dedicated fingerprint.
- Resolved selections recorded per refdes (MPN/LCSC#); generated-footprint
  hashes.
- CIRCUIT emits `docs/build-test.md`: input/tool versions and hashes, exact BOM,
  assertion evidence, connectivity summary, artifact hashes, and preservation.
  Compiler BOM JSON uses a canonical semantic hash that excludes only its
  volatile top-level `build_id`; malformed JSON fails and every other field
  remains bound to approval.
- `fab-out` archives the set: final `.kicad_pcb`, BOM, CPL, Gerbers, drills,
  DRC report, and the manifest that hashes them.

## Architecture — Tool vs Projects

- **The Tool** = one repo (`~/Projects/pcbforge`): module library, assertion/
  rule library, JLC rule and manufacturing-policy profiles, scripts, agent
  manuals. Versioned, grows.
- **A Project** = one board. Self-contained, consumes the tool, pins versions.

`pcbforge` installed globally, on PATH. Projects call `pcbforge <verb>`.

```
pcbforge/                    ← THE TOOL
  rules/jlc-2layer.json  jlc-4layer.json
  policies/pcbforge-standard-v1.yaml
  modules/                   versioned circuit modules + renders + index.md
  asserts/                   shared rule/assertion library
  toolchain/                 pinned compiler env (uv.lock — atopile 0.15.7)
  scripts/                   public pcbforge CLI; pinned ato, kicad-cli (9),
                             and cubemx (6.18) wrappers
  agent/
    operating-manual.md      actor split, resume, approval protocol
    spec-interview.md        step-one playbook + spec.md schema
    architect.md             module-graph procedure + approval gate
    mcu.md                   exact-device, pinmux, .ioc + audit procedure
    circuit.md               authored review + implementation procedure
    build-test.md            deterministic CIRCUIT acceptance procedure
    layout-handoff.md        placement contract + handoff procedure
  README.md                  quickstart (how to start a session)

my-stm32-thing/              ← A PROJECT
  spec.md                    living design doc (the spine)
  policy.yaml                manufacturing declarations, evidence, exceptions
  STATUS.md                  tracked generated workflow dashboard + gates
  src/                       circuit code (modules, board top)
  docs/architecture.md       tracked Mermaid architecture review artifact
  firmware/<project>.ioc     authoritative MCU configuration
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
| `STATUS.md` | "what is done, blocked, and next" | evidence + append-only gates |
| Source tree + board file | "what evidence exists right now" | checked by `pcbforge status` |

Orient routine: read instructions → read spec.md + STATUS.md → run
`pcbforge status --check --write` → report the current focus, blockers, and
next actions. Derived file/check evidence carries mechanical truth; append-only
events carry explicit human declarations. Neither can override a missing
requirement from the other.

## Agent tool access

Verbs must be plain CLI, exit-code + stdout/stderr clean — any agent with
shell + file read/write drives them. No plugin binding. Vendor-neutral.

## CLI surface

The implemented v1 commands are `status`, `init`, `finish-architect`,
`check-ioc`, `check-parts`, `check-policy`, `policy`,
`check-circuit-review`, `check-build-test`, `prepare-layout`,
`check-layout-handoff`, `fab-out`, and `check-fab-out`. Spec is not a verb —
chat. **`init` is create-only**
and refuses to touch an initialized project. PCBForge v1 does not change
layers or upgrade an initialized project; restart it from the revised SPEC.

`ioc2code`, VERIFY audits beyond DRC, live `verify-stock`, and `publish`
automation remain roadmap debt. Until implemented, agents follow the
manual procedures and state that they are doing so.

## Docs & bootstrap (kept from B, verbs updated)

- **User-facing** `README.md`: quickstart, the copy-paste line pointing agent
  at `agent/operating-manual.md`. Trigger phrase: **"pcbforge: new board"**.
- **AI-facing** `agent/*.md`: vendor-neutral operating manuals.
- Bootstrap: first board relies on README line; after `init`, project-local
  instructions file grounds every future session.

## Scope boundary

**IS:** spec interview, AI-led exact MCU/pin selection, checked CubeMX `.ioc`,
code-capture toolchain (compiler wrapper, ioc2code, refdes lock), module
library + publish + renders, assertion/check suite, layout copilot (brief +
audits + render review), sourcing verify, fab output gen.

**IS NOT (v1):** automatic or unrequested placement/routing, autorouting,
AI-owned layout approval, simulation,
ordering/payments, making explanatory review evidence authoritative, or
adopting an existing project as if it had pre-source approval.

## Known costs (accepted 2026-07-24)

| Cost | Mitigation |
|---|---|
| Review evidence duplicates the proposed code-owned connectivity | freeze it as pre-source approval evidence; bind SVG semantics to the exact model, then compare that model directly with compiled output |
| Compiler dependency (atopile young) | SKiDL fallback, then option D; ejection: netlist + `.kicad_pcb` are plain KiCad — boards outlive tool |
| Board-1 cost (DSL + ioc2code + rules port) | crossover ~board 3–5 — hypothesis; pilot + early boards test it |
| Pin swap during routing re-coded by hand | KiCad is forward-annotation-dominant anyway |
| Weird analog corner easier drawn | raw-netlist island module; ugly but contained |
| Refdes churn breaks placement mapping | designator lock file (pilot criterion) |
| KiCad pinned to previous major (9.x) while 10 is current | time-boxed: upgrade trigger + acceptance rerun defined in decision record; v1 hobby scope needs nothing 10-exclusive |

## Pilot (gate before full build)

**Status 2026-07-24 — pilot stage 1 run, independently verified.** atopile 0.15.7 +
KiCad 10.0.3: **blocked** (can't read KiCad-10 boards, atopile#1822;
atomic-failure slice passed — input byte-identical after failed sync).
atopile 0.15.7 + KiCad 9.0.9: reader/writer round-trip **clean** on official
multichannel-mixer fixture (114 footprints / 576 segments / 29 vias / 6
zones; canonicalization only — token-multiset identical, re-serialization
byte-stable, KiCad renders result) → **KiCad pinned 9.x** (decision record).
Evidence: `pilots/*/REPORT.md` + `results/`.

Pilot stage 2 — **the first fresh board is the pilot vehicle** (mixer port
descoped; existing-project adoption is a non-goal). Remaining pass/fail, tested in board
order:

1. MCU slice: AI-authored `.ioc` + `check-ioc` is implemented; remaining gate
   is ioc2code feasibility (parse checked `.ioc` → MCU module).
2. Typed-interface + assertion expressiveness covers the JLC rule set;
   compiler-native electrical checks plus exact circuit-model parity are adequate.
3. **In-loop visual circuit review** (implementation ready for
   pilot): complete authored explanatory SVG and exact model before source,
   passive-purpose annotations, semantic coverage, exact compiled parity, and
   deterministic acceptance before final CIRCUIT approval.
4. **Sync contract holds** (see Handoff): no-op idempotence, intended deltas
   only, atomic failure, component/pad/net identity stability.
5. Registry/versioning health, breaking-change cadence tolerable.

Gates while building board 1:

- **CIRCUIT comprehension gate (criterion 3):** the user must be able to explain
  every major block and passive purpose from the explanatory SVG before
  implementation. Inadequate presentation blocks CIRCUIT.
- **Sync drill (criterion 4):** immediately after the first placement
  session, scripted on the live board with the pilots' fingerprint tooling:
  no-op rebuild fingerprint check, controlled add / rename / footprint-swap /
  remove, induced build failure — placement and routing must survive all.
- Board 1 carries scaffolding debt by design: `init`, `check-ioc`,
  `prepare-layout`, `check-layout-handoff`, and `fab-out` are implemented;
  the MCU module is
  AI-transcribed from the checked `.ioc` with the one-to-one audit in
  `agent/mcu.md` while ioc2code matures.

## Remaining build order

1. Exercise pilot stage 2 on the first fresh board; its gates decide compiler
   continuation.
2. Implement `ioc2code` after the checked manual one-to-one process is proven.
3. Implement VERIFY audits beyond DRC and live sourcing refresh.
4. Grow the module library from proven boards and add `publish` automation.

## Open questions

- Module layout-constraint schema — field list (`decap_max_mm`, keepout,
  `pair_with`…): define during pilot.
- Layout copilot: file audits + `kicad-cli` renders are the headless default;
  supported KiCad IPC API for live in-editor checkpoints later (optional).
- Review-depth default: which diffs demand user eyes?
- Module distribution: atopile registry vs tool-repo local imports (registry
  component API was DNS-dead during pilot — local-first leaning).
- LCSC/JLC stock API access (carried over from B — research).
