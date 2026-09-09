# PCBForge agent operating manual

Read [WORKFLOW.md](../WORKFLOW.md) for the normative phase order and completion rules.
Read the project specification, policy and status before resuming work.
Use `pcbforge status --check --write`, then `pcbforge status --next` to inspect current evidence and the next action.

## Ownership and authority

The saved native KiCad schematic owns component identity, fields, footprints and connections.
Independent requirements, sourced facts and Python tests determine electrical acceptance.
The user owns PCB placement, routing, material design decisions and ordering.
The agent develops the circuit and review evidence within the approved requirements.

Use the pinned `scripts/kicad-cli` for KiCad 10.0.3 and `scripts/cubemx` for CubeMX validation.
Atopile is absent from the current runtime. Existing projects use their historical pinned checkout.
Never rewrite an old project's pin to bypass compatibility checks.

Present materially different design choices when the existing requirements do not resolve them.
Explain the recommendation, tradeoffs and affected artifacts. Continue independent work while awaiting the decision.
Carry out decisions that the user already authorized. Do not ask for the same permission again.

Never originate a user approval. A passing check, silence or a general request to continue does not approve a phase packet.
Record approval only when the user has explicitly approved the relevant artifacts.
Generate fabrication files within the workflow. Ordering and payments remain user actions.

## Standard review and approval protocol

Prepare the complete, reviewable result before asking for approval.
Run `pcbforge status review <phase>`, then present its artifacts, checks, material changes and unresolved assessments.
After the user explicitly approves that packet, record it with:

```sh
pcbforge status approve <phase> --last-reviewed --note "<user approval>"
```

ARCHITECT uses `status review architect --stage proposal` and the matching approval command with `--stage proposal`.
CIRCUIT uses a single final schematic review. Its checked PCB synchronization step does not need a second human approval.
The placement brief uses `status review layout --stage handoff` and the matching approval command with `--stage handoff`.

Review records bind the artifacts that were presented. `--last-reviewed` fails if they change before approval.
The explicit `--fingerprint <sha256>` form is also available.
Never use `status mark <phase> complete`. Checks cannot grant or revive a human approval.

After an upstream change, refresh checks and run `pcbforge status review --cascade`.
Present the root change and every eligible unchanged gate. After the user approves the packet, record:

```sh
pcbforge status renew --last-reviewed --note "<user approval>"
```

Renewal stops at changed content, failed checks or an explicit reopen. It preserves the previous approval fingerprints in history.
For a circuit revision, record `status mark circuit reopened` with the reason.
Edit the saved schematic, repeat its checks and approval, then prepare and verify a new native PCB update.
Do not recapture the architecture baseline for a circuit-only change.

A checked status write records stale approvals durably. Preserve completed work and backups when a transition becomes inactive.
A drawing-only edit preserves electrical approval but requires fresh presentation checks and review of the changed sheets.

`policy.yaml` requests exceptions. It does not approve them.
After the user approves an exact exception, record `pcbforge policy approve-exception <id> --note "<decision>"`.
After fabrication output, refresh sourcing and record the user's review with `pcbforge policy confirm-sourcing`.
ORDER remains blocked until that sourcing confirmation is current.

## LAYOUT assist

Perform a spatial edit only when the user explicitly requests it during open LAYOUT with a current handoff.
That authorization covers the named task. Preserve the schematic-owned circuit data.
Ensure KiCad has no unsaved PCB changes before applying edits.

Use `apply-pattern` or `apply-floorplan` when the request matches those tools.
Use their dry run to inspect the proposed moves. They create backups and verify the result.
For other authorized edits, create a PCB backup and state its path before editing.

Change only the requested positions, sides, rotations, tracks, vias, zones or graphics.
Keep reference identity, footprint assignments, fields, pad nets and board rules intact.
Report the actual changes, DRC results and unresolved work.
Record `pcbforge status mark layout ai-assisted --note "<request and result>"` as append-only history.
The user still declares layout complete and approves verification.

## Engineering review

Use official symbols and footprints first, including MCU and connector libraries.
Check actual pin functions and package dimensions against manufacturer sources.
Use custom assets only when the official library cannot represent the part, and record the search rationale.
Prefer JLC basic parts where the specification and policy permit them. Record exact supplier identity and research dates.

Readability lint uses approximate text extents. Inspect the saved schematic previews yourself.
Apply the [human-readable capture rules](../WORKFLOW.md#human-readable-schematic-capture) during schematic capture and review.
Use direct wires within functional blocks and labels primarily between blocks or sheets.
Show supporting components beside their associated circuit, with visible local connections.
Check that a reader can understand each block without a search for matching net labels.
Do not treat ERC success or label counts as proof of human comprehension.
Check continuous functional paths, power direction, labels, crossings, hierarchy and field placement.
Review independent test coverage and calculations. ERC alone is insufficient.

Live stock lookup and engineering audits beyond the implemented checks remain manual research tasks.
The native PCB update and final visual acceptance remain user-operated steps.
Do not describe simulated updates or command-line exports as successful GUI validation.
