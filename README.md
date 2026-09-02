# pcbforge

AI-assisted PCB development for hobby boards. Schematic capture happens in
**code** (atopile), written primarily by an AI agent under your review; **you
own layout and routing**. The tool primes, spots, and audits; it touches copper
only when you explicitly ask it to attempt a specific placement or routing
task, and you still approve the result. PCBForge targets KiCad 9, JLCPCB
assembly, and STM32.

The normative phase sequence, ownership boundaries, and approval gates are in
[WORKFLOW.md](WORKFLOW.md). [DESIGN.md](DESIGN.md) records rationale,
invariants, architecture, and decision history.

**Status:** pre-board-1. Pilot stage 1 and project initialization are done;
pilot stage 2 rides the first real board. Some later workflow steps remain manual
until their scripts exist.

## Requirements

- macOS with [uv](https://docs.astral.sh/uv/) on PATH
- KiCad 9 app at `/Applications/KiCad 9/`
- STM32CubeMX 6.18 at `/Applications/STMicroelectronics/STM32CubeMX.app`
- An AI coding agent with shell and file access

One-time bootstrap:

```bash
cd ~/Projects/pcbforge/toolchain && uv sync --frozen
export PATH="$HOME/Projects/pcbforge/scripts:$PATH"
```

Sanity checks:

```bash
./scripts/ato --version
./scripts/kicad-cli version
./scripts/cubemx version
pcbforge --help
```

The expected tool versions are atopile 0.15.7, KiCad 9.0.9, and CubeMX 6.18.
Always use these wrappers rather than global or PATH-resolved tools.

The public `pcbforge` command is project-pinned. Before importing workflow
code, it reads the target project's `.pcbforge` file and selects a clean
registered Git worktree at the exact revision with the matching lockfile and
installed environment. If that worktree is unavailable, it stops without
touching the project and prints the commands needed to install it. New
projects must run from a clean checkout.

## Start a new board

1. Create an empty project directory outside this repository and start an AI
   agent session there.
2. Give the agent this prompt:

   > Read `~/Projects/pcbforge/agent/operating-manual.md`, then run the spec
   > interview per `~/Projects/pcbforge/agent/spec-interview.md`.
   > pcbforge: new board

3. Work through the interview until `spec.md`, `policy.yaml`, and `STATUS.md`
   describe the intended board. Approval happens explicitly in conversation;
   the agent follows the operating manual to record it.
4. Let the agent run `pcbforge init` after the SPEC gate is current. The
   create-only command validates the contracts, smoke-builds a temporary
   scaffold, and installs it atomically.
5. Continue through [WORKFLOW.md](WORKFLOW.md). The detailed work instructions
   live in `agent/`; your hands-on time concentrates on placement and routing
   in KiCad 9 — ask the agent for a placement or routing pass when you want one
   — and ordering remains human-owned.

To resume later, start an agent session in the project directory. The generated
project instructions, `spec.md`, `policy.yaml`, and `STATUS.md` provide the
context; no chat history is required. The agent refreshes deterministic checks
with `pcbforge status --check --write` and reports the recorded next action.

## Repo map

| Path | What |
|---|---|
| `WORKFLOW.md` | normative phase sequence, ownership, outputs, and gates |
| `DESIGN.md` | rationale, invariants, architecture, scope, and decisions |
| `agent/` | AI operating manual and phase-specific playbooks |
| `toolchain/` | pinned compiler environment and lockfile |
| `scripts/` | public CLI and pinned tool wrappers |
| `pcbforge/` | CLI implementation and project scaffold generator |
| `rules/` | versioned conservative JLC/KiCad rule profiles |
| `policies/` | versioned manufacturing and technology policy profiles |
| `modules/` | indexed circuit module library |
| `patterns/` | indexed vendor reference layout patterns |
| `pilots/` | pilot evidence, reports, scripts, and machine-readable results |

PCBForge v1 supports freshly initialized projects only. Projects created with
an earlier workflow must be restarted rather than upgraded in place.
