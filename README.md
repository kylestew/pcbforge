# pcbforge

AI-assisted PCB development for hobby boards. Schematic capture happens in
**code** (atopile), written primarily by an AI agent under your review; **you
own layout and routing** — the tool primes, spots, and audits but never
touches copper. Targets KiCad 9 + JLCPCB assembly + STM32.

Full rationale, contracts, and decision history: [DESIGN.md](DESIGN.md).

**Status:** pre-board-1. Pilot phase 1 done (toolchain verified); phase 2
rides the first real board. Expect scaffolding debt: some steps below are
manual until their scripts exist.

## Requirements

- macOS with [uv](https://docs.astral.sh/uv/) on PATH
- KiCad 9 app at `/Applications/KiCad 9/` (KiCad 10 is deliberately NOT used
  — see DESIGN.md decision record)
- An AI coding agent with shell + file access (any vendor)

One-time bootstrap:

```bash
cd ~/Projects/pcbforge/toolchain && uv sync --frozen
```

Sanity: `./scripts/ato --version` → `0.15.7`, `./scripts/kicad-cli version`
→ `9.0.9`. Always use these wrappers, never a global `ato` or PATH
`kicad-cli`.

## Start a new board

1. Make an empty project directory (outside this repo) and start your AI
   agent session in it.
2. Paste this line:

   > Read `~/Projects/pcbforge/agent/operating-manual.md`, then run the spec
   > interview per `~/Projects/pcbforge/agent/spec-interview.md`.
   > pcbforge: new board

3. The agent starts by asking for your initial idea, then interviews you and
   writes `spec.md`. You gate it: when the spec is good, the project moves on
   to scaffold → architecture → implementation.
4. Your time concentrates at the end of the middle: **layout and routing in
   KiCad 9**, with the agent as spotter (briefs, audits, render review).
5. `fab/` outputs upload to JLCPCB. Ordering stays human.

Resuming days later: just start a session in the project dir — the agent
re-orients from `spec.md` and the files. No chat history needed.

## Repo map

| Path | What |
|---|---|
| `DESIGN.md` | the contract — philosophy, workflow, invariants, decisions |
| `agent/` | manuals the AI reads (`operating-manual`, `spec-interview`) |
| `toolchain/` | pinned compiler env (atopile 0.15.7, Python 3.14, uv.lock) |
| `scripts/` | pinned wrappers (`ato`, `kicad-cli`) + verbs as they land |
| `modules/` | circuit module library (grows per board; empty at start) |
| `pilots/` | pilot evidence: reports, scripts, machine-readable results |
