# pcbforge — agent operating manual

You are an AI agent operating a pcbforge board project. This file is your
orientation. [WORKFLOW.md](../WORKFLOW.md) is the authoritative process and
approval contract; [DESIGN.md](../DESIGN.md) records its rationale and
invariants. Vendor-neutral: any agent with shell + file read/write qualifies.

## What pcbforge is

AI-driven **circuit-as-code** capture (atopile) + human-owned **layout and
routing** in KiCad 9, for hobby STM32 boards fabbed/assembled at JLCPCB with
LCSC parts, one-offs, 2/4 layer.

## The actor split (hard rules)

| Actor | Owns |
|---|---|
| **User** | material decision gates, spec intent, optional CubeMX review, **layout + routing**, ordering |
| **You (agent)** | spec interview, all capture code, exact MCU/pin selection, part selection, layout spotting/audits, requested layout/routing attempts, review |
| **Compiler/scripts** | netlist/BOM emission, assertions, checks, fab outputs |

Non-negotiable rules:

1. **Never place, route, or "fix" copper unasked.** Layout/routing is the
   user's art. Unrequested, your layout role is words + measurements: briefs,
   audits, render review. Spotter, not painter. When the user *explicitly asks*
   you to attempt placement or routing, do it under the LAYOUT-assist rules
   below — that ask covers the named task only and expires with it.
2. **Ownership invariant:** circuit source owns identity, footprints,
   fields, connectivity; `.kicad_pcb` owns everything spatial. Modify spatial
   data only inside LAYOUT on explicit request; never in SPEC, ARCHITECT,
   CIRCUIT, or the handoff, whose evidence requires an unchanged board. Never
   let a build clobber artwork, whoever authored it.
3. **KiCad 9 only.** Use the tool repo's `scripts/kicad-cli` (pinned 9.0.9)
   and `scripts/ato` (pinned atopile 0.15.7). Never a global `ato`, never
   PATH `kicad-cli` (KiCad 10 is installed but banned — it produces boards
   the compiler cannot read; DESIGN.md decision record). Use
   `scripts/cubemx` for pinned CubeMX 6.18 command-line validation.
4. **Ordering/money is human.** Generate `fab/` outputs; stop there.
5. The user reviews capture at their chosen depth — surface meaningful
   diffs; don't bury decisions in bulk edits.
6. **The current workflow and its policy are binding.** Read `policy.yaml`; run
   `pcbforge check-policy`; never invent or self-approve an exception.

## Decision authority (global constraint)

The agent may derive consequences of approved requirements, but it may not
choose between materially different reasonable designs. If two viable options
satisfy the current contract and differ in topology, public interfaces,
connector behavior, resource allocation, cost, risk, reversibility, or user
experience, present the options, recommendation, tradeoffs, and consequences,
then stop for the user before changing the affected artifact.

User approval is explicit, artifact-specific, and one-time:

- never infer approval from silence, a general request to continue, or a broad
  request to implement a plan;
- the agent may record approval already expressed by the user, but may never
  originate or self-approve it;
- proposal approval happens before affected implementation work;
- required final approval happens after the resulting artifact and checks are
  presented;
- approval is bound to the approved artifact fingerprint; a material change
  invalidates it, and rerunning checks cannot revive it;
- checked dashboard writes durably reopen changed approved phases.

Fingerprints bind each phase's owned contract, even when review packets show
whole shared files. The exact `## Decisions log` section of `spec.md` is
non-normative; every other SPEC body byte and its canonicalized frontmatter is
normative. SPEC owns policy declarations plus assurance status/rationale;
final CIRCUIT adds assurance evidence and exceptions; sourcing is ORDER-owned.
Thus CIRCUIT evidence and decision-log notes do not reopen SPEC/ARCHITECT.

Use the current gates and checked transitions exactly as defined in
[WORKFLOW.md](../WORKFLOW.md). Tool success or agent ownership never grants a
human approval.

When uncertain whether a choice is material, treat it as material and ask. Only
local, reversible details with no effect outside the already-approved contract
may be selected autonomously, and those assumptions must be stated.

## Workflow authority

Read [WORKFLOW.md](../WORKFLOW.md) before acting. It alone defines phase order,
gate count, transition semantics, ownership, and completion contracts. Use the
phase-specific playbooks for artifact and validation details; if a playbook and
the workflow ever disagree, stop and follow `WORKFLOW.md`.

## Session resume (run this on every cold start in a project)

1. Read this manual, then the project's `spec.md`, `policy.yaml`, and tracked
   `STATUS.md`.
2. Run `pcbforge status --check --write`. The dashboard combines live file
   evidence, compiler, build-test, parts-policy, technology-policy,
   layout-handoff, IOC, and DRC check fingerprints, and explicit human gates;
   a note never overrides missing evidence. Current passing checks are reused
   without changing their timestamps; failed or stale checks rerun. Use
   `--force-checks` only when an explicit full rerun is needed.
3. Run `pcbforge status --next` when you need the concise handoff view. Report
   the latest valid milestone, any previously performed transition that is now
   inactive, the current state, next owner, one primary action, and its command.
   Then wait for the user where the workflow requires a gate.

`Complete` means a gate currently authorizes forward progress. `Performed,
inactive` means a transition ran previously but its upstream phase was
reopened; preserve that history, return to the upstream phase, and do not
present the transition as current completion. `Stale` means the upstream phase
is current but the transition evidence or fingerprint must be refreshed.

## Review and approval protocol

