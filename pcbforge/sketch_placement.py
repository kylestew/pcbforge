"""`pcbforge sketch-placement`: coarse floorplan variants before detailed work.

Placement starts with a decision nobody writes down: which region of the board
each functional group occupies. This command makes that decision explicit and
comparable. It packs one rectangle per group -- sized from the real footprint
areas, positioned by simulated annealing against the contract's own constraints
-- and shows two or three arrangements with their tradeoffs priced out term by
term, so the choice is made on numbers rather than on the first arrangement
anyone happened to draw.

It never touches the board and never edits `placement.yaml`. The user picks a
variant and pastes its `floorplan:` block into the contract; from then on
`check-placement` measures the real placement against the adopted plan.

The solver is deliberately small: a hand-rolled annealer over rectangle centres
on a 1 mm grid, pure Python and `random`, seeded so every run of the same seed
is identical. A real packing solver would be a dependency and a black box for a
problem whose answer a human is going to overrule anyway.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from pcbforge.board_geometry import BoardGeometry, BoardGeometryError, read_board_geometry
from pcbforge.build_test import (
    BoardEvidence,
    BuildTestError,
    BuildTestInputError,
    read_board_evidence,
)
from pcbforge.fsutil import AtomicWriteError, commit_outputs
from pcbforge.initialize import InitInputError, ProjectSpec, read_spec
from pcbforge.markdown_metadata import metadata_trailer
from pcbforge.placement import (
    PLACEMENT_FILENAME,
    PlacementContract,
    PlacementError,
    read_placement_contract,
    split_endpoint,
)

SKETCH_SCHEMA = 1
REPORT_FILENAME = Path("docs/placement-sketch.md")
VARIANT_LABELS = "ABCDEFGH"

#: Group rectangles are sized from real footprint area plus room to route.
ROUTING_MARGIN = 1.8
MIN_ASPECT = 0.5
MAX_ASPECT = 2.0
GRID_MM = 1.0

ITERATIONS = 20_000
TEMPERATURE_START = 50.0
TEMPERATURE_END = 0.1
MAX_SHIFT_MM = 5.0
#: Two variants that place every group within this distance are the same idea.
DISTINCT_MM = 3.0
EXTRA_SEEDS = 3
#: A constraint with less than this much room to spare is worth pointing at.
TIGHT_MM = 1.0

WEIGHTS = {
    "overlap": 100.0,
    "out-of-bounds": 100.0,
    "proximity": 10.0,
    "separation": 10.0,
    "board-edge": 10.0,
    "order": 10.0,
    "wirelength": 1.0,
    "compactness": 0.1,
}


class SketchError(RuntimeError):
    """The floorplan sketch could not be produced."""


class SketchInputError(SketchError):
    """The project is not in a state to sketch a floorplan."""


@dataclass(frozen=True)
class GroupBox:
    """One group reduced to a rectangle to be placed."""

    identifier: str
    width: float
    height: float
    fixed_centre: tuple[float, float] | None

    @property
    def fixed(self) -> bool:
        return self.fixed_centre is not None


@dataclass(frozen=True)
class PlacedRect:
    identifier: str
    x: float
    y: float
    width: float
    height: float

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class Variant:
    label: str
    seed: int
    rects: tuple[PlacedRect, ...]
    costs: Mapping[str, float]
    tight: tuple[str, ...]

    @property
    def total(self) -> float:
        return sum(WEIGHTS[term] * value for term, value in self.costs.items())


@dataclass(frozen=True)
class SketchResult:
    project_dir: Path
    board_mm: tuple[float, float]
    variants: tuple[Variant, ...]
    report_path: Path
    svg_paths: tuple[Path, ...]
    wrote: bool

    @property
    def summary(self) -> str:
        noun = "variant" if len(self.variants) == 1 else "variants"
        best = min(self.variants, key=lambda item: item.total)
        return (
            f"{len(self.variants)} {noun} on a "
            f"{self.board_mm[0]:g} x {self.board_mm[1]:g} mm board; "
            f"{best.label} costs least at {best.total:.1f}"
        )


def _overlap_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    return width * height if width > 0 and height > 0 else 0.0


def _outside_area(
    rect: tuple[float, float, float, float],
    board: tuple[float, float],
) -> float:
    x, y, width, height = rect
    inside = _overlap_area(rect, (0.0, 0.0, board[0], board[1]))
    return max(0.0, width * height - inside)


def _rect_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    dx = max(0.0, max(ax, bx) - min(ax + aw, bx + bw))
    dy = max(0.0, max(ay, by) - min(ay + ah, by + bh))
    return math.hypot(dx, dy)


def _edge_gap(
    rect: tuple[float, float, float, float],
    board: tuple[float, float],
    edge: str,
) -> float:
    x, y, width, height = rect
    gaps = {
        "north": y,
        "south": board[1] - (y + height),
        "west": x,
        "east": board[0] - (x + width),
    }
    if edge == "any":
        return min(gaps.values())
    return gaps[edge]


@dataclass(frozen=True)
class _Problem:
    """Everything the cost function needs, resolved once before annealing."""

    boxes: tuple[GroupBox, ...]
    board: tuple[float, float]
    index: Mapping[str, int]
    proximity: tuple[tuple[str, int, int, float], ...]
    separation: tuple[tuple[str, int, int, float], ...]
    board_edge: tuple[tuple[str, int, str, float], ...]
    order: tuple[tuple[str, tuple[int, ...], str], ...]
    shared_nets: tuple[tuple[int, int, int], ...]


def _group_of(contract: PlacementContract) -> dict[str, str]:
    return {
        reference: group.identifier
        for group in contract.groups
        for reference in group.references
    }


def _build_problem(
    contract: PlacementContract,
    boxes: Sequence[GroupBox],
    board: tuple[float, float],
    evidence: BoardEvidence,
) -> _Problem:
    index = {box.identifier: position for position, box in enumerate(boxes)}
    owner = _group_of(contract)

    def groups_for(subject: str) -> int | None:
        parsed = split_endpoint(subject)
        if parsed is None:
            return None
        return index.get(owner.get(parsed[0], ""))

    proximity = []
    separation = []
    board_edge = []
    order = []
    for constraint in contract.constraints:
        positions = [groups_for(subject) for subject in constraint.subjects]
        if any(position is None for position in positions):
            continue
        if constraint.kind == "proximity" and positions[0] != positions[1]:
            proximity.append(
                (constraint.identifier, positions[0], positions[1], constraint.max_mm)
            )
        elif constraint.kind == "separation" and positions[0] != positions[1]:
            separation.append(
                (constraint.identifier, positions[0], positions[1], constraint.min_mm)
            )
        elif constraint.kind == "board-edge":
            board_edge.append(
                (
                    constraint.identifier,
                    positions[0],
                    constraint.edge or "any",
                    constraint.max_mm,
                )
            )
        elif constraint.kind == "order" and len(set(positions)) == len(positions):
            order.append(
                (constraint.identifier, tuple(positions), constraint.direction or "")
            )

    nets: dict[str, set[int]] = {}
    for reference, _, net in evidence.pad_nets:
        position = index.get(owner.get(reference, ""))
        if position is not None and net:
            nets.setdefault(net, set()).add(position)
    counts: dict[tuple[int, int], int] = {}
    for members in nets.values():
        ordered = sorted(members)
        for first in range(len(ordered)):
            for second in range(first + 1, len(ordered)):
                key = (ordered[first], ordered[second])
                counts[key] = counts.get(key, 0) + 1

    return _Problem(
        tuple(boxes),
        board,
        index,
        tuple(proximity),
        tuple(separation),
        tuple(board_edge),
        tuple(order),
        tuple((first, second, count) for (first, second), count in sorted(counts.items())),
    )


def _costs(
    problem: _Problem,
    state: Sequence[tuple[float, float, float, float]],
) -> dict[str, float]:
    """Every cost term, unweighted, for one arrangement."""
    overlap = 0.0
    for first in range(len(state)):
        for second in range(first + 1, len(state)):
            overlap += _overlap_area(state[first], state[second])

    out_of_bounds = sum(_outside_area(rect, problem.board) for rect in state)

    proximity = sum(
        max(0.0, _rect_gap(state[first], state[second]) - limit)
        for _, first, second, limit in problem.proximity
    )
    separation = sum(
        max(0.0, limit - _rect_gap(state[first], state[second]))
        for _, first, second, limit in problem.separation
    )
    board_edge = sum(
        max(0.0, _edge_gap(state[position], problem.board, edge) - limit)
        for _, position, edge, limit in problem.board_edge
    )

    order = 0.0
    for _, positions, direction in problem.order:
        axis = 1 if direction in {"north-to-south", "south-to-north"} else 0
        ascending = direction in {"west-to-east", "north-to-south"}
        centres = [
            state[position][axis] + state[position][axis + 2] / 2
            for position in positions
        ]
        for first, second in zip(centres, centres[1:]):
            if not (first < second if ascending else first > second):
                order += 1.0

    wirelength = 0.0
    for first, second, count in problem.shared_nets:
        ax = state[first][0] + state[first][2] / 2
        ay = state[first][1] + state[first][3] / 2
        bx = state[second][0] + state[second][2] / 2
        by = state[second][1] + state[second][3] / 2
        wirelength += count * math.hypot(ax - bx, ay - by)

    if state:
        min_x = min(rect[0] for rect in state)
        min_y = min(rect[1] for rect in state)
        max_x = max(rect[0] + rect[2] for rect in state)
        max_y = max(rect[1] + rect[3] for rect in state)
        compactness = (max_x - min_x) * (max_y - min_y)
    else:
        compactness = 0.0

    return {
        "overlap": overlap,
        "out-of-bounds": out_of_bounds,
        "proximity": proximity,
        "separation": separation,
        "board-edge": board_edge,
        "order": order,
        "wirelength": wirelength,
        "compactness": compactness,
    }


def _total(costs: Mapping[str, float]) -> float:
    return sum(WEIGHTS[term] * value for term, value in costs.items())


def _snap(value: float, span: float, limit: float) -> float:
    """Keep a rectangle's corner on the grid and inside the board where it fits."""
    highest = max(0.0, limit - span)
    return min(max(0.0, round(value / GRID_MM) * GRID_MM), highest)


