# Roamer Rev A atopile pilot

This directory validates pcbforge's circuit-as-code design against the immutable
Roamer Rev A manufacturing release.

## Source baseline

- Repository: `/Users/kylestewart/Projects/roamer-bot`
- Tag: `rev-a-jlcpcb-2026-07-18`
- Commit: `3bc3361573dd21070efe9f76bba473947e2a0c21`
- KiCad: 10.0.3

`baseline/source/` was extracted from that tag. The roamer worktree is never an
input and is never modified.

## Safety

All atopile commands run through `scripts/ato`. The wrapper marks the process as
CI and redirects config, logs, Python installs, and package caches into this
pilot. It must not install a global KiCad plugin or edit user KiCad settings.

The compatibility gate runs before the full circuit port. A failure to preserve
KiCad 10 user-authored board data stops the pilot and becomes the result. The
current pinned toolchain is blocked at that gate; see `REPORT.md`.

## Reproduce

```sh
./pilots/roamer-rev-a/run-pilot bootstrap
./pilots/roamer-rev-a/run-pilot baseline
./pilots/roamer-rev-a/run-pilot compatibility
./pilots/roamer-rev-a/run-pilot all
```

See `REPORT.md` for the decision and supporting evidence.

`compatibility` currently exits `1` intentionally after writing its evidence:
atopile `0.15.7` cannot parse the KiCad 10 named-net fixture.
