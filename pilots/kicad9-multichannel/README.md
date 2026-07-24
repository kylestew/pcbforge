# KiCad 9 multichannel mixer atopile pilot

This pilot exercises atopile against a substantial, routed KiCad 9 design
without touching a working hardware repository.

## Selected fixture

The fixture is the `multichannel` demo bundled with KiCad 9.0.9:

- 114 footprints
- 81 PCB nets
- 576 routed segments
- 29 vias
- 6 zones
- hierarchical schematic with eight repeated channel sheets

The board uses format `20241229` and identifies its generator as KiCad `9.0`.
The captured routed-board SHA-256 is
`44b7c119a0d05c98e9f294659879184038a5f6c30073c04d9536990429ca413e`.

The fixture's component definitions identify CERN DEM JLC/JMW as authors and
carry CC-BY-SA 4.0 notices. See `NOTICE.md`.

## Why this board

Nineteen official KiCad 9 demo boards were evaluated. Several either failed
atopile parsing or lost unsupported fields during serialization. This board
retains the complete normalized token multiset, all placements, and all
top-level routed geometry. Atopile changes only ordering, numeric formatting,
and quoting of `ki_fp_filters`, then reaches a stable canonical form.

The candidate scan is recorded in `results/candidate-evaluation.json`.

## Tool isolation

The pilot explicitly uses:

```text
/Applications/KiCad 9/KiCad.app/Contents/MacOS/kicad-cli
```

KiCad 10 remains separately installed at `/Applications/KiCad`.

Atopile and all caches and configuration are contained inside this pilot.

## Run

```sh
./pilots/kicad9-multichannel/run-pilot bootstrap
./pilots/kicad9-multichannel/run-pilot baseline
./pilots/kicad9-multichannel/run-pilot compatibility
./pilots/kicad9-multichannel/run-pilot all
```

`baseline` uses KiCad 9 to export the schematic graph, run ERC and DRC, render
the board, and fingerprint immutable inputs.

`compatibility` makes atopile parse the complete routed board, project it into
its high-level PCB model, reconstruct its netlist, and serialize a copy. The
copy must be accepted by KiCad 9 and become byte-stable on its next round trip.
The command never writes to `baseline/source`.

An atopile-managed no-op synchronization comes after the circuit port because
the legacy demo does not contain atopile ownership metadata.