def _initial_state(
    problem: _Problem,
    rng: random.Random,
) -> list[tuple[float, float, float, float]]:
    state = []
    for box in problem.boxes:
        if box.fixed_centre is not None:
            centre_x, centre_y = box.fixed_centre
            state.append(
                (
                    centre_x - box.width / 2,
                    centre_y - box.height / 2,
                    box.width,
                    box.height,
                )
            )
            continue
        state.append(
            (
                _snap(rng.uniform(0, problem.board[0]), box.width, problem.board[0]),
                _snap(rng.uniform(0, problem.board[1]), box.height, problem.board[1]),
                box.width,
                box.height,
            )
        )
    return state


def _anneal(
    problem: _Problem,
    seed: int,
    iterations: int = ITERATIONS,
) -> list[tuple[float, float, float, float]]:
    rng = random.Random(seed)
    movable = [
        position for position, box in enumerate(problem.boxes) if not box.fixed
    ]
    state = _initial_state(problem, rng)
    if not movable:
        return state
    current = _total(_costs(problem, state))
    best_state = list(state)
    best = current

    decay = (TEMPERATURE_END / TEMPERATURE_START) ** (1.0 / max(1, iterations - 1))
    temperature = TEMPERATURE_START
    for _ in range(iterations):
        candidate = list(state)
        choice = rng.random()
        if choice < 0.6 or len(movable) < 2:
            position = rng.choice(movable)
            x, y, width, height = candidate[position]
            candidate[position] = (
                _snap(x + rng.uniform(-MAX_SHIFT_MM, MAX_SHIFT_MM), width, problem.board[0]),
                _snap(y + rng.uniform(-MAX_SHIFT_MM, MAX_SHIFT_MM), height, problem.board[1]),
                width,
                height,
            )
        elif choice < 0.85:
            first, second = rng.sample(movable, 2)
            fx, fy, fw, fh = candidate[first]
            sx, sy, sw, sh = candidate[second]
            candidate[first] = (_snap(sx, fw, problem.board[0]), _snap(sy, fh, problem.board[1]), fw, fh)
            candidate[second] = (_snap(fx, sw, problem.board[0]), _snap(fy, sh, problem.board[1]), sw, sh)
        else:
            position = rng.choice(movable)
            x, y, width, height = candidate[position]
            candidate[position] = (
                _snap(x, height, problem.board[0]),
                _snap(y, width, problem.board[1]),
                height,
                width,
            )

        proposed = _total(_costs(problem, candidate))
        delta = proposed - current
        if delta <= 0 or rng.random() < math.exp(-delta / max(temperature, 1e-9)):
            state = candidate
            current = proposed
            if current < best:
                best = current
                best_state = list(state)
        temperature *= decay
    return best_state


