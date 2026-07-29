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

The public `pcbforge` command is project-pinned. Before importing workflow
code, it reads the target project's `.pcbforge` file and selects a clean
registered Git worktree at that exact revision with the matching lockfile and
installed environment. If the pinned worktree is unavailable, the command
stops without touching the project and prints the `git worktree add` and
`uv sync` commands needed to install it. New projects and explicit migrations
must run from a clean checkout; dirty tool state is never recorded as a
reproducible pin.

## Start a new board

1. Make an empty project directory (outside this repo) and start your AI
   agent session in it.
2. Paste this line:

   > Read `~/Projects/pcbforge/agent/operating-manual.md`, then run the spec
   > interview per `~/Projects/pcbforge/agent/spec-interview.md`.
   > pcbforge: new board

3. The agent starts by asking for your initial idea, then interviews you and
   writes `spec.md`, `policy.yaml`, and the tracked `STATUS.md` dashboard. You
   gate the requirements and initial policy together. When they are good,
   initialize the create-only scaffold:

   SPEC approval happens in the conversation: tell the agent when the draft is
   good. The agent runs the status commands that create the dashboard and
   record that approval; you do not need to run those commands yourself.

   ```bash
   pcbforge init
   ```

   Initialization requires the current artifact-bound SPEC/policy approval,
   validates both contracts, creates the atopile/KiCad 9 project, applies
   conservative JLC rules, and smoke-builds it before installing any generated files. The
   generated `AGENTS.md` then directs the AI to present the initialized
   scaffold for your explicit INIT approval before beginning ARCHITECT.
   ARCHITECT retains separate approvals before coding and after the compiled
   architecture audit.
4. After ARCHITECT approval, the AI follows `agent/mcu.md`: it selects the
   exact STM32 and pin mapping, creates `firmware/<project>.ioc`, and runs
   `pcbforge check-ioc`. You may open the file in CubeMX 6.18 to review or
   edit it, but CubeMX authoring is not required.
5. Your time concentrates at the end of the middle: **layout and routing in
   KiCad 9**, with the agent as spotter (briefs, audits, render review).
6. `fab/` outputs upload to JLCPCB. Ordering stays human.

Resuming days later: just start a session in the project dir — the agent reads
`spec.md` and `policy.yaml`, refreshes `STATUS.md`, and proposes its recorded
next actions. No chat history is needed.

Across every phase, the AI may derive consequences of approved requirements
but may not silently choose between materially different reasonable designs.
It must present options and stop before changing the affected artifact. Human
approval is required before every phase completes, including tool-led phases.
Passing evidence moves a phase to `Awaiting approval`. The AI presents the
phase-specific review packet and fingerprint, waits for an unambiguous
conversational approval, and only then records it. Changed approved artifacts
are durably reopened by dashboard writes, and rerunning checks cannot revive
an old approval.

Optional dashboard commands (the agent normally refreshes the dashboard and
records conversational approvals for you):

```bash
pcbforge status
pcbforge status --check --write
pcbforge status review layout
pcbforge status approve layout --fingerprint <sha256> \
  --note "Placement reviewed and explicitly approved"
pcbforge check-policy
pcbforge policy approve-exception <id> --note "Approved tradeoff"
pcbforge check-circuit-review --stage proposal --write
pcbforge status review circuit --stage proposal
pcbforge check-build-test
pcbforge check-build-test --write-report
pcbforge brief
pcbforge check-brief
```

Static status is read-only and fast. `--check` runs applicable pinned build,
Step 5 CIRCUIT acceptance, placement-brief, parts-policy, CubeMX, and KiCad DRC
validation before rendering the same status model.

Schema 14 merges physical implementation and build + test into one CIRCUIT
phase. It combines authored circuit review, deterministic acceptance,
manufacturing policy, and universal phase approvals. The
tool-owned `policies/pcbforge-standard-v1.yaml` hard-locks JLCPCB, STM32,
2/4-layer boards, SWD, pinned tools, exact part identity, spatial ownership,
and human ordering authority. Project `policy.yaml` records standard
construction, package/process declarations, protection/testability evidence,
sourcing evidence, and explicit exception requests. Routine validation is
offline; approval commands only persist decisions already made by the user.

Before physical implementation, create the exact circuit proposal model and
deliberately authored browser-readable SVG described by `agent/circuit.md`.
Run `pcbforge check-circuit-review --stage proposal --write`, present the SVG,
narrative, exact model summary, and fingerprint, and receive explicit approval
before source edits. After implementation, the final check compares that frozen
model directly with compiled Atopile references, values, footprints, MPN/LCSC
identities, and physical-pin endpoint topology. Atopile remains authoritative;
PCBForge does not generate a KiCad schematic for this review.

During physical implementation, run `pcbforge check-parts` directly. It rejects
project-local KiCad assets for recognized commodity chip resistors, capacitors,
and LEDs and reports the canonical official-library replacement. The same audit
must pass before CIRCUIT can complete.

