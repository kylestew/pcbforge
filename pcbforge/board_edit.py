"""Move placed footprints inside a KiCad 9 board, byte by byte.

This is the only module in the toolchain that writes a `.kicad_pcb`, and it does
so under a narrow contract: it changes a footprint's `(at ...)` and the child
angles that follow from it, and nothing else. Circuit-owned identity and
connectivity are never touched, and the caller must have an explicit user
request plus a current LAYOUT handoff approval before calling in.

Editing text rather than re-serializing is deliberate. `sexpr.dumps` round-trips
the structure but not KiCad's exact whitespace, so a full rewrite would produce
a diff covering the whole file and make the actual change unreviewable.

The child-angle rule -- rotating a footprint by a delta shifts every child
`(at x y angle)` by the same delta -- was verified against the tracked fixture
`pilots/kicad9-multichannel/baseline/source/multichannel_mixer.kicad_pcb`, whose
114 footprints sit at four distinct rotations: every pad angle there equals the
footprint's own rotation modulo 360, plus its pad-local angle. See
`tests/test_board_edit.py`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pcbforge.board_geometry import BoardGeometryError, read_board_geometry
from pcbforge.build_test import (
    BuildTestError,
    BuildTestInputError,
    board_topology_bytes,
    read_board_evidence,
)
from pcbforge.fsutil import AtomicWriteError, commit_outputs

BACKUP_DIRNAME = "layout-backups"
#: Verification tolerances: tight enough that a wrong edit cannot pass, loose
#: enough to absorb the decimal formatting the board file round-trips through.
POSITION_TOLERANCE_MM = 0.001
ROTATION_TOLERANCE_DEG = 0.01

_AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
_FOOTPRINT_LINE = re.compile(r"^([ \t]*)\(footprint ", re.MULTILINE)


class BoardEditError(RuntimeError):
    """The board could not be edited, or the edit did not verify."""


@dataclass(frozen=True)
class Move:
    reference: str
    x: float
    y: float
    rotation: float


def format_number(value: float) -> str:
    """Format a coordinate the way KiCad does: up to 6 decimals, no padding."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def find_footprint_block(text: str, reference: str) -> tuple[int, int]:
    """Return the [start, end) span of one footprint block.

    Located by its `Reference` property rather than by counting parentheses,
    then widened to the enclosing top-level footprint form.
    """
    marker = f'(property "Reference" "{reference}"'
    index = text.find(marker)
    if index < 0:
        raise BoardEditError(f"no footprint {reference} in the board")
    if text.find(marker, index + 1) >= 0:
        raise BoardEditError(f"reference {reference} appears more than once")

    opening = None
    for match in _FOOTPRINT_LINE.finditer(text, 0, index):
        opening = match
    if opening is None:
        raise BoardEditError(f"footprint {reference} has no enclosing block")
    # KiCad 9 indents with tabs, but never assume it: the block ends at the
    # first line closing at exactly the opening line's indentation, so any
    # deeper-indented `)` inside the footprint is skipped.
    indent = opening.group(1)
    closing = re.compile(rf"^{re.escape(indent)}\)$", re.MULTILINE).search(text, index)
    if closing is None:
        raise BoardEditError(f"footprint {reference} is never closed")
    end = closing.end()
    return opening.start(), end + 1 if text[end : end + 1] == "\n" else end


