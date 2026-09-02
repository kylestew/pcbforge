"""Tests for `pcbforge.placement_check`.

Boards and contracts are written inline, as `tests/test_placement.py` does, and
scaffolded into a real project by reusing its `PlacementFixture.project`. The
contract parser validates every constraint endpoint against the board, so each
fixture board and its `placement.yaml` have to agree; the helpers below keep the
pair in one place.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from pcbforge.cli import main
from pcbforge.compatibility import EXPECTED_GUIDANCE
from pcbforge.markdown_metadata import metadata_yaml
from pcbforge.placement import brief_inputs
from pcbforge.placement_check import (
    PLACEMENT_CHECK_SCHEMA,
    REPORT_FILENAME,
    PlacementCheckError,
    PlacementCheckInputError,
    check_placement,
)
from tests.test_placement import PlacementFixture

BOARD_HEADER = """(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
    (31 "F.CrtYd" user "F.Courtyard")
    (29 "B.CrtYd" user "B.Courtyard")
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3V3")
"""

# A 50 x 40 outline matching spec.md board_mm, anchored at the usual origin.
OUTLINE = """  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
"""


def footprint(
    reference: str,
    x: float,
    y: float,
    *,
    rotation: float = 0.0,
    side: str = "F.Cu",
    half: float = 1.0,
    name: str = "Test:Part",
    net: str = "GND",
    net_index: int = 1,
) -> str:
    """A two-pad footprint with a square courtyard of side ``2 * half``."""
    crtyd = "B.CrtYd" if side == "B.Cu" else "F.CrtYd"
    return f"""  (footprint "{name}"
    (layer "{side}")
    (at {x} {y} {rotation})
    (property "Reference" "{reference}")
    (fp_rect (start {-half} {-half}) (end {half} {half}) (layer "{crtyd}"))
    (pad "1" smd rect (at {-half / 2} 0) (size 0.5 0.5) (net {net_index} "{net}"))
    (pad "2" smd rect (at {half / 2} 0) (size 0.5 0.5) (net {net_index} "{net}"))
  )
"""


def contract(constraints: str, groups: str) -> str:
    body = f"constraints:\n{constraints}" if constraints else "constraints: []"
    return f"""placement_schema: 1
board:
  strategy: Test strategy.
  rules:
    - Test rule.
groups:
{groups}
placement_order: [everything]
{body}
net_classes:
  - name: power
    rationale: Ground return.
    nets: [GND]
    clearance_mm: 0.2
    track_width_mm: 0.5
    via_diameter_mm: 0.7
    via_drill_mm: 0.3
checklist:
  - Check the board.
"""


def one_group(references: str) -> str:
    return f"""  - id: everything
    priority: 1
    region: anywhere
    rationale: Test group.
    references: [{references}]"""


def _contract_ignoring_the_board(project: Path):
    """Freeze the contract as parsed now, so a later board edit cannot fail it.

    `read_placement_contract` validates endpoints against the board, so removing
    a footprint would otherwise raise before the evaluators ever ran. Freezing it
    is what lets the test reach the `unmeasured` path.
    """
    from pcbforge.placement import read_placement_contract as _read

    return _read(project)


class CheckFixture(PlacementFixture):
    """Scaffolds a real project, then replaces its board and contract."""

    def build(
        self,
        root: Path,
        board_body: str,
        constraints: str,
        references: str,
        *,
        current_step6: bool = True,
        outline: str = OUTLINE,
    ) -> Path:
        project = self.project(root, current_step6=current_step6)
        (project / "garden-logger.kicad_pcb").write_text(
            BOARD_HEADER + outline + board_body + ")\n",
            encoding="utf-8",
        )
        (project / "placement.yaml").write_text(
            contract(constraints, one_group(references)),
            encoding="utf-8",
        )
        # PlacementFixture pins only the keys its own tests need. The CLI runs a
        # compatibility preflight over the full set, so complete it here rather
        # than mocking the command away.
        pins = yaml.safe_load((project / ".pcbforge").read_text(encoding="utf-8"))
        pins["pcbforge"] = {"revision": "0" * 40, "dirty": False}
        pins["guidance"] = dict(EXPECTED_GUIDANCE)
        (project / ".pcbforge").write_text(
            yaml.safe_dump(pins, sort_keys=False), encoding="utf-8"
        )
        return project

    def findings(self, result) -> dict[str, tuple[str, str]]:
        return {
            item.identifier: (item.status, item.measured) for item in result.findings
        }


class ProximityTests(CheckFixture):
    def measure(self, subjects: str, limit: float, gap: float = 10.0):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 110 + gap, 120),
                f"""  - id: close
    type: proximity
    subjects: [{subjects}]
    max_mm: {limit}
    rationale: Keep it short.""",
                "U1, C1",
            )
            return check_placement(project)

    def test_pad_to_pad_uses_centre_distance(self) -> None:
        result = self.measure("U1.2, C1.1", 12.0, gap=10.0)
        status, measured = self.findings(result)["close"]
        # Pad 2 of U1 sits +0.5, pad 1 of C1 sits -0.5, so 10 - 1 = 9.
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "9.00 mm")

    def test_pad_to_pad_fails_beyond_the_limit(self) -> None:
        status, measured = self.findings(self.measure("U1.2, C1.1", 2.0))["close"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "9.00 mm")

    def test_reference_endpoints_use_nearest_edges(self) -> None:
        result = self.measure("U1, C1", 12.0, gap=10.0)
        status, measured = self.findings(result)["close"]
        # Courtyards are 2 mm wide, so the gap between edges is 10 - 2 = 8.
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "8.00 mm")

    def test_mixed_pad_and_reference_uses_nearest_edges(self) -> None:
        result = self.measure("U1.2, C1", 12.0, gap=10.0)
        status, measured = self.findings(result)["close"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "8.25 mm")


class SeparationTests(CheckFixture):
    def measure(self, limit: float, gap: float):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("U2", 110 + gap, 120),
                f"""  - id: apart
    type: separation
    subjects: [U1, U2]
    min_mm: {limit}
    rationale: Keep them apart.""",
                "U1, U2",
            )
            return check_placement(project)

    def test_separation_passes_when_far_enough(self) -> None:
        status, measured = self.findings(self.measure(5.0, 10.0))["apart"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "8.00 mm")

    def test_separation_fails_when_too_close(self) -> None:
        status, _ = self.findings(self.measure(9.0, 10.0))["apart"]
        self.assertEqual(status, "fail")


class BoardEdgeTests(CheckFixture):
    def measure(self, edge: str, x: float, y: float, limit: float = 2.0):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("J1", x, y),
                f"""  - id: at-edge
    type: board-edge
    subjects: [J1]
    edge: {edge}
    max_mm: {limit}
    rationale: Cable access.""",
                "J1",
            )
            return self.findings(check_placement(project))["at-edge"]

    def test_west_edge(self) -> None:
        # Outline spans x 100..150; a courtyard from 101 to 103 is 1 mm in.
        self.assertEqual(self.measure("west", 102, 120), ("pass", "1.00 mm"))

    def test_east_edge(self) -> None:
        self.assertEqual(self.measure("east", 148, 120), ("pass", "1.00 mm"))

    def test_north_edge(self) -> None:
        self.assertEqual(self.measure("north", 120, 102), ("pass", "1.00 mm"))

    def test_south_edge(self) -> None:
        self.assertEqual(self.measure("south", 120, 138), ("pass", "1.00 mm"))

    def test_any_edge_takes_the_nearest(self) -> None:
        self.assertEqual(self.measure("any", 102, 120), ("pass", "1.00 mm"))

    def test_named_edge_fails_when_the_part_hugs_another_edge(self) -> None:
        status, _ = self.measure("east", 102, 120)
        self.assertEqual(status, "fail")

    def test_unmeasured_without_an_outline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("J1", 102, 120),
                """  - id: at-edge
    type: board-edge
    subjects: [J1]
    edge: west
    max_mm: 2
    rationale: Cable access.""",
                "J1",
                outline="",
            )
            result = check_placement(project)
        status, _ = self.findings(result)["at-edge"]
        self.assertEqual(status, "unmeasured")


class KeepoutTests(CheckFixture):
    def test_keepout_measures_the_nearest_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("H1", 110, 120) + footprint("U1", 116, 120),
                """  - id: clear
    type: keepout
    subjects: [H1]
    keepout: components
    min_mm: 3
    rationale: Fastener clearance.""",
                "H1, U1",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["clear"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "4.00 mm")
        self.assertIn("U1", dict(
            (item.identifier, item.detail) for item in result.findings
        )["clear"])

    def test_keepout_measures_vias_too(self) -> None:
        via = '  (via (at 112 120) (size 0.8) (drill 0.4) (net 1))\n'
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("H1", 110, 120) + footprint("U1", 116, 120) + via,
                """  - id: clear
    type: keepout
    subjects: [H1]
    keepout: components and vias
    min_mm: 3
    rationale: Fastener clearance.""",
                "H1, U1",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["clear"]
        self.assertEqual(status, "fail")
        # The via is 0.8 mm of copper at x=112, so its edge sits at 111.6 and
        # H1's courtyard ends at 111. Measuring to the centre would say 1.00.
        self.assertEqual(measured, "0.60 mm")


class ManualTests(CheckFixture):
    def test_orientation_is_manual_and_reports_the_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("J1", 110, 120, rotation=90, side="B.Cu"),
                """  - id: faces-out
    type: orientation
    subjects: [J1]
    direction: opening faces west
    rationale: Cable access.""",
                "J1",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["faces-out"]
        self.assertEqual(status, "manual")
        self.assertIn("rotation 90", measured)
        self.assertIn("back", measured)


class UnmeasurableTests(CheckFixture):
    def test_endpoint_missing_from_the_board_is_unmeasured_not_an_error(self) -> None:
        """The contract parser proves endpoints exist, so a miss means the board moved.

        Reported rather than raised: an exception here would escape into
        ``run_status_checks`` and take the whole dashboard down.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 120, 120),
                """  - id: close
    type: proximity
    subjects: [U1.1, C1.1]
    max_mm: 5
    rationale: Keep it short.""",
                "U1, C1",
            )
            frozen = _contract_ignoring_the_board(project)
            # Drop C1 only from the board, after the contract validated against it.
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    footprint("C1", 120, 120), ""
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "pcbforge.placement_check.read_placement_contract",
                return_value=frozen,
            ):
                result = check_placement(project)
        status, _ = self.findings(result)["close"]
        self.assertEqual(status, "unmeasured")
        detail = {item.identifier: item.detail for item in result.findings}
        self.assertIn("C1 is not on the board", detail["close"])

    def test_missing_pad_is_unmeasured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 120, 120),
                """  - id: close
    type: proximity
    subjects: [U1.1, C1.1]
    max_mm: 5
    rationale: Keep it short.""",
                "U1, C1",
            )
            frozen = _contract_ignoring_the_board(project)
            board = project / "garden-logger.kicad_pcb"
            board.write_text(
                board.read_text(encoding="utf-8").replace(
                    '(pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (net 1 "GND"))',
                    "",
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "pcbforge.placement_check.read_placement_contract",
                return_value=frozen,
            ):
                result = check_placement(project)
        status, _ = self.findings(result)["close"]
        self.assertEqual(status, "unmeasured")


class OrderTests(CheckFixture):
    """PA3 `order`: subject centres projected onto one axis, strictly monotonic."""

    def measure(self, direction: str, positions: list[tuple[float, float]]):
        references = ["U1", "C1", "R1"][: len(positions)]
        body = "".join(
            footprint(reference, x, y)
            for reference, (x, y) in zip(references, positions)
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                body,
                f"""  - id: flow
    type: order
    subjects: [{", ".join(references)}]
    direction: {direction}
    rationale: Signal flows one way.""",
                ", ".join(references),
            )
            return check_placement(project)

    def test_west_to_east_passes_when_x_increases(self) -> None:
        result = self.measure("west-to-east", [(110, 120), (120, 125), (130, 118)])
        status, measured = self.findings(result)["flow"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "U1 110.00, C1 120.00, R1 130.00")

    def test_west_to_east_fails_and_names_the_offending_pair(self) -> None:
        result = self.measure("west-to-east", [(110, 120), (130, 125), (120, 118)])
        finding = next(item for item in result.findings if item.identifier == "flow")
        self.assertEqual(finding.status, "fail")
        self.assertEqual(finding.detail, "C1 is not before R1")

    def test_north_to_south_increases_because_y_grows_downward(self) -> None:
        status, _ = self.findings(
            self.measure("north-to-south", [(110, 110), (120, 120), (130, 130)])
        )["flow"]
        self.assertEqual(status, "pass")

    def test_south_to_north_is_the_reverse(self) -> None:
        status, _ = self.findings(
            self.measure("south-to-north", [(110, 130), (120, 120), (130, 110)])
        )["flow"]
        self.assertEqual(status, "pass")

    def test_east_to_west_fails_when_x_increases(self) -> None:
        status, _ = self.findings(
            self.measure("east-to-west", [(110, 120), (120, 120)])
        )["flow"]
        self.assertEqual(status, "fail")

    def test_equal_coordinates_are_not_an_order(self) -> None:
        status, _ = self.findings(
            self.measure("west-to-east", [(115, 120), (115, 125)])
        )["flow"]
        self.assertEqual(status, "fail")


class LoopTests(CheckFixture):
    """PA3 `loop`: the closed perimeter through the listed pads, in order."""

    def measure(self, limit: float, gap: float = 10.0):
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 110 + gap, 120),
                f"""  - id: return-path
    type: loop
    subjects: [U1.1, U1.2, C1.1]
    max_mm: {limit}
    rationale: Keep the return path tight.""",
                "U1, C1",
            )
            return check_placement(project)

    def test_measures_the_closed_perimeter(self) -> None:
        # U1.1 at (109.5, 120), U1.2 at (110.5, 120), C1.1 at (119.5, 120):
        # 1 + 9 + 10 = 20, all three collinear.
        status, measured = self.findings(self.measure(25.0))["return-path"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "20.00 mm")

    def test_fails_beyond_the_limit(self) -> None:
        status, measured = self.findings(self.measure(15.0))["return-path"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "20.00 mm")