def _group_boxes(
    contract: PlacementContract,
    geometry: BoardGeometry,
    origin: tuple[float, float],
) -> tuple[GroupBox, ...]:
    """Size each group from its real footprint area, with room to route."""
    boxes = []
    for group in contract.groups:
        placed = []
        for reference in group.references:
            try:
                placed.append(geometry.footprint(reference))
            except KeyError:
                continue
        if not placed:
            raise SketchInputError(
                f"group {group.identifier} has no footprint on the board"
            )
        area = sum(item.box.area for item in placed) * ROUTING_MARGIN
        largest = max(placed, key=lambda item: item.box.area).box
        aspect = largest.width / largest.height if largest.height else 1.0
        aspect = min(max(aspect, MIN_ASPECT), MAX_ASPECT)
        width = math.sqrt(max(area, 1.0) * aspect)
        height = math.sqrt(max(area, 1.0) / aspect)

        # A group of mounting holes is drilled where the enclosure says, not
        # where a solver would prefer; anything else is free to move.
        mounting = all(
            item.footprint.split(":")[-1].startswith("MountingHole")
            and (item.x, item.y) != (0.0, 0.0)
            for item in placed
        )
        centre = None
        if mounting:
            centre = (
                sum(item.x for item in placed) / len(placed) - origin[0],
                sum(item.y for item in placed) / len(placed) - origin[1],
            )
        boxes.append(GroupBox(group.identifier, width, height, centre))
    return tuple(boxes)


