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
`uv sync` commands needed to install it. New projects must run from a clean
checkout; dirty tool state is never recorded as a reproducible pin.

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
   generated `AGENTS.md` opens ARCHITECT directly; initialization is a visible
   transition, not a separately approved phase.
4. Inside ARCHITECT, the AI follows `agent/architect.md` and `agent/mcu.md`.
   The proposal covers both the functional graph and exact STM32/package,
   resource, and provisional pin plan. After proposal approval the AI creates
   the source skeleton, `firmware/<project>.ioc`, and `src/mcu.ato`, then runs
   the build, CubeMX round-trip, and one-to-one audit before final ARCHITECT
   approval. You may optionally inspect the IOC in CubeMX 6.18.
5. Your time concentrates at the end of the middle: **layout and routing in
   KiCad 9**, with the agent as spotter (briefs, audits, render review).
6. `fab/` outputs upload to JLCPCB. Ordering stays human.

Resuming days later: just start a session in the project dir — the agent reads
`spec.md` and `policy.yaml`, refreshes `STATUS.md`, and proposes its recorded
next actions. No chat history is needed.

Across every phase, the AI may derive consequences of approved requirements
but may not silently choose between materially different reasonable designs.
It must present options and stop before changing the affected artifact. Human
approval is required before every numbered phase completes. Initialization is
automatic after SPEC approval, while the CIRCUIT-to-LAYOUT handoff has its own
explicit transfer approval.
Passing evidence moves a phase to `Awaiting approval`. The AI presents the
phase-specific review packet and fingerprint, waits for an unambiguous
conversational approval, and only then records it. Changed approved artifacts
are durably reopened by dashboard writes, and rerunning checks cannot revive
an old approval.

Approval fingerprints follow contract ownership rather than hashing every
shared file wholesale. The exact `## Decisions log` section in `spec.md` is
non-normative; the rest of the Markdown body and the canonicalized YAML
frontmatter remain approval-bound. SPEC binds policy declarations and
assurance dispositions, while assurance evidence and exceptions join the
final CIRCUIT contract. Sourcing is excluded from CIRCUIT approval and is
bound at ORDER through its dedicated sourcing fingerprint. These scopes avoid
reopening SPEC or ARCHITECT when CIRCUIT fills in later evidence; they do not
weaken review packets, which still display the complete source files.

Optional dashboard commands (the agent normally refreshes the dashboard and
records conversational approvals for you):

```bash
pcbforge status
pcbforge status --check --write
pcbforge status --check --force-checks --write
pcbforge status --next
pcbforge status review layout
pcbforge status approve layout --fingerprint <sha256> \
  --note "Placement reviewed and explicitly approved"
pcbforge status review --cascade
pcbforge status renew --fingerprint <sha256> \
  --note "Reviewed the root change and unchanged downstream gates"
pcbforge check-policy
pcbforge policy approve-exception <id> --note "Approved tradeoff"
pcbforge check-circuit-review --stage proposal --write
pcbforge status review circuit --stage proposal
pcbforge check-build-test
pcbforge check-build-test --write-report
pcbforge prepare-layout
pcbforge check-layout-handoff
pcbforge status review layout --stage handoff
```

Static status is read-only and fast. `--check` runs applicable pinned build,
CIRCUIT acceptance, layout-handoff, parts-policy, CubeMX, and KiCad DRC
validation before rendering the same status model.

After an upstream edit, cascade review can consolidate renewal of downstream
gates whose phase-owned content is provably unchanged. It uses saved current
checks, stops before any real delta, and still requires one explicit
conversational approval before the agent records the renewal.

The workflow has nine numbered phases, eight required. MCU work is inside
ARCHITECT; physical implementation and build + test form one CIRCUIT phase;
initialization and the layout handoff are visible transitions. CIRCUIT combines
authored circuit review, deterministic acceptance,
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
generates the tracked `docs/build-test.md`. Passing checks are reused while
their content fingerprints remain current; `--force-checks` reruns every
applicable check. A status cycle performs at most one successful frozen build,
and build-test validates the compiler artifacts from that build. CIRCUIT reaches
`Awaiting approval` only when the proposal approval, implementation parity,
parts/policy/IOC checks, and build-test evidence are all current. One final
CIRCUIT fingerprint covers the implemented and tested result.
The report labels the compiler BOM digest as semantic: it canonicalizes the
JSON and excludes only the compiler's volatile top-level `build_id`. All
electrically meaningful BOM fields remain approval-bound, while the other
compiler artifacts retain raw byte hashes.

For the CIRCUIT-to-LAYOUT handoff, follow `agent/layout-handoff.md` and write
the authoritative `placement.yaml`: every PCB reference appears in exactly one ordered group;
typed constraints use current `REF` / `REF.PAD` endpoints; routing classes use
exact current nets and safe JLC dimensions. `pcbforge prepare-layout` generates
`docs/placement-brief.md` and merges only `pcbforge:` classes into the KiCad
project. It preserves user classes and verifies the PCB is byte-identical.
`pcbforge check-layout-handoff` is read-only. LAYOUT cannot begin until the user
approves `docs/placement-brief.md` beside the approved current CIRCUIT overview;
record that gate with:

```bash
pcbforge status approve layout --stage handoff \
  --fingerprint <sha256> --note "..."
```

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
| `agent/` | AI manuals (`operating-manual`, `spec-interview`, `architect`, `mcu`, `circuit`, `build-test`, `layout-handoff`) |
| `toolchain/` | pinned compiler env (atopile 0.15.7, Python 3.14, uv.lock) |
| `scripts/` | public `pcbforge` CLI + pinned wrappers (`ato`, `kicad-cli`, `cubemx`) |
| `pcbforge/` | CLI implementation and project scaffold generator |
| `rules/` | versioned conservative JLC/KiCad rule profiles |
| `policies/` | versioned manufacturing and technology policy profiles |
| `modules/` | indexed circuit module library (explicitly empty at start) |
| `pilots/` | pilot evidence: reports, scripts, machine-readable results |

PCBForge v1 supports freshly initialized projects only. Projects created with
an earlier workflow must be restarted rather than upgraded in place.
