"""Tests for `pcbforge.sketch_placement`.

Cost terms are exercised on hand-built rectangle states, which keeps them
readable and independent of whatever the annealer happens to find. The solver
itself is tested by its outcome on a seeded toy board rather than by its path:
annealing is stochastic, so asserting a particular arrangement would be
asserting an implementation detail.
"""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import yaml

from pcbforge.cli import main
from pcbforge.placement import read_placement_contract
from pcbforge.placement_check import check_placement
from pcbforge.sketch_placement import (
    REPORT_FILENAME,
    WEIGHTS,
    GroupBox,
    PlacedRect,
    SketchInputError,
    Variant,
    _build_problem,
    _costs,
    _solve,
    _svg,
    floorplan_block,
    sketch_placement,
)
from tests.test_placement import TOOL_ROOT
from tests.test_placement_check import (
    BOARD_HEADER,
    OUTLINE,
    CheckFixture,
    footprint,
)

BOARD = (40.0, 30.0)


def boxes(*sizes: tuple[str, float, float]) -> tuple[GroupBox, ...]:
    return tuple(
        GroupBox(identifier, width, height, None)
        for identifier, width, height in sizes
    )


class CostTests(unittest.TestCase):
    """Each term measured on a state built by hand, one behaviour at a time."""

    def problem(self, contract=None, group_boxes=None, evidence=None):
        from types import SimpleNamespace

        contract = contract or SimpleNamespace(groups=(), constraints=())
        group_boxes = group_boxes or boxes(("a", 10, 10), ("b", 10, 10))
        evidence = evidence or SimpleNamespace(pad_nets=())
        return _build_problem(contract, group_boxes, BOARD, evidence)

    def test_overlap_is_the_shared_area(self) -> None:
        costs = _costs(self.problem(), [(0, 0, 10, 10), (5, 5, 10, 10)])
        self.assertAlmostEqual(costs["overlap"], 25.0)

    def test_touching_rectangles_do_not_overlap(self) -> None:
        costs = _costs(self.problem(), [(0, 0, 10, 10), (10, 0, 10, 10)])
        self.assertAlmostEqual(costs["overlap"], 0.0)

    def test_out_of_bounds_is_the_area_off_the_board(self) -> None:
        costs = _costs(self.problem(), [(35, 0, 10, 10), (0, 0, 10, 10)])
        self.assertAlmostEqual(costs["out-of-bounds"], 50.0)

    def test_a_rectangle_fully_on_the_board_costs_nothing(self) -> None:
        costs = _costs(self.problem(), [(0, 0, 10, 10), (20, 10, 10, 10)])
        self.assertAlmostEqual(costs["out-of-bounds"], 0.0)

    def test_compactness_is_the_bounding_box_area(self) -> None:
        costs = _costs(self.problem(), [(0, 0, 10, 10), (20, 10, 10, 10)])
        self.assertAlmostEqual(costs["compactness"], 30.0 * 20.0)

    def constrained(self, kind: str, **fields):
        from types import SimpleNamespace

        constraint = SimpleNamespace(
            identifier="c1",
            kind=kind,
            subjects=fields.pop("subjects"),
            min_mm=fields.pop("min_mm", None),
            max_mm=fields.pop("max_mm", None),
            edge=fields.pop("edge", None),
            direction=fields.pop("direction", None),
        )
        contract = SimpleNamespace(
            groups=(
                SimpleNamespace(identifier="a", references=("U1",)),
                SimpleNamespace(identifier="b", references=("U2",)),
            ),
            constraints=(constraint,),
        )
        return self.problem(contract=contract)

    def test_proximity_costs_only_the_excess(self) -> None:
        problem = self.constrained("proximity", subjects=("U1", "U2"), max_mm=5.0)
        # A 12 mm gap against a 5 mm limit is 7 mm of excess.
        costs = _costs(problem, [(0, 0, 10, 10), (22, 0, 10, 10)])
        self.assertAlmostEqual(costs["proximity"], 7.0)
        costs = _costs(problem, [(0, 0, 10, 10), (13, 0, 10, 10)])
        self.assertAlmostEqual(costs["proximity"], 0.0)

    def test_separation_costs_the_shortfall(self) -> None:
        problem = self.constrained("separation", subjects=("U1", "U2"), min_mm=8.0)
        costs = _costs(problem, [(0, 0, 10, 10), (12, 0, 10, 10)])
        self.assertAlmostEqual(costs["separation"], 6.0)
        costs = _costs(problem, [(0, 0, 10, 10), (20, 0, 10, 10)])
        self.assertAlmostEqual(costs["separation"], 0.0)

    def test_board_edge_costs_the_distance_beyond_the_limit(self) -> None:
        problem = self.constrained(
            "board-edge", subjects=("U1",), edge="west", max_mm=2.0
        )
        costs = _costs(problem, [(9, 0, 10, 10), (0, 20, 10, 10)])
        self.assertAlmostEqual(costs["board-edge"], 7.0)

    def test_board_edge_any_takes_the_nearest_side(self) -> None:
        problem = self.constrained(
            "board-edge", subjects=("U1",), edge="any", max_mm=1.0
        )
        costs = _costs(problem, [(30, 10, 10, 10), (0, 0, 10, 10)])
        self.assertAlmostEqual(costs["board-edge"], 0.0)

    def test_order_counts_violated_pairs(self) -> None:
        problem = self.constrained(
            "order", subjects=("U1", "U2"), direction="west-to-east"
        )
        self.assertAlmostEqual(
            _costs(problem, [(0, 0, 10, 10), (20, 0, 10, 10)])["order"], 0.0
        )
        self.assertAlmostEqual(
            _costs(problem, [(20, 0, 10, 10), (0, 0, 10, 10)])["order"], 1.0
        )

    def test_wirelength_weights_centre_distance_by_shared_nets(self) -> None:
        from types import SimpleNamespace

        contract = SimpleNamespace(
            groups=(
                SimpleNamespace(identifier="a", references=("U1",)),
                SimpleNamespace(identifier="b", references=("U2",)),
            ),
            constraints=(),
        )
        evidence = SimpleNamespace(
            pad_nets=(
                ("U1", "1", "GND"),
                ("U2", "1", "GND"),
                ("U1", "2", "VCC"),
                ("U2", "2", "VCC"),
            )
        )
        problem = self.problem(contract=contract, evidence=evidence)
        # Two shared nets, centres 20 mm apart.
        costs = _costs(problem, [(0, 0, 10, 10), (20, 0, 10, 10)])
        self.assertAlmostEqual(costs["wirelength"], 40.0)


