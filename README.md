# PCBForge

PCBForge supports an AI-assisted PCB workflow with native KiCad schematics, independent electrical tests and explicit user approvals.
It uses **KiCad 10.0.3** and Python 3.14. Atopile is not installed or used by the current workflow.

Install KiCad at `/Applications/KiCad/KiCad.app`, then prepare the Python environment:

```sh
uv sync --project toolchain --frozen
./scripts/kicad-cli version
./scripts/pcbforge --help
```

The MCU checks also require the CubeMX installation configured in `scripts/cubemx`.
Keep the tool checkout clean before initializing a project. New projects pin its Git revision and dependency lock.
The launcher dispatches an existing project to its pinned checkout. Old projects remain on their historical workflow.

Create a project directory and write `spec.md` and `policy.yaml` with the agent.
Follow [WORKFLOW.md](WORKFLOW.md), the normative workflow, to review SPEC and initialize the project.
The agent then develops the architecture, IOC, editable schematic and electrical acceptance tests.

```sh
./scripts/pcbforge status --next /path/to/project
./scripts/pcbforge check-circuit --write-report /path/to/project
```

After schematic approval, the user performs KiCad's native PCB update. PCBForge verifies the update before layout begins.

| Location | Purpose |
|---|---|
| `pcbforge/` | Validation, schematic editing, workflow and fabrication tools |
| `agent/` | Agent procedures and the shared approval protocol |
| `rules/`, `policies/` | Pinned manufacturing rules and policy |
| `modules/` | Catalog of reusable native schematic blocks |
| `patterns/` | Sourced placement patterns |
| `tests/` | Unit tests and real KiCad regression tests |
| `pilots/` | Recorded experiments and representative projects |

Run the test suite with:

```sh
uv run --project toolchain python -m unittest discover -s tests -t . -q
```

See [DESIGN.md](DESIGN.md) for ownership and reproducibility decisions.
