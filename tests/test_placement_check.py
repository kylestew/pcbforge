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

from pcbforge.build_test import fingerprint_inputs
from pcbforge.cli import main
from pcbforge.compatibility import EXPECTED_GUIDANCE
from pcbforge.markdown_metadata import metadata_yaml
from pcbforge.placement import (
    BRIEF_FILENAME,
    PlacementInputError,
    brief_inputs,
    generate_brief,
    read_placement_contract,
)
from pcbforge.placement_check import (
    PLACEMENT_CHECK_SCHEMA,
    REPORT_FILENAME,
    PlacementCheckError,
    PlacementCheckInputError,
    check_placement,
    placement_check_inputs,
)
from tests.test_placement import TOOL_ROOT, PlacementFixture

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


ANCHOR_PATTERN = """pattern_schema: 1
id: testdriver
part:
  partnumber_match: "^TEST8316[CR]T"
  footprint_match: "VQFN-40"
fidelity: sketch
source:
  document: "TEST8316 datasheet, Layout Example figure"
  layers: 2
  captured: 2026-09-02
frame: >-
  Offsets are in the anchor footprint's local frame with anchor rotation 0.
roles:
  - id: vm-bypass-1
    anchor_pads: ["9"]
    footprint_match: "^Capacitor_SMD"
    near_side: west
    max_mm: 2.5
    rationale: First high-frequency VM bypass at pin 9.
  - id: vm-bypass-2
    anchor_pads: ["10"]
    footprint_match: "^Capacitor_SMD"
    near_side: east
    max_mm: 2.5
    rationale: Second VM bypass at pin 10.
rules:
  - id: ep-thermal-vias
    type: vias-under-pad
    anchor_pad: "41"
    min_count: 9
    rationale: Exposed-pad heat path to the back copper.
  - id: gnd-pour-back
    type: note
    text: Solid GND pour under the package on the opposite layer.
    rationale: Heat spreading.
"""

EXACT_PATTERN = """pattern_schema: 1
id: testdriver
part:
  partnumber_match: "^TEST8316[CR]T"
  footprint_match: "VQFN-40"
fidelity: exact
source:
  document: "TEST8316EVM design files, revision B"
  layers: 2
  captured: 2026-09-02
frame: >-
  Offsets are in the anchor footprint's local frame with anchor rotation 0.
roles:
  - id: vm-bypass-1
    anchor_pads: ["9"]
    footprint_match: "^Capacitor_SMD"
    offset_mm: [-4.5, 0]
    rotation_deg: 0
    tolerance_mm: 0.5
    rationale: First high-frequency VM bypass at pin 9.
"""


def anchor(x: float, y: float, *, rotation: float = 0.0, partnumber="TEST8316CT") -> str:
    """A VQFN-40 with VM pads at local (-3, 0) and (+3, 0) and an exposed pad."""
    return f"""  (footprint "Package_DFN_QFN:VQFN-40-1EP_6x6mm"
    (layer "F.Cu")
    (at {x} {y} {rotation})
    (property "Reference" "U2")
    (property "Partnumber" "{partnumber}")
    (fp_rect (start -3.2 -3.2) (end 3.2 3.2) (layer "F.CrtYd"))
    (pad "9" smd rect (at -3 0) (size 0.4 0.4) (net 3 "VM_A"))
    (pad "10" smd rect (at 3 0) (size 0.4 0.4) (net 4 "VM_B"))
    (pad "41" smd rect (at 0 0) (size 4 4) (net 1 "GND"))
  )
"""


def capacitor(
    reference: str,
    x: float,
    y: float,
    net_index: int,
    net: str,
    *,
    rotation: float = 0.0,
    side: str = "F.Cu",
) -> str:
    crtyd = "B.CrtYd" if side == "B.Cu" else "F.CrtYd"
    return f"""  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "{side}")
    (at {x} {y} {rotation})
    (property "Reference" "{reference}")
    (property "Partnumber" "CAP")
    (fp_rect (start -0.6 -0.4) (end 0.6 0.4) (layer "{crtyd}"))
    (pad "1" smd rect (at -0.5 0) (size 0.3 0.3) (net {net_index} "{net}"))
    (pad "2" smd rect (at 0.5 0) (size 0.3 0.3) (net 1 "GND"))
  )
"""


