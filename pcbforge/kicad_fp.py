"""Resolve native footprint libraries and inspect physical pads."""

from __future__ import annotations

from pathlib import Path

from pcbforge import sexpr



class FootprintError(RuntimeError):
    """A footprint could not be located or parsed."""


def footprints_dir(tool_root: Path) -> Path:
    from pcbforge.kicad_tools import library_dir
    try:
        return library_dir("footprints", tool_root)
    except (OSError, RuntimeError) as exc:
        raise FootprintError(str(exc)) from exc



def footprint_path(
    footprint: str,
    directory: Path | None,
    project_dir: Path | None = None,
) -> Path | None:
    """Resolve ``Lib:Name`` to a ``.kicad_mod`` file: project table first, then official libraries."""
    if ":" not in footprint:
        return None
    lib, name = footprint.split(":", 1)
    if not lib or not name or "/" in name or "/" in lib:
        return None
    # KiCad project tables override global libraries. Honor that precedence.
    if project_dir is not None:
        table = Path(project_dir) / "fp-lib-table"
        if table.is_file():
            try:
                root = sexpr.parse(table.read_text())
                for entry in sexpr.children(root, "lib"):
                    if sexpr.atom(sexpr.child(entry, "name")) != lib:
                        continue
                    uri = sexpr.atom(sexpr.child(entry, "uri"))
                    uri = uri.replace("${KIPRJMOD}", str(Path(project_dir).resolve()))
                    path = (Path(uri) / f"{name}.kicad_mod").resolve()
                    if not path.is_relative_to(Path(project_dir).resolve()):
                        raise FootprintError("project footprint library must be inside the project")
                    return path if path.is_file() else None
            except (OSError, sexpr.SExprError) as exc:
                raise FootprintError(f"invalid fp-lib-table: {exc}") from exc
    path = Path(directory) / f"{lib}.pretty" / f"{name}.kicad_mod" if directory else None
    return path if path and path.is_file() else None



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
