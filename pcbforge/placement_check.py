"""Measure a live KiCad board against the `placement.yaml` contract.

Read-only with respect to the board: this module never moves a footprint or
touches copper. Its only output is `docs/placement-check.md`.

Deliberately ungated. The user may run it at any point during LAYOUT, including
after a reopen, so it never requires a current CIRCUIT acceptance or an approved
handoff. It reports what the board looks like now; deciding what to do about
that is the user's job.

Findings carry four outcomes. `pass` and `fail` are measured against a stated
limit. `manual` is for the constraint kinds that describe intent no geometry can
settle -- orientation, accessibility, airflow -- and reports the current position
so a reviewer can judge them. `unmeasured` means the measurement could not be
taken: no board outline, or an endpoint the board no longer carries. An
unresolvable endpoint is reported, never raised, because the contract validator
already proved every endpoint existed, so a miss here means the board moved
under the contract and that is precisely what the user needs told.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from pcbforge.board_geometry import (
    BoardGeometry,
    BoardGeometryError,
    Box,
    FootprintGeometry,
    read_board_geometry,
    to_board,
    to_local,
)
from pcbforge.build_test import BuildTestError, BuildTestInputError
from pcbforge.fsutil import AtomicWriteError, commit_outputs
from pcbforge.initialize import InitInputError, ProjectSpec, read_spec
from pcbforge.markdown_metadata import metadata_trailer, metadata_yaml
from pcbforge.patterns import PATTERNS_DIRNAME
from pcbforge.placement import (
    PLACEMENT_FILENAME,
    PlacementConstraint,
    PlacementContract,
    PlacementError,
    PlacementInputError,
    read_placement_contract,
    split_endpoint,
)

PLACEMENT_CHECK_SCHEMA = 1
REPORT_FILENAME = Path("docs/placement-check.md")

#: Minimum overlap on BOTH axes before two courtyards count as colliding.
OVERLAP_TOLERANCE_MM = 0.05
#: How many worst offenders to name inside an aggregated finding.
WORST_LISTED = 3
#: Slack when comparing an outline drawn on a coarse grid against spec.md.
SIZE_TOLERANCE_MM = 0.01

MANUAL_KINDS = frozenset({"orientation", "accessibility", "airflow"})
#: Report ordering: worst first, so a reader sees failures before anything else.
STATUSES = ("fail", "unmeasured", "manual", "pass")
#: Summary ordering, which reads naturally rather than worst-first.
SUMMARY_STATUSES = ("pass", "fail", "manual", "unmeasured")
_STATUS_ORDER = {status: index for index, status in enumerate(STATUSES)}
_KIND_ORDER = {
    "constraint": 0,
    "pattern": 1,
    "floorplan": 2,
    "overlap": 3,
    "outline": 4,
}
#: How far the drawn outline may differ from an adopted floorplan's board size.
FLOORPLAN_SIZE_TOLERANCE_MM = 0.5
#: How far a satellite's rotation may drift from the pattern before it is a
#: different placement rather than a placed one.
ROTATION_TOLERANCE_DEG = 1.0


class PlacementCheckError(RuntimeError):
    """The placement check could not be completed."""


class PlacementCheckInputError(PlacementCheckError):
    """The project, contract, or board is malformed."""


@dataclass(frozen=True)
class Finding:
    """One measured statement about the board."""

    kind: str
    identifier: str
    status: str
    measured: str
    limit: str
    detail: str = ""

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (
            _KIND_ORDER.get(self.kind, len(_KIND_ORDER)),
            _STATUS_ORDER.get(self.status, len(STATUSES)),
            self.identifier,
        )


@dataclass(frozen=True)
class PlacementCheckResult:
    project_dir: Path
    board_sha256: str
    findings: tuple[Finding, ...]
    warnings: tuple[str, ...]
    report: str
    report_path: Path
    wrote_report: bool

    def count(self, status: str) -> int:
        return sum(1 for finding in self.findings if finding.status == status)

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.status == "fail")

    @property
    def summary(self) -> str:
        return ", ".join(
            f"{self.count(status)} {status}" for status in SUMMARY_STATUSES
        )


def _millimetres(value: float) -> str:
    return f"{value:.2f} mm"


def _box_of(
    geometry: BoardGeometry,
    endpoint: str,
) -> tuple[Box | None, tuple[float, float] | None, str]:
    """Return the box and centre for an endpoint, plus a reason when it fails."""
    parsed = split_endpoint(endpoint)
    if parsed is None:
        return None, None, f"malformed endpoint {endpoint!r}"
    reference, pad_number = parsed
    try:
        footprint = geometry.footprint(reference)
    except KeyError:
        return None, None, f"{reference} is not on the board"
    if pad_number is None:
        return footprint.box, footprint.box.centre, ""
    try:
        pad = footprint.pad(pad_number)
    except KeyError:
        return None, None, f"{endpoint} is not on the board"
    # pad.box already carries the pad's own rotation and custom primitives.
    return pad.box, (pad.x, pad.y), ""


def _is_pad(endpoint: str) -> bool:
    parsed = split_endpoint(endpoint)
    return parsed is not None and parsed[1] is not None


def _unmeasured(constraint: PlacementConstraint, reason: str) -> Finding:
    return Finding(
        "constraint",
        constraint.identifier,
        "unmeasured",
        "not measured",
        "",
        reason,
    )


def _edge_gap(box: Box, outline: Box, edge: str) -> float:
    """Gap from a box to a named side of the board outline."""
    gaps = {
        "north": box.min_y - outline.min_y,
        "south": outline.max_y - box.max_y,
        "west": box.min_x - outline.min_x,
        "east": outline.max_x - box.max_x,
    }
    if edge == "any":
        return min(gaps.values())
    return gaps[edge]


def _evaluate_constraint(
    constraint: PlacementConstraint,
    geometry: BoardGeometry,
) -> Finding:
    if constraint.kind in MANUAL_KINDS:
        described = []
        for endpoint in constraint.subjects:
            parsed = split_endpoint(endpoint)
            if parsed is None:
                continue
            try:
                footprint = geometry.footprint(parsed[0])
            except KeyError:
                described.append(f"{endpoint} is not on the board")
                continue
            described.append(
                f"{footprint.reference} at "
                f"({footprint.x:g}, {footprint.y:g}) "
                f"rotation {footprint.rotation:g}, {footprint.side}"
            )
        return Finding(
            "constraint",
            constraint.identifier,
            "manual",
            "; ".join(described),
            "judge by eye",
            constraint.direction or constraint.edge or "",
        )

    boxes: list[Box] = []
    centres: list[tuple[float, float]] = []
    for endpoint in constraint.subjects:
        box, centre, reason = _box_of(geometry, endpoint)
        if box is None or centre is None:
            return _unmeasured(constraint, reason)
        boxes.append(box)
        centres.append(centre)

    if constraint.kind == "proximity":
        limit = constraint.max_mm or 0.0
        if all(_is_pad(endpoint) for endpoint in constraint.subjects):
            (ax, ay), (bx, by) = centres[0], centres[1]
            measured = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            basis = "pad centres"
        else:
            measured = boxes[0].distance_to(boxes[1])
            basis = "nearest edges"
        return Finding(
            "constraint",
            constraint.identifier,
            "pass" if measured <= limit else "fail",
            _millimetres(measured),
            f"<= {limit:g} mm",
            f"{' to '.join(constraint.subjects)}, {basis}",
        )

    if constraint.kind == "separation":
        limit = constraint.min_mm or 0.0
        measured = boxes[0].distance_to(boxes[1])
        return Finding(
            "constraint",
            constraint.identifier,
            "pass" if measured >= limit else "fail",
            _millimetres(measured),
            f">= {limit:g} mm",
            f"{' to '.join(constraint.subjects)}, nearest edges",
        )

    if constraint.kind == "board-edge":
        if geometry.outline is None:
            return _unmeasured(constraint, "the board has no Edge.Cuts outline")
        limit = constraint.max_mm or 0.0
        edge = constraint.edge or "any"
        measured = _edge_gap(boxes[0], geometry.outline, edge)
        return Finding(
            "constraint",
            constraint.identifier,
            "pass" if measured <= limit else "fail",
            _millimetres(measured),
            f"<= {limit:g} mm",
            f"{constraint.subjects[0]} to the {edge} edge",
        )

    if constraint.kind == "order":
        direction = constraint.direction or ""
        # y grows downward, so north-to-south is also an increasing projection.
        axis = 1 if direction in {"north-to-south", "south-to-north"} else 0
        ascending = direction in {"west-to-east", "north-to-south"}
        values = [centre[axis] for centre in centres]
        # Strict: two parts at the same coordinate are not in a defined order.
        out_of_order = next(
            (
                index
                for index, (first, second) in enumerate(zip(values, values[1:]))
                if not (first < second if ascending else first > second)
            ),
            None,
        )
        if out_of_order is None:
            detail = ", ".join(constraint.subjects)
        else:
            detail = (
                f"{constraint.subjects[out_of_order]} is not before "
                f"{constraint.subjects[out_of_order + 1]}"
            )
        return Finding(
            "constraint",
            constraint.identifier,
            "pass" if out_of_order is None else "fail",
            ", ".join(
                f"{endpoint} {value:.2f}"
                for endpoint, value in zip(constraint.subjects, values)
            ),
            direction,
            detail,
        )

    if constraint.kind == "loop":
        limit = constraint.max_mm or 0.0
        closed = centres + centres[:1]
        measured = sum(
            ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            for (ax, ay), (bx, by) in zip(closed, closed[1:])
        )
        return Finding(
            "constraint",
            constraint.identifier,
            "pass" if measured <= limit else "fail",
            _millimetres(measured),
            f"<= {limit:g} mm",
            f"closed loop through {', '.join(constraint.subjects)}",
        )

    return _unmeasured(constraint, f"no evaluator for {constraint.kind}")


def _keepout_findings(
    constraint: PlacementConstraint,
    geometry: BoardGeometry,
) -> list[Finding]:
    """One finding per subject, since each fastener has its own clearance."""
    limit = constraint.min_mm or 0.0
    siblings = set()
    for endpoint in constraint.subjects:
        parsed = split_endpoint(endpoint)
        if parsed is not None:
            siblings.add(parsed[0])
    findings = []
    for endpoint in constraint.subjects:
        box, _, reason = _box_of(geometry, endpoint)
        if box is None:
            findings.append(_unmeasured(constraint, reason))
            continue
        worst: tuple[float, str] | None = None
        for other in geometry.footprints:
            # A constraint's own subjects never offend each other: two mounting
            # holes 40 mm apart must not fail their own 3 mm clearance.
            if other.reference in siblings:
                continue
            gap = box.distance_to(other.box)
            if worst is None or gap < worst[0]:
                worst = (gap, f"{other.reference} (footprint)")
        for index, via in enumerate(geometry.vias, start=1):
            radius = via.diameter / 2
            gap = box.distance_to(
                Box(via.x - radius, via.y - radius, via.x + radius, via.y + radius)
            )
            if worst is None or gap < worst[0]:
                worst = (gap, f"via {index} on {via.net or 'no net'}")
        if worst is None:
            findings.append(
                _unmeasured(constraint, "nothing on the board to measure against")
            )
            continue
        identifier = (
            constraint.identifier
            if len(constraint.subjects) == 1
            else f"{constraint.identifier} [{endpoint}]"
        )
        findings.append(
            Finding(
                "constraint",
                identifier,
                "pass" if worst[0] >= limit else "fail",
                _millimetres(worst[0]),
                f">= {limit:g} mm",
                f"{endpoint} nearest {worst[1]}",
            )
        )
    return findings


def _rotation_delta(first: float, second: float) -> float:
    """Smallest angle between two rotations, in degrees."""
    delta = abs(first - second) % 360.0
    return min(delta, 360.0 - delta)


def _anchor_frame_side(local: tuple[float, float]) -> str:
    """Which side of the anchor a local point lies on. y grows downward."""
    x, y = local
    if abs(x) >= abs(y):
        return "east" if x >= 0 else "west"
    return "south" if y >= 0 else "north"


def _pattern_finding(
    identifier: str,
    status: str,
    measured: str,
    limit: str,
    detail: str,
) -> Finding:
    return Finding("pattern", identifier, status, measured, limit, detail)


def _exact_role_finding(
    identifier: str,
    role,
    anchor: FootprintGeometry,
    satellite: FootprintGeometry,
) -> Finding:
    expected_x, expected_y = to_board(anchor, role.offset_mm)
    offset = (
        (satellite.x - expected_x) ** 2 + (satellite.y - expected_y) ** 2
    ) ** 0.5
    expected_rotation = (anchor.rotation + role.rotation_deg) % 360.0
    rotation_delta = _rotation_delta(satellite.rotation, expected_rotation)
    expected_side = (
        anchor.side
        if role.side == "same"
        else ("back" if anchor.side == "front" else "front")
    )
    ok = (
        offset <= role.tolerance_mm
        and rotation_delta <= ROTATION_TOLERANCE_DEG
        and satellite.side == expected_side
    )
    return _pattern_finding(
        identifier,
        "pass" if ok else "fail",
        f"{_millimetres(offset)} off, rotation {rotation_delta:g}\u00b0, "
        f"{satellite.side}",
        f"<= {role.tolerance_mm:g} mm, {ROTATION_TOLERANCE_DEG:g}\u00b0, "
        f"{expected_side}",
        f"{satellite.reference} against {anchor.reference} "
        f"({expected_x:.2f}, {expected_y:.2f})",
    )


def _sketch_role_finding(
    identifier: str,
    role,
    anchor: FootprintGeometry,
    satellite: FootprintGeometry,
) -> Finding:
    anchor_pad_number = role.anchor_pads[0]
    try:
        anchor_pad = anchor.pad(anchor_pad_number)
    except KeyError:
        return _pattern_finding(
            identifier,
            "unmeasured",
            "not measured",
            "",
            f"{anchor.reference} has no pad {anchor_pad_number}",
        )
    shared = [pad for pad in satellite.pads if pad.net and pad.net == anchor_pad.net]
    if not shared:
        return _pattern_finding(
            identifier,
            "unmeasured",
            "not measured",
            "",
            f"{satellite.reference} has no pad on {anchor_pad.net}",
        )
    distance = min(
        ((pad.x - anchor_pad.x) ** 2 + (pad.y - anchor_pad.y) ** 2) ** 0.5
        for pad in shared
    )
    side = _anchor_frame_side(to_local(anchor, satellite.centre))
    ok = distance <= role.max_mm and side == role.near_side
    return _pattern_finding(
        identifier,
        "pass" if ok else "fail",
        f"{_millimetres(distance)}, {side} of {anchor.reference}",
        f"<= {role.max_mm:g} mm, {role.near_side}",
        f"{anchor.reference}.{anchor_pad_number} to the nearest "
        f"{satellite.reference} pad on {anchor_pad.net}",
    )


def _rule_finding(
    identifier: str,
    rule,
    anchor: FootprintGeometry,
    geometry: BoardGeometry,
) -> Finding:
    if rule.kind == "note":
        return _pattern_finding(
            identifier,
            "manual",
            rule.text or "",
            "judge by eye",
            f"around {anchor.reference}",
        )
    try:
        pad = anchor.pad(rule.anchor_pad)
    except KeyError:
        return _pattern_finding(
            identifier,
            "unmeasured",
            "not measured",
            "",
            f"{anchor.reference} has no pad {rule.anchor_pad}",
        )
    count = sum(
        1
        for via in geometry.vias
        if via.net == pad.net and pad.box.contains((via.x, via.y))
    )
    return _pattern_finding(
        identifier,
        "pass" if count >= rule.min_count else "fail",
        f"{count} vias",
        f">= {rule.min_count} vias",
        f"in {anchor.reference}.{rule.anchor_pad} on {pad.net or 'no net'}",
    )


def _pattern_findings(
    contract: PlacementContract,
    geometry: BoardGeometry,
) -> list[Finding]:
    """One finding per bound role and per rule of every declared pattern.

    Identifiers carry the group so two groups using the same pattern, and so
    the same role ids, stay distinguishable in the report.
    """
    findings: list[Finding] = []
    for group, binding in contract.patterns:
        pattern = binding.pattern
        try:
            anchor = geometry.footprint(binding.anchor)
        except KeyError:
            findings.append(
                _pattern_finding(
                    f"{group}/{pattern.identifier}",
                    "unmeasured",
                    "not measured",
                    "",
                    f"anchor {binding.anchor} is not on the board",
                )
            )
            continue
        for role_id, reference in binding.roles:
            identifier = f"{group}/{role_id}"
            role = pattern.role(role_id)
            if reference is None:
                findings.append(
                    _pattern_finding(
                        identifier,
                        "unmeasured",
                        "not measured",
                        "",
                        "role is unbound",
                    )
                )
                continue
            try:
                satellite = geometry.footprint(reference)
            except KeyError:
                findings.append(
                    _pattern_finding(
                        identifier,
                        "unmeasured",
                        "not measured",
                        "",
                        f"{reference} is not on the board",
                    )
                )
                continue
            if pattern.fidelity == "exact":
                findings.append(
                    _exact_role_finding(identifier, role, anchor, satellite)
                )
            else:
                findings.append(
                    _sketch_role_finding(identifier, role, anchor, satellite)
                )
        for rule in pattern.rules:
            findings.append(
                _rule_finding(f"{group}/{rule.identifier}", rule, anchor, geometry)
            )
    return findings


def _floorplan_findings(
    contract: PlacementContract,
    geometry: BoardGeometry,
) -> list[Finding]:
    """Measure the real placement against the adopted coarse floorplan.

    Floorplan rectangles are board-relative, so every footprint centre is taken
    relative to the outline's own corner before it is compared.
    """
    floorplan = contract.floorplan
    if floorplan is None:
        return []
    findings: list[Finding] = []
    outline = geometry.outline
    if outline is None:
        return [
            Finding(
                "floorplan",
                "outline-matches-floorplan",
                "unmeasured",
                "not measured",
                "",
                "the board has no Edge.Cuts outline",
            )
        ]

    width_error = abs(outline.width - floorplan.board_mm[0])
    height_error = abs(outline.height - floorplan.board_mm[1])
    worst = max(width_error, height_error)
    findings.append(
        Finding(
            "floorplan",
            "outline-matches-floorplan",
            "pass" if worst <= FLOORPLAN_SIZE_TOLERANCE_MM else "fail",
            f"{outline.width:.2f} x {outline.height:.2f} mm",
            f"{floorplan.board_mm[0]:g} x {floorplan.board_mm[1]:g} mm "
            f"+/- {FLOORPLAN_SIZE_TOLERANCE_MM:g} mm",
            f"variant {floorplan.variant}",
        )
    )

    references = {
        group.identifier: group.references for group in contract.groups
    }
    for rect in floorplan.rects:
        box = Box(rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)
        placed = []
        for reference in references.get(rect.identifier, ()):
            try:
                placed.append(geometry.footprint(reference))
            except KeyError:
                continue
        if not placed:
            findings.append(
                Finding(
                    "floorplan",
                    rect.identifier,
                    "unmeasured",
                    "not measured",
                    "",
                    "no footprint of this group is on the board",
                )
            )
            continue
        centres = [
            (item.box.centre[0] - outline.min_x, item.box.centre[1] - outline.min_y)
            for item in placed
        ]
        inside = [box.contains(centre) for centre in centres]
        centroid = (
            sum(centre[0] for centre in centres) / len(centres),
            sum(centre[1] for centre in centres) / len(centres),
        )
        overhang = max(
            0.0,
            box.min_x - centroid[0],
            centroid[0] - box.max_x,
            box.min_y - centroid[1],
            centroid[1] - box.max_y,
        )
        strays = [
            item.reference
            for item, is_inside in zip(placed, inside)
            if not is_inside
        ]
        findings.append(
            Finding(
                "floorplan",
                rect.identifier,
                "pass" if all(inside) else "fail",
                f"{sum(inside)} of {len(inside)} centres inside, "
                f"centroid {_millimetres(overhang)} outside",
                "every footprint centre inside",
                ""
                if not strays
                else "outside: "
                + ", ".join(strays[:WORST_LISTED])
                + (f" (+{len(strays) - WORST_LISTED} more)" if len(strays) > WORST_LISTED else ""),
            )
        )
    return findings


def _overlap_findings(geometry: BoardGeometry) -> list[Finding]:
    findings = []
    placed = [
        item
        for item in geometry.footprints
        if not item.footprint.split(":")[-1].startswith("MountingHole")
    ]
    for index, first in enumerate(placed):
        for second in placed[index + 1 :]:
            if first.side != second.side:
                continue
            overlap_x = min(first.box.max_x, second.box.max_x) - max(
                first.box.min_x, second.box.min_x
            )
            overlap_y = min(first.box.max_y, second.box.max_y) - max(
                first.box.min_y, second.box.min_y
            )
            if overlap_x <= OVERLAP_TOLERANCE_MM or overlap_y <= OVERLAP_TOLERANCE_MM:
                continue
            estimated = [
                item.reference
                for item in (first, second)
                if item.box_source != "courtyard"
            ]
            note = (
                f"; estimated extent for {', '.join(estimated)}" if estimated else ""
            )
            findings.append(
                Finding(
                    "overlap",
                    f"{first.reference}/{second.reference}",
                    "fail",
                    f"{overlap_x:.2f} x {overlap_y:.2f} mm",
                    "no overlap",
                    f"both on the {first.side}{note}",
                )
            )
    if not findings:
        # One row, not a silent empty table, so a clean board says so.
        return [
            Finding(
                "overlap",
                "courtyard-overlaps",
                "pass",
                f"0 of {len(placed)} footprints collide",
                "no overlap",
                "",
            )
        ]
    return findings


def _outline_findings(
    geometry: BoardGeometry,
    spec: ProjectSpec,
) -> list[Finding]:
    if geometry.outline is None:
        return [
            Finding(
                "outline",
                "footprints-inside-outline",
                "unmeasured",
                "not measured",
                "every footprint inside",
                "the board has no Edge.Cuts outline",
            ),
            Finding(
                "outline",
                "outline-within-spec",
                "unmeasured",
                "not measured",
                f"<= {spec.board_mm[0]:g} x {spec.board_mm[1]:g} mm",
                "the board has no Edge.Cuts outline",
            ),
        ]

    outline = geometry.outline
    outside: list[tuple[float, str]] = []
    for item in geometry.footprints:
        if outline.contains_box(item.box):
            continue
        overhang = max(
            outline.min_x - item.box.min_x,
            item.box.max_x - outline.max_x,
            outline.min_y - item.box.min_y,
            item.box.max_y - outline.max_y,
        )
        outside.append((overhang, item.reference))
    outside.sort(reverse=True)
    worst = ", ".join(
        f"{reference} {overhang:.1f} mm" for overhang, reference in outside[:WORST_LISTED]
    )

    findings = [
        Finding(
            "outline",
            "footprints-inside-outline",
            "fail" if outside else "pass",
            f"{len(outside)} of {len(geometry.footprints)} outside",
            "every footprint inside",
            f"worst {worst}" if worst else "",
        )
    ]

    # Compare sorted pairs: a 50 x 40 budget also allows a 40 x 50 board.
    budget = sorted(spec.board_mm)
    actual = sorted((outline.width, outline.height))
    oversize = any(
        measured > allowed + SIZE_TOLERANCE_MM
        for measured, allowed in zip(actual, budget)
    )
    findings.append(
        Finding(
            "outline",
            "outline-within-spec",
            "fail" if oversize else "pass",
            f"{outline.width:g} x {outline.height:g} mm",
            f"<= {spec.board_mm[0]:g} x {spec.board_mm[1]:g} mm, either orientation",
            "board_mm in spec.md",
        )
    )
    return findings


def _warnings(
    geometry: BoardGeometry,
    contract: PlacementContract,
    spec: ProjectSpec,
) -> list[str]:
    warnings = []
    if geometry.layer_count != spec.layers:
        warnings.append(
            f"the board has {geometry.layer_count} copper layers; "
            f"spec.md declares {spec.layers}"
        )
    fallback = sorted(
        item.reference for item in geometry.footprints if item.box_source != "courtyard"
    )
    if fallback:
        shown = ", ".join(fallback[:8])
        more = "" if len(fallback) <= 8 else f" (+{len(fallback) - 8} more)"
        warnings.append(
            "no courtyard on "
            f"{shown}{more}; their extents are estimated from fab, silkscreen, "
            "and pad geometry and may be smaller than the true keepout"
        )
    measured = sum(
        1 for item in contract.constraints if item.kind not in MANUAL_KINDS
    )
    if not measured:
        warnings.append("the contract declares no measurable constraint")
    if geometry.outline is not None and geometry.footprints:
        outside = sum(
            1
            for item in geometry.footprints
            if not geometry.outline.contains_box(item.box)
        )
        if outside * 2 > len(geometry.footprints):
            warnings.append(
                "most footprints are outside the board outline; "
                "placement has not started, so most distances are expected to fail"
            )
    if any(item.kind == "keepout" for item in contract.constraints):
        warnings.append(
            "keepout constraints measure footprint bodies and vias only; "
            "tracks, copper pours, and silkscreen are not checked"
        )
    return sorted(set(warnings))


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _table(findings: Sequence[Finding]) -> str:
    header = (
        "| ID | Status | Measured | Limit | Detail |\n|---|---|---|---|---|"
    )
    if not findings:
        return f"{header}\n| None | — | — | — | — |"
    rows = "\n".join(
        "| "
        + " | ".join(
            (
                _escape(item.identifier),
                item.status,
                _escape(item.measured),
                _escape(item.limit) or "—",
                _escape(item.detail) or "—",
            )
        )
        + " |"
        for item in findings
    )
    return f"{header}\n{rows}"


def _render_report(
    project_name: str,
    result_findings: Sequence[Finding],
    warnings: Sequence[str],
    board_sha256: str,
    counts: Mapping[str, int],
) -> str:
    metadata = yaml.safe_dump(
        {
            "pcbforge_placement_check_schema": PLACEMENT_CHECK_SCHEMA,
            "result": "fail" if counts["fail"] else "pass",
            "board_sha256": board_sha256,
            "counts": {status: counts[status] for status in SUMMARY_STATUSES},
        },
        sort_keys=False,
    ).rstrip()
    ordered = sorted(result_findings, key=lambda item: item.sort_key)
    by_kind = {
        kind: [item for item in ordered if item.kind == kind]
        for kind in ("constraint", "pattern", "floorplan", "overlap", "outline")
    }
    summary = ", ".join(f"{counts[status]} {status}" for status in SUMMARY_STATUSES)
    verdict = "FAIL" if counts["fail"] else "PASS"
    warning_lines = (
        "\n".join(f"- {item}" for item in warnings) if warnings else "- None."
    )
    body = f"""# {project_name} placement check

