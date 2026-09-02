"""Tests for `pcbforge.board_geometry`.

Provenance for the golden values
--------------------------------

The golden fixture is
`pilots/kicad9-multichannel/baseline/source/multichannel_mixer.kicad_pcb`:
KiCad's own `multichannel` demo, vendored from the KiCad 9.0.9 macOS image and
licensed CC-BY-SA 4.0. See `pilots/kicad9-multichannel/NOTICE.md`. Its sibling
`multichannel_mixer-unrouted.kicad_pcb` carries `(version 20241030)` and serves
as the version-rejection fixture.

The transform this module implements was verified against KiCad itself rather
than reasoned about. `kicad-cli pcb export ipcd356` emits a netlist carrying
every pad's ABSOLUTE position, in units of 0.0001 inch with y pointing up, so a
coordinate converts with `mm = value * 0.00254` and y is negated. Its quantum is
0.00254 mm, which is the noise floor of the result.

    scripts/kicad-cli pcb export ipcd356 -o out.d356 board.kicad_pcb

Result, kicad-cli 9.0.9, 2026-09-02, 114 footprints (81 on the back), 265 pads
compared:

    worst error         0.0017 mm   (front 0.0017, back 0.0017)
    within one quantum  265 of 265

REJECTED HYPOTHESIS. Applying an x mirror to back-side pad offsets -- the
intuitive reading of "the back is mirrored" -- inflates the worst error to
5.4006 mm. KiCad already stores back-side child offsets mirrored, so the same
transform applies to both sides and no mirror belongs in the code. This
paragraph exists so nobody "fixes" the missing mirror.

Regenerate any of the above with:

    uv run --project toolchain python \\
        pilots/kicad9-multichannel/scripts/check_board_geometry.py

Tests here never shell out to `kicad-cli`. `scripts/kicad-cli` hard-codes a
macOS path and exits 2 when absent, so such a test would silently skip
everywhere and protect nothing; the repo gates its only real-CLI tests behind
PCBFORGE_RUN_REAL_INTEGRATION for the same reason. The baked values below are
exact decimals read from the committed board, and so are more precise than the
oracle that validated the method.
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from pcbforge.board_geometry import (
    BOARD_FORMAT_VERSION,
    BoardGeometry,
    BoardGeometryError,
    Box,
    read_board_geometry,
    union,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]
MULTICHANNEL = (
    TOOL_ROOT
    / "pilots"
    / "kicad9-multichannel"
    / "baseline"
    / "source"
    / "multichannel_mixer.kicad_pcb"
)
UNROUTED = MULTICHANNEL.with_name("multichannel_mixer-unrouted.kicad_pcb")

HEADER = """(kicad_pcb
  (version 20241229)
  (generator "pcbnew")
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
    (31 "F.CrtYd" user "F.Courtyard")
    (29 "B.CrtYd" user "B.Courtyard")
    (35 "F.Fab" user)
    (33 "B.Fab" user)
  )
"""


def board(body: str) -> str:
    return HEADER + body + "\n)\n"


def two_pad_footprint(at: str, layer: str = "F.Cu", reference: str = "U1") -> str:
    """A footprint with pads at local (-1, 0) and (+1, 0)."""
    return f"""  (footprint "Test:Part"
    (layer "{layer}")
    (at {at})
    (property "Reference" "{reference}")
    (pad "1" smd rect (at -1 0) (size 0.5 0.5) (net 1 "GND"))
    (pad "2" smd rect (at 1 0) (size 0.5 0.5) (net 2 "VCC"))
  )
