"""Tests for `pcbforge.patterns`.

Patterns are written inline and dropped into a `patterns/` directory, because
`read_pattern` requires the file stem to equal the pattern id. Binding takes a
`BoardFacts`, so these tests build one directly instead of scaffolding a board;
`board_facts` is covered separately against real PA1 geometry.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pcbforge.patterns import (
    BoardFacts,
    PatternInputError,
    bind,
    board_facts,
    read_pattern,
    resolve_pattern_path,
)

SKETCH = """pattern_schema: 1
id: testdriver
part:
  partnumber_match: "^TEST8316[CR]T"
  footprint_match: "VQFN-40"
fidelity: sketch
source:
  document: "TEST8316 datasheet, Layout Example figure"
  layers: 4
  captured: 2026-09-02
  notes:
    - "The EVM is four-layer; on two layers keep the GND pour on the back."
frame: >-
  Offsets are in the anchor footprint's local frame with anchor rotation 0.
roles:
  - id: vm-bypass-1
    anchor_pads: ["9"]
    footprint_match: "^Capacitor_SMD"
    side: same
    near_side: west
    max_mm: 2.0
    rationale: First high-frequency VM bypass directly at pin 9.
  - id: vm-bypass-2
    anchor_pads: ["10"]
    footprint_match: "^Capacitor_SMD"
    near_side: east
    max_mm: 2.0
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
    rationale: Heat spreading and return path.
"""

EXACT = """pattern_schema: 1
id: testdriver
part:
  partnumber_match: "^TEST8316[CR]T"
  footprint_match: "VQFN-40"
fidelity: exact
source:
  document: "TEST8316EVM design files, revision B"
  layers: 4
  captured: 2026-09-02
frame: >-
  Offsets are in the anchor footprint's local frame with anchor rotation 0.
roles:
  - id: vm-bypass-1
    anchor_pads: ["9"]
    footprint_match: "^Capacitor_SMD"
    offset_mm: [-4.2, 1.25]
    rotation_deg: 90
    tolerance_mm: 0.5
    rationale: First high-frequency VM bypass directly at pin 9.