def move_footprint(text: str, move: Move) -> str:
    """Rewrite one footprint's placement, leaving every other byte alone."""
    start, end = find_footprint_block(text, move.reference)
    block = text[start:end]

    boundary = block.find("(property")
    if boundary < 0:
        raise BoardEditError(f"footprint {move.reference} has no property block")
    placement = _AT_RE.search(block, 0, boundary)
    if placement is None:
        raise BoardEditError(f"footprint {move.reference} has no placement")

    previous_rotation = float(placement.group(3) or 0.0)
    delta = (move.rotation - previous_rotation) % 360.0

    rotation_text = (
        "" if move.rotation % 360.0 == 0 else f" {format_number(move.rotation)}"
    )
    head = (
        block[: placement.start()]
        + f"(at {format_number(move.x)} {format_number(move.y)}{rotation_text})"
    )

    def shift(match: re.Match[str]) -> str:
        if match.group(3) is None:
            # A two-number child offset is relative to the footprint frame and
            # rotates with it implicitly; only stored angles need the delta.
            return match.group(0)
        angle = (float(match.group(3)) + delta) % 360.0
        return (
            f"(at {match.group(1)} {match.group(2)} {format_number(angle)})"
            if angle
            else f"(at {match.group(1)} {match.group(2)})"
        )

    tail = _AT_RE.sub(shift, block[placement.end() :])
    return text[:start] + head + tail + text[end:]


def backup_board(board_path: Path) -> Path:
    """Copy the board aside before editing it. The directory is git-ignored."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = board_path.parent / BACKUP_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        backup = directory / f"{board_path.stem}-{stamp}{board_path.suffix}"
        shutil.copy2(board_path, backup)
    except OSError as exc:
        raise BoardEditError(f"cannot back up {board_path.name}: {exc}") from exc
    return backup


def _verify(board_path: Path, moves: Sequence[Move], topology: bytes) -> list[str]:
    try:
        geometry = read_board_geometry(board_path)
        evidence = read_board_evidence(board_path)
    except (BoardGeometryError, BuildTestError, BuildTestInputError) as exc:
        return [f"the edited board no longer reads: {exc}"]

    errors = []
    if board_topology_bytes(evidence) != topology:
        errors.append("the edit changed circuit-owned identity or connectivity")
    for move in moves:
        try:
            placed = geometry.footprint(move.reference)
        except KeyError:
            errors.append(f"{move.reference} is missing after the edit")
            continue
        offset = ((placed.x - move.x) ** 2 + (placed.y - move.y) ** 2) ** 0.5
        drift = abs(placed.rotation - move.rotation) % 360.0
        drift = min(drift, 360.0 - drift)
        if offset > POSITION_TOLERANCE_MM:
            errors.append(
                f"{move.reference} landed {offset:.4f} mm from its target"
            )
        if drift > ROTATION_TOLERANCE_DEG:
            errors.append(
                f"{move.reference} landed {drift:.4f}° from its target rotation"
            )
    return errors


def apply_moves(board_path: Path, moves: Sequence[Move]) -> Path:
    """Back up, move every footprint, then prove the result. Restores on doubt.

    Returns the backup path. A verification failure restores the original and
    raises, so a half-applied board never survives the call.
    """
    board_path = Path(board_path)
    if not moves:
        raise BoardEditError("no footprints to move")
    try:
        original = board_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BoardEditError(f"cannot read {board_path}: {exc}") from exc
    try:
        topology = board_topology_bytes(read_board_evidence(board_path))
    except (BuildTestError, BuildTestInputError) as exc:
        raise BoardEditError(str(exc)) from exc

    edited = original
    for move in moves:
        edited = move_footprint(edited, move)

    backup = backup_board(board_path)
    try:
        commit_outputs(((board_path, edited.encode()),), label="board placement")
    except AtomicWriteError as exc:
        raise BoardEditError(str(exc)) from exc

    errors = _verify(board_path, moves, topology)
    if errors:
        shutil.copy2(backup, board_path)
        raise BoardEditError(
            "the edit did not verify and the board was restored:\n  - "
            + "\n  - ".join(errors)
        )
    return backup


__all__ = [
    "BACKUP_DIRNAME",
    "POSITION_TOLERANCE_MM",
    "ROTATION_TOLERANCE_DEG",
    "BoardEditError",
    "Move",
    "apply_moves",
    "backup_board",
    "find_footprint_block",
    "format_number",
    "move_footprint",
]