"""


class GeometryFixture(unittest.TestCase):
    def geometry(self, body: str) -> BoardGeometry:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board.kicad_pcb"
            path.write_text(board(body), encoding="utf-8")
            return read_board_geometry(path)

    def error(self, body: str) -> str:
        with self.assertRaises(BoardGeometryError) as caught:
            self.geometry(body)
        return str(caught.exception)


class BoxTests(unittest.TestCase):
    def test_width_height_area_and_centre(self) -> None:
        box = Box(1.0, 2.0, 4.0, 6.0)
        self.assertAlmostEqual(box.width, 3.0)
        self.assertAlmostEqual(box.height, 4.0)
        self.assertAlmostEqual(box.area, 12.0)
        self.assertEqual(box.centre, (2.5, 4.0))

    def test_grow_expands_every_side(self) -> None:
        self.assertEqual(Box(0, 0, 2, 2).grow(0.5), Box(-0.5, -0.5, 2.5, 2.5))

    def test_contains_includes_points_on_the_edge(self) -> None:
        box = Box(0, 0, 2, 2)
        self.assertTrue(box.contains((0.0, 0.0)))
        self.assertTrue(box.contains((2.0, 2.0)))
        self.assertTrue(box.contains((1.0, 1.0)))
        self.assertFalse(box.contains((2.1, 1.0)))

    def test_contains_respects_clearance(self) -> None:
        self.assertTrue(Box(0, 0, 2, 2).contains((2.4, 1.0), clearance=0.5))

    def test_contains_box(self) -> None:
        outer = Box(0, 0, 10, 10)
        self.assertTrue(outer.contains_box(Box(1, 1, 9, 9)))
        self.assertTrue(outer.contains_box(Box(0, 0, 10, 10)))
        self.assertFalse(outer.contains_box(Box(-1, 1, 9, 9)))

    def test_overlaps_is_false_for_boxes_that_only_touch(self) -> None:
        self.assertFalse(Box(0, 0, 1, 1).overlaps(Box(1, 0, 2, 1)))
        self.assertTrue(Box(0, 0, 1, 1).overlaps(Box(0.9, 0, 2, 1)))

    def test_overlaps_respects_clearance(self) -> None:
        self.assertTrue(Box(0, 0, 1, 1).overlaps(Box(1.2, 0, 2, 1), clearance=0.5))

    def test_distance_to_is_zero_when_overlapping_or_touching(self) -> None:
        self.assertEqual(Box(0, 0, 2, 2).distance_to(Box(1, 1, 3, 3)), 0.0)
        self.assertEqual(Box(0, 0, 1, 1).distance_to(Box(1, 0, 2, 1)), 0.0)

    def test_distance_to_is_the_axis_gap_when_the_boxes_share_a_span(self) -> None:
        self.assertAlmostEqual(Box(0, 0, 1, 1).distance_to(Box(3, 0, 4, 1)), 2.0)

    def test_distance_to_is_the_corner_diagonal_when_they_do_not(self) -> None:
        # PA2 inherits this meaning for separation and keepout constraints.
        self.assertAlmostEqual(Box(0, 0, 1, 1).distance_to(Box(4, 5, 5, 6)), 5.0)

    def test_union_of_nothing_is_none(self) -> None:
        self.assertIsNone(union([]))

    def test_union_bounds_every_input(self) -> None:
        self.assertEqual(
            union([Box(0, 0, 1, 1), Box(3, -2, 4, 0)]), Box(0, -2, 4, 1)
        )


class TransformTests(GeometryFixture):
    def pads(self, at: str, **kwargs: str) -> dict[str, tuple[float, float]]:
        geometry = self.geometry(two_pad_footprint(at, **kwargs))
        footprint = geometry.footprint(kwargs.get("reference", "U1"))
        return {pad.number: (pad.x, pad.y) for pad in footprint.pads}

    def test_rotation_zero(self) -> None:
        pads = self.pads("100 50 0")
        self.assertAlmostEqual(pads["1"][0], 99.0)
        self.assertAlmostEqual(pads["1"][1], 50.0)
        self.assertAlmostEqual(pads["2"][0], 101.0)

    def test_rotation_ninety(self) -> None:
        pads = self.pads("100 50 90")
        self.assertAlmostEqual(pads["1"][0], 100.0)
        self.assertAlmostEqual(pads["1"][1], 51.0)
        self.assertAlmostEqual(pads["2"][1], 49.0)

    def test_rotation_one_eighty(self) -> None:
        pads = self.pads("100 50 180")
        self.assertAlmostEqual(pads["1"][0], 101.0)
        self.assertAlmostEqual(pads["2"][0], 99.0)

    def test_rotation_two_seventy_matches_negative_ninety(self) -> None:
        self.assertEqual(self.pads("100 50 270"), self.pads("100 50 -90"))

    def test_rotation_forty_five(self) -> None:
        pads = self.pads("100 50 45")
        offset = math.sqrt(2) / 2
        self.assertAlmostEqual(pads["1"][0], 100.0 - offset)
        self.assertAlmostEqual(pads["1"][1], 50.0 + offset)

    def test_rotation_defaults_to_zero_when_absent(self) -> None:
        self.assertEqual(self.pads("100 50"), self.pads("100 50 0"))

    def test_back_side_uses_the_same_transform_without_mirroring(self) -> None:
        """Locked by the oracle: a mirror here costs 5.4 mm. See module docstring."""
        front = self.pads("100 50 90")
        back = self.pads("100 50 90", layer="B.Cu")
        self.assertEqual(front, back)

    def test_side_is_reported(self) -> None:
        self.assertEqual(
            self.geometry(two_pad_footprint("100 50 0")).footprint("U1").side, "front"
        )
        self.assertEqual(
            self.geometry(two_pad_footprint("100 50 0", layer="B.Cu"))
            .footprint("U1")
            .side,
            "back",
        )


class BoundingBoxTests(GeometryFixture):
    def test_courtyard_wins_over_fab_and_pads(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (fp_rect (start -5 -5) (end 5 5) (layer "F.CrtYd"))
    (fp_rect (start -1 -1) (end 1 1) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5))
  )
"""
        )
        footprint = geometry.footprint("U1")
        self.assertEqual(footprint.box_source, "courtyard")
        self.assertEqual(footprint.box, Box(95.0, 95.0, 105.0, 105.0))

    def test_fallback_unions_fab_silk_and_pads_then_grows(self) -> None:
        """A pin-1 dot on F.Fab must not shrink the box below the real body.

        This is the U2 case from kinetic-tile: no courtyard, and the only fab
        graphic is a 0.06 mm marker. A courtyard-then-fab precedence reports a
        5x7 mm package as 0.06 mm.
        """
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (fp_circle (center -2 -2) (end -1.97 -2) (layer "F.Fab"))
    (pad "1" smd rect (at -2 0) (size 1 1))
    (pad "2" smd rect (at 2 0) (size 1 1))
  )