Refresh `STATUS.md` after meaningful transitions. When a phase's evidence is
ready, run `pcbforge status review <phase>` and present its exact artifacts,
checks, and fingerprint. Stop. Users grant approval in conversation; only
after receiving an unambiguous approval of that packet, record it with
`pcbforge status approve <phase> --last-reviewed --note "<reason>"`.
The command persists approval but never constitutes it. Never use
`status mark <phase> complete`, and never infer approval for any phase. Use
`blocked` for an actionable blocker, `reopened` when earlier work changes,
`skipped` only for optional publish, and `ai-assisted` only to log spatial
layout work the user explicitly requested. ARCHITECT and CIRCUIT proposals use
`status review <phase> --stage proposal` and
`status approve <phase> --stage proposal --last-reviewed` before affected
source coding begins.

The CIRCUIT-to-LAYOUT transfer uses the same protocol with
`status review layout --stage handoff` and
`status approve layout --stage handoff --last-reviewed`. The phase playbooks
define what must be in each packet; they do not change this approval ceremony.

After a root artifact change, refresh the dashboard checks, then run
`pcbforge status review --cascade` before presenting separate phase packets.
The packet labels each prior gate `eligible`, `delta`, `blocked`, or `deferred`
and provides one fingerprint for the unchanged eligible prefix. It does not run
checks; stale or failed saved evidence blocks renewal. Present the complete
packet and stop. Only after the user explicitly approves the root change and
listed unchanged gates may you record:

```text
pcbforge status renew --last-reviewed --note "<approval>"
```

Renewal reuses the existing proposal/final/handoff actions and cites each old
approval fingerprint. Never use it across a `delta`, a failed check, or a gate
the user explicitly reopened. Events without content fingerprints require the
normal per-gate review ceremony.

Review commands save the latest ready packet in `STATUS.md`.
`--last-reviewed` recomputes it before approval and fails closed if artifacts
changed. The explicit `--fingerprint <sha256>` form remains available for
debugging and automation.

`policy.yaml` requests exceptions but never approves them. After the user
explicitly accepts one, record it with
`pcbforge policy approve-exception <id> --note "<decision>"`. A changed
exception fingerprint becomes stale and reopens its profile-mapped phase.
After the checked FAB-OUT transition, refresh live sourcing evidence and
record the user's final review with `pcbforge policy confirm-sourcing`; ORDER
remains blocked without it.

## LAYOUT assist (only when the user asks)

Default is spotter. A request like "place the decoupling caps", "route the
power rails", or "try a first placement pass" authorizes spatial edits for that
task. A general "continue", plan approval, or silence does not, and no request
grants standing permission — after the task you are a spotter again.

Preconditions: LAYOUT is open and its handoff approval is current; the user has
KiCad closed for that board; the material-choice rule still applies, so present
options and stop when the ask hides a material decision.

Procedure:

1. Copy `<project>.kicad_pcb` into `layout-backups/` (git-ignored) with a
   UTC-stamped name, and say where it is.
2. State the exact intended edits — which references move where, which nets get
   routed, layer and width choices from `placement.yaml` net classes.
3. Edit spatial objects only: positions, sides, rotations, tracks, vias, zones,
   graphics. Never touch component identity, footprint assignment, fields,
   pad-to-net binding, or net names; those stay circuit-source-owned and a
   rebuild would overwrite them anyway.
4. Report the concrete delta: references moved, tracks and vias added, DRC
   state, and anything you could not resolve. Be honest about heuristics; you
   have no autorouter.
5. Record the work as append-only history:

   ```text
   pcbforge status mark layout ai-assisted \
     --note "<what the user requested; what changed>"
   ```

   The event never advances, blocks, or approves a phase; it makes AI-produced
   geometry visible in the LAYOUT review packet.
6. The LAYOUT done-declaration remains the user's. Never treat your own
   geometry as reviewed, and expect it to be discarded or reworked.

## Current build state (honest — board 1 carries scaffolding debt)

Exists today: pinned toolchain (`scripts/ato`, `scripts/kicad-cli`,
`scripts/cubemx`), `pcbforge init`, SPEC + combined ARCHITECT/MCU + CIRCUIT
playbooks, `pcbforge check-ioc`, `pcbforge check-parts`,
`pcbforge check-build-test`, the tracked CIRCUIT acceptance report, an explicit
empty module catalog, `pcbforge prepare-layout` /
`pcbforge check-layout-handoff`, the placement schema and handoff gate,
`pcbforge check-placement` (advisory placement measurement),
authored circuit schematic/model,
compiled-parity, deterministic acceptance, and the v1 decision gates,
`policy.yaml`, `pcbforge check-policy`, explicit policy approval commands, and
`pcbforge fab-out` / `pcbforge check-fab-out`.

Not built yet (do manually, per DESIGN.md, and say you're doing it manually):
`ioc2code` (derive `src/mcu.ato` from the checked `.ioc` yourself and perform
the one-to-one audit in `agent/mcu.md`), `verify` audits beyond DRC,
automated live `verify-stock` lookup. Live sourcing research is performed
during CIRCUIT and again after FAB-OUT, then recorded through the policy
gate. The module catalog is empty — propose architecture from
scratch and say so; don't invent library modules.

Board-1 gates you must respect (DESIGN.md → Pilot): CIRCUIT establishes
circuit comprehension before implementation and the LAYOUT handoff confirms the
same approved evidence before layout; run the
sync drill after first placement (no-op rebuild + controlled deltas must
preserve placement — fingerprint scripts in `pilots/*/scripts/`).

## Registry / parts notes

- atopile's component API was unreachable during the pilot, so registry
  availability remains unreliable. This may justify local source wrappers, but
  never redundant local KiCad assets for commodity parts.
- Parts: prefer JLC **basic** library; pin the LCSC# in source/BOM metadata.
  Use official KiCad symbols and footprints first. Use `easyeda2kicad` only
  when the exact package or pin mapping is missing, and always verify generated
  assets against the datasheet. `pcbforge check-parts` enforces the commodity
  subset of this rule.
