# Native KiCad workflow implementation

Branch: `feature/native-kicad-workflow`.
Target: KiCad 10.0.3. Fresh projects only. No Atopile runtime.

The saved schematic owns the circuit. Independent requirements and tests own
acceptance. The PCB owns physical layout. One circuit approval precedes the
native GUI PCB update. A checked transition completes CIRCUIT.

Implementation sequence:

1. Remove the compiler and pin the native KiCad tools and formats.
2. Implement saved-schematic extraction, safe editing, previews and diffs.
3. Implement electrical tests, part/IOC checks and saved-file readability lint.
4. Replace circuit gates and add PCB update preparation and verification.
5. Connect policy, placement and fabrication to schematic evidence.
6. Update instructions and run unit, real KiCad and pilot acceptance checks.

Acceptance includes native saves followed by AI edits, hierarchical instances,
deliberate electrical faults, source staleness and preserved routed geometry.
The GUI synchronization and final visual review remain user-operated steps.

## Validation

The native CLI, scaffold, electrical checks, approval gates, PCB synchronization,
policy, placement and fabrication integration are implemented. Atopile and its
compatibility patches are absent from source, lockfile and installed runtime.

The final suite ran 479 tests: 477 passed and two opt-in external checks were skipped.
Both external checks passed separately: real two- and four-layer KiCad initialization,
and a CubeMX round trip. Native tests also cover repeated hierarchy, shared multi-unit
fields, excluded parts, transactions, drawing visibility, staleness and PCB preservation.

The representative pilot extracted 69 components across three connected sheets.
It preserved unchanged saves, cosmetic graph identity and routed PCB hashes.
A deliberately incorrect bypass capacitor failed the independent requirement test.
The historical drawing findings remain recorded; no full hardware approval is claimed.

See [the validation report](pilots/native-kicad/REPORT.md) for results and reproduction commands.
User-operated GUI synchronization and final visual acceptance remain unverified.