"""
        )
        footprint = geometry.footprint("U1")
        self.assertEqual(footprint.box_source, "fallback")
        # Pads span x -2.5..2.5, the fab dot reaches y -2.03; grown by 0.25.
        self.assertAlmostEqual(footprint.box.min_x, 97.25)
        self.assertAlmostEqual(footprint.box.max_x, 102.75)
        self.assertAlmostEqual(footprint.box.min_y, 97.72)
        self.assertAlmostEqual(footprint.box.max_y, 100.75)

    def test_back_side_reads_b_crtyd_not_f_crtyd(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "B.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (fp_rect (start -9 -9) (end 9 9) (layer "F.CrtYd"))
    (fp_rect (start -2 -2) (end 2 2) (layer "B.CrtYd"))
  )
"""
        )
        self.assertEqual(geometry.footprint("U1").box, Box(98.0, 98.0, 102.0, 102.0))

    def test_rotated_courtyard_bounds_points_not_local_corners(self) -> None:
        """A rotated outline must be bounded from its own points.

        Bounding the local points first and transforming that box's corners
        circumscribes the shape and over-estimates it. The shape here is a
        diamond, whose convex hull is strictly smaller than its axis-aligned
        bounding box, so the two methods disagree: 2*sqrt(2) the right way and
        3*sqrt(2) the wrong way. A rectangle cannot show this, because its own
        corners already are its local bounding box.

        Every footprint in the golden fixture sits at a right angle, where both
        methods agree, so this synthetic case is the only guard.
        """
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 45)
    (property "Reference" "U1")
    (fp_poly (pts (xy 0 -2) (xy 1 0) (xy 0 2) (xy -1 0)) (layer "F.CrtYd"))
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertAlmostEqual(box.width, 2 * math.sqrt(2))
        self.assertAlmostEqual(box.height, 2 * math.sqrt(2))
        self.assertNotAlmostEqual(box.width, 3 * math.sqrt(2))

    def test_rectangle_contributes_all_four_corners(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 90)
    (property "Reference" "U1")
    (fp_rect (start -1 -3) (end 1 3) (layer "F.CrtYd"))
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertAlmostEqual(box.width, 6.0)
        self.assertAlmostEqual(box.height, 2.0)

    def test_arc_and_circle_contribute(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (fp_circle (center 0 0) (end 2 0) (layer "F.CrtYd"))
    (fp_arc (start -3 0) (mid 0 -4) (end 3 0) (layer "F.CrtYd"))
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertEqual(box, Box(97.0, 96.0, 103.0, 102.0))

    def test_polygon_contributes_every_point(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (fp_poly (pts (xy -1 -1) (xy 4 -1) (xy 4 2) (xy -1 2)) (layer "F.CrtYd"))
  )
"""
        )
        self.assertEqual(geometry.footprint("U1").box, Box(99.0, 99.0, 104.0, 102.0))

    def test_pad_fallback_swaps_size_for_a_ninety_degree_pad(self) -> None:
        """The pad angle is ALREADY absolute and must not be re-rotated.

        A 1x2 pad stored at angle 90 on a footprint rotated 90 is 2 wide and 1
        tall. Re-applying the footprint rotation would report 1x2.
        """
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 90)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0 90) (size 1 2))
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertAlmostEqual(box.width, 2.0 + 0.5)
        self.assertAlmostEqual(box.height, 1.0 + 0.5)

    def test_pad_fallback_squares_a_non_orthogonal_pad(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0 45) (size 1 2))
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertAlmostEqual(box.width, 2.5)
        self.assertAlmostEqual(box.height, 2.5)

    def test_custom_pad_uses_its_primitives(self) -> None:
        """(size ...) describes only the anchor of a custom pad."""
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (pad "1" smd custom (at 0 0) (size 1 0.5)
      (primitives
        (gr_poly (pts (xy 0.5 1.5) (xy 0 1.5) (xy 0 -1.5) (xy 0.5 -1.5)) (width 0))
      )
    )
  )
"""
        )
        box = geometry.footprint("U1").box
        self.assertAlmostEqual(box.height, 3.0 + 0.5)

    def test_footprint_without_pads_or_graphics_gets_a_zero_area_box(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Logo"
    (layer "F.Cu")
    (at 120 130 0)
    (property "Reference" "G1")
  )
