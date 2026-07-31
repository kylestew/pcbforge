# PCBForge workflow

This is the concise, normative process map. `DESIGN.md` records rationale and
history; the playbooks under `agent/` define the detailed work.

## Process map

```mermaid
flowchart LR
    SPEC["1 SPEC"] --> INIT{{"initialize"}}
    INIT --> ARCH["2 ARCHITECT<br/>includes MCU"]
    ARCH --> BASELINE{{"architecture baseline"}}
    BASELINE --> CIRCUIT["3 CIRCUIT"]
    CIRCUIT --> HANDOFF{{"layout handoff"}}
    HANDOFF --> LAYOUT["4 LAYOUT<br/>placement + routing"]
    LAYOUT --> VERIFY["5 VERIFY"]
    VERIFY --> FAB{{"fab-out"}}
    FAB --> ORDER["6 ORDER"]
    ORDER --> PUBLISH["7 PUBLISH<br/>optional"]
```

There are seven numbered phases, six required. Initialization, architecture
baseline, layout handoff, and FAB-OUT remain visible in `STATUS.md` as
transitions rather than numbered phases.

| # | Phase | Primary lead | Completion contract |
|---:|---|---|---|
| 1 | SPEC | AI + user | Approved `spec.md` and policy baseline |
| — | SPEC → ARCHITECT: initialize | Tool | Atomic scaffold and compiler smoke test |
| 2 | ARCHITECT | AI + user | Approved graph and exact MCU plan; built skeleton, checked IOC, matching MCU source, audits |
| — | ARCHITECT → CIRCUIT: architecture baseline | AI + tool | Proposal current; build and IOC pass; spatial board unchanged; source baseline captured |
| 3 | CIRCUIT | AI + tool | Approved authored circuit model; exact parts and source; parity, policy, parts, build, and acceptance checks |
| — | CIRCUIT → LAYOUT: layout handoff | AI + tool + user | Current placement contract, generated brief, checks, and explicit handoff approval |
| 4 | LAYOUT | User | Placement and routing declared complete in one spatially bound approval |
| 5 | VERIFY | Tool + AI | DRC, audits, and render review approved |
| — | VERIFY → ORDER: FAB-OUT | Tool | Validated Gerber, drill, BOM, CPL, and archive packet fingerprint |
| 6 | ORDER | User | Current sourcing confirmed and order approved |
| 7 | PUBLISH | AI + user | Proven reusable modules published, or explicitly skipped |

## Authority and approvals

The eight required human decisions are SPEC, ARCHITECT proposal, CIRCUIT
proposal and final, layout handoff, LAYOUT done, VERIFY, and ORDER.
Initialization, ARCHITECT finalization, and FAB-OUT are checked tool
transitions. The layout handoff stays explicit because it transfers an exact
circuit into user-owned physical work.

The agent may derive consequences of approved requirements. It may not silently
choose between materially different reasonable designs. A choice is material
when alternatives affect topology, public interfaces, connector behavior,
resource allocation, cost, risk, reversibility, or user experience. Present
the alternatives, recommendation, tradeoffs, and consequences, then wait.

Passing tools never grants approval. The normal gate is:

```text
pcbforge status review <phase>
# present the packet and wait for the user
pcbforge status approve <phase> --last-reviewed --note "<approval>"
```

ARCHITECT and CIRCUIT have proposal gates before affected source work:

```text
pcbforge status review <phase> --stage proposal
pcbforge status approve <phase> --stage proposal \
  --last-reviewed --note "<approval>"
```

After implementing and auditing the approved ARCHITECT proposal, record its
checked transition without another user approval:

```text
pcbforge finish-architect
```

Changed approved artifacts reopen the affected gate. Restoring old bytes or
rerunning checks does not revive approval.

When an upstream contract changes, refresh saved checks and run
`pcbforge status review --cascade` before repeating individual gates. The
cascade packet proves which gate-owned semantic slices are unchanged and stops
at the first real delta or non-current check. After the user explicitly
approves that consolidated packet, record the eligible prefix once:

```text
pcbforge status renew --last-reviewed --note "<approval>"
```

Renewal appends ordinary proposal/final/handoff approval events with links to
their prior fingerprints; it never infers approval or crosses a changed gate.
Cascade review consumes current saved checks and does not run tools itself.
The explicit `--fingerprint <sha256>` form remains available as a fallback;
`--last-reviewed` recomputes and rejects any packet changed since review.

Fingerprint scope follows phase ownership. `spec.md` contributes canonical
YAML frontmatter plus all body bytes except the exact `## Decisions log`
section. SPEC binds the policy profile, manufacturing/component declarations,
and assurance status/rationale; final CIRCUIT additionally binds assurance
evidence and declared exceptions. Sourcing belongs to ORDER's separate
fingerprint. ARCHITECT and CIRCUIT therefore consume the semantic SPEC
contract without being reopened by later decisions-log notes. The human review
packet still lists the full files even where the approval hash uses a scoped
digest.

## 1. SPEC

Follow `agent/spec-interview.md`. Resolve purpose, power, rails, MCU family,
peripherals, connectors, board dimensions, 2/4-layer choice, fabrication
policy, risks, and material alternatives. SPEC produces `spec.md`,
`policy.yaml`, and a pre-project `STATUS.md`.

Review and approve the exact SPEC fingerprint. The dashboard then shows the
initialization transition as ready. Later implementation evidence in
`policy.yaml` does not rewrite this baseline approval; changing a declaration,
assurance status, or rationale does.

