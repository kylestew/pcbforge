<!-- pcbforge-architect-schema: 2 -->
# ARCHITECT procedure

Follow [WORKFLOW.md](../WORKFLOW.md) and the standard review and approval protocol in [the operating manual](operating-manual.md).

Write `docs/architecture.md` with marker `pcbforge-architecture-diagram-schema: 2`.
Show functional blocks, power domains, interfaces and direction in a Mermaid diagram.
Map every specification requirement to a block or a sourced assessment.
The diagram describes function; it is not another schematic netlist.

Write `docs/mcu.md` with the exact MCU, package, peripheral assignments, boot support, debug access and power requirements.
Present the architecture and MCU proposal to the user before creating the IOC implementation.
Do not select unrelated circuit parts during the MCU workstream.

After proposal approval, create and validate the IOC using [mcu.md](mcu.md).
Perform a one-to-one audit of physical pins and planned interfaces.
Run `pcbforge finish-architect` to record `review/circuit/architecture-baseline.json`.
The checked transition opens CIRCUIT without another user approval. Preserve the existing board throughout this step.
