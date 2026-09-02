"""`pcbforge apply-pattern`: stamp an exact reference layout around its anchor.

One of only two commands in the toolchain that move footprints, and it runs only
when a user asks for it by name. The agent does not reach for this on its own
initiative; see `agent/operating-manual.md` for the assist rules it sits under.

The user places the anchor. This command places the satellites the pattern binds
around it, at the vendor's offsets. It refuses a `sketch` pattern outright: those
millimetres were read off a figure by eye and are not precise enough to place a
part by, however good they are for measuring one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pcbforge.board_edit import BoardEditError, Move, apply_moves
from pcbforge.board_geometry import (
    BoardGeometry,
    BoardGeometryError,
    FootprintGeometry,
    read_board_geometry,
    to_board,
)
from pcbforge.initialize import InitInputError, read_spec
from pcbforge.placement import (
    PlacementError,
    PlacementGroup,
    read_placement_contract,
)
from pcbforge.placement_check import board_drift_warning
from pcbforge.status import layout_assist_is_authorized

#: How many footprints must share one position before it reads as "unplaced".
STACKED_LIMIT = 3
STACKED_TOLERANCE_MM = 0.01


class ApplyPatternError(RuntimeError):
    """The pattern could not be applied."""


class ApplyPatternInputError(ApplyPatternError):
    """The project, the request, or the board is not in a state to apply one."""


@dataclass(frozen=True)
class PlannedMove:
    role: str
    reference: str
    before: tuple[float, float, float]
    after: tuple[float, float, float]

    @property
    def distance_mm(self) -> float:
        return (
            (self.after[0] - self.before[0]) ** 2
            + (self.after[1] - self.before[1]) ** 2
        ) ** 0.5


@dataclass(frozen=True)
class ApplyPatternResult:
    project_dir: Path
    group: str
    pattern: str
    anchor: str
    moves: tuple[PlannedMove, ...]
    warnings: tuple[str, ...]
    backup: Path | None
    applied: bool

    @property
    def summary(self) -> str:
        noun = "footprint" if len(self.moves) == 1 else "footprints"
        verb = "moved" if self.applied else "would move"
        return f"{verb} {len(self.moves)} {noun} to match {self.pattern}"


def _group_with_pattern(contract, group_id: str) -> PlacementGroup:
    for group in contract.groups:
        if group.identifier == group_id:
            if group.pattern is None:
                raise ApplyPatternInputError(
                    f"group {group_id} declares no pattern"
                )
            return group
    known = ", ".join(
        group.identifier for group in contract.groups if group.pattern is not None
    )
    raise ApplyPatternInputError(
        f"unknown group {group_id!r}; groups with a pattern: {known or 'none'}"
    )


def _anchor_is_unplaced(geometry: BoardGeometry, anchor: FootprintGeometry) -> bool:
    """A pile of footprints on one point is an unplaced board, not a layout."""
    stacked = sum(
        1
        for item in geometry.footprints
        if abs(item.x - anchor.x) <= STACKED_TOLERANCE_MM
        and abs(item.y - anchor.y) <= STACKED_TOLERANCE_MM
    )
    return stacked > STACKED_LIMIT


def apply_pattern(
    project_dir: Path,
    group_id: str,
    *,
    dry_run: bool = False,
) -> ApplyPatternResult:
    """Move an exact pattern's bound satellites into place around its anchor."""
    project_dir = Path(project_dir).expanduser().resolve()
    if not layout_assist_is_authorized(project_dir):
        raise ApplyPatternInputError(
            "spatial edits need an open LAYOUT with a current handoff approval"
        )
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise ApplyPatternInputError(str(exc)) from exc
    try:
        contract = read_placement_contract(project_dir)
    except PlacementError as exc:
        raise ApplyPatternInputError(str(exc)) from exc

    group = _group_with_pattern(contract, group_id)
    binding = dict(contract.patterns).get(group_id)
    if binding is None:
        raise ApplyPatternInputError(f"group {group_id} has no resolved pattern")
    pattern = binding.pattern
    if pattern.fidelity != "exact":
        raise ApplyPatternInputError(
            f"pattern {pattern.identifier} is {pattern.fidelity} fidelity: its "
            "offsets were transcribed by eye and cannot place a part"
        )
    if binding.unbound:
        raise ApplyPatternInputError(
            f"pattern {pattern.identifier} has unbound roles: "
            f"{', '.join(binding.unbound)}"
        )

    board_path = project_dir / f"{spec.name}.kicad_pcb"
    try:
        geometry = read_board_geometry(board_path)
    except BoardGeometryError as exc:
        raise ApplyPatternInputError(str(exc)) from exc

    try:
        anchor = geometry.footprint(binding.anchor)
    except KeyError as exc:
        raise ApplyPatternInputError(
            f"anchor {binding.anchor} is not on the board"
        ) from exc
    if _anchor_is_unplaced(geometry, anchor):
        raise ApplyPatternInputError(
            f"anchor {binding.anchor} is still at the unplaced position: place it "
            "in KiCad first, then rerun"
        )

    moves: list[PlannedMove] = []
    for role_id, reference in binding.roles:
        role = pattern.role(role_id)
        try:
            satellite = geometry.footprint(reference)
        except KeyError as exc:
            raise ApplyPatternInputError(
                f"{reference} is not on the board"
            ) from exc
        wanted_side = (
            anchor.side
            if role.side == "same"
            else ("back" if anchor.side == "front" else "front")
        )
        if satellite.side != wanted_side:
            raise ApplyPatternInputError(
                f"role {role_id} needs {reference} on the {wanted_side} side; "
                f"flip {reference} to the {wanted_side} side in KiCad, then rerun"
            )
        x, y = to_board(anchor, role.offset_mm)
        rotation = (anchor.rotation + role.rotation_deg) % 360.0
        moves.append(
            PlannedMove(
                role_id,
                reference,
                (satellite.x, satellite.y, satellite.rotation),
                (x, y, rotation),
            )
        )

    warnings = [
        warning
        for warning in (board_drift_warning(project_dir, board_path),)
        if warning is not None
    ]

    if dry_run:
        return ApplyPatternResult(
            project_dir,
            group.identifier,
            pattern.identifier,
            binding.anchor,
            tuple(moves),
            tuple(warnings),
            None,
            False,
        )

    try:
        backup = apply_moves(
            board_path,
            [Move(move.reference, *move.after) for move in moves],
        )
    except BoardEditError as exc:
        raise ApplyPatternError(str(exc)) from exc

    return ApplyPatternResult(
        project_dir,
        group.identifier,
        pattern.identifier,
        binding.anchor,
        tuple(moves),
        tuple(warnings),
        backup,
        True,
    )


__all__ = [
    "ApplyPatternError",
    "ApplyPatternInputError",
    "ApplyPatternResult",
    "PlannedMove",
    "apply_pattern",
]
