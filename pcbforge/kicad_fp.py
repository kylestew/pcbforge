"""Pad discovery from KiCad footprints for the review schematic.

Before a board exists (Gate A) the only pad list is the footprint the model
names. The pinned KiCad 9 bundle supplies official footprints; a project's
own copies live under ``src/parts/<Lib>/<Name>.kicad_mod`` (atopile atomic
parts). Every physical pad counts, connected or not, so official symbols
can be checked pin for pin.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pcbforge import sexpr

_SHIM_RE = re.compile(r'^KICAD9_CLI="(?P<path>[^"]+)"', re.MULTILINE)


class FootprintError(RuntimeError):
    """A footprint could not be located or parsed."""


def footprints_dir(tool_root: Path) -> Path:
    """Locate the pinned KiCad 9 stock footprint directory via the CLI shim."""
    shim = Path(tool_root) / "scripts" / "kicad-cli"
    try:
        text = shim.read_text(encoding="utf-8")
    except OSError as exc:
        raise FootprintError(f"cannot read {shim}: {exc}") from exc
    match = _SHIM_RE.search(text)
    if match is None:
        raise FootprintError(f"{shim} does not pin KICAD9_CLI")
    cli = Path(match.group("path"))
    candidate = cli.parents[1] / "SharedSupport" / "footprints"
    if not candidate.is_dir():
        raise FootprintError(f"KiCad 9 footprint library directory not found: {candidate}")
    return candidate


def footprint_path(
    footprint: str,
    directory: Path | None,
    project_dir: Path | None = None,
) -> Path | None:
    """Resolve ``Lib:Name`` to a ``.kicad_mod`` file: official first, then project-local."""
    if ":" not in footprint:
        return None
    lib, name = footprint.split(":", 1)
    if not lib or not name or "/" in name or "/" in lib:
        return None
    candidates: list[Path] = []
    if directory is not None:
        candidates.append(Path(directory) / f"{lib}.pretty" / f"{name}.kicad_mod")
    if project_dir is not None:
        parts = Path(project_dir) / "src" / "parts"
        candidates.append(parts / lib / f"{name}.kicad_mod")
        candidates.append(parts / f"{lib}.pretty" / f"{name}.kicad_mod")
        if parts.is_dir():
            candidates += sorted(parts.rglob(f"{name}.kicad_mod"))
    for path in candidates:
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=None)
def _pads_of(path: str) -> frozenset[str]:
    try:
        root = sexpr.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, sexpr.SExprError) as exc:
        raise FootprintError(f"cannot read footprint {path}: {exc}") from exc
    if sexpr.head(root) not in ("footprint", "module"):
        raise FootprintError(f"{path} is not a KiCad footprint")
    return frozenset(
        sexpr.atom(pad) for pad in sexpr.children(root, "pad") if sexpr.atom(pad)
    )


def footprint_pads(
    footprint: str,
    directory: Path | None,
    project_dir: Path | None = None,
) -> tuple[set[str], Path] | None:
    """Every named pad of the footprint, with the file it came from, or None."""
    path = footprint_path(footprint, directory, project_dir)
    if path is None:
        return None
    return set(_pads_of(str(path))), path


__all__ = ["FootprintError", "footprint_pads", "footprint_path", "footprints_dir"]
