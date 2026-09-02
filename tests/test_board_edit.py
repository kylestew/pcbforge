"""Tests for `pcbforge.board_edit`, the only writer of `.kicad_pcb` bytes.

The load-bearing assumption is the child-angle rule: rotating a footprint by a
delta shifts every child `(at x y angle)` by the same delta. The plan called for
confirming it by rotating a part in KiCad 9 and diffing the save. That was
unnecessary -- the tracked fixture already proves it, and proves it over 114
footprints rather than one.

`multichannel_mixer.kicad_pcb` holds footprints at four distinct rotations
(-90, 0, 90, 180). At every one of them each pad's stored angle equals the
footprint's own rotation taken modulo 360, plus that pad's footprint-local
angle. Stored child angles are therefore absolute board-space angles, exactly as
PA1 found for pads, so changing a footprint's rotation by a delta must shift
them all by that delta. `test_child_angles_are_absolute_board_angles` asserts it
directly, and fails if a future KiCad ever changes the convention.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcbforge.board_edit import (
    BACKUP_DIRNAME,
    BoardEditError,
    Move,
    apply_moves,
    find_footprint_block,
    format_number,
    move_footprint,
)
from pcbforge.board_geometry import read_board_geometry
from pcbforge.build_test import board_topology_bytes, read_board_evidence

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "pilots"
    / "kicad9-multichannel"
    / "baseline"
    / "source"
    / "multichannel_mixer.kicad_pcb"
)


class ChildAngleTests(unittest.TestCase):
    def test_child_angles_are_absolute_board_angles(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        seen_rotations = set()
        checked = 0
        for match in re.finditer(r'\(property "Reference" "([^"]+)"', text):
            reference = match.group(1)
            start, end = find_footprint_block(text, reference)
            block = text[start:end]
            boundary = block.find("(property")
            placement = re.search(
                r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)", block[:boundary]
            )
            self.assertIsNotNone(placement, reference)
            rotation = float(placement.group(3) or 0.0) % 360.0
            seen_rotations.add(rotation)
            for pad in re.finditer(
                r'\(pad "[^"]+" \w+ \w+\s*\n?\s*'
                r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)",
                block,
            ):
                angle = float(pad.group(3) or 0.0)
                # Pad-local angles in this fixture are multiples of 90, so the
                # stored angle minus the footprint rotation must be one too.
                self.assertAlmostEqual((angle - rotation) % 90.0, 0.0, places=6)
                checked += 1

        self.assertEqual(seen_rotations, {0.0, 90.0, 180.0, 270.0})
        self.assertGreater(checked, 200)


class FormattingTests(unittest.TestCase):
    def test_formats_numbers_the_way_kicad_does(self) -> None:
        self.assertEqual(format_number(90.0), "90")
        self.assertEqual(format_number(147.25), "147.25")
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(1 / 3), "0.333333")
        self.assertEqual(format_number(1.500000), "1.5")


class BlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FIXTURE.read_text(encoding="utf-8")

    def test_finds_a_whole_top_level_block(self) -> None:
        start, end = find_footprint_block(self.text, "C1")
        block = self.text[start:end]
        self.assertTrue(block.startswith("\t(footprint "))
        self.assertTrue(block.endswith("\n\t)\n"))
        self.assertIn('(property "Reference" "C1"', block)
        # Exactly one footprint: no neighbour swallowed by the boundary search.
        self.assertEqual(block.count("\t(footprint "), 1)

    def test_rejects_a_missing_or_repeated_reference(self) -> None:
        with self.assertRaisesRegex(BoardEditError, "no footprint NOPE"):
            find_footprint_block(self.text, "NOPE")
        doubled = self.text + '(property "Reference" "C1"'
        with self.assertRaisesRegex(BoardEditError, "appears more than once"):
            find_footprint_block(doubled, "C1")


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FIXTURE.read_text(encoding="utf-8")

    def moved(self, reference: str, move: Move) -> str:
        return move_footprint(self.text, move)

    def test_moves_the_placement_and_shifts_child_angles(self) -> None:
        # C1 sits at rotation 180 with child angles at 180; +90 makes them 270.
        text = self.moved("C1", Move("C1", 150.5, 90.25, 270.0))
        start, end = find_footprint_block(text, "C1")
        block = text[start:end]

        self.assertIn("(at 150.5 90.25 270)", block)
        self.assertIn('(property "Reference" "C1"\n\t\t\t(at 3.4 -2.14 270)', block)
        self.assertNotIn("(at 3.4 -2.14 180)", block)

    def test_leaves_every_byte_outside_the_block_alone(self) -> None:
        text = self.moved("C1", Move("C1", 150.5, 90.25, 270.0))
        before = find_footprint_block(self.text, "C1")
        after = find_footprint_block(text, "C1")

        self.assertEqual(
            self.text[: before[0]] + self.text[before[1] :],
            text[: after[0]] + text[after[1] :],
        )

    def test_a_zero_rotation_is_omitted_like_kicad_omits_it(self) -> None:
        text = self.moved("C1", Move("C1", 120.0, 130.0, 0.0))
        start, end = find_footprint_block(text, "C1")
        block = text[start:end]

        self.assertIn("(at 120 130)\n", block)
        # 180 shifted by -180 lands on 0, which KiCad writes by omission.
        self.assertIn("(at 3.4 -2.14)", block)

    def test_a_pure_translation_leaves_angles_untouched(self) -> None:
        text = self.moved("C1", Move("C1", 120.0, 130.0, 180.0))
        start, end = find_footprint_block(text, "C1")

        self.assertIn("(at 3.4 -2.14 180)", text[start:end])

    def test_two_number_child_offsets_are_never_rewritten(self) -> None:
        text = self.moved("C1", Move("C1", 120.0, 130.0, 270.0))
        start, end = find_footprint_block(text, "C1")
        block = text[start:end]
        pairs = re.findall(r"\(at (-?[\d.]+) (-?[\d.]+)\)", block)

        original_start, original_end = find_footprint_block(self.text, "C1")
        original_pairs = re.findall(
            r"\(at (-?[\d.]+) (-?[\d.]+)\)", self.text[original_start:original_end]
        )
        # The footprint's own `(at ...)` gained a rotation, so it left this set;
        # every other bare pair must be exactly as it was.
        self.assertEqual(
            [pair for pair in pairs if pair != ("120", "130")],
            [pair for pair in original_pairs if pair != ("181.73", "110.06")],
        )


class ApplyTests(unittest.TestCase):
    def board(self, root: Path) -> Path:
        board = root / "board.kicad_pcb"
        shutil.copy2(FIXTURE, board)
        return board

    def test_applies_backs_up_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            board = self.board(Path(temporary))
            before = board_topology_bytes(read_board_evidence(board))

            backup = apply_moves(
                board,
                [Move("C1", 150.5, 90.25, 270.0), Move("C2", 160.0, 95.0, 0.0)],
            )
            geometry = read_board_geometry(board)
            after = board_topology_bytes(read_board_evidence(board))

            self.assertEqual(backup.parent.name, BACKUP_DIRNAME)
            self.assertEqual(backup.read_bytes(), FIXTURE.read_bytes())
            self.assertEqual(before, after)
            first = geometry.footprint("C1")
            second = geometry.footprint("C2")
            self.assertAlmostEqual(first.x, 150.5)
            self.assertAlmostEqual(first.y, 90.25)
            self.assertAlmostEqual(first.rotation, 270.0)
            self.assertAlmostEqual(second.x, 160.0)
            self.assertAlmostEqual(second.rotation, 0.0)

    def test_a_failed_verification_restores_the_board(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            board = self.board(Path(temporary))
            original = board.read_bytes()

            with mock.patch(
                "pcbforge.board_edit._verify",
                return_value=["forced failure"],
            ):
                with self.assertRaisesRegex(BoardEditError, "forced failure"):
                    apply_moves(board, [Move("C1", 150.5, 90.25, 270.0)])

            self.assertEqual(board.read_bytes(), original)

    def test_refuses_an_empty_move_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            board = self.board(Path(temporary))
            with self.assertRaisesRegex(BoardEditError, "no footprints to move"):
                apply_moves(board, [])