"""
        )
        footprint = geometry.footprint("G1")
        self.assertEqual(footprint.box_source, "none")
        self.assertEqual(footprint.box, Box(120.0, 130.0, 120.0, 130.0))


class FootprintTests(GeometryFixture):
    def test_properties_are_captured(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "U1")
    (property "Partnumber" "DRV8316CTRGFR" (hide yes))
    (property "Datasheet" "")
  )
"""
        )
        properties = geometry.footprint("U1").properties
        self.assertEqual(properties["Partnumber"], "DRV8316CTRGFR")
        self.assertEqual(properties["Datasheet"], "")

    def test_through_hole_flag(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "J1")
    (pad "1" smd rect (at 0 0) (size 1 1) (net 1 "GND"))
    (pad "2" thru_hole circle (at 1 0) (size 1 1) (drill 0.5) (net 1 "GND"))
    (pad "" np_thru_hole circle (at 2 0) (size 2.2 2.2) (drill 2.2))
  )
"""
        )
        footprint = geometry.footprint("J1")
        self.assertFalse(footprint.pad("1").through_hole)
        self.assertTrue(footprint.pad("2").through_hole)
        self.assertTrue(footprint.pad("").through_hole)

    def test_pad_net_defaults_to_empty(self) -> None:
        geometry = self.geometry(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
    (property "Reference" "H1")
    (pad "" np_thru_hole circle (at 0 0) (size 2 2) (drill 2))
  )
"""
        )
        self.assertEqual(geometry.footprint("H1").pad("").net, "")

    def test_footprints_are_sorted_by_reference(self) -> None:
        geometry = self.geometry(
            two_pad_footprint("100 100 0", reference="U9")
            + two_pad_footprint("110 100 0", reference="C1")
        )
        self.assertEqual([item.reference for item in geometry.footprints], ["C1", "U9"])

    def test_lookup_raises_key_error(self) -> None:
        geometry = self.geometry(two_pad_footprint("100 100 0"))
        with self.assertRaises(KeyError):
            geometry.footprint("NOPE")
        with self.assertRaises(KeyError):
            geometry.pad("U1", "99")

    def test_duplicate_reference_is_rejected(self) -> None:
        message = self.error(
            two_pad_footprint("100 100 0") + two_pad_footprint("110 100 0")
        )
        self.assertIn("duplicate footprint references: U1", message)

    def test_unsupported_layer_is_rejected(self) -> None:
        message = self.error(two_pad_footprint("100 100 0", layer="F.SilkS"))
        self.assertIn("unsupported layer 'F.SilkS'", message)

    def test_missing_reference_is_rejected(self) -> None:
        message = self.error(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (at 100 100 0)
  )