class SolverTests(unittest.TestCase):
    def problem(self):
        from types import SimpleNamespace

        contract = SimpleNamespace(groups=(), constraints=())
        return _build_problem(
            contract,
            boxes(("a", 12, 8), ("b", 12, 8), ("c", 10, 10), ("d", 8, 8)),
            (40.0, 30.0),
            SimpleNamespace(pad_nets=()),
        )

    def test_packs_four_groups_without_overlap_or_spill(self) -> None:
        variants = _solve(self.problem(), variants=1, seed=1, iterations=4000)
        (variant,) = variants

        self.assertAlmostEqual(variant.costs["overlap"], 0.0, places=6)
        self.assertAlmostEqual(variant.costs["out-of-bounds"], 0.0, places=6)

    def test_the_same_seed_gives_the_same_arrangement(self) -> None:
        first = _solve(self.problem(), variants=1, seed=7, iterations=800)
        second = _solve(self.problem(), variants=1, seed=7, iterations=800)

        self.assertEqual(first[0].rects, second[0].rects)

    def test_variants_are_labelled_and_seeded_in_order(self) -> None:
        variants = _solve(self.problem(), variants=3, seed=1, iterations=600)

        self.assertEqual([item.label for item in variants], ["A", "B", "C"])
        self.assertTrue(all(item.seed >= 1 for item in variants))

    def test_identical_arrangements_are_not_offered_twice(self) -> None:
        # One group in a one-cell board can only be placed one way, so every
        # seed produces the same arrangement and only one variant survives.
        from types import SimpleNamespace

        problem = _build_problem(
            SimpleNamespace(groups=(), constraints=()),
            boxes(("only", 10, 10)),
            (10.0, 10.0),
            SimpleNamespace(pad_nets=()),
        )
        variants = _solve(problem, variants=3, seed=1, iterations=200)

        self.assertEqual(len(variants), 1)


class SvgTests(unittest.TestCase):
    def test_the_diagram_is_well_formed_xml(self) -> None:
        from types import SimpleNamespace

        problem = _build_problem(
            SimpleNamespace(groups=(), constraints=()),
            boxes(("power-in", 10, 10), ("controller", 10, 10)),
            BOARD,
            SimpleNamespace(pad_nets=()),
        )
        variant = Variant(
            "A",
            1,
            (
                PlacedRect("power-in", 0, 0, 10, 10),
                PlacedRect("controller", 20, 10, 10, 10),
            ),
            {term: 0.0 for term in WEIGHTS},
            (),
        )
        root = ElementTree.fromstring(_svg(variant, problem))

        self.assertTrue(root.tag.endswith("svg"))
        labels = [
            element.text
            for element in root.iter()
            if element.tag.endswith("text")
        ]
        self.assertEqual(labels, ["power-in", "controller"])


