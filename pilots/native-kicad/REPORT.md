# Native KiCad validation report

Validated on 2026-09-06 with KiCad 10.0.3 and STM32CubeMX 6.18.
Implementation revision: `3414af4` on `feature/native-kicad-workflow`.

The automated implementation checks pass. GUI synchronization and final visual acceptance remain unverified.
Atopile is absent from the dependency lock and installed runtime. Python dependencies now contain only PyYAML.

| Check | Result |
|---|---|
| Full regression suite | 479 tests: 477 passed, 2 opt-in external tests skipped |
| Real KiCad initialization | Passed for fresh two- and four-layer projects, with clean revision pins |
| Real CubeMX round trip | Passed for the frozen STM32F103CBT6 IOC |
| Native extraction | 69 components and 67 nets across three connected Roamer sheets |
| Unchanged native save | Original file bytes preserved |
| Cosmetic native edit | Electrical graph and identities preserved; backup created |
| Deliberate electrical fault | Changing C11 from 100 nF to 1 pF failed the independent bypass requirement |
| Fault recovery | Backup matched the original; restored graph matched the baseline |
| Source and routed PCB | Original hashes preserved |
| Workflow and update verification | Unit tests passed for one circuit approval, simulated updates, parity and spatial preservation |

The two skipped tests passed in separate explicit runs. CubeMX required permission to write its normal application log outside the sandbox.
The default suite also runs real KiCad extraction and electrical checks; these are not confined to the opt-in tests.

The pilot found two native drawing details that required corrections: top-level field visibility and bottom-justified label text.
Both now have regression coverage.

The historical schematic retains 106 readability findings: 69 missing-purpose warnings, 16 crossing warnings, and 21 blocking drawing findings.
These findings remain available for review in [READABILITY-FINDINGS.json](READABILITY-FINDINGS.json).
No exclusions or approvals were invented to clear them.
The historical design also lacks the new sourced-facts contract. This pilot does not claim complete electrical acceptance or migrate that project.

[RESULTS.json](RESULTS.json) records measurements, source hashes, preview hashes and the detected semantic change.
The disposable native projects and previews were written to `/private/tmp/pcbforge-native-pilot-final`.
Follow [README.md](README.md) to reproduce the pilot in a new directory.

Regression commands:

```sh
toolchain/.venv/bin/python -m unittest discover -s tests -t . -q
PCBFORGE_RUN_REAL_INTEGRATION=1 toolchain/.venv/bin/python -m unittest tests.test_initialize.RealToolchainIntegrationTests tests.test_ioc.RealCubeMxIntegrationTests -v
```

A user-operated KiCad save and native PCB update must still be checked before claiming GUI acceptance.
Simulated PCB updates establish verifier behavior only. Final usability and hardware acceptance require review of the actual artifacts.