"""
        )
        self.assertIn("has no Reference property", message)

    def test_missing_at_is_rejected(self) -> None:
        message = self.error(
            """  (footprint "Test:Part"
    (layer "F.Cu")
    (property "Reference" "U1")
  )
"""
        )
        self.assertIn("footprint U1 has no (at ...)", message)


class OutlineViaZoneTests(GeometryFixture):
    def test_outline_from_four_gr_lines(self) -> None:
        geometry = self.geometry(
            """  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts"))
  (gr_line (start 160 100) (end 160 160) (layer "Edge.Cuts"))
  (gr_line (start 160 160) (end 100 160) (layer "Edge.Cuts"))
  (gr_line (start 100 160) (end 100 100) (layer "Edge.Cuts"))
"""
        )
        self.assertEqual(geometry.outline, Box(100.0, 100.0, 160.0, 160.0))
        self.assertEqual(geometry.outline_segments, 4)

    def test_outline_from_one_gr_rect(self) -> None:
        geometry = self.geometry(
            '  (gr_rect (start 10 20) (end 40 60) (layer "Edge.Cuts"))\n'
        )
        self.assertEqual(geometry.outline, Box(10.0, 20.0, 40.0, 60.0))
        self.assertEqual(geometry.outline_segments, 1)

    def test_outline_ignores_other_layers(self) -> None:
        geometry = self.geometry(
            '  (gr_line (start 0 0) (end 500 500) (layer "F.SilkS"))\n'
        )
        self.assertIsNone(geometry.outline)
        self.assertEqual(geometry.outline_segments, 0)

    def test_vias_resolve_net_names_from_the_top_level_table(self) -> None:
        """A via carries only `(net N)`; the name lives in the board's table."""
        geometry = self.geometry(
            """  (net 0 "")
  (net 1 "GND")
  (via (at 120 130) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))
"""
        )
        via = geometry.vias[0]
        self.assertEqual(via.net, "GND")
        self.assertEqual((via.x, via.y), (120.0, 130.0))
        self.assertEqual((via.diameter, via.drill), (0.8, 0.4))

    def test_via_with_an_unknown_net_has_an_empty_name(self) -> None:
        geometry = self.geometry(
            '  (via (at 1 2) (size 0.8) (drill 0.4) (net 7))\n'
        )
        self.assertEqual(geometry.vias[0].net, "")

    def test_zone_with_a_singular_layer(self) -> None:
        geometry = self.geometry(
            """  (zone (net 3) (net_name "vbias") (layer "F.Cu")
    (polygon (pts (xy 10 10) (xy 20 10) (xy 20 30)))
  )
"""
        )
        zone = geometry.zones[0]
        self.assertEqual(zone.layer, "F.Cu")
        self.assertEqual(zone.net, "vbias")
        self.assertEqual(zone.box, Box(10.0, 10.0, 20.0, 30.0))

    def test_zone_with_plural_layers_takes_the_first(self) -> None:
        geometry = self.geometry(
            """  (zone (net 0) (net_name "") (layers "F.Cu" "B.Cu")
    (polygon (pts (xy 0 0) (xy 5 5)))
  )
"""
        )
        self.assertEqual(geometry.zones[0].layer, "F.Cu")

    def test_zone_box_comes_from_polygon_not_filled_polygon(self) -> None:
        geometry = self.geometry(
            """  (zone (net 0) (net_name "") (layer "F.Cu")
    (polygon (pts (xy 0 0) (xy 5 5)))
    (filled_polygon (layer "F.Cu") (pts (xy 900 900) (xy 999 999)))
  )
"""
        )
        self.assertEqual(geometry.zones[0].box, Box(0.0, 0.0, 5.0, 5.0))

    def test_zone_without_a_polygon_is_skipped(self) -> None:
        geometry = self.geometry(
            '  (zone (net 0) (net_name "") (layer "F.Cu") (hatch none 0.5))\n'
        )
        self.assertEqual(geometry.zones, ())