Within CIRCUIT, create the tracked exact acceptance contract `build-test.yaml`.
The internal build-test gate runs a pinned frozen build,
executes the contract's marked atopile assertions, validates exact
LCSC/MPN/footprint/quantity selections against the emitted BOM, checks
BOM-to-PCB designator and footprint parity, requires resolved connectivity,
and verifies that a no-op build preserved all spatial PCB work.
`pcbforge status --check --write` records the current check fingerprint and
generates the tracked `docs/build-test.md`. CIRCUIT reaches
`Awaiting approval` only when the proposal approval, implementation parity,
parts/policy/IOC checks, and build-test evidence are all current. One final
CIRCUIT fingerprint covers the implemented and tested result.
The report labels the compiler BOM digest as semantic: it canonicalizes the
JSON and excludes only the compiler's volatile top-level `build_id`. All
electrically meaningful BOM fields remain approval-bound, while the other
compiler artifacts retain raw byte hashes.

For Step 6, follow `agent/brief.md` and write the authoritative
`placement.yaml`: every PCB reference appears in exactly one ordered group;
typed constraints use current `REF` / `REF.PAD` endpoints; routing classes use
exact current nets and safe JLC dimensions. `pcbforge brief` generates
`brief.md` and merges only `pcbforge:` classes into the KiCad project. It
preserves user classes and verifies the PCB is byte-identical.
`pcbforge check-brief` is read-only. Step 6 completes only after the user
approves `brief.md` beside the approved current CIRCUIT overview;
record that gate with
`pcbforge status approve brief --fingerprint <sha256> --note "..."`.

After FAB-OUT, refresh live JLC availability and lifecycle evidence for the
exact BOM. Once the user confirms it, record
`pcbforge policy confirm-sourcing --note "..."`. ORDER cannot complete unless
that confirmation still matches the policy sourcing records, CIRCUIT BOM, and
fabrication outputs.

## Repo map

| Path | What |
|---|---|
| `WORKFLOW.md` | concise phase sequence, ownership, outputs, and gates |
| `DESIGN.md` | the contract — philosophy, workflow, invariants, decisions |
| `agent/` | AI manuals (`operating-manual`, `spec-interview`, `architect`, `mcu`, `circuit`, `build-test`, `brief`) |
| `toolchain/` | pinned compiler env (atopile 0.15.7, Python 3.14, uv.lock) |
| `scripts/` | public `pcbforge` CLI + pinned wrappers (`ato`, `kicad-cli`, `cubemx`) |
| `pcbforge/` | CLI implementation and project scaffold generator |
| `rules/` | versioned conservative JLC/KiCad rule profiles |
| `policies/` | versioned manufacturing and technology policy profiles |
| `modules/` | indexed circuit module library (explicitly empty at start) |
| `pilots/` | pilot evidence: reports, scripts, machine-readable results |

Existing initialized projects are not rewritten automatically. Migrate a
generated schema-7-through-9 project explicitly:

```bash
pcbforge migrate-policy /path/to/project
pcbforge check-policy /path/to/project
pcbforge policy approve-baseline /path/to/project \
  --note "Approved migrated policy baseline"
```

This migrates directly to schema 14. It pins the profile, generates
`policy.yaml` from discoverable facts, updates generated guidance, and leaves
applicability and sourcing items for review. It never infers approval.

For a generated schema-10 policy project, run:

```bash
pcbforge migrate-approvals /path/to/project
```

Current artifact-bound SPEC, ARCHITECT, and BRIEF approvals are preserved when
their fingerprints still match and every preceding required approval remains
current. Completed phases without a provable sequential approval reopen and
require `status review` plus explicit approval. Approved policy exceptions
retain their targeted reopening behavior. Temper was the first
universal-approval migration pilot.

For an existing schema-11 project, use the legacy migration alias to reach the
current circuit-review workflow:

```bash
pcbforge migrate-schematic-review /path/to/project
```

An already completed legacy IMPLEMENT phase must first be rewound, or migrated
with `--adopt-existing`. Adoption is labelled in STATUS and never claims the
circuit received pre-source proposal approval. A clean migration reopens MCU
once so its renewed approval captures the current source baseline.

For an existing schema-12 project, replace the native KiCad review gate:

```bash
pcbforge migrate-circuit-review /path/to/project
```

Legacy KiCad review files are preserved but ignored. A current MCU handoff
baseline is retained; changed post-baseline source requires explicit
`--adopt-existing`. The migration never generates or deletes review artwork.

For an existing schema-13 project, merge the old IMPLEMENT and build + test
phases into CIRCUIT:

```bash
pcbforge migrate-circuit-phase /path/to/project
```

The migration renames active review artifacts to `review/circuit` and
`docs/circuit-*`. CIRCUIT remains complete only when both old phase approvals
are current; otherwise it reopens at the combined gate. Running the command
again is a no-op.
