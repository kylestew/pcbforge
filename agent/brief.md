<!-- pcbforge-brief-schema: 5 -->
# Legacy schema-14 placement brief playbook

This file exists only for schema-14 projects that have not yet run
`pcbforge migrate-phase-transitions`. New projects use
`agent/layout-handoff.md`.

Use the outputs, `placement.yaml` schema, manufacturing limits, completeness
rules, spatial-ownership rules, and staleness rules in
`agent/layout-handoff.md`. For schema 14, BRIEF remains numbered phase 6 and
uses these legacy commands:

```sh
pcbforge brief
pcbforge check-brief
pcbforge status review brief
```

Present `docs/placement-brief.md` beside the current approved CIRCUIT
explanatory SVG. Generation and checks do not grant approval. After the user
explicitly accepts the exact packet, record:

```sh
pcbforge status approve brief --fingerprint <sha256> \
  --note "Approved docs/placement-brief.md beside the current CIRCUIT overview"
```

If the circuit presentation is inadequate, record
`pcbforge status mark brief blocked --note "<reason>"` and stop before LAYOUT.
Never move footprints, create geometry, route, or change the PCB while
preparing BRIEF.
