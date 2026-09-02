"""Tests for `pcbforge apply-floorplan`.

Mirrors `tests/test_apply_pattern.py`, including its reason for mocking the
LAYOUT authorization gate, and adds the spill report.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.apply_floorplan import (
    GAP_MM,
    ApplyFloorplanError,
    ApplyFloorplanInputError,
    apply_floorplan,
)
from pcbforge.board_edit import BACKUP_DIRNAME
from pcbforge.board_geometry import read_board_geometry
from pcbforge.build_test import board_topology_bytes, read_board_evidence
from pcbforge.cli import main
from pcbforge.placement_check import check_placement
from tests.test_placement_check import CheckFixture, footprint

#: The fixture outline runs 100..150 x 100..140, so board-relative (5, 5) is
#: absolute (105, 105).
PLAN = """floorplan:
  variant: A
  seed: 1
  board_mm: [50, 40]
  groups:
    - id: everything
      rect_mm: [5, 5, 20, 20]
"""

TIGHT_PLAN = PLAN.replace("rect_mm: [5, 5, 20, 20]", "rect_mm: [5, 5, 3, 3]")

# Three parts scattered across the board, none of them in the rectangle.
SCATTERED = (
    footprint("U1", 145, 135, half=2.0)
    + footprint("C1", 140, 105)
    + footprint("R1", 148, 120)
)


class FloorplanFixture(CheckFixture):
    def scaffold(self, root: Path, *, board: str = SCATTERED, plan: str = PLAN) -> Path:
        project = self.build(root, board, "", "U1, C1, R1")
        placement = project / "placement.yaml"
        placement.write_text(
            placement.read_text(encoding="utf-8") + plan, encoding="utf-8"
        )
        return project

    def apply(self, project: Path, *, groups=("everything",), dry_run: bool = False):
        with mock.patch(
            "pcbforge.apply_floorplan.layout_assist_is_authorized",
            return_value=True,
        ):
            return apply_floorplan(project, groups, dry_run=dry_run)


class DryRunTests(FloorplanFixture):
    def test_plans_every_footprint_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            result = self.apply(project, dry_run=True)

            self.assertEqual(board.read_bytes(), before)
            self.assertFalse((project / BACKUP_DIRNAME).exists())

        self.assertFalse(result.applied)
        self.assertEqual(
            sorted(move.reference for move in result.moves), ["C1", "R1", "U1"]
        )
        self.assertEqual(result.spilled, ())
        self.assertEqual(
            result.summary, "would place 3 footprints in 1 group(s)"
        )

    def test_the_largest_footprint_takes_the_centre(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self.apply(
                self.scaffold(Path(temporary)), dry_run=True
            )

        largest = next(move for move in result.moves if move.reference == "U1")
        # The rectangle spans 105..125 both ways, so its centre is (115, 115).
        self.assertAlmostEqual(largest.after[0], 115.0)
        self.assertAlmostEqual(largest.after[1], 115.0)


class ApplyTests(FloorplanFixture):
    def test_places_the_group_and_preserves_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            topology = board_topology_bytes(read_board_evidence(board))

            result = self.apply(project)
            geometry = read_board_geometry(board)

            self.assertTrue(result.applied)
            self.assertEqual(
                board_topology_bytes(read_board_evidence(board)), topology
            )
            self.assertEqual(result.backup.parent.name, BACKUP_DIRNAME)
            for reference in ("U1", "C1", "R1"):
                box = geometry.footprint(reference).box
                self.assertGreaterEqual(box.min_x, 105.0 - 1e-6, reference)
                self.assertLessEqual(box.max_x, 125.0 + 1e-6, reference)
                self.assertGreaterEqual(box.min_y, 105.0 - 1e-6, reference)
                self.assertLessEqual(box.max_y, 125.0 + 1e-6, reference)

    def test_packed_footprints_keep_their_distance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            self.apply(project)
            geometry = read_board_geometry(project / "garden-logger.kicad_pcb")

        boxes = [geometry.footprint(item).box for item in ("U1", "C1", "R1")]
        for first in range(len(boxes)):
            for second in range(first + 1, len(boxes)):
                self.assertFalse(
                    boxes[first].overlaps(boxes[second], clearance=GAP_MM)
                )

    def test_rotation_and_side_survive_the_move(self) -> None:
        board = (
            footprint("U1", 145, 135, half=2.0, rotation=90)
            + footprint("C1", 140, 105, side="B.Cu")
            + footprint("R1", 148, 120)
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary), board=board)
            self.apply(project)
            geometry = read_board_geometry(project / "garden-logger.kicad_pcb")

        self.assertAlmostEqual(geometry.footprint("U1").rotation, 90.0)
        self.assertEqual(geometry.footprint("C1").side, "back")

    def test_the_floorplan_check_then_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            before = self.findings(check_placement(project))["everything"]
            self.apply(project)
            after = self.findings(check_placement(project))["everything"]

        self.assertEqual(before[0], "fail")
        self.assertEqual(after, ("pass", "3 of 3 centres inside, centroid 0.00 mm outside"))


class SpillTests(FloorplanFixture):
    def test_reports_what_did_not_fit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary), plan=TIGHT_PLAN)
            result = self.apply(project, dry_run=True)

        spilled = {move.reference for move in result.spilled}
        # A 3 x 3 rectangle holds the 4 mm part and nothing else.
        self.assertTrue(spilled)
        self.assertIn("spilled outside", result.summary)
        self.assertTrue(
            any("did not fit" in warning for warning in result.warnings)
        )


class RefusalTests(FloorplanFixture):
    def test_refuses_without_an_authorized_layout_assist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            with self.assertRaisesRegex(
                ApplyFloorplanInputError, "current handoff approval"
            ):
                apply_floorplan(project, ("everything",))

            self.assertEqual(board.read_bytes(), before)

    def test_refuses_a_contract_with_no_floorplan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), SCATTERED, "", "U1, C1, R1")
            with self.assertRaisesRegex(
                ApplyFloorplanInputError, "declares no floorplan"
            ):
                self.apply(project)

    def test_refuses_an_unknown_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with self.assertRaisesRegex(
                ApplyFloorplanInputError, "no rectangle for nope"
            ):
                self.apply(project, groups=("nope",))

    def test_refuses_an_empty_group_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with self.assertRaisesRegex(ApplyFloorplanInputError, "at least one group"):
                self.apply(project, groups=())

    def test_refuses_a_board_with_no_outline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(
                Path(temporary), SCATTERED, "", "U1, C1, R1", outline=""
            )
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8") + PLAN, encoding="utf-8"
            )
            with self.assertRaisesRegex(ApplyFloorplanInputError, "no Edge.Cuts"):
                self.apply(project)

    def test_a_failed_verification_leaves_the_board_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            with mock.patch(
                "pcbforge.board_edit._verify", return_value=["forced failure"]
            ):
                with self.assertRaisesRegex(ApplyFloorplanError, "forced failure"):
                    self.apply(project)

            self.assertEqual(board.read_bytes(), before)


class CliTests(FloorplanFixture):
    def test_dry_run_exits_zero_and_prints_the_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with mock.patch(
                "pcbforge.apply_floorplan.layout_assist_is_authorized",
                return_value=True,
            ):
                with mock.patch("sys.stdout") as stdout:
                    code = main(
                        [
                            "apply-floorplan",
                            str(project),
                            "--groups",
                            "everything",
                            "--dry-run",
                        ]
                    )

        printed = "".join(
            str(call.args[0]) for call in stdout.write.call_args_list if call.args
        )
        self.assertEqual(code, 0)
        self.assertIn("would place 3 footprints", printed)

    def test_an_unknown_group_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with mock.patch(
                "pcbforge.apply_floorplan.layout_assist_is_authorized",
                return_value=True,
            ):
                with mock.patch("sys.stderr"):
                    code = main(
                        ["apply-floorplan", str(project), "--groups", "nope"]
                    )

        self.assertEqual(code, 2)