def _board_size(
    geometry: BoardGeometry,
    spec: ProjectSpec,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """The board rectangle and its origin, preferring the drawn outline."""
    outline = geometry.outline
    if outline is not None and outline.width > 1.0 and outline.height > 1.0:
        return (outline.width, outline.height), (outline.min_x, outline.min_y)
    return (float(spec.board_mm[0]), float(spec.board_mm[1])), (0.0, 0.0)


def _tight_constraints(
    problem: _Problem,
    state: Sequence[tuple[float, float, float, float]],
) -> tuple[str, ...]:
    """Constraints that are violated, or close enough to worry about."""
    tight = []
    for identifier, first, second, limit in problem.proximity:
        gap = _rect_gap(state[first], state[second])
        if gap > limit - TIGHT_MM:
            tight.append(f"{identifier}: {gap:.1f} mm apart, wants <= {limit:g} mm")
    for identifier, first, second, limit in problem.separation:
        gap = _rect_gap(state[first], state[second])
        if gap < limit + TIGHT_MM:
            tight.append(f"{identifier}: {gap:.1f} mm apart, wants >= {limit:g} mm")
    for identifier, position, edge, limit in problem.board_edge:
        gap = _edge_gap(state[position], problem.board, edge)
        if gap > limit - TIGHT_MM:
            tight.append(
                f"{identifier}: {gap:.1f} mm from the {edge} edge, wants <= {limit:g} mm"
            )
    for identifier, positions, direction in problem.order:
        axis = 1 if direction in {"north-to-south", "south-to-north"} else 0
        ascending = direction in {"west-to-east", "north-to-south"}
        centres = [
            state[position][axis] + state[position][axis + 2] / 2
            for position in positions
        ]
        if any(
            not (first < second if ascending else first > second)
            for first, second in zip(centres, centres[1:])
        ):
            tight.append(f"{identifier}: groups are not ordered {direction}")
    return tuple(tight)


def _distinct(
    candidate: Sequence[PlacedRect],
    existing: Sequence[Variant],
) -> bool:
    for variant in existing:
        previous = {rect.identifier: rect.centre for rect in variant.rects}
        if all(
            math.dist(rect.centre, previous.get(rect.identifier, (1e9, 1e9)))
            < DISTINCT_MM
            for rect in candidate
        ):
            return False
    return True


def _solve(
    problem: _Problem,
    variants: int,
    seed: int,
    iterations: int,
) -> tuple[Variant, ...]:
    results: list[Variant] = []
    attempt = 0
    while len(results) < variants and attempt < variants + EXTRA_SEEDS:
        current_seed = seed + attempt
        attempt += 1
        state = _anneal(problem, current_seed, iterations)
        rects = tuple(
            PlacedRect(box.identifier, *values)
            for box, values in zip(problem.boxes, state)
        )
        if results and not _distinct(rects, results):
            continue
        results.append(
            Variant(
                VARIANT_LABELS[len(results)],
                current_seed,
                rects,
                _costs(problem, state),
                _tight_constraints(problem, state),
            )
        )
    return tuple(results)


def _svg(variant: Variant, problem: _Problem) -> str:
    """A plain hand-written diagram: board, group rectangles, constraint lines."""
    width, height = problem.board
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 -4 {width + 8:g} '
        f'{height + 8:g}" width="{width * 6:g}" height="{height * 6:g}">',
        '<defs><pattern id="fixed" width="4" height="4" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<line x1="0" y1="0" x2="0" y2="4" stroke="#888" stroke-width="1"/>'
        "</pattern></defs>",
        f'<rect x="0" y="0" width="{width:g}" height="{height:g}" fill="#fbfbfb" '
        'stroke="#222" stroke-width="0.4"/>',
    ]
    fixed = {box.identifier for box in problem.boxes if box.fixed}
    for rect in variant.rects:
        fill = "url(#fixed)" if rect.identifier in fixed else "#dceaf7"
        parts.append(
            f'<rect x="{rect.x:.2f}" y="{rect.y:.2f}" width="{rect.width:.2f}" '
            f'height="{rect.height:.2f}" fill="{fill}" stroke="#25506f" '
            'stroke-width="0.3"/>'
        )
        centre_x, centre_y = rect.centre
        parts.append(
            f'<text x="{centre_x:.2f}" y="{centre_y:.2f}" font-size="2.2" '
            f'text-anchor="middle" fill="#12303f">{_escape(rect.identifier)}</text>'
        )
    for _, first, second, _limit in problem.proximity:
        parts.append(_line(variant.rects[first], variant.rects[second], "#2e8b57"))
    for _, first, second, _limit in problem.separation:
        parts.append(_line(variant.rects[first], variant.rects[second], "#c0392b"))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _line(first: PlacedRect, second: PlacedRect, colour: str) -> str:
    ax, ay = first.centre
    bx, by = second.centre
    return (
        f'<line x1="{ax:.2f}" y1="{ay:.2f}" x2="{bx:.2f}" y2="{by:.2f}" '
        f'stroke="{colour}" stroke-width="0.25" stroke-dasharray="1 1"/>'
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def floorplan_block(variant: Variant, board: tuple[float, float]) -> str:
    """The YAML the user pastes into `placement.yaml` to adopt a variant."""
    groups = "\n".join(
        f"    - id: {rect.identifier}\n"
        f"      rect_mm: [{rect.x:g}, {rect.y:g}, "
        f"{rect.width:.1f}, {rect.height:.1f}]"
        for rect in variant.rects
    )
    return (
        "floorplan:\n"
        f"  variant: {variant.label}\n"
        f"  seed: {variant.seed}\n"
        f"  board_mm: [{board[0]:g}, {board[1]:g}]\n"
        "  groups:\n"
        f"{groups}"
    )


def _render_report(
    project_name: str,
    result_variants: Sequence[Variant],
    board: tuple[float, float],
) -> str:
    metadata = yaml.safe_dump(
        {
            "pcbforge_placement_sketch_schema": SKETCH_SCHEMA,
            "board_mm": [board[0], board[1]],
            "variants": [
                {"label": item.label, "seed": item.seed, "cost": round(item.total, 3)}
                for item in result_variants
            ],
        },
        sort_keys=False,
    ).rstrip()

    sections = []
    for variant in result_variants:
        rows = "\n".join(
            f"| {term} | {variant.costs[term]:.2f} | {WEIGHTS[term]:g} | "
            f"{WEIGHTS[term] * variant.costs[term]:.2f} |"
            for term in WEIGHTS
        )
        tight = (
            "\n".join(f"- {item}" for item in variant.tight)
            if variant.tight
            else "- None: every constraint has room to spare."
        )
        sections.append(
            f"""## Variant {variant.label}

![Variant {variant.label}](placement-sketch-{variant.label}.svg)

Seed {variant.seed}. Total cost **{variant.total:.1f}**.

| Term | Raw | Weight | Weighted |
|---|---:|---:|---:|
{rows}

Tight constraints:

{tight}

```yaml
{floorplan_block(variant, board)}
```"""
        )

    body = f"""# {project_name} placement sketch

> Generated by PCBForge from `placement.yaml` and the current board. These are
> proposals, not decisions: nothing here changes the board or the contract.
> Adopt one by pasting its `floorplan:` block into `placement.yaml`, after
> which `pcbforge check-placement` measures the real placement against it.
> Rerun `pcbforge sketch-placement`; do not edit this report manually.

Board {board[0]:g} x {board[1]:g} mm. Rectangles are group areas grown
{ROUTING_MARGIN:g}x for routing, positioned board-relative with y downward.
Hatched rectangles are fixed. Green lines are proximity constraints between
groups, red lines separation.

{chr(10).join(sections)}
"""
    return body.rstrip() + metadata_trailer(metadata)


def sketch_placement(
    project_dir: Path,
    *,
    variants: int = 3,
    seed: int = 1,
    iterations: int = ITERATIONS,
    write: bool = True,
) -> SketchResult:
    """Propose coarse floorplans. Never touches the board or the contract."""
    project_dir = Path(project_dir).expanduser().resolve()
    if variants < 1 or variants > len(VARIANT_LABELS):
        raise SketchInputError(f"variants must be between 1 and {len(VARIANT_LABELS)}")
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise SketchInputError(str(exc)) from exc
    if not (project_dir / PLACEMENT_FILENAME).is_file():
        raise SketchInputError(
            f"missing {PLACEMENT_FILENAME}; run the CIRCUIT-to-LAYOUT handoff first"
        )
    try:
        contract = read_placement_contract(project_dir)
    except PlacementError as exc:
        raise SketchInputError(str(exc)) from exc
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    try:
        geometry = read_board_geometry(board_path)
        evidence = read_board_evidence(board_path)
    except (BoardGeometryError, BuildTestError, BuildTestInputError) as exc:
        raise SketchInputError(str(exc)) from exc

    board, origin = _board_size(geometry, spec)
    boxes = _group_boxes(contract, geometry, origin)
    problem = _build_problem(contract, boxes, board, evidence)
    solved = _solve(problem, variants, seed, iterations)

    report = _render_report(spec.name, solved, board)
    report_path = project_dir / REPORT_FILENAME
    svg_paths = tuple(
        project_dir / "docs" / f"placement-sketch-{variant.label}.svg"
        for variant in solved
    )
    wrote = False
    if write:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        outputs = [(report_path, report.encode())]
        outputs.extend(
            (path, _svg(variant, problem).encode())
            for path, variant in zip(svg_paths, solved)
        )
        try:
            wrote = any(commit_outputs(outputs, label="placement sketch"))
        except AtomicWriteError as exc:
            raise SketchError(str(exc)) from exc

    return SketchResult(
        project_dir,
        board,
        solved,
        REPORT_FILENAME,
        tuple(path.relative_to(project_dir) for path in svg_paths),
        wrote,
    )


__all__ = [
    "GRID_MM",
    "ITERATIONS",
    "REPORT_FILENAME",
    "ROUTING_MARGIN",
    "SKETCH_SCHEMA",
    "WEIGHTS",
    "GroupBox",
    "PlacedRect",
    "SketchError",
    "SketchInputError",
    "SketchResult",
    "Variant",
    "floorplan_block",
    "sketch_placement",
]