def vias(count: int, *, net: int = 1) -> str:
    """`count` vias spread inside the 4 x 4 exposed pad at (120, 120)."""
    offsets = [(-1, -1), (0, -1), (1, -1), (-1, 0), (0, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    return "".join(
        f'  (via (at {120 + dx} {120 + dy}) (size 0.6) (drill 0.3)'
        f' (layers "F.Cu" "B.Cu") (net {net}))\n'
        for dx, dy in offsets[:count]
    )


class PatternFixture(CheckFixture):
    """Scaffolds a project whose one group declares a reference pattern."""

    def build_pattern(
        self,
        root: Path,
        board_body: str,
        references: str,
        *,
        pattern: str = ANCHOR_PATTERN,
        bind: str = "",
    ) -> Path:
        project = self.project(root)
        (project / "garden-logger.kicad_pcb").write_text(
            BOARD_HEADER + OUTLINE + board_body + ")\n",
            encoding="utf-8",
        )
        group = f"""  - id: everything
    priority: 1
    region: anywhere
    rationale: Test group.
    references: [{references}]
    pattern:
      id: testdriver
      anchor: U2{bind}"""
        (project / "placement.yaml").write_text(
            contract("", group), encoding="utf-8"
        )
        (project / "patterns").mkdir(exist_ok=True)
        (project / "patterns" / "testdriver.yaml").write_text(
            pattern, encoding="utf-8"
        )
        pins = yaml.safe_load((project / ".pcbforge").read_text(encoding="utf-8"))
        pins["pcbforge"] = {"revision": "0" * 40, "dirty": False}
        pins["guidance"] = dict(EXPECTED_GUIDANCE)
        (project / ".pcbforge").write_text(
            yaml.safe_dump(pins, sort_keys=False), encoding="utf-8"
        )
        # The scaffold fingerprints the board it wrote; this one replaced it, and
        # `generate_brief` refuses a stale CIRCUIT acceptance.
        (project / "docs" / "build-test.md").write_text(
            f"""---
pcbforge_build_test_report_schema: 1
result: pass
build: default
fingerprint: {fingerprint_inputs(project)}
---
# Pass
""",
            encoding="utf-8",
        )
        return project


class PatternTests(PatternFixture):
    """PA4: the board measured against a bound vendor reference layout."""

    def sketch(self, body: str, references: str = "U2, C3, C4"):
        with tempfile.TemporaryDirectory() as temporary:
            return check_placement(self.build_pattern(Path(temporary), body, references))

    def test_sketch_roles_measure_distance_and_side(self) -> None:
        # Pad 9 lands at (117, 120) and C3's pad 1 at (115, 120): 2 mm, west.
        result = self.sketch(
            anchor(120, 120)
            + capacitor("C3", 115.5, 120, 3, "VM_A")
            + capacitor("C4", 124.5, 120, 4, "VM_B")
            + vias(9)
        )
        found = self.findings(result)
        self.assertEqual(
            found["everything/vm-bypass-1"],
            ("pass", "2.00 mm, west of U2"),
        )
        self.assertEqual(
            found["everything/vm-bypass-2"],
            ("pass", "1.00 mm, east of U2"),
        )

    def test_a_satellite_on_the_wrong_side_of_the_anchor_fails(self) -> None:
        # C3 is within 2.5 mm of pad 9 but sits north of the anchor, not west.
        result = self.sketch(
            anchor(120, 120)
            + capacitor("C3", 120, 117, 3, "VM_A")
            + capacitor("C4", 124.5, 120, 4, "VM_B")
            + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "fail")
        self.assertIn("north of U2", measured)

    def test_a_distant_satellite_fails(self) -> None:
        result = self.sketch(
            anchor(120, 120)
            + capacitor("C3", 110, 120, 3, "VM_A")
            + capacitor("C4", 124.5, 120, 4, "VM_B")
            + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "fail")
        # C3 sits at 110; its pad 1 at 109.5 is 7.5 mm from pad 9 at 117.
        self.assertEqual(measured, "7.50 mm, west of U2")

    def test_a_rotated_anchor_rotates_the_expected_sides(self) -> None:
        # At 90 degrees the anchor's local west points south on the board, so a
        # capacitor below the part is still "west" in the anchor's own frame.
        result = self.sketch(
            anchor(120, 120, rotation=90)
            + capacitor("C3", 120, 124.5, 3, "VM_A")
            + capacitor("C4", 120, 115.5, 4, "VM_B")
            + vias(9)
        )
        found = self.findings(result)
        self.assertEqual(found["everything/vm-bypass-1"][0], "pass")
        self.assertEqual(found["everything/vm-bypass-2"][0], "pass")

    def test_counts_only_vias_inside_the_pad_on_its_net(self) -> None:
        found = self.findings(
            self.sketch(
                anchor(120, 120)
                + capacitor("C3", 115.5, 120, 3, "VM_A")
                + capacitor("C4", 124.5, 120, 4, "VM_B")
                + vias(9)
            )
        )
        self.assertEqual(found["everything/ep-thermal-vias"], ("pass", "9 vias"))

        found = self.findings(
            self.sketch(
                anchor(120, 120)
                + capacitor("C3", 115.5, 120, 3, "VM_A")
                + capacitor("C4", 124.5, 120, 4, "VM_B")
                + vias(4)
                + vias(9, net=2)  # +3V3 vias in the same place never count
            )
        )
        self.assertEqual(found["everything/ep-thermal-vias"], ("fail", "4 vias"))

    def test_note_rules_are_manual(self) -> None:
        found = self.findings(
            self.sketch(
                anchor(120, 120)
                + capacitor("C3", 115.5, 120, 3, "VM_A")
                + capacitor("C4", 124.5, 120, 4, "VM_B")
                + vias(9)
            )
        )
        status, measured = found["everything/gnd-pour-back"]
        self.assertEqual(status, "manual")
        self.assertIn("Solid GND pour", measured)

    def test_an_unbound_role_is_unmeasured_not_a_failure(self) -> None:
        result = self.sketch(
            anchor(120, 120) + capacitor("C3", 115.5, 120, 3, "VM_A") + vias(9),
            references="U2, C3",
        )
        self.assertEqual(
            self.findings(result)["everything/vm-bypass-2"],
            ("unmeasured", "not measured"),
        )

    def exact(self, body: str, references: str = "U2, C3"):
        with tempfile.TemporaryDirectory() as temporary:
            return check_placement(
                self.build_pattern(
                    Path(temporary), body, references, pattern=EXACT_PATTERN
                )
            )

    def test_exact_roles_measure_offset_rotation_and_side(self) -> None:
        result = self.exact(
            anchor(120, 120) + capacitor("C3", 115.5, 120, 3, "VM_A") + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "pass")
        self.assertEqual(measured, "0.00 mm off, rotation 0°, front")

    def test_exact_roles_fail_outside_the_tolerance(self) -> None:
        result = self.exact(
            anchor(120, 120) + capacitor("C3", 114.0, 120, 3, "VM_A") + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "1.50 mm off, rotation 0°, front")

    def test_exact_roles_fail_on_rotation_alone(self) -> None:
        result = self.exact(
            anchor(120, 120)
            + capacitor("C3", 115.5, 120, 3, "VM_A", rotation=90)
            + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "fail")
        self.assertEqual(measured, "0.00 mm off, rotation 90°, front")

    def test_a_rotated_anchor_moves_the_expected_position(self) -> None:
        # Local (-4.5, 0) under a 90 degree anchor lands 4.5 mm south of it.
        result = self.exact(
            anchor(120, 120, rotation=90)
            + capacitor("C3", 120, 124.5, 3, "VM_A", rotation=90)
            + vias(9)
        )
        self.assertEqual(self.findings(result)["everything/vm-bypass-1"][0], "pass")

    def test_a_satellite_on_the_wrong_board_side_fails(self) -> None:
        result = self.exact(
            anchor(120, 120)
            + capacitor("C3", 115.5, 120, 3, "VM_A", side="B.Cu")
            + vias(9)
        )
        status, measured = self.findings(result)["everything/vm-bypass-1"]
        self.assertEqual(status, "fail")
        self.assertIn("back", measured)

    def test_pattern_findings_reach_the_report(self) -> None:
        result = self.sketch(
            anchor(120, 120)
            + capacitor("C3", 115.5, 120, 3, "VM_A")
            + capacitor("C4", 124.5, 120, 4, "VM_B")
            + vias(9)
        )
        self.assertIn("## Reference patterns", result.report)
        self.assertIn("everything/vm-bypass-1", result.report)


class PatternContractTests(PatternFixture):
    """PA4 contract behaviour: binding, the brief, warnings, the fingerprint.

    Lives beside the check tests rather than in `test_placement.py` because
    binding needs a board carrying a `Partnumber` property and real pad nets,
    which is exactly the fixture `PatternTests` already builds.
    """

    BOARD = (
        anchor(120, 120)
        + capacitor("C3", 115.5, 120, 3, "VM_A")
        + capacitor("C4", 124.5, 120, 4, "VM_B")
        + vias(9)
    )

    def contract_for(self, project: Path):
        return read_placement_contract(project, tool_root=TOOL_ROOT)

    def test_binding_resolves_each_role_to_a_real_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            contract = self.contract_for(project)

        (group, binding) = contract.patterns[0]
        self.assertEqual(group, "everything")
        self.assertEqual(binding.anchor, "U2")
        self.assertEqual(
            binding.roles, (("vm-bypass-1", "C3"), ("vm-bypass-2", "C4"))
        )

    def test_an_explicit_bind_overrides_the_net_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(
                Path(temporary),
                self.BOARD,
                "U2, C3, C4",
                bind="\n      bind:\n        vm-bypass-1: C4",
            )
            with self.assertRaisesRegex(PlacementInputError, "shares no net"):
                self.contract_for(project)

    def test_an_anchor_outside_the_group_is_rejected_at_parse_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8").replace("anchor: U2", "anchor: U9"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                PlacementInputError, "U9 is not a reference in this group"
            ):
                self.contract_for(project)

    def test_an_unknown_pattern_id_is_an_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            (project / "patterns" / "testdriver.yaml").unlink()
            with self.assertRaisesRegex(PlacementInputError, "unknown pattern"):
                self.contract_for(project)

    def test_warnings_cover_fidelity_layers_and_unbound_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(
                Path(temporary),
                anchor(120, 120) + capacitor("C3", 115.5, 120, 3, "VM_A") + vias(9),
                "U2, C3",
                pattern=ANCHOR_PATTERN.replace("layers: 2", "layers: 4"),
            )
            contract = self.contract_for(project)

        self.assertEqual(
            contract.warnings,
            (
                "pattern testdriver role vm-bypass-2 is unbound in group "
                "everything: name it under bind: or drop it",
                "pattern testdriver was captured on 4 layers; this board has 2",
                "pattern testdriver is sketch fidelity: it is measured but "
                "cannot be applied",
            ),
        )

    def test_the_brief_shows_the_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            generate_brief(project, tool_root=TOOL_ROOT)
            brief = (project / BRIEF_FILENAME).read_text(encoding="utf-8")

        self.assertIn("- Pattern: testdriver (sketch fidelity, anchor U2)", brief)
        self.assertIn("| Role | Reference |", brief)
        self.assertIn("| vm-bypass-1 | C3 |", brief)
        self.assertIn("| vm-bypass-2 | C4 |", brief)

    def brief_fingerprint(self, project: Path) -> str:
        generate_brief(project, tool_root=TOOL_ROOT)
        brief = (project / BRIEF_FILENAME).read_text(encoding="utf-8")
        return yaml.safe_load(metadata_yaml(brief))["fingerprint"]

    def test_editing_a_bound_pattern_changes_the_handoff_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            before = self.brief_fingerprint(project)
            pattern = project / "patterns" / "testdriver.yaml"
            pattern.write_text(
                pattern.read_text(encoding="utf-8").replace("max_mm: 2.5", "max_mm: 1.5"),
                encoding="utf-8",
            )
            after = self.brief_fingerprint(project)

        self.assertNotEqual(before, after)

    def test_an_undeclared_pattern_file_never_touches_the_fingerprint(self) -> None:
        """The `patterns` segment is conditional on purpose.

        An unconditional segment would change `_contract_fingerprint` for every
        project that does not use the feature, staling handoff approvals that
        nothing about the design had invalidated.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8").replace(
                    "\n    pattern:\n      id: testdriver\n      anchor: U2", ""
                ),
                encoding="utf-8",
            )
            before = self.brief_fingerprint(project)
            pattern = project / "patterns" / "testdriver.yaml"
            pattern.write_text(
                pattern.read_text(encoding="utf-8").replace("max_mm: 2.5", "max_mm: 1.5"),
                encoding="utf-8",
            )
            after = self.brief_fingerprint(project)

        self.assertEqual(before, after)

    def test_a_project_pattern_is_a_visible_handoff_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_pattern(Path(temporary), self.BOARD, "U2, C3, C4")
            inputs = brief_inputs(project)
            checked = placement_check_inputs(project)

        self.assertIn("patterns/testdriver.yaml", [
            path.relative_to(path.parents[1]).as_posix() for path in inputs
        ])
        self.assertTrue(any(path.name == "testdriver.yaml" for path in checked))