class RejectionTests(unittest.TestCase):
    def read(self, text: str | None) -> str:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board.kicad_pcb"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            with self.assertRaises(BoardGeometryError) as caught:
                read_board_geometry(path)
        return str(caught.exception)

    def test_missing_file_is_rejected(self) -> None:
        self.assertIn("missing board.kicad_pcb", self.read(None))

    def test_unparsable_text_is_rejected(self) -> None:
        self.assertIn("not a KiCad board", self.read("(kicad_pcb"))

    def test_empty_file_is_rejected(self) -> None:
        self.assertIn("not a KiCad board", self.read(""))

    def test_non_board_root_is_rejected(self) -> None:
        self.assertIn("not a KiCad board", self.read('(kicad_sch (version 20241229))'))

    def test_old_board_version_is_rejected(self) -> None:
        message = self.read("(kicad_pcb (version 20241030))")
        self.assertIn("unsupported board version '20241030'", message)
        self.assertIn(BOARD_FORMAT_VERSION, message)

    def test_absent_version_is_rejected(self) -> None:
        self.assertIn("unsupported board version ''", self.read("(kicad_pcb)"))


class LayerCountTests(GeometryFixture):
    def test_counts_only_copper_layers(self) -> None:
        self.assertEqual(self.geometry("").layer_count, 2)

    def test_counts_inner_copper_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "board.kicad_pcb"
            path.write_text(
                """(kicad_pcb
  (version 20241229)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" mixed)
    (3 "B.Cu" signal)
    (25 "Edge.Cuts" user)
    (35 "F.Fab" user)
  )
)
""",
                encoding="utf-8",
            )
            self.assertEqual(read_board_geometry(path).layer_count, 4)


