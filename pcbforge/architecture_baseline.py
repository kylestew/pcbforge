"""Architecture baselines for the native schematic workflow."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from pcbforge.schematic import SchematicError, canonical
from pcbforge.fsutil import commit_outputs

ArchitectureBaselineError = SchematicError
BASELINE_PATH = Path("review/circuit/architecture-baseline.json")


def _architecture(project_dir):
    paths = [project_dir / "docs/architecture.md", project_dir / "docs/mcu.md", *sorted((project_dir / "firmware").glob("*.ioc"))]
    if len(paths) < 3 or any(not p.is_file() for p in paths):
        raise SchematicError("architecture baseline needs architecture, MCU plan and IOC")
    return {p.relative_to(project_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def capture_implementation_baseline(project_dir):
    path = project_dir / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    commit_outputs([(path, canonical({"schema": 2, "architecture": _architecture(project_dir)}))], label="architecture baseline")
    return path


def baseline_is_current(project_dir):
    try:
        data = json.loads((project_dir / BASELINE_PATH).read_text())
        current = data.get("schema") == 2 and data.get("architecture") == _architecture(project_dir)
        return current, "architecture baseline is current" if current else "architecture baseline is stale"
    except (OSError, ValueError) as exc:
        return False, str(exc)
