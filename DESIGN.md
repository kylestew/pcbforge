# PCBForge design

PCBForge separates circuit implementation, electrical acceptance and physical layout.
The saved KiCad schematic is the circuit implementation. The PCB stores physical design.
Requirements, sourced facts and independent Python tests define acceptance.

## Native capture decision

The native workflow removes Atopile, its registry dependency, compiler adapters and duplicate review model.
AI and human editors use the same schematic. KiCad resolves connectivity and exports the circuit graph and BOM.
The graph is derived evidence. It is never an authored replacement for the schematic.

A capable AI still benefits from deterministic checks. Increasing model capability does not remove the need for electrical requirements,
exact pin mappings, stable object identity, repeatable tests or usable drawings.
The checks therefore attach to saved KiCad artifacts and independent acceptance tests.

The supported installation is KiCad 10.0.3. This pin makes file formats, libraries and checks reproducible.
It is not an Atopile compatibility requirement. Update it through a tested toolchain change.

## Ownership and synchronization

Native symbol and sheet UUIDs preserve schematic-to-PCB identity.
A transactional editor preserves untouched records and validates edits before replacing source files.
Users can edit and save the same sheets directly in KiCad.

After schematic approval, the user performs KiCad's native PCB update.
PCBForge compares the result with the approved graph and a recorded pre-update board backup.
It protects retained placement, pad geometry, copper, zones, outline and graphics.
It recognizes approved net renames and ignores regenerated zone fill caches.
The tool verifies synchronization without controlling the GUI or silently restoring the PCB.

## Evidence and approvals

CIRCUIT has one human schematic approval, followed by a checked synchronization transition.
The review binds electrical intent and drawing presentation. The recorded electrical approval survives later cosmetic drawing edits.
Cosmetic edits invalidate presentation checks and previews. Electrical changes invalidate dependent approvals and evidence.

Approvals remain append-only and bind phase-specific fingerprints.
The specification's Decisions log is non-normative. Requirements elsewhere in the specification remain normative.
SPEC binds policy declarations. CIRCUIT also binds assurance evidence and exceptions. ORDER owns sourcing currency.
An upstream change can renew unchanged downstream gates only through an explicit user-approved cascade packet.

## Limits

ERC finds classes of connection errors. It cannot prove that a circuit meets its requirements.
Readability lint measures geometry with approximate text extents. It cannot replace visual inspection.
Python acceptance tests can encode incorrect assumptions. Review their requirements, limits and sources separately from the drawing.
Package pin counts alone do not establish pin function correctness.

Manufacturing policy, layout ownership, explicit exceptions and ordering remain governed by [WORKFLOW.md](WORKFLOW.md).
GUI synchronization and final visual acceptance require a user-operated validation step.

## Reproducibility and scope

Fresh projects pin the tool revision, dependency lock, KiCad version, rules and guidance schemas.
Existing projects stay on their historical checkout. There is no in-place migration or alternate compiler mode.

Reusable circuits are native schematic blocks with documented interfaces, tests, sourced facts and hardware provenance.
The catalog distinguishes examples from proven modules. Publication requires explicit review.

The historical Atopile experiments remain under `pilots/`. Their results describe that earlier toolchain.
They are not dependencies of the native workflow.