class ProjectTests(CheckFixture):
    """The command end to end on a scaffolded project."""

    def build_sketch(self, root: Path) -> Path:
        body = (
            footprint("U1", 110, 110, half=2.0)
            + footprint("C1", 120, 110)
            + footprint("R1", 130, 130)
        )
        return self.build(root, body, "", "U1, C1, R1")

    def test_writes_a_report_and_one_svg_per_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_sketch(Path(temporary))
            result = sketch_placement(project, variants=2, seed=1, iterations=500)

            self.assertEqual(result.board_mm, (50.0, 40.0))
            self.assertTrue((project / REPORT_FILENAME).is_file())
            for path in result.svg_paths:
                self.assertTrue((project / path).is_file())
            report = (project / REPORT_FILENAME).read_text(encoding="utf-8")

        self.assertIn("## Variant A", report)
        self.assertIn("| overlap |", report)
        self.assertIn("floorplan:", report)

    def test_the_board_and_contract_are_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_sketch(Path(temporary))
            board = (project / "garden-logger.kicad_pcb").read_bytes()
            contract = (project / "placement.yaml").read_bytes()

            sketch_placement(project, variants=1, seed=1, iterations=300)

            self.assertEqual((project / "garden-logger.kicad_pcb").read_bytes(), board)
            self.assertEqual((project / "placement.yaml").read_bytes(), contract)

    def test_the_yaml_block_parses_back_into_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_sketch(Path(temporary))
            result = sketch_placement(
                project, variants=1, seed=1, iterations=500, write=False
            )
            block = floorplan_block(result.variants[0], result.board_mm)
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8") + block + "\n",
                encoding="utf-8",
            )
            contract = read_placement_contract(project, tool_root=TOOL_ROOT)

        self.assertIsNotNone(contract.floorplan)
        self.assertEqual(contract.floorplan.variant, "A")
        self.assertEqual(contract.floorplan.board_mm, (50.0, 40.0))
        self.assertEqual(
            [rect.identifier for rect in contract.floorplan.rects], ["everything"]
        )
        # The block is valid YAML on its own, which is what "paste this" means.
        self.assertIn("floorplan", yaml.safe_load(block))

    def test_refuses_an_impossible_variant_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_sketch(Path(temporary))
            with self.assertRaisesRegex(SketchInputError, "variants must be"):
                sketch_placement(project, variants=0)

    def test_cli_exits_zero_and_reports_each_variant(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as temporary:
            project = self.build_sketch(Path(temporary))
            with mock.patch("sys.stdout") as stdout:
                code = main(["sketch-placement", str(project), "--variants", "1"])

        printed = "".join(
            str(call.args[0]) for call in stdout.write.call_args_list if call.args
        )
        self.assertEqual(code, 0)
        self.assertIn("placement sketch", printed)
        self.assertIn("docs/placement-sketch.md", printed)


class FloorplanFindingTests(CheckFixture):
    """PA6's contribution to `check-placement`."""

    def measure(self, floorplan: str, x: float = 110.0):
        body = footprint("U1", x, 110) + footprint("C1", x + 3, 112)
        with tempfile.TemporaryDirectory() as temporary:
            project = self.build(Path(temporary), body, "", "U1, C1")
            placement = project / "placement.yaml"
            placement.write_text(
                placement.read_text(encoding="utf-8") + floorplan,
                encoding="utf-8",
            )
            return check_placement(project)

    #: The outline runs 100..150 x 100..140, so board-relative (10, 10) is (110, 110).
    PLAN = """floorplan:
  variant: A
  seed: 1
  board_mm: [50, 40]
  groups:
    - id: everything
      rect_mm: [5, 5, 20, 20]
"""

    def test_a_group_inside_its_rectangle_passes(self) -> None:
        found = self.findings(self.measure(self.PLAN))
        self.assertEqual(
            found["everything"], ("pass", "2 of 2 inside, centroid 0.00 mm outside")
        )
        self.assertEqual(found["outline-matches-floorplan"][0], "pass")

    def test_a_group_outside_its_rectangle_fails_and_names_the_strays(self) -> None:
        result = self.measure(self.PLAN, x=140.0)
        finding = next(
            item for item in result.findings if item.identifier == "everything"
        )
        self.assertEqual(finding.status, "fail")
        self.assertIn("0 of 2 inside", finding.measured)
        # Listed in the group's own reference order, which is deterministic.
        self.assertIn("outside: U1, C1", finding.detail)

    def test_a_board_size_mismatch_fails(self) -> None:
        plan = self.PLAN.replace("board_mm: [50, 40]", "board_mm: [30, 25]").replace(
            "rect_mm: [5, 5, 20, 20]", "rect_mm: [5, 5, 20, 15]"
        )
        found = self.findings(self.measure(plan))
        self.assertEqual(found["outline-matches-floorplan"][0], "fail")

    def test_floorplan_findings_reach_the_report(self) -> None:
        result = self.measure(self.PLAN)
        self.assertIn("## Adopted floorplan", result.report)
        self.assertIn("outline-matches-floorplan", result.report)
