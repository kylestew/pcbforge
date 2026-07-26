# pcbforge workflow

This is the operational summary of the pcbforge board-development process.
[`DESIGN.md`](DESIGN.md) remains the authoritative contract; the agent
playbooks contain the detailed procedures for individual phases.

## Overview

```mermaid
flowchart LR
    SPEC[SPEC] --> INIT[init]
    INIT --> ARCH[ARCHITECT]
    ARCH -->|User approval| MCU[MCU]
    MCU --> IMPLEMENT[IMPLEMENT]
    IMPLEMENT --> BUILD[build + test]
    BUILD --> BRIEF[brief]
    BRIEF --> LAYOUT[LAYOUT]
    LAYOUT --> ROUTE[ROUTE]
    ROUTE --> VERIFY[verify]
    VERIFY --> FAB[fab-out]
    FAB --> ORDER[order]
    ORDER --> PUBLISH[publish]

    classDef user fill:#f6d365,stroke:#8a6500,color:#111;
    classDef agent fill:#9fd3ff,stroke:#23618f,color:#111;
    classDef tool fill:#b8e6bf,stroke:#357a40,color:#111;

    class SPEC,ARCH,MCU,IMPLEMENT,BRIEF,PUBLISH agent;
    class INIT,BUILD,VERIFY,FAB tool;
    class LAYOUT,ROUTE,ORDER user;
```

Blue phases are primarily AI-led, green phases are deterministic tooling, and
yellow phases are owned by the user. ARCHITECT has a mandatory user approval
gate. CubeMX review during MCU is optional.

## Phases

| Phase | Lead | Durable output | Gate or completion condition |
|---|---|---|---|
| 1. SPEC | AI interview; user decides intent | Approved `spec.md` and tracked `STATUS.md` | Explicit SPEC completion event |
| 2. init | Tool | Project scaffold, pinned metadata, KiCad shell, rules, project-local `AGENTS.md` | Spec validates and the scaffold smoke-builds |
| 3. ARCHITECT | AI; user approves | Compiling functional skeleton in `src/` and tracked `docs/architecture.md` | Explicit STATUS approval event referencing the diagram |
| 4. MCU | AI; optional user CubeMX review | Checked `firmware/<project>.ioc` and matching `src/mcu.ato` | Pinmux validates and MCU code passes a one-to-one audit |
| 5. IMPLEMENT | AI | Physical module bodies, selected parts, values, footprints, and constraints | Approved architecture is fully implemented |
| 6. build + test | Compiler and checks | Netlist, BOM, assertions, and build reports | All deterministic checks pass |
| 7. brief | Tool and AI | Placement brief and seeded board rules/net classes | Board is ready for human layout |
| 8. LAYOUT | User | Component placement in KiCad | User considers placement complete |
| 9. ROUTE | User | Routed copper in KiCad | User considers routing complete |
| 10. verify | Tools and AI | DRC, scripted audits, and render review | Manufacturing and layout checks pass |
| 11. fab-out | Tool | JLCPCB Gerbers, drills, BOM, CPL, and upload archive | Outputs regenerate cleanly from the project |
| 12. order | User | Fabrication order | User reviews files, uploads, and authorizes spending |
| 13. publish | AI prepares; user curates | Proven reusable module, version, documentation, and render | Module has real-board evidence and a stable interface |

## 1. SPEC

The AI follows `agent/spec-interview.md` and interviews the user about purpose,
power, rails, MCU family, peripherals, connectors, dimensions, layer count,
debugging, quantity, cost, and special constraints.

The result is `spec.md`:

- YAML frontmatter is the machine-readable initialization contract.
- Markdown prose records intent, reasoning, risks, and decisions.
- The user explicitly approves the baseline before initialization.

Once the draft validates, the agent creates `STATUS.md` with
`pcbforge status --write`. Explicit approval is recorded with
`pcbforge status mark spec complete --note "..."`; initialization preserves
and refreshes this dashboard.

SPEC is conversational; it is not a CLI command.

## 2. init

Run:

```bash
pcbforge init
```

Initialization is create-only. It validates `spec.md`, creates the atopile
project, KiCad 9 board shell, JLC rules, output directories, firmware
directory, pinned `.pcbforge` metadata, and project-local `AGENTS.md`. The
scaffold is installed only after a successful compiler smoke test.

Reinstalling pcbforge is not required for an existing checkout. Existing
projects are never reinitialized to adopt workflow changes.

## 3. ARCHITECT

The AI follows `agent/architect.md` and converts the approved requirements into
a compiling functional graph:

- power tree and every required rail;
- generic MCU boundary;
- functional modules and external connectors;
- typed interfaces such as power, I²C, SPI, UART, USB, CAN, and SWD;
- one-to-one coverage of the specification.

ARCHITECT deliberately excludes exact parts, component values, footprints, MCU
pins, CubeMX configuration, placement, routing, and other spatial board data.

