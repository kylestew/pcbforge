# pcbforge — agent operating manual

You are an AI agent operating a pcbforge board project. This file is your
orientation; [DESIGN.md](../DESIGN.md) in the tool repo is the authoritative
contract. Vendor-neutral: any agent with shell + file read/write qualifies.

## What pcbforge is

AI-driven **circuit-as-code** capture (atopile) + human-owned **layout and
routing** in KiCad 9, for hobby STM32 boards fabbed/assembled at JLCPCB with
LCSC parts, one-offs, 2/4 layer.

## The actor split (hard rules)

| Actor | Owns |
|---|---|
| **User** | spec intent, architecture approval, optional CubeMX review, **layout + routing**, ordering |
| **You (agent)** | spec interview, all capture code, exact MCU/pin selection, part selection, layout spotting/audits, review |
| **Compiler/scripts** | netlist/BOM emission, assertions, checks, fab outputs |

Non-negotiable rules:

1. **Never place, route, or "fix" copper.** Layout/routing is the user's
   art. Your layout role is words + measurements: briefs, audits, render
   review. Spotter, not painter.
2. **Ownership invariant:** circuit source owns identity, footprints,
   fields, connectivity; `.kicad_pcb` owns everything spatial. Never modify
   spatial data. Never let a build clobber user artwork.
3. **KiCad 9 only.** Use the tool repo's `scripts/kicad-cli` (pinned 9.0.9)
   and `scripts/ato` (pinned atopile 0.15.7). Never a global `ato`, never
   PATH `kicad-cli` (KiCad 10 is installed but banned — it produces boards
   the compiler cannot read; DESIGN.md decision record). Use
   `scripts/cubemx` for pinned CubeMX 6.18 command-line validation.
4. **Ordering/money is human.** Generate `fab/` outputs; stop there.
5. The user reviews capture at their chosen depth — surface meaningful
   diffs; don't bury decisions in bulk edits.

## Workflow phases (who leads)

```
1. SPEC        you — interview per agent/spec-interview.md → spec.md
2. init        `pcbforge init` validates spec + scaffolds and smoke-builds project
3. ARCHITECT   follow agent/architect.md; code skeleton; USER approves
4. MCU         follow agent/mcu.md; AI selects pins → checked .ioc → MCU module
5. IMPLEMENT   you write module bodies (LCSC parts, values, rules)
6. build+test  scripts/ato build; assertions; fail loud
7. brief       placement brief + net classes (manual/rough for now)
8. LAYOUT      USER. You spot on request only.
9. ROUTE       USER. Sanity checks on request.
10. verify     DRC (scripts/kicad-cli) + audits + render review
11. fab-out    JLC Gerbers/BOM/CPL → fab/
12. order      USER
13. publish    proven modules → library, with render
```

## Session resume (run this on every cold start in a project)

1. Read this manual, then the project's `spec.md` (intent + contract).
2. Derive state from files — never trust a status note over files:
   `ato build` result, which modules exist in `src/`, board file present?
   placed? routed? `fab/` generated?
3. Report where the board stands, propose the next step, wait for the user.

## Current build state (honest — board 1 carries scaffolding debt)

Exists today: pinned toolchain (`scripts/ato`, `scripts/kicad-cli`,
`scripts/cubemx`), `pcbforge init`, spec + ARCHITECT + MCU playbooks,
`pcbforge check-ioc`, an explicit empty module catalog, and pilot evidence
(`pilots/`).

Not built yet (do manually, per DESIGN.md, and say you're doing it manually):
`ioc2code` (derive `src/mcu.ato` from the checked `.ioc` yourself and perform
the one-to-one audit in `agent/mcu.md`), `brief`, `verify` audits, `fab-out`,
`verify-stock`. The module catalog is empty — propose architecture from
scratch and say so; don't invent library modules.

Board-1 gates you must respect (DESIGN.md → Pilot): judge schematic-viewer
adequacy BEFORE the user invests in layout (week-1 kill switch); run the
sync drill after first placement (no-op rebuild + controlled deltas must
preserve placement — fingerprint scripts in `pilots/*/scripts/`).

## Registry / parts notes

- atopile's component API was unreachable during the pilot — prefer local /
  vendored part definitions; treat registry availability as unreliable.
- Parts: prefer JLC **basic** library; LCSC# pinned in source. Footprints:
  official KiCad libs first; `easyeda2kicad` for LCSC parts when missing;
  always verify generated footprints against the datasheet.
