<!-- pcbforge-build-test-schema: 1 -->
# Build + test playbook

Use this playbook as the deterministic acceptance gate inside the CIRCUIT phase
defined in [`WORKFLOW.md`](../WORKFLOW.md). It proves that the pinned compiler
resolves the intended exact BOM and PCB connectivity, executes the declared
electrical assertions, emits the required artifacts, and preserves all
user-owned spatial board data.

Live stock and pricing, placement, routing, KiCad PCB DRC, and fabrication
output are separate gates. Explanatory circuit review and exact compiled parity
are also required within CIRCUIT. `pcbforge check-policy` cross-checks the
exact BOM against the tracked offline sourcing evidence, but does not claim
that the evidence is still live. Do not claim that this gate proves these
separate facts.

## 1. Write the acceptance contract

Create tracked `build-test.yaml` with schema 1:

```yaml
build_test_schema: 1
build: default

bom:
  - lcsc: C12345
    mpn: STM32G071KBT6
    footprint: Package_QFP:LQFP-32_7x7mm_P0.8mm
    quantity: 1

board_footprints: 38

assertions:
  - rail-3v3-tolerance
  - regulator-headroom
```

Derive this file from the approved implementation, not from whatever the last
compiler output happened to contain:

- list every resolved BOM line exactly once by LCSC ID;
- copy the intended exact MPN and KiCad footprint;
- aggregate the intended quantity for repeated parts;
- record the expected total physical PCB footprints;
- list every required source assertion by stable test ID.

The BOM is exact. Missing, unexpected, duplicate, unselected, or mismatched
components fail. Every BOM usage/designator must appear exactly once on the
PCB. Canonical `H*` mounting-hole and `TP*` test-point footprints are permitted
as intentionally unfitted PCB features and count toward `board_footprints`;
other non-BOM PCB footprints fail.

## 2. Identify executable assertions

Put a unique marker immediately before each required atopile assertion:

```ato
# pcbforge-test: rail-3v3-tolerance
assert power_3v3.voltage within 3.3V +/- 5%
```

Use kebab-case IDs. The IDs in source and `build-test.yaml` must match exactly.
The checker verifies traceability; the pinned compiler evaluates the assertion.
Encode every applicable deterministic electrical acceptance rule, including
rail tolerance, regulator headroom/load, required pullups, or other
project-specific invariants that atopile can express. Never substitute prose
for an available compiler assertion.

## 3. Run the gate

From the project root:

```sh
pcbforge status --check --write
```

This performs a frozen pinned build and requires:

1. valid project guidance and `build-test.yaml`;
2. compiler success, including compiler-native electrical checks and all
   atopile assertions;
3. compiler manifest, BOM JSON, and BOM CSV artifacts for the selected build;
4. exact LCSC, MPN, footprint, quantity, and designator agreement;
5. exact BOM-to-PCB reference parity and expected footprint count;
6. resolved pad-to-net connectivity on the PCB;
7. an unchanged no-op spatial fingerprint: footprint placement and side,
   tracks, vias, zones, board outline, graphics, and user artwork;
8. a current tracked `docs/build-test.md` report.

Use `pcbforge check-build-test` for a non-writing diagnostic run, or
`pcbforge check-build-test --write-report` to save the report directly. A
non-writing pass does not complete CIRCUIT; the dashboard also requires the
current tracked report. These direct commands always run their own frozen
build. The status cycle builds once and validates those same artifacts. A
failed run never overwrites the last passing report.

## 4. Resolve failures

- Compiler/assertion failure: fix circuit source, not generated build output.
- Contract mismatch: determine whether implementation or reviewed intent is
  wrong; change `build-test.yaml` only when the intended design changed.
- BOM/PCB parity failure: reconcile source identity, designators, footprints,
  and connectivity, then rebuild.
- Spatial-preservation failure: stop. Inspect the diff and restore the
  ownership invariant before continuing; never accept a build that moved or
  rewrote user artwork.
- Stale report: rerun the full checked write after the relevant change.

## 5. Handoff

Review `docs/build-test.md` and present:

- the exact BOM and quantities;
- the assertion IDs and what they prove;
- footprint/designator and connectivity totals;
- artifact and input hashes;
- spatial-preservation result;
- any limitation that remains for visual review, DRC, stock, or fabrication.

Current saved evidence is part of the final CIRCUIT packet. Follow the
[standard review and approval protocol](operating-manual.md#review-and-approval-protocol)
for that gate, present the packet, and stop.