class OverlapTests(CheckFixture):
    def board_with(self, *footprints: str) -> str:
        return "".join(footprints)

    def test_overlapping_courtyards_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("U2", 111, 120),
                "",
                "U1, U2",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["U1/U2"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "1.00 x 2.00 mm")

    def test_opposite_sides_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120)
                + footprint("U2", 111, 120, side="B.Cu"),
                "",
                "U1, U2",
            )
            result = check_placement(project)
        self.assertNotIn("U1/U2", self.findings(result))

    def test_mounting_holes_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120)
                + footprint(
                    "H1",
                    111,
                    120,
                    name="MountingHole:MountingHole_2.2mm_M2",
                ),
                "",
                "U1, H1",
            )
            result = check_placement(project)
        self.assertNotIn("U1/H1", self.findings(result))
        self.assertNotIn("H1/U1", self.findings(result))

    def test_touching_courtyards_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("U2", 112, 120),
                "",
                "U1, U2",
            )
            result = check_placement(project)
        self.assertNotIn("U1/U2", self.findings(result))


class OutlineTests(CheckFixture):
    def test_aggregates_footprints_outside_the_outline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120)
                + footprint("C1", 200, 120)
                + footprint("C2", 180, 120),
                "",
                "U1, C1, C2",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["footprints-inside-outline"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "2 of 3 outside")
        detail = {item.identifier: item.detail for item in result.findings}
        self.assertIn("C1 51.0 mm", detail["footprints-inside-outline"])
        self.assertIn("C2 31.0 mm", detail["footprints-inside-outline"])

    def test_passes_when_everything_is_inside(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120),
                "",
                "U1",
            )
            result = check_placement(project)
        status, measured = self.findings(result)["footprints-inside-outline"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "0 of 1 outside")

    def test_outline_matching_spec_board_mm_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            result = check_placement(project)
        status, measured = self.findings(result)["outline-within-spec"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "50 x 40 mm")

    def test_oversize_outline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120),
                "",
                "U1",
                outline='  (gr_rect (start 100 100) (end 160 145) (layer "Edge.Cuts"))\n',
            )
            result = check_placement(project)
        status, measured = self.findings(result)["outline-within-spec"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "60 x 45 mm")

    def test_both_outline_findings_unmeasured_without_an_outline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary), footprint("U1", 110, 120), "", "U1", outline=""
            )
            result = check_placement(project)
        found = self.findings(result)
        self.assertEqual(found["footprints-inside-outline"][0], "unmeasured")
        self.assertEqual(found["outline-within-spec"][0], "unmeasured")