class MultichannelBoardTests(unittest.TestCase):
    """Golden tests against the committed KiCad demo. See module docstring."""

    geometry: BoardGeometry

    @classmethod
    def setUpClass(cls) -> None:
        cls.geometry = read_board_geometry(MULTICHANNEL)

    def test_board_level_counts(self) -> None:
        self.assertEqual(self.geometry.version, 20241229)
        self.assertEqual(self.geometry.layer_count, 2)
        self.assertEqual(len(self.geometry.footprints), 114)
        self.assertEqual(len(self.geometry.vias), 29)
        self.assertEqual(len(self.geometry.zones), 6)
        self.assertEqual(self.geometry.outline_segments, 4)
        self.assertEqual(self.geometry.outline, Box(89.46, 43.66, 199.46, 154.66))

    def test_sides_and_uniqueness(self) -> None:
        sides = [item.side for item in self.geometry.footprints]
        self.assertEqual(sides.count("back"), 81)
        self.assertEqual(sides.count("front"), 33)
        references = [item.reference for item in self.geometry.footprints]
        self.assertEqual(len(set(references)), 114)
        self.assertEqual(references, sorted(references))

    def test_box_sources(self) -> None:
        fallback = sorted(
            item.reference
            for item in self.geometry.footprints
            if item.box_source == "fallback"
        )
        self.assertEqual(
            fallback,
            [
                "C1", "C13", "C14", "C2", "C22", "C23",
                "C31", "C32", "C4", "C40", "C5", "J5",
            ],
        )
        self.assertEqual(
            sum(
                1
                for item in self.geometry.footprints
                if item.box_source == "courtyard"
            ),
            102,
        )

    def test_back_side_rotated_footprint_pads_are_absolute(self) -> None:
        footprint = self.geometry.footprint("C19")
        self.assertEqual(footprint.side, "back")
        self.assertEqual((footprint.x, footprint.y, footprint.rotation), (123.0, 129.0, 90.0))
        self.assertEqual(footprint.box_source, "courtyard")
        self.assertEqual(footprint.box, Box(121.85, 126.65, 124.15, 131.35))
        first = footprint.pad("1")
        self.assertAlmostEqual(first.x, 123.0)
        self.assertAlmostEqual(first.y, 130.5)
        self.assertEqual(first.net, "Net-(C19-Pad1)")
        second = footprint.pad("2")
        self.assertAlmostEqual(second.x, 123.0)
        self.assertAlmostEqual(second.y, 127.5)

    def test_front_side_rotated_footprint_pads_are_absolute(self) -> None:
        footprint = self.geometry.footprint("J5")
        self.assertEqual(footprint.side, "front")
        self.assertEqual(footprint.box_source, "fallback")
        first = footprint.pad("1")
        self.assertAlmostEqual(first.x, 185.085)
        self.assertAlmostEqual(first.y, 87.385)
        self.assertTrue(first.through_hole)
        self.assertEqual((first.size_x, first.size_y), (4.3, 1.7))
        self.assertAlmostEqual(footprint.pad("2").x, 188.185)
        self.assertAlmostEqual(footprint.pad("2").y, 82.385)

    def test_zone_layer_forms_and_nets(self) -> None:
        zones = self.geometry.zones
        self.assertEqual(zones[0].layer, "F.Cu")
        self.assertEqual(zones[0].net, "vbias")
        self.assertEqual(zones[1].net, "GND")
        self.assertEqual({zone.layer for zone in zones}, {"F.Cu"})

    def test_via_nets_resolve_through_the_board_table(self) -> None:
        self.assertEqual(
            sorted({via.net for via in self.geometry.vias}), ["+12V", "GND", "vbias"]
        )

    def test_every_pad_centre_lies_inside_its_footprint_box(self) -> None:
        outside = [
            (item.reference, pad.number)
            for item in self.geometry.footprints
            for pad in item.pads
            if not item.box.contains((pad.x, pad.y))
        ]
        self.assertEqual(outside, [])

    def test_every_footprint_box_lies_inside_the_board_outline(self) -> None:
        assert self.geometry.outline is not None
        outside = [
            item.reference
            for item in self.geometry.footprints
            if not self.geometry.outline.contains_box(item.box)
        ]
        self.assertEqual(outside, [])

    def test_unrouted_sibling_board_is_rejected_for_its_version(self) -> None:
        with self.assertRaises(BoardGeometryError) as caught:
            read_board_geometry(UNROUTED)
        self.assertIn("unsupported board version '20241030'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
