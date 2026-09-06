<!-- pcbforge-circuit-schema: 2 -->
# CIRCUIT procedure

Follow [WORKFLOW.md](../WORKFLOW.md) and the standard review and approval protocol in [the operating manual](operating-manual.md).
Start after the checked architecture baseline.

1. Read the specification, functional diagram, MCU plan and checked IOC.
2. Define independent electrical requirements before drawing the implementation. Cover each interface and each supply path.
3. Select exact parts. Check manufacturer pin tables, operating limits, package drawings and recommended application circuits.
4. Use official KiCad symbols and footprints first. Use `Device:R` with `Resistor_SMD:R_0603_1608Metric` for an ordinary 0603 resistor.
5. Put supplier/BOM metadata in native fields. Record MPN, LCSC, Datasheet and `pcbforge_purpose` on every fitted component.
6. Put reviewed limits and package pin functions in `electrical-facts.yaml`. Keep sources specific enough to audit.
7. Edit the native schematic with KiCad or the transactional API in [circuit-kicad.md](circuit-kicad.md).
8. Write Python acceptance tests against extracted connectivity and values. Use the checks described in [electrical-tests.md](electrical-tests.md).
9. Run `pcbforge check-parts` and `pcbforge check-circuit --write-report`. Resolve every blocking finding.
10. Inspect the previews. Check readable paths, power direction, labels, reference positions and sheet interfaces.

Present the electrical requirements, exact parts, calculations, semantic delta and every changed sheet to the user.
Explain remaining engineering assessments and finding exclusions. Use the standard review and approval protocol.

After schematic approval, run `pcbforge prepare-pcb-update` and give the user the native KiCad update instructions from WORKFLOW.md.
After the user saves the PCB, run `pcbforge check-pcb-update` and `pcbforge finish-circuit`.
Then prepare the placement brief. Do not record another CIRCUIT human approval for synchronization.

For a circuit revision, preserve retained symbol UUIDs. Recheck the independent tests against the changed saved schematic.
Keep the previous synchronization snapshot and PCB backup until the new update passes.