class GatingTests(CheckFixture):
    def test_runs_without_a_current_circuit_acceptance(self) -> None:
        """The user may run this at any point in LAYOUT, including after a reopen."""
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120),
                "",
                "U1",
                current_step6=False,
            )
            self.assertFalse((project / "docs").exists())
            result = check_placement(project, write_report=True)
            wrote = (project / REPORT_FILENAME).is_file()
        self.assertTrue(wrote)
        self.assertTrue(result.wrote_report)

    def test_missing_placement_yaml_is_an_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            (project / "placement.yaml").unlink()
            with self.assertRaises(PlacementCheckInputError) as caught:
                check_placement(project)
        self.assertIn("missing placement.yaml", str(caught.exception))

    def test_missing_project_directory_is_an_input_error(self) -> None:
        with self.assertRaises(PlacementCheckInputError):
            check_placement(Path("/nonexistent/pcbforge-project"))


class ReportTests(CheckFixture):
    def test_report_is_byte_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 118, 120),
                """  - id: close
    type: proximity
    subjects: [U1.2, C1.1]
    max_mm: 2
    rationale: Keep it short.""",
                "U1, C1",
            )
            first = check_placement(project, write_report=True)
            second = check_placement(project, write_report=True)
        self.assertEqual(first.report, second.report)
        self.assertTrue(first.wrote_report)
        self.assertFalse(second.wrote_report)

    def test_report_carries_a_metadata_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120),
                "",
                "U1",
            )
            result = check_placement(project, write_report=True)
            text = (project / REPORT_FILENAME).read_text(encoding="utf-8")
        data = yaml.safe_load(metadata_yaml(text))
        self.assertEqual(
            data["pcbforge_placement_check_schema"], PLACEMENT_CHECK_SCHEMA
        )
        self.assertEqual(data["board_sha256"], result.board_sha256)
        self.assertEqual(data["result"], "pass" if not result.failures else "fail")
        self.assertEqual(sum(data["counts"].values()), len(result.findings))

    def test_report_escapes_pipes_in_user_prose(self) -> None:
        """A direction is user text and lands in a table cell."""
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("J1", 110, 120),
                """  - id: faces-out
    type: orientation
    subjects: [J1]
    direction: opening faces west | not east
    rationale: Cable access.""",
                "J1",
            )
            result = check_placement(project)
        self.assertIn("west \\| not east", result.report)

    def test_writing_the_report_does_not_stale_the_layout_handoff(self) -> None:
        """`brief_inputs` names its files; nothing globs docs/."""
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            before = brief_inputs(project)
            check_placement(project, write_report=True)
            after = brief_inputs(project)
        self.assertEqual(before, after)
        self.assertNotIn(
            REPORT_FILENAME.name, [path.name for path in after]
        )

    def test_the_board_is_never_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()
            check_placement(project, write_report=True)
            after = board.read_bytes()
        self.assertEqual(before, after)