"""


def facts(**overrides) -> BoardFacts:
    """Two identical bypass capacitors around one driver, by default distinct."""
    footprints = {
        "U2": "Package_DFN_QFN:VQFN-40-1EP_6x6mm",
        "C3": "Capacitor_SMD:C_0402_1005Metric",
        "C4": "Capacitor_SMD:C_0402_1005Metric",
        "R1": "Resistor_SMD:R_0402_1005Metric",
    }
    partnumbers = {"U2": "TEST8316CT", "C3": "CAP1", "C4": "CAP2", "R1": "RES1"}
    pad_nets = {
        "U2": (("9", "VM_A"), ("10", "VM_B"), ("41", "GND")),
        "C3": (("1", "VM_A"), ("2", "GND")),
        "C4": (("1", "VM_B"), ("2", "GND")),
        "R1": (("1", "VM_A"), ("2", "GND")),
    }
    data = {
        "footprints": footprints,
        "partnumbers": partnumbers,
        "pad_nets": pad_nets,
    }
    data.update(overrides)
    return BoardFacts(**data)


class PatternFixture(unittest.TestCase):
    def pattern(self, root: Path, text: str = SKETCH, name: str = "testdriver"):
        directory = root / "patterns"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        return read_pattern(path)

    def rejects(self, text: str, pattern: str, name: str = "testdriver") -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(PatternInputError, pattern):
                self.pattern(Path(temporary), text, name)


class ParsingTests(PatternFixture):
    def test_reads_a_sketch_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pattern = self.pattern(Path(temporary))

        self.assertEqual(pattern.identifier, "testdriver")
        self.assertEqual(pattern.fidelity, "sketch")
        self.assertEqual(pattern.source.layers, 4)
        self.assertEqual(pattern.source.captured, "2026-09-02")
        self.assertEqual(len(pattern.source.notes), 1)
        self.assertEqual(
            tuple(role.identifier for role in pattern.roles),
            ("vm-bypass-1", "vm-bypass-2"),
        )
        first = pattern.role("vm-bypass-1")
        self.assertEqual(first.anchor_pads, ("9",))
        self.assertEqual(first.satellite_pads, 1)
        self.assertEqual(first.near_side, "west")
        self.assertEqual(first.max_mm, 2.0)
        self.assertIsNone(first.offset_mm)
        self.assertEqual(pattern.role("vm-bypass-2").side, "same")
        self.assertEqual(
            tuple((rule.identifier, rule.kind) for rule in pattern.rules),
            (("ep-thermal-vias", "vias-under-pad"), ("gnd-pour-back", "note")),
        )
        self.assertEqual(pattern.rules[0].min_count, 9)

    def test_reads_an_exact_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pattern = self.pattern(Path(temporary), EXACT)

        role = pattern.role("vm-bypass-1")
        self.assertEqual(pattern.fidelity, "exact")
        self.assertEqual(role.offset_mm, (-4.2, 1.25))
        self.assertEqual(role.rotation_deg, 90.0)
        self.assertEqual(role.tolerance_mm, 0.5)
        self.assertIsNone(role.near_side)

    def test_unquoted_pad_numbers_are_still_pad_numbers(self) -> None:
        # YAML turns `anchor_pads: [9]` into an int; a pad number is a string.
        with tempfile.TemporaryDirectory() as temporary:
            pattern = self.pattern(
                Path(temporary),
                SKETCH.replace('anchor_pads: ["9"]', "anchor_pads: [9]").replace(
                    'anchor_pad: "41"', "anchor_pad: 41"
                ),
            )

        self.assertEqual(pattern.role("vm-bypass-1").anchor_pads, ("9",))
        self.assertEqual(pattern.rules[0].anchor_pad, "41")

    def test_rejects_a_mismatched_file_name(self) -> None:
        self.rejects(SKETCH, "expected 'other' to match the file name", name="other")

    def test_rejects_unknown_keys_and_schema(self) -> None:
        self.rejects(
            SKETCH.replace("pattern_schema: 1", "pattern_schema: 2"),
            "unsupported version",
        )
        self.rejects(
            SKETCH.replace("fidelity: sketch", "fidelity: sketch\nextra: no"),
            "unknown keys: extra",
        )
        self.rejects(
            SKETCH.replace("    side: same", "    side: same\n    nope: 1"),
            r"roles\[0\]: unknown keys: nope",
        )

    def test_rejects_a_bad_fidelity_and_crossed_role_keys(self) -> None:
        self.rejects(SKETCH.replace("fidelity: sketch", "fidelity: rough"), "fidelity")
        self.rejects(
            SKETCH.replace(
                "    near_side: west",
                "    near_side: west\n    offset_mm: [1, 2]",
            ),
            "offset_mm not allowed in a sketch pattern",
        )
        self.rejects(
            EXACT.replace(
                "    rotation_deg: 90",
                "    rotation_deg: 90\n    near_side: west",
            ),
            "near_side not allowed in an? exact pattern",
        )
        self.rejects(
            EXACT.replace("    offset_mm: [-4.2, 1.25]\n", ""),
            r"offset_mm: expected \[x, y\] in millimetres",
        )

    def test_rejects_bad_ids_duplicates_and_regexes(self) -> None:
        self.rejects(SKETCH.replace("id: vm-bypass-2", "id: VM_Bypass2"), "kebab-case")
        self.rejects(
            SKETCH.replace("id: vm-bypass-2", "id: vm-bypass-1"),
            "roles: duplicate IDs: vm-bypass-1",
        )
        self.rejects(
            SKETCH.replace('partnumber_match: "^TEST8316[CR]T"', 'partnumber_match: "["'),
            "invalid regular expression",
        )

    def test_rejects_malformed_rules(self) -> None:
        self.rejects(
            SKETCH.replace("    type: vias-under-pad", "    type: draw-a-pour"),
            "type: expected one of note, vias-under-pad",
        )
        self.rejects(
            SKETCH.replace("    min_count: 9\n", ""),
            "min_count: expected a positive integer",
        )
        self.rejects(
            SKETCH.replace(
                "    text: Solid GND pour under the package on the opposite layer.",
                "    anchor_pad: 41",
            ),
            "anchor_pad: not allowed for note",
        )


class ResolutionTests(PatternFixture):
    def test_a_project_copy_wins_over_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            tool = root / "tool"
            for base in (project, tool):
                (base / "patterns").mkdir(parents=True)
                (base / "patterns" / "testdriver.yaml").write_text(
                    SKETCH, encoding="utf-8"
                )

            self.assertEqual(
                resolve_pattern_path("testdriver", project, tool),
                project / "patterns" / "testdriver.yaml",
            )

            (project / "patterns" / "testdriver.yaml").unlink()
            self.assertEqual(
                resolve_pattern_path("testdriver", project, tool),
                tool / "patterns" / "testdriver.yaml",
            )

            with self.assertRaisesRegex(PatternInputError, "unknown pattern 'nope'"):
                resolve_pattern_path("nope", project, tool)


class BindingTests(PatternFixture):
    def bind(self, board=None, references=("U2", "C3", "C4", "R1"), overrides=None):
        with tempfile.TemporaryDirectory() as temporary:
            pattern = self.pattern(Path(temporary))
        return bind(pattern, "U2", references, board or facts(), overrides)

    def test_distinct_nets_bind_each_role_uniquely(self) -> None:
        binding = self.bind()
        self.assertEqual(binding.roles, (("vm-bypass-1", "C3"), ("vm-bypass-2", "C4")))
        self.assertEqual(binding.unbound, ())
        self.assertEqual(
            binding.payload(),
            {
                "pattern": "testdriver",
                "anchor": "U2",
                "roles": {"vm-bypass-1": "C3", "vm-bypass-2": "C4"},
            },
        )

    def test_the_footprint_filter_excludes_a_resistor_on_the_same_net(self) -> None:
        # R1 shares VM_A with C3 but is not a capacitor, so it never competes.
        self.assertEqual(self.bind().roles[0], ("vm-bypass-1", "C3"))

    def test_a_role_with_no_candidate_is_unbound(self) -> None:
        board = facts(
            footprints={
                "U2": "Package_DFN_QFN:VQFN-40-1EP_6x6mm",
                "C3": "Capacitor_SMD:C_0402_1005Metric",
            },
            partnumbers={"U2": "TEST8316CT", "C3": "CAP1"},
            pad_nets={
                "U2": (("9", "VM_A"), ("10", "VM_B"), ("41", "GND")),
                "C3": (("1", "VM_A"), ("2", "GND")),
            },
        )
        binding = self.bind(board, references=("U2", "C3"))
        self.assertEqual(binding.roles, (("vm-bypass-1", "C3"), ("vm-bypass-2", None)))
        self.assertEqual(binding.unbound, ("vm-bypass-2",))

    def test_two_identical_candidates_are_an_error_not_a_guess(self) -> None:
        board = facts(
            pad_nets={
                "U2": (("9", "VM"), ("10", "VM"), ("41", "GND")),
                "C3": (("1", "VM"), ("2", "GND")),
                "C4": (("1", "VM"), ("2", "GND")),
                "R1": (("1", "OTHER"), ("2", "GND")),
            }
        )
        with self.assertRaisesRegex(
            PatternInputError,
            "role vm-bypass-1 matches C3, C4; add an explicit bind",
        ):
            self.bind(board)

    def test_an_override_disambiguates_and_leaves_one_candidate(self) -> None:
        board = facts(
            pad_nets={
                "U2": (("9", "VM"), ("10", "VM"), ("41", "GND")),
                "C3": (("1", "VM"), ("2", "GND")),
                "C4": (("1", "VM"), ("2", "GND")),
                "R1": (("1", "OTHER"), ("2", "GND")),
            }
        )
        binding = self.bind(board, overrides={"vm-bypass-1": "C4"})
        self.assertEqual(binding.roles, (("vm-bypass-1", "C4"), ("vm-bypass-2", "C3")))

    def test_rejects_a_bad_override(self) -> None:
        with self.assertRaisesRegex(PatternInputError, "unknown role 'nope'"):
            self.bind(overrides={"nope": "C3"})
        with self.assertRaisesRegex(PatternInputError, "not another reference"):
            self.bind(overrides={"vm-bypass-1": "U9"})
        with self.assertRaisesRegex(PatternInputError, "shares no net"):
            self.bind(overrides={"vm-bypass-1": "C4"})

    def test_rejects_an_anchor_that_is_the_wrong_part(self) -> None:
        board = facts(partnumbers={"U2": "SOMETHING_ELSE", "C3": "", "C4": "", "R1": ""})
        with self.assertRaisesRegex(PatternInputError, "U2 is SOMETHING_ELSE"):
            self.bind(board)

        board = facts(
            footprints={
                "U2": "Package_QFP:LQFP-48",
                "C3": "Capacitor_SMD:C_0402_1005Metric",
                "C4": "Capacitor_SMD:C_0402_1005Metric",
                "R1": "Resistor_SMD:R_0402_1005Metric",
            }
        )
        with self.assertRaisesRegex(PatternInputError, "U2 is Package_QFP:LQFP-48"):
            self.bind(board)

    def test_rejects_an_anchor_outside_the_group(self) -> None:
        with self.assertRaisesRegex(PatternInputError, "not a reference in its group"):
            self.bind(references=("C3", "C4"))

    def test_rejects_an_anchor_pad_with_no_net(self) -> None:
        board = facts(
            pad_nets={
                "U2": (("10", "VM_B"), ("41", "GND")),
                "C3": (("1", "VM_A"), ("2", "GND")),
                "C4": (("1", "VM_B"), ("2", "GND")),
                "R1": (("1", "VM_A"), ("2", "GND")),
            }
        )
        with self.assertRaisesRegex(PatternInputError, r"U2 pad\(s\) 9, which carry no net"):
            self.bind(board)


class BoardFactsTests(unittest.TestCase):
    def test_reduces_real_geometry_to_identity_and_nets(self) -> None:
        from pcbforge.board_geometry import read_board_geometry

        board = (
            Path(__file__).resolve().parents[1]
            / "pilots"
            / "kicad9-multichannel"
            / "baseline"
            / "source"
            / "multichannel_mixer.kicad_pcb"
        )
        derived = board_facts(read_board_geometry(board))

        self.assertEqual(len(derived.footprints), len(derived.pad_nets))
        reference = sorted(derived.footprints)[0]
        self.assertIn(":", derived.footprints[reference])
        # Unconnected pads are dropped: an empty net would match every role.
        self.assertTrue(
            all(net for pads in derived.pad_nets.values() for _, net in pads)
        )
