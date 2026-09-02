"""Tests for `pcbforge apply-pattern`.

Reuses the pattern fixtures from `tests/test_placement_check.py`, which already
build a board carrying a `Partnumber` property and real pad nets.

The LAYOUT authorization gate is mocked in the tests that exercise the edit
itself, as `tests/test_status.py` already mocks `_current_layout_handoff`;
driving a fixture project all the way to an approved handoff would test the
status machinery rather than this command. `RefusalTests` covers the gate itself
against a project that genuinely is not in LAYOUT.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.apply_pattern import (
    ApplyPatternError,
    ApplyPatternInputError,
    apply_pattern,
)
from pcbforge.board_edit import BACKUP_DIRNAME
from pcbforge.board_geometry import read_board_geometry
from pcbforge.build_test import board_topology_bytes, read_board_evidence
from pcbforge.cli import main
from tests.test_placement_check import (
    ANCHOR_PATTERN,
    EXACT_PATTERN,
    PatternFixture,
    anchor,
    capacitor,
    vias,
)

# C3 starts well away from its pattern position at (115.5, 120) rotation 0.
DISPLACED = (
    anchor(120, 120)
    + capacitor("C3", 130, 135, 3, "VM_A", rotation=90)
    + vias(9)
)


class ApplyFixture(PatternFixture):
    def scaffold(self, root: Path, *, board: str = DISPLACED, pattern=EXACT_PATTERN):
        return self.build_pattern(root, board, "U2, C3", pattern=pattern)

    def apply(self, project: Path, *, dry_run: bool = False):
        with mock.patch(
            "pcbforge.apply_pattern.layout_assist_is_authorized",
            return_value=True,
        ):
            return apply_pattern(project, "everything", dry_run=dry_run)


class DryRunTests(ApplyFixture):
    def test_reports_the_move_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()
            result = self.apply(project, dry_run=True)

            self.assertFalse(result.applied)
            self.assertIsNone(result.backup)
            self.assertEqual(board.read_bytes(), before)
            self.assertFalse((project / BACKUP_DIRNAME).exists())

        (move,) = result.moves
        self.assertEqual(move.reference, "C3")
        self.assertEqual(move.role, "vm-bypass-1")
        self.assertEqual(move.before, (130.0, 135.0, 90.0))
        self.assertEqual(move.after, (115.5, 120.0, 0.0))
        self.assertAlmostEqual(move.distance_mm, 20.8626, places=3)
        self.assertEqual(result.summary, "would move 1 footprint to match testdriver")


class ApplyTests(ApplyFixture):
    def test_moves_the_satellite_and_leaves_the_anchor_alone(self) -> None:
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
            satellite = geometry.footprint("C3")
            self.assertAlmostEqual(satellite.x, 115.5)
            self.assertAlmostEqual(satellite.y, 120.0)
            self.assertAlmostEqual(satellite.rotation, 0.0)
            placed_anchor = geometry.footprint("U2")
            self.assertAlmostEqual(placed_anchor.x, 120.0)
            self.assertAlmostEqual(placed_anchor.y, 120.0)

            self.assertIsNotNone(result.backup)
            self.assertEqual(result.backup.parent.name, BACKUP_DIRNAME)

    def test_the_backup_holds_the_board_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            result = self.apply(project)

            self.assertEqual(result.backup.read_bytes(), before)
            self.assertNotEqual(board.read_bytes(), before)

    def test_applying_twice_is_a_no_op_the_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"

            self.apply(project)
            once = board.read_bytes()
            self.apply(project)

            self.assertEqual(board.read_bytes(), once)

    def test_the_check_then_agrees_with_the_pattern(self) -> None:
        from pcbforge.placement_check import check_placement

        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            before = check_placement(project)
            self.apply(project)
            after = check_placement(project)

        self.assertEqual(
            self.findings(before)["everything/vm-bypass-1"][0], "fail"
        )
        self.assertEqual(
            self.findings(after)["everything/vm-bypass-1"],
            ("pass", "0.00 mm off, rotation 0°, front"),
        )


class RefusalTests(ApplyFixture):
    def test_refuses_without_an_authorized_layout_assist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            # No mock here: the fixture project has no approved LAYOUT handoff.
            with self.assertRaisesRegex(
                ApplyPatternInputError, "current handoff approval"
            ):
                apply_pattern(project, "everything")

            self.assertEqual(board.read_bytes(), before)

    def test_refuses_a_sketch_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(
                Path(temporary),
                board=DISPLACED + capacitor("C4", 124.5, 120, 4, "VM_B"),
                pattern=ANCHOR_PATTERN,
            )
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8").replace(
                    "references: [U2, C3]", "references: [U2, C3, C4]"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ApplyPatternInputError, "sketch fidelity"
            ):
                self.apply(project)

    def test_refuses_an_unknown_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with mock.patch(
                "pcbforge.apply_pattern.layout_assist_is_authorized",
                return_value=True,
            ):
                with self.assertRaisesRegex(ApplyPatternInputError, "unknown group"):
                    apply_pattern(project, "nope")

    def test_refuses_an_unplaced_anchor(self) -> None:
        # Five footprints stacked on one point is an unplaced board.
        stacked = (
            anchor(0, 0)
            + capacitor("C3", 0, 0, 3, "VM_A")
            + capacitor("C4", 0, 0, 4, "VM_B")
            + capacitor("C5", 0, 0, 1, "GND")
            + capacitor("C6", 0, 0, 1, "GND")
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary), board=stacked)
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8").replace(
                    "references: [U2, C3]", "references: [U2, C3, C4, C5, C6]"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ApplyPatternInputError, "still at the unplaced position"
            ):
                self.apply(project)

    def test_refuses_a_satellite_on_the_wrong_side(self) -> None:
        board = (
            anchor(120, 120)
            + capacitor("C3", 130, 135, 3, "VM_A", side="B.Cu")
            + vias(9)
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary), board=board)
            with self.assertRaisesRegex(
                ApplyPatternInputError, "flip C3 to the front side in KiCad"
            ):
                self.apply(project)

    def test_a_failed_verification_leaves_the_board_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            board = project / "garden-logger.kicad_pcb"
            before = board.read_bytes()

            with mock.patch(
                "pcbforge.board_edit._verify", return_value=["forced failure"]
            ):
                with self.assertRaisesRegex(ApplyPatternError, "forced failure"):
                    self.apply(project)

            self.assertEqual(board.read_bytes(), before)


class CliTests(ApplyFixture):
    def run_cli(self, project: Path, *args: str) -> int:
        with mock.patch(
            "pcbforge.apply_pattern.layout_assist_is_authorized",
            return_value=True,
        ):
            return main(["apply-pattern", str(project), "--group", "everything", *args])

    def test_dry_run_exits_zero_and_prints_the_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with mock.patch("sys.stdout") as stdout:
                code = self.run_cli(project, "--dry-run")

        printed = "".join(
            str(call.args[0]) for call in stdout.write.call_args_list if call.args
        )
        self.assertEqual(code, 0)
        self.assertIn("would move 1 footprint", printed)
        self.assertIn("C3", printed)

    def test_an_unknown_group_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.scaffold(Path(temporary))
            with mock.patch("sys.stderr"):
                code = main(
                    ["apply-pattern", str(project), "--group", "nope", "--dry-run"]
                )

        self.assertEqual(code, 2)