> Generated by PCBForge from `placement.yaml` and the current board. Advisory
> only: it measures placement, it never changes it and never gates a phase.
> Rerun `pcbforge check-placement --write-report`; do not edit this report
> manually.

## Result

**{verdict}** — {summary}.

Measured against board sha256 `{board_sha256}`.

## Contract constraints

{_table(by_kind["constraint"])}

## Reference patterns

{_table(by_kind["pattern"])}

## Adopted floorplan

{_table(by_kind["floorplan"])}

## Courtyard overlaps

{_table(by_kind["overlap"])}

## Board outline

{_table(by_kind["outline"])}

## Warnings

{warning_lines}
"""
    return body.rstrip() + metadata_trailer(metadata)


def _read_project(
    project_dir: Path,
) -> tuple[ProjectSpec, PlacementContract, BoardGeometry, Path, bytes]:
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise PlacementCheckInputError(
            f"project directory does not exist: {project_dir}"
        )
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise PlacementCheckInputError(str(exc)) from exc
    if not (project_dir / PLACEMENT_FILENAME).is_file():
        raise PlacementCheckInputError(
            f"missing {PLACEMENT_FILENAME}; run the CIRCUIT-to-LAYOUT handoff first"
        )
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    try:
        board_bytes = board_path.read_bytes()
    except FileNotFoundError as exc:
        raise PlacementCheckInputError(f"missing {board_path.name}") from exc
    except OSError as exc:
        raise PlacementCheckInputError(f"cannot read {board_path}: {exc}") from exc
    try:
        contract = read_placement_contract(project_dir)
    except PlacementInputError as exc:
        raise PlacementCheckInputError(str(exc)) from exc
    except (PlacementError, BuildTestInputError, BuildTestError) as exc:
        raise PlacementCheckInputError(str(exc)) from exc
    try:
        geometry = read_board_geometry(board_path)
    except BoardGeometryError as exc:
        raise PlacementCheckInputError(str(exc)) from exc
    return spec, contract, geometry, board_path, board_bytes


def check_placement(
    project_dir: Path,
    *,
    write_report: bool = False,
) -> PlacementCheckResult:
    """Measure the board against placement.yaml. Never changes the board."""
    project_dir = Path(project_dir).expanduser().resolve()
    spec, contract, geometry, board_path, board_bytes = _read_project(project_dir)

    findings: list[Finding] = []
    for constraint in contract.constraints:
        if constraint.kind == "keepout":
            findings.extend(_keepout_findings(constraint, geometry))
        else:
            findings.append(_evaluate_constraint(constraint, geometry))
    findings.extend(_pattern_findings(contract, geometry))
    findings.extend(_floorplan_findings(contract, geometry))
    findings.extend(_overlap_findings(geometry))
    findings.extend(_outline_findings(geometry, spec))
    findings.sort(key=lambda item: item.sort_key)

    warnings = _warnings(geometry, contract, spec)
    board_sha256 = hashlib.sha256(board_bytes).hexdigest()
    counts = {
        status: sum(1 for item in findings if item.status == status)
        for status in STATUSES
    }
    report = _render_report(spec.name, findings, warnings, board_sha256, counts)

    wrote = False
    if write_report:
        report_path = project_dir / REPORT_FILENAME
        report_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            (wrote,) = commit_outputs(
                ((report_path, report.encode()),),
                label="placement check report",
            )
        except AtomicWriteError as exc:
            raise PlacementCheckError(str(exc)) from exc

    if board_path.read_bytes() != board_bytes:
        raise PlacementCheckError(
            f"safety invariant failed: {board_path.name} changed during the check"
        )

    return PlacementCheckResult(
        project_dir,
        board_sha256,
        tuple(findings),
        tuple(warnings),
        report,
        REPORT_FILENAME,
        wrote,
    )


def board_drift_warning(project_dir: Path, board_path: Path) -> str | None:
    """Warn when the board has moved on since the last recorded check.

    Lives here because it reads this module's own report format. The spatial
    edit commands call it so they can say "the numbers you are working from are
    stale" without refusing to run: the user asked for the edit, and a missing
    or old check is a reason to mention it, not to block.
    """
    report = Path(project_dir) / REPORT_FILENAME
    if not report.is_file():
        return (
            "no placement check has been recorded; run `pcbforge check-placement "
            "--write-report` to see what this changes"
        )
    try:
        metadata = yaml.safe_load(metadata_yaml(report.read_text(encoding="utf-8")))
        recorded = metadata["board_sha256"]
        current = hashlib.sha256(Path(board_path).read_bytes()).hexdigest()
    except (OSError, UnicodeError, KeyError, TypeError, yaml.YAMLError):
        return f"cannot read the board fingerprint from {REPORT_FILENAME.as_posix()}"
    if current != recorded:
        return (
            f"the board changed since {REPORT_FILENAME.as_posix()} was written; "
            "its measurements are stale"
        )
    return None


def placement_check_inputs(project_dir: Path) -> tuple[Path, ...]:
    """Visible inputs for dashboard diagnostics and check fingerprinting.

    Deliberately does not resolve ``project_dir``: `status._fingerprint` takes
    each path relative to the directory it was handed, so resolving here would
    break fingerprinting whenever that directory is a symlink, as it is under
    the macOS temporary directory every test uses.
    """
    paths: Iterable[Path] = (
        project_dir / PLACEMENT_FILENAME,
        *sorted(project_dir.glob("*.kicad_pcb")),
        # A project-local pattern changes what the check measures, so editing
        # one must re-run it rather than reuse the recorded result.
        *sorted((project_dir / PATTERNS_DIRNAME).glob("*.yaml")),
    )
    return tuple(path for path in paths if path.is_file())


__all__ = [
    "board_drift_warning",
    "PLACEMENT_CHECK_SCHEMA",
    "SUMMARY_STATUSES",
    "REPORT_FILENAME",
    "Finding",
    "PlacementCheckError",
    "PlacementCheckInputError",
    "PlacementCheckResult",
    "check_placement",
    "placement_check_inputs",
]
