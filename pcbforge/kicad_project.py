"""Read and update the KiCad project file (``<name>.kicad_pro``) safely."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pcbforge.fsutil import AtomicWriteError, commit_outputs


class KicadProjectError(RuntimeError):
    """The KiCad project file is missing or malformed."""


def read_project(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KicadProjectError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KicadProjectError(f"invalid KiCad project {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KicadProjectError(f"KiCad project {path} must be a JSON mapping")
    return data


def project_text(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def write_outputs(outputs: Sequence[tuple[Path, bytes]], *, label: str) -> tuple[bool, ...]:
    try:
        return commit_outputs(outputs, label=label)
    except AtomicWriteError as exc:
        raise KicadProjectError(str(exc)) from exc


def register_root_sheet(project_path: Path, root_uuid: str, *, board_path: Path | None = None) -> bool:
    """Point the project's ``sheets`` list at the generated root sheet.

    Every other key is preserved. When ``board_path`` is given the board bytes
    are asserted unchanged across the write, mirroring ``prepare-layout``.
    """
    data = read_project(project_path)
    wanted = [[root_uuid, "Root"]]
    if data.get("sheets") == wanted:
        return False
    before = board_path.read_bytes() if board_path is not None and board_path.is_file() else None
    data["sheets"] = wanted
    (wrote,) = write_outputs(((project_path, project_text(data).encode()),), label="review schematic registration")
    if before is not None and board_path.read_bytes() != before:
        raise KicadProjectError(f"safety invariant failed: {board_path.name} changed while registering the sheet")
    return wrote


def root_sheet_uuid(project_path: Path) -> str | None:
    try:
        data = read_project(project_path)
    except KicadProjectError:
        return None
    sheets = data.get("sheets")
    if isinstance(sheets, list) and sheets and isinstance(sheets[0], list) and sheets[0]:
        return str(sheets[0][0])
    return None


__all__ = [
    "KicadProjectError",
    "project_text",
    "read_project",
    "register_root_sheet",
    "root_sheet_uuid",
    "write_outputs",
]
