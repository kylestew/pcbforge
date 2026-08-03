"""All-or-nothing file commits shared by generator commands."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Sequence


class AtomicWriteError(RuntimeError):
    """A staged output set could not be committed."""


def stage_file(path: Path, contents: bytes) -> Path:
    """Write contents beside path and return the temporary file."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return Path(temporary_name)


def _restore(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    os.replace(stage_file(path, original), path)


def commit_outputs(
    outputs: Sequence[tuple[Path, bytes]],
    *,
    label: str,
) -> tuple[bool, ...]:
    """Replace every path atomically, rolling back if any replace fails."""
    try:
        originals = {
            path: path.read_bytes() if path.exists() else None for path, _ in outputs
        }
    except OSError as exc:
        raise AtomicWriteError(f"cannot stage {label}: {exc}") from exc
    changed = tuple(originals[path] != contents for path, contents in outputs)
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    try:
        for (path, contents), is_changed in zip(outputs, changed, strict=True):
            if is_changed:
                staged[path] = stage_file(path, contents)
        for path, _ in outputs:
            if path in staged:
                os.replace(staged.pop(path), path)
                replaced.append(path)
    except OSError as exc:
        rollback_errors = []
        for path in reversed(replaced):
            try:
                _restore(path, originals[path])
            except OSError as rollback:
                rollback_errors.append(f"{path.name}: {rollback}")
        detail = (
            "; rollback failed: " + "; ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise AtomicWriteError(
            f"could not atomically write {label}: {exc}{detail}"
        ) from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return changed


def remove_paths(paths: Sequence[Path]) -> None:
    """Delete paths, ignoring missing ones."""
    for path in paths:
        path.unlink(missing_ok=True)
