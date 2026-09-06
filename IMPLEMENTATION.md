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

## Implementation checkpoint

The native CLI, scaffold, electrical checks, approval gates, PCB synchronization,
policy, placement and fabrication integration are implemented. Atopile and its
compatibility patches have been removed from source, lockfile and installed runtime.

The full regression run passed 475 tests with two opt-in external checks skipped.
A subsequent 29-test run passed after adding native sheet interfaces and shared
multi-unit field updates. Real KiCad extraction covers repeated hierarchy,
multi-unit symbols, excluded parts and transactional saves.

Clean-revision initialization and the representative native pilot run next.
User-operated GUI synchronization and final visual acceptance remain pending.

Regression command: `toolchain/.venv/bin/python -m unittest discover -s tests -t . -q`.
