# pcbforge

AI-assisted PCB development for hobby boards. Schematic capture happens in
**code** (atopile), written primarily by an AI agent under your review; **you
own layout and routing** — the tool primes, spots, and audits but never
touches copper. Targets KiCad 9 + JLCPCB assembly + STM32.

Operational phase summary: [WORKFLOW.md](WORKFLOW.md). Full rationale,
contracts, and decision history: [DESIGN.md](DESIGN.md).

**Status:** pre-board-1. Pilot phase 1 and project initialization are done;
phase 2 rides the first real board. Some later workflow steps remain manual
until their scripts exist.

## Requirements

- macOS with [uv](https://docs.astral.sh/uv/) on PATH
- KiCad 9 app at `/Applications/KiCad 9/` (KiCad 10 is deliberately NOT used
  — see DESIGN.md decision record)
- STM32CubeMX 6.18 at `/Applications/STMicroelectronics/STM32CubeMX.app`
- An AI coding agent with shell + file access (any vendor)

One-time bootstrap:

```bash
cd ~/Projects/pcbforge/toolchain && uv sync --frozen
export PATH="$HOME/Projects/pcbforge/scripts:$PATH"
```

Sanity: `./scripts/ato --version` → `0.15.7`, `./scripts/kicad-cli version`
→ `9.0.9`, and `./scripts/cubemx version` → `6.18`. Always use these
wrappers, never global or PATH-resolved tools.

Persist the PATH line in your shell profile if desired. `pcbforge --help`
should then show the available workflow verbs.

## Start a new board

1. Make an empty project directory (outside this repo) and start your AI
   agent session in it.
2. Paste this line:

   > Read `~/Projects/pcbforge/agent/operating-manual.md`, then run the spec
   > interview per `~/Projects/pcbforge/agent/spec-interview.md`.
   > pcbforge: new board

3. The agent starts by asking for your initial idea, then interviews you and
   writes `spec.md` plus the tracked `STATUS.md` dashboard. You gate it. When
   the spec is good, initialize the create-only scaffold:

   ```bash
   pcbforge init
   ```

   Initialization validates `spec.md`, creates the atopile/KiCad 9 project,
   applies conservative JLC rules, and smoke-builds it before installing any
   generated files. The generated `AGENTS.md` then directs the AI through the
   ARCHITECT playbook, its tracked Mermaid architecture diagram, and its
   explicit user-approval gate.
4. After ARCHITECT approval, the AI follows `agent/mcu.md`: it selects the
   exact STM32 and pin mapping, creates `firmware/<project>.ioc`, and runs
   `pcbforge check-ioc`. You may open the file in CubeMX 6.18 to review or
   edit it, but CubeMX authoring is not required.
5. Your time concentrates at the end of the middle: **layout and routing in
   KiCad 9**, with the agent as spotter (briefs, audits, render review).
6. `fab/` outputs upload to JLCPCB. Ordering stays human.

Resuming days later: just start a session in the project dir — the agent reads
`spec.md`, refreshes `STATUS.md`, and proposes its recorded next actions. No
chat history is needed.

Useful dashboard commands:

```bash
pcbforge status
pcbforge status --check --write
pcbforge status mark layout complete --note "Placement reviewed in KiCad"
```

Static status is read-only and fast. `--check` runs applicable pinned build,
CubeMX, and KiCad DRC validation before rendering the same status model.

## Repo map

| Path | What |
|---|---|
| `WORKFLOW.md` | concise phase sequence, ownership, outputs, and gates |
| `DESIGN.md` | the contract — philosophy, workflow, invariants, decisions |
| `agent/` | AI manuals (`operating-manual`, `spec-interview`, `architect`, `mcu`) |
| `toolchain/` | pinned compiler env (atopile 0.15.7, Python 3.14, uv.lock) |
| `scripts/` | public `pcbforge` CLI + pinned wrappers (`ato`, `kicad-cli`, `cubemx`) |
| `pcbforge/` | CLI implementation and project scaffold generator |
| `rules/` | versioned conservative JLC/KiCad rule profiles |
| `modules/` | indexed circuit module library (explicitly empty at start) |
| `pilots/` | pilot evidence: reports, scripts, machine-readable results |

Existing initialized projects are not rewritten automatically when agent
guidance changes. Temper is manually migrated to the current guidance;
other older projects can read `agent/architect.md` and `agent/mcu.md`
directly until a general migration command exists.
