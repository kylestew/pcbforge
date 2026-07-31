<!-- pcbforge-mcu-schema: 1 -->
# pcbforge — MCU workstream inside ARCHITECT

This subordinate playbook operationalizes the MCU portion of ARCHITECT in
[`WORKFLOW.md`](../WORKFLOW.md). The AI selects the exact STM32 and its pin
mapping, creates the CubeMX configuration, and proves that CubeMX can consume
it. The user may review the result in CubeMX, but does not have to author it.

## Preconditions

1. Read the project-local `AGENTS.md`, the complete `spec.md`, `STATUS.md`,
   `agent/operating-manual.md`, and the proposed architecture graph.
2. Before source work, record the exact device/package and provisional resource
   plan in `docs/mcu.md`; it is part of the ARCHITECT proposal packet.
3. Run the pinned `scripts/ato build` and record the KiCad board hash. MCU work
   may change connectivity later, but it must never change spatial board data.
4. Treat `src/mcu.ato` as an interface contract at the start of this phase.
   Preserve every approved public interface.

## Select the exact device

Translate the approved MCU interface into a resource checklist: power and
grounds, SWD, debug UART when enabled, buses, ADC/DAC channels, timer/PWM
channels, USB/CAN, interrupts, DMA needs, clocks, boot/reset behavior, and
spare capacity.

Choose the exact orderable STM32 and package before ARCHITECT proposal
approval. Check current availability and
price, the official datasheet, package pinout, errata where relevant, and the
pinned CubeMX database. Prefer a device that:

- belongs to the `spec.md` STM32 family;
- satisfies every required peripheral without multiplexing conflicts;
- leaves practical flash, RAM, timer, DMA, and spare-pin margin;
- avoids unnecessary package size or cost;
- remains compatible with the approved module interfaces.

Make the choice yourself when one candidate clearly satisfies the contract.
Ask the user only when a material tradeoff remains, such as cost versus spare
capacity, package size versus routability, oscillator strategy, availability,
or mutually exclusive peripheral mappings. Show the concrete alternatives and
your recommendation.

## Write the proposal plan

Before proposal approval, `docs/mcu.md` must present the exact device/package,
interface-to-peripheral allocation, provisional physical pin map, clocks,
DMA/timer/interrupt use, debug access, spare resources, sourcing evidence, and
every material tradeoff. Do not create or revise the IOC or `src/mcu.ato`
before that packet is approved.

## Allocate pins and create the `.ioc`

Use the canonical tracked path:

```text
firmware/<project>.ioc
```

Start from a CubeMX 6.18 configuration for the selected device, then assign:

1. power, ground, reset, boot, and the chosen clock sources;
2. `SYS_JTMS-SWDIO` and `SYS_JTCK-SWCLK` for SWD;
3. fixed-function or most-constrained peripherals;
4. ADC and other noise-sensitive functions;
5. timer, DMA, and interrupt-constrained functions;
6. flexible GPIO and remaining buses;
7. intentional spares and reserved pins.

Configure peripheral modes and clocks, not just pin names. Preserve a useful
margin and avoid assignments that create an obvious layout burden. Do not
place, route, or edit KiCad spatial data while doing so.

Give each application pin a unique upper-snake-case logical label whenever
CubeMX supports one. Labels describe the approved interface and role, not the
physical pin. The debug UART labels are exactly `DEBUG_UART_TX` and
`DEBUG_UART_RX`. Examples include `CLIMATE_I2C_SCL`, `BLE_UART_RX`,
`STATUS_LED`, and `PAIR_BUTTON`.

The `.ioc` is authoritative for the exact MCU, package, pins, peripheral
modes, and clocks. Do not hand-edit `src/mcu.ato` first and reverse-engineer
the `.ioc` afterward.

## Validate and present

From the project directory, run:

```bash
<pcbforge-root>/scripts/pcbforge check-ioc
```

This validates the project contract, required debug signals, logical labels,
peripheral coverage, and CubeMX metadata. It then asks pinned CubeMX 6.18 to
load and save the configuration in a temporary directory and compares the
meaningful assignments. It never rewrites the source `.ioc`.

Resolve every error before continuing. Also inspect the resulting mapping
against the official package pinout and the approved interface checklist; a
successful parser round trip does not prove that a system-level choice is
good.

Present:

1. exact orderable part number, package, and selection rationale;
2. interface-to-peripheral allocation;
3. physical pin, logical label, signal, and mode table;
4. clock sources and important derived clocks;
5. DMA, timer, ADC, interrupt, boot, and reset choices where relevant;
6. spare and reserved resources;
7. sourcing evidence, assumptions, warnings, and resolved tradeoffs;
8. the successful `check-ioc` result.

Also present the MCU support circuit in a conventional electrical view when it
already exists: supplies and grounds, local decoupling, reset, boot, clock,
SWD, and assigned application pins. If exact support components or topology
remain undecided, label the view provisional and state clearly that the
complete authored circuit overview and passive-purpose explanations are mandatory at
the first CIRCUIT proposal gate before any physical source edits.

Offer to pause while the user opens `firmware/<project>.ioc` in CubeMX 6.18.
This review is optional and is not an approval gate. If the user saves any
CubeMX changes, treat them as deliberate overrides: do not overwrite them,
show the semantic changes, rerun `check-ioc`, and reconcile all derived code.

## Derive the MCU module

`ioc2code` is not implemented yet. Say so. Until it exists, manually replace
the interface-only body of `src/mcu.ato` from the checked `.ioc` while
preserving the approved public interfaces.

Perform a one-to-one audit after transcription:

- every application label maps to exactly one MCU pin;
- every public interface maps to the intended peripheral signals;
- all MCU power, ground, decoupling, reset, boot, and clock pins are covered;
- SWD and the optional debug UART remain accessible;
- no pin or signal exists only in `src/mcu.ato`;
- no required `.ioc` assignment is absent from `src/mcu.ato`.

Run the pinned compiler and report the source diff and audit result before
finishing ARCHITECT. ARCHITECT is technically ready only when the IOC passes,
the derived module matches it, and the approved functional graph is still
satisfied. Then run `pcbforge finish-architect`. This checked transition
requires the current proposal approval, captures
`review/circuit/source-baseline.json`, and opens CIRCUIT without another user
approval. CIRCUIT blocks if physical source or board topology changes before
its proposal approval.
