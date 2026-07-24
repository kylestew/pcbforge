# Roamer Rev A atopile pilot report

Date: 2026-07-24

## Decision

**Blocked — stop before the full Roamer port.**

Pinned atopile `0.15.7` cannot load a KiCad `10.0.3` board. KiCad 10 accepts
and renders the contained fixture, but atopile fails during `Loading PCB` on
the first KiCad 10 named-net pad field:

```text
UnexpectedType in kicad.pcb.Pad field 'number' at 249:9:
got string "2" but expected unquoted number
source: (net "2")
```

This is an early compatibility-gate failure, so the full circuit translation,
ioc2code prototype, assertion port, and schematic-viewer evaluation were not
started. The agreed pilot rule was to stop here rather than spend the porting
budget or silently change generators.

## What was established

### Released-design baseline

The baseline is extracted from Roamer tag `rev-a-jlcpcb-2026-07-18`, commit
`3bc3361573dd21070efe9f76bba473947e2a0c21`. The source worktree is not used
or modified.

| Check | Result |
|---|---:|
| KiCad version | `10.0.3` |
| Board format | `20260206` |
| Schematic components | 69 |
| Nets | 67 |
| Connected nets | 43 |
| Explicit no-connect nets | 24 |
| Board footprints | 70 |
| Board user-art objects | 771 |
| KiCad ERC | 0 violations |
| JLC BOM rows | 30 |
| JLC CPL rows | 57 |

The baseline also confirms an existing cross-domain mismatch that a future
port must resolve explicitly:

- the tagged CubeMX file assigns `PB10=I2C2_SCL` and `PB11=I2C2_SDA`;
- released schematic MCU pins 21 and 22 are explicitly unconnected.

The pilot does not rewrite either released artifact.

### Compatibility gate

The toolchain is locked by `uv.lock`:

- Python `3.14`;
- atopile `0.15.7`;
- atopile-kicad-python `0.5.1`;
- KiCad CLI `10.0.3`.

The gate proceeded in progressively narrower steps:

1. atopile generated an empty KiCad 9-format board successfully.
2. atopile generated a one-part board successfully using a vendored,
   commit-pinned official example part, avoiding the unavailable component
   API.
3. The fixture was expressed in KiCad 10 format (`20260206`) with KiCad 10
   named nets, one placed footprint, two routed segments, a via, a zone, an
   outline, and silkscreen artwork.
4. KiCad `10.0.3` loaded and rendered that fixture successfully.
5. The no-op atopile synchronization failed while parsing the first named-net
   pad, before PCB update.
6. The failed build left the input byte-identical:
   `65e2dc06fd15dfd8d350b1ddd544bb14691f59971350c4f2304960ffe7e4565a`.

The atomic-failure slice of the sync contract therefore passed for this parser
failure. No-op idempotence, intended-delta isolation, or identity stability
cannot be assessed because atopile never reaches synchronization.

The component API hostname also failed DNS resolution during the first generic
resistor attempt. That is not the deciding failure: the vendored part removed
network part selection from the compatibility test, and the KiCad 10 parser
failure remained.

### Containment note

Before the pilot wrapper existed, an initial atopile help/version invocation
ran atopile's normal startup and created its global KiCad 9 plugin loader,
enabled the KiCad 9 plugin API, and created
`~/Library/Logs/atopile/build_logs.db`. Those files were not removed because no
pre-run snapshot existed. Every reproducible pilot command now runs through
`scripts/ato`, sets CI/non-interactive mode, and redirects configuration,
cache, Python installs, and logs under this pilot.

## Why stopping is the right result

The released Roamer PCB is already KiCad 10 and contains the human-owned layout
the design contract promises to preserve. Downgrading that board to KiCad 9
would move risk onto placement, routing, zones, rules, and identity—the exact
assets the gate is meant to protect. A full circuit port cannot provide useful
evidence until the compiler can read the real board format.

This also appears broader than accepting one quoted value: KiCad 10 uses named
net references across pads, tracks, vias, and zones and changed layer numbering
and serialized board structure. Any local compatibility patch must be tested
as a reader/writer change, not treated as a one-line parser relaxation.

## Recommended next experiment

Resume only when one of these is deliberately selected:

1. Pin an atopile release that declares KiCad 10 PCB support.
2. Maintain a small, version-pinned atopile/PCB-parser patch for KiCad 10.
3. Re-open the compiler choice and explicitly authorize the SKiDL fallback.

For options 1 or 2, rerun this fixture first. The acceptance condition is:

- atopile build exits zero;
- KiCad 10 renders the result;
- placement, segments, via, zone, outline, and text fingerprints are unchanged;
- a second build is byte-identical;
- controlled add, rename, footprint-swap, and remove cases are atomic and
  limited to compiler-owned data.

Only then should the Roamer graph be ported. When that happens, use a
pilot-local corrected `.ioc` policy for PB10/PB11 and keep the tagged release
as the comparison oracle.

## Reproduction

From the pcbforge repository root:

```sh
./pilots/roamer-rev-a/run-pilot bootstrap
./pilots/roamer-rev-a/run-pilot baseline
./pilots/roamer-rev-a/run-pilot compatibility
```

`compatibility` is expected to exit `1` with the blocked message. It writes the
captured compiler failure and machine-readable outcome before exiting.

Evidence:

- `results/baseline-manifest.json`
- `results/baseline-graph.json`
- `results/baseline-board.json`
- `results/compatibility-build.txt`
- `results/compatibility-result.json`
- `results/compat-before.json`
- `results/compat-after-failed-sync.json`