The AI creates `docs/architecture.md` as soon as the proposed graph becomes
concrete and revises it alongside the skeleton. Its Mermaid `flowchart LR`
shows each functional module, external boundary, and top-level typed
connection exactly once. It represents architecture, not an electrical
schematic: parts, values, footprints, MCU pins, CubeMX data, placement, and
routing are excluded.

After the final build, the AI audits the diagram against the top-level `App`
instances, typed connections, and spec boundaries. The review package contains
the tracked diagram rendered inline, interface table, requirement coverage,
reuse status, audit, risks, source diff, and build result.

The workflow cannot continue until the user explicitly approves the
architecture and the agent records a STATUS completion event referencing
`docs/architecture.md`. Later module-graph or public-interface changes record
an ARCHITECT reopen event and require a refreshed diagram and renewed approval.

## Living status dashboard

`STATUS.md` is the single tracked dashboard for all 13 phases. Its generated
body shows the current phase, completed required-phase count, blockers, next
actions, phase-by-phase evidence, and recent events. Its frontmatter stores
append-only human gates plus fingerprinted build, IOC, and DRC results.

- `pcbforge status` is a fast, read-only inspection.
- `pcbforge status --write` refreshes the document without running slow tools.
- `pcbforge status --check --write` refreshes applicable pinned validations.
- `pcbforge status mark <phase> <action> --note "..."` records explicit
  completion, blocker, reopen, or optional-publish skip events.

Mechanical phases cannot complete without current evidence. Human-owned phases
cannot complete from file heuristics alone. Reopening an earlier phase makes
older downstream confirmations stale until they are reconfirmed.

## 4. MCU

The AI follows `agent/mcu.md`:

1. Convert the approved MCU interface into a peripheral and resource checklist.
2. Select the exact orderable STM32 and package.
3. Ask the user only when a material tradeoff remains.
4. Allocate pins, peripheral modes, clocks, DMA/timer resources, SWD, and the
   optional debug UART.
5. Create the authoritative `firmware/<project>.ioc`.
6. Run `pcbforge check-ioc`.
7. Present the selected part, pin table, clocks, spare resources, assumptions,
   and sourcing evidence.
8. Offer optional review in CubeMX 6.18.

If the user saves changes from CubeMX, those changes are deliberate overrides.
The AI shows the semantic difference, reruns `check-ioc`, and reconciles every
derived artifact.

`ioc2code` is not implemented yet. Until it exists, the AI derives
`src/mcu.ato` manually from the checked `.ioc` and performs the explicit
one-to-one audit in the MCU playbook.

## 5–7. IMPLEMENT, build + test, and brief

During IMPLEMENT, the AI replaces architecture placeholders with physical
circuit definitions: exact components, values, footprints, LCSC identifiers,
decoupling, pull resistors, protection, and electrical constraints.

The pinned compiler then builds the project and runs all available assertions
and electrical checks. Failures are resolved before layout.

The brief phase translates circuit intent into written placement guidance,
priorities, keepouts, sensitive relationships, and board-rule preparation.
The AI remains a spotter: it may describe spatial requirements but must not
place components or edit copper.

## 8–9. LAYOUT and ROUTE

The user owns all placement and routing in KiCad 9. These files are the spatial
source of truth.

The AI may inspect renders and measurements, identify risks, and provide
written suggestions when asked. It must never move footprints, draw tracks,
change zones, or silently rewrite human spatial work.

## 10–12. verify, fab-out, and order

Verification combines:

- KiCad DRC using the pinned CLI;
- pcbforge electrical and manufacturing audits;
- AI review of board renders;
- confirmation that requested JLCPCB constraints are satisfied.

`fab-out` creates the Gerbers, drill files, BOM, CPL, and upload archive. The
tool stops before any external upload or purchase.

The user reviews the final package, uploads it to JLCPCB, and authorizes all
ordering and spending.

## 13. publish

A project-local module becomes eligible for publication only after it has been
proven on a real board. Publishing records its provenance, freezes or versions
its public interfaces, creates documentation and a render, and adds it to the
module index.

Publishing is curated rather than automatic. The user may decline publication
or request presentation cleanup without changing the proven circuit.

## Current automation boundary

Implemented now:

- pinned atopile, KiCad 9, and CubeMX 6.18 wrappers;
- SPEC, ARCHITECT, and MCU agent playbooks;
- AI-generated tracked Mermaid architecture artifacts;
- `pcbforge init`;
- `pcbforge check-ioc`;
- compiler builds and current pilot checks.

Still manual or future tooling:

- `ioc2code`;
- compiler-derived architecture-diagram generation and validation;
- placement brief generation;
- complete verification audits;
- `fab-out`, stock verification, and module publishing commands.

The workflow remains valid while these commands are developed, but the AI must
state clearly whenever it is performing a documented step manually.