## SPEC → ARCHITECT: initialize

Immediately after SPEC approval, the agent runs:

```text
pcbforge init /absolute/path/to/project
```

The tool validates the approved inputs, generates the scaffold in a temporary
directory, runs the pinned compiler smoke test, and commits the create-only
outputs atomically. Success records a transition event and opens ARCHITECT
directly. There is no INIT review or approval.

Failure leaves no partial scaffold. The CLI records a visible blocked
initialization transition when it can safely update the pre-project dashboard;
fix the stated cause and retry `pcbforge init`.

## 2. ARCHITECT, including MCU

Follow `agent/architect.md` and its subordinate `agent/mcu.md`.

Before implementation, draft:

- `docs/architecture.md`: functional blocks, typed interfaces, external
  boundaries, coverage, reuse, and material architecture choices;
- `docs/mcu.md`: exact STM32 and package, resource allocation, provisional pin
  map, clocks, DMA/timers/interrupts, debug, spares, sourcing, alternatives,
  and unresolved risks.

Present both in the ARCHITECT proposal packet. Only after proposal approval may
the agent write the architecture skeleton, `firmware/<project>.ioc`, and
`src/mcu.ato`.

Final ARCHITECT evidence requires:

- the pinned Atopile build passes;
- `pcbforge check-ioc` proves a CubeMX round trip;
- IOC assignments and `src/mcu.ato` pass the one-to-one audit;
- the diagram matches every top-level instance, interface, and boundary;
- spatial board data remains unchanged.

`pcbforge finish-architect` captures
`review/circuit/source-baseline.json` and opens CIRCUIT after those checks pass.
A material graph, MCU, package, resource, pin, IOC, or public-interface change
returns to the ARCHITECT proposal gate.

## 3. CIRCUIT

Follow `agent/circuit.md`. Before physical source edits, author the exact
review-only circuit model and explanatory SVG under `review/circuit/`, plus
`docs/circuit-proposal.md`. Obtain CIRCUIT proposal approval.

Then implement the complete circuit: physical connections, exact parts,
values, official symbols/footprints, constraints, sourcing, protection,
testability, and the deterministic acceptance contract. Standard commodity
parts such as 0603 resistors, capacitors, and LEDs must use official KiCad
assets; a supplier ID is metadata, not a reason to generate a local part.

Final evidence includes current:

- pinned build and IOC check;
- `pcbforge check-parts`;
- `pcbforge check-policy`;
- authored model/SVG versus source, BOM, and PCB parity;
- `build-test.yaml`, marked assertions, and `docs/build-test.md`;
- concise source-owned KiCad net names matching every proposal-model
  `compiler_name`, with unused single-pad nets named `NC_<REF>_<PIN>`;
- circuit-owned PCB topology with spatial data preserved.

One final CIRCUIT approval covers the implemented circuit and its acceptance
evidence. Its policy scope includes assurance evidence and exceptions, but not
sourcing currency, which is reviewed again and bound at ORDER.

## CIRCUIT → LAYOUT: layout handoff

Follow `agent/layout-handoff.md`. Write `placement.yaml` with all footprint
groups, qualitative constraints, review checklist, and exact net classes. Then:

```text
pcbforge prepare-layout
pcbforge check-layout-handoff
pcbforge status review layout --stage handoff
# present the packet and wait for the user
pcbforge status approve layout --stage handoff \
  --last-reviewed --note "<approval>"
```

`prepare-layout` generates `docs/placement-brief.md` and merges only
PCBForge-owned net classes. It never moves footprints or edits copper.

The handoff fingerprint binds the current CIRCUIT approval, build-test report,
policy evidence, `placement.yaml`, generated brief, exact board topology, and
PCBForge-owned net classes. A topology or contract change reopens the handoff;
ordinary spatial placement does not.

## 4–7. Physical and release phases

Placement and routing remain distinct user-owned activities inside LAYOUT.
The agent may prime constraints, spot issues, and audit on request, but never
moves footprints or copper. One final LAYOUT approval binds both activities.

VERIFY runs DRC plus visual and process audits. The future FAB-OUT generator
creates, validates, fingerprints, and records the manufacturing-packet
transition without ordering. ORDER requires that current transition, refreshed
live sourcing, explicit sourcing confirmation, and human purchase authority.
PUBLISH is optional and may be skipped.

## Dashboard and resume

`STATUS.md` is the single tracked dashboard. Its frontmatter stores append-only
phase, transition, policy, and check records; its Markdown body is generated.
On every cold start:

```text
pcbforge status --check --write /absolute/path/to/project
```

Current passing checks retain their original timestamps and are skipped, so an
unchanged cold start launches no external validators. Use
`pcbforge status --check --force-checks --write` when every applicable check
must run again. Failed checks always rerun.

Report the current numbered phase or transition, blockers, and next actions.
Never edit the dashboard body or use `status mark ... complete`.

For a compact session handoff, run:

```text
pcbforge status --next /absolute/path/to/project
```

Both views identify the latest valid milestone, current phase or transition,
next owner, one primary action, and its exact command. A transition that ran
before its upstream phase was reopened is shown as `Performed, inactive`; it
remains in history but does not authorize forward progress. `Stale` instead
means the upstream phase is current while transition evidence must be
refreshed.

PCBForge v1 is a clean break: only freshly initialized projects are supported.
An unsupported artifact version must be restarted rather than upgraded in
place.
