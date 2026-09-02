"""`pcbforge apply-floorplan`: first-pass placement from an adopted floorplan.

The second and last command that writes the board, and like `apply-pattern` it
runs only when the user asks for it by name. It is deliberately blunt: it moves
**every** footprint of the named groups into that group's floorplan rectangle,
including ones the user already positioned by hand. That is what a first pass
is. Run it before careful work, not after, and run `--dry-run` first.

Packing is greedy rather than optimal: largest part at the rectangle's centre,
the rest spiralling outward on a grid until each one fits without touching a
neighbour. Anything that will not fit is placed just outside and reported as
spilled rather than silently stacked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pcbforge.board_edit import BoardEditError, Move, apply_moves
from pcbforge.board_geometry import BoardGeometry, BoardGeometryError, Box, read_board_geometry
from pcbforge.initialize import InitInputError, read_spec
from pcbforge.placement import PlacementError, read_placement_contract
from pcbforge.placement_check import board_drift_warning
from pcbforge.status import layout_assist_is_authorized

#: Edge-to-edge room left between two packed footprints.
GAP_MM = 0.5
#: Candidate centres are tried on this grid, nearest the rectangle centre first.
GRID_MM = 0.5
#: How far outside its rectangle a spilled footprint may be pushed.
SPILL_MARGIN_MM = 10.0


class ApplyFloorplanError(RuntimeError):
    """The floorplan could not be applied."""


class ApplyFloorplanInputError(ApplyFloorplanError):
    """The project or the request is not in a state to apply one."""


@dataclass(frozen=True)
class PlannedMove:
    group: str
    reference: str
    before: tuple[float, float]
    after: tuple[float, float]
    spilled: bool

    @property
    def distance_mm(self) -> float:
        return (
            (self.after[0] - self.before[0]) ** 2
            + (self.after[1] - self.before[1]) ** 2
        ) ** 0.5


@dataclass(frozen=True)
class ApplyFloorplanResult:
    project_dir: Path
    groups: tuple[str, ...]
    moves: tuple[PlannedMove, ...]
    warnings: tuple[str, ...]
    backup: Path | None
    applied: bool

    @property
    def spilled(self) -> tuple[PlannedMove, ...]:
        return tuple(move for move in self.moves if move.spilled)

    @property
    def summary(self) -> str:
        noun = "footprint" if len(self.moves) == 1 else "footprints"
        verb = "placed" if self.applied else "would place"
        spilled = len(self.spilled)
        tail = f", {spilled} spilled outside" if spilled else ""
        return (
            f"{verb} {len(self.moves)} {noun} in "
            f"{len(self.groups)} group(s){tail}"
        )


def _candidates(rect: Box, margin: float) -> list[tuple[float, float]]:
    """Grid centres within a rectangle, nearest its middle first."""
    centre_x, centre_y = rect.centre
    points = []
    steps_x = int((rect.width + 2 * margin) / GRID_MM) + 1
    steps_y = int((rect.height + 2 * margin) / GRID_MM) + 1
    for row in range(steps_y):
        y = rect.min_y - margin + row * GRID_MM
        for column in range(steps_x):
            x = rect.min_x - margin + column * GRID_MM
            points.append((x, y))
    points.sort(key=lambda point: ((point[0] - centre_x) ** 2 + (point[1] - centre_y) ** 2, point))
    return points


def _pack(rect: Box, footprints) -> list[tuple[str, tuple[float, float], bool]]:
    """Place the largest part at the centre, then fill outward around it."""
    ordered = sorted(footprints, key=lambda item: (-item.box.area, item.reference))
    inside = _candidates(rect, 0.0)
    outside = _candidates(rect, SPILL_MARGIN_MM)
    taken: list[Box] = []
    placed: list[tuple[str, tuple[float, float], bool]] = []

    for item in ordered:
        half_width = item.box.width / 2
        half_height = item.box.height / 2
        chosen = None
        spilled = False
        for pool, is_spill in ((inside, False), (outside, True)):
            for point in pool:
                box = Box(
                    point[0] - half_width,
                    point[1] - half_height,
                    point[0] + half_width,
                    point[1] + half_height,
                )
                if is_spill is False and not rect.contains_box(box):
                    continue
                if any(box.overlaps(other, clearance=GAP_MM) for other in taken):
                    continue
                chosen = (point, box)
                spilled = is_spill
                break
            if chosen is not None:
                break
        if chosen is None:
            # Nothing fits even outside: leave it where it is rather than stack.
            placed.append((item.reference, item.box.centre, True))
            continue
        taken.append(chosen[1])
        placed.append((item.reference, chosen[0], spilled))
    return placed


def apply_floorplan(
    project_dir: Path,
    group_ids: tuple[str, ...],
    *,
    dry_run: bool = False,
) -> ApplyFloorplanResult:
    """Move whole groups into their adopted floorplan rectangles."""
    project_dir = Path(project_dir).expanduser().resolve()
    if not group_ids:
        raise ApplyFloorplanInputError("name at least one group with --groups")
    if not layout_assist_is_authorized(project_dir):
        raise ApplyFloorplanInputError(
            "spatial edits need an open LAYOUT with a current handoff approval"
        )
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise ApplyFloorplanInputError(str(exc)) from exc
    try:
        contract = read_placement_contract(project_dir)
    except PlacementError as exc:
        raise ApplyFloorplanInputError(str(exc)) from exc

    floorplan = contract.floorplan
    if floorplan is None:
        raise ApplyFloorplanInputError(
            "placement.yaml declares no floorplan; run `pcbforge sketch-placement` "
            "and paste the chosen block first"
        )
    rects = {rect.identifier: rect for rect in floorplan.rects}
    unknown = [group for group in group_ids if group not in rects]
    if unknown:
        raise ApplyFloorplanInputError(
            f"the floorplan has no rectangle for {', '.join(unknown)}"
        )

    board_path = project_dir / f"{spec.name}.kicad_pcb"
    try:
        geometry = read_board_geometry(board_path)
    except BoardGeometryError as exc:
        raise ApplyFloorplanInputError(str(exc)) from exc
    if geometry.outline is None:
        raise ApplyFloorplanInputError(
            "the board has no Edge.Cuts outline, so floorplan rectangles have no "
            "origin to be placed against"
        )
    origin = (geometry.outline.min_x, geometry.outline.min_y)

    references = {group.identifier: group.references for group in contract.groups}
    moves: list[PlannedMove] = []
    for group in group_ids:
        rect = rects[group]
        absolute = Box(
            origin[0] + rect.x,
            origin[1] + rect.y,
            origin[0] + rect.x + rect.width,
            origin[1] + rect.y + rect.height,
        )
        placed = []
        for reference in references.get(group, ()):
            try:
                placed.append(geometry.footprint(reference))
            except KeyError:
                continue
        if not placed:
            raise ApplyFloorplanInputError(
                f"group {group} has no footprint on the board"
            )
        by_reference = {item.reference: item for item in placed}
        for reference, centre, spilled in _pack(absolute, placed):
            item = by_reference[reference]
            # Move the origin by however far the box centre has to travel; the
            # footprint keeps its rotation and side.
            offset = (centre[0] - item.box.centre[0], centre[1] - item.box.centre[1])
            moves.append(
                PlannedMove(
                    group,
                    reference,
                    (item.x, item.y),
                    (item.x + offset[0], item.y + offset[1]),
                    spilled,
                )
            )

    warnings = [
        warning
        for warning in (board_drift_warning(project_dir, board_path),)
        if warning is not None
    ]
    if any(move.spilled for move in moves):
        warnings.append(
            "some footprints did not fit their rectangle and were placed just "
            "outside it; widen the floorplan or split the group"
        )

    if dry_run:
        return ApplyFloorplanResult(
            project_dir, tuple(group_ids), tuple(moves), tuple(warnings), None, False
        )

    try:
        backup = apply_moves(
            board_path,
            [
                Move(
                    move.reference,
                    move.after[0],
                    move.after[1],
                    geometry.footprint(move.reference).rotation,
                )
                for move in moves
            ],
        )
    except BoardEditError as exc:
        raise ApplyFloorplanError(str(exc)) from exc

    return ApplyFloorplanResult(
        project_dir, tuple(group_ids), tuple(moves), tuple(warnings), backup, True
    )


__all__ = [
    "GAP_MM",
    "SPILL_MARGIN_MM",
    "ApplyFloorplanError",
    "ApplyFloorplanInputError",
    "ApplyFloorplanResult",
    "PlannedMove",
    "apply_floorplan",
]
