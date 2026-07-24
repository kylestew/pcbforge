# KiCad 9 atopile pilot report

## Decision

Proceed with the atopile circuit port using the official KiCad 9 multichannel
mixer fixture.

The pinned atopile toolchain can parse, project, serialize, and reopen the
complete routed board without losing normalized PCB content. KiCad 9.0.9
accepts and renders the serialized result.

This is a reader/writer compatibility result. Atopile-managed synchronization
will be tested after the circuit has been represented in `.ato`, because the
legacy board has no atopile ownership metadata.

## Toolchain

- atopile: `0.15.7`
- KiCad: `9.0.9`
- PCB format: `20241229`
- KiCad 9 CLI:
  `/Applications/KiCad 9/KiCad.app/Contents/MacOS/kicad-cli`
- KiCad 10 remains installed separately at `/Applications/KiCad`

The KiCad 9 application passed macOS disk-image verification, code-signature
verification, and Gatekeeper notarization checks.

## Fixture selection

Nineteen PCB files from the official KiCad 9.0.9 demo corpus were evaluated.
Some exposed parser errors; others lost fields such as teardrops or auxiliary
origins during serialization.

The multichannel mixer was selected because it is substantial and its
serialization preserves the complete normalized token multiset:

- 114 schematic components and PCB footprints
- 80 schematic nets
- 81 PCB net-table entries, including net zero
- 576 routed segments
- 29 vias
- 6 zones
- repeated hierarchical channel sheets

The routed source board SHA-256 is
`44b7c119a0d05c98e9f294659879184038a5f6c30073c04d9536990429ca413e`.

The previously considered local 6502 board was rejected because atopile's
serializer dropped board setup data, embedded-font data, and a pad teardrop.
The source project was never modified and its temporary copy was moved out of
the workspace.

## Compatibility result

Atopile loaded:

- 114 footprints
- 81 nets
- 576 segments
- 29 vias
- 6 zones

Its high-level PCB projection retained all 114 footprint collections and
reconstructed 64 populated nets.

The first serialized board differs byte-for-byte because atopile:

- reorders footprint child records;
- normalizes decimal formatting;
- quotes `ki_fp_filters` property names.

Those changes are canonicalization rather than content loss:

- source and serialized files have the same 358,144 normalized tokens;
- their normalized token-multiset hashes are identical;
- all footprint placements are identical;
- all tracks, vias, zones, and top-level user art are identical;
- a second parse/serialize cycle is byte-identical to the first;
- KiCad 9 renders the serialized board successfully.

Evidence is in `results/compatibility-result.json`.

## Existing design-rule findings

The fixture is an upstream demo, not a clean manufacturing release. KiCad 9
reports existing findings that must be treated as baseline rather than new
atopile regressions:

- ERC: 45 findings
  - 32 footprint-link warnings
  - 9 library-symbol mismatch warnings
  - 3 undriven power-pin errors
  - 1 unconnected-pin error
- DRC: 103 findings
  - 81 library-footprint mismatch warnings
  - 12 clearance errors
  - 10 other warnings

The circuit port must preserve or improve these counts and must not silently
change the released connectivity or routed geometry.

## Next phase

Port the repeated channel strip as one atopile module, instantiate the eight
channels at the root, then add shared power and I/O circuitry. Connectivity
acceptance should use KiCad's exported 80-net schematic graph; atopile's current
high-level PCB reconstruction produces only 64 populated nets and is therefore
not an equivalent standalone oracle.

After the port matches all 114 components and 80 schematic nets, run a managed
no-op synchronization on a disposable board copy. Require preservation of
placements, routes, vias, zones, user art, and the existing ERC/DRC baseline
before attempting a controlled circuit change.