class SummaryTests(CheckFixture):
    def test_summary_counts_every_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("C1", 130, 120),
                """  - id: close
    type: proximity
    subjects: [U1.2, C1.1]
    max_mm: 2
    rationale: Keep it short.
  - id: faces-out
    type: orientation
    subjects: [U1]
    direction: opening faces west
    rationale: Cable access.""",
                "U1, C1",
            )
            result = check_placement(project)
        self.assertEqual(result.count("fail"), 1)
        self.assertEqual(result.count("manual"), 1)
        self.assertEqual(result.count("pass"), 3)
        self.assertIn("1 fail", result.summary)
        self.assertTrue(result.summary.startswith("3 pass"))


class CliTests(CheckFixture):
    def test_exit_zero_when_everything_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            with mock.patch("builtins.print"):
                code = main(["check-placement", str(project)])
        self.assertEqual(code, 0)

    def test_exit_one_when_a_finding_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("U1", 110, 120) + footprint("U2", 111, 120),
                "",
                "U1, U2",
            )
            with mock.patch("builtins.print") as output:
                code = main(["check-placement", str(project)])
            rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", rendered)
        self.assertIn("U1/U2", rendered)

    def test_exit_two_on_an_input_error(self) -> None:
        with (
            mock.patch(
                "pcbforge.cli.check_placement",
                side_effect=PlacementCheckInputError("bad contract"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-placement", "/tmp/project"]), 2)

    def test_exit_one_on_a_runtime_error(self) -> None:
        with (
            mock.patch(
                "pcbforge.cli.check_placement",
                side_effect=PlacementCheckError("cannot write"),
            ),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(main(["check-placement", "/tmp/project"]), 1)

    def test_manual_findings_only_print_with_verbose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary),
                footprint("J1", 110, 120),
                """  - id: faces-out
    type: orientation
    subjects: [J1]
    direction: opening faces west
    rationale: Cable access.""",
                "J1",
            )
            with mock.patch("builtins.print") as quiet:
                main(["check-placement", str(project)])
            quiet_text = "\n".join(str(c.args[0]) for c in quiet.call_args_list)
            with mock.patch("builtins.print") as loud:
                main(["check-placement", str(project), "--verbose"])
            loud_text = "\n".join(str(c.args[0]) for c in loud.call_args_list)
        self.assertNotIn("faces-out", quiet_text)
        self.assertIn("faces-out", loud_text)

    def test_write_report_reports_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), footprint("U1", 110, 120), "", "U1")
            with mock.patch("builtins.print") as output:
                main(["check-placement", str(project), "--write-report"])
            rendered = "\n".join(str(c.args[0]) for c in output.call_args_list)
        self.assertIn("docs/placement-check.md", rendered)
        self.assertIn("PCB unchanged", rendered)


if __name__ == "__main__":
    unittest.main()
