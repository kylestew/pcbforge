"""Test doubles for the KiCad CLI and a minimal generated review schematic."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from pcbforge.circuit_review import SCHEMATIC_AUDIT_SCHEMA, SCH_FORMAT_VERSION

FIXTURE_SYMBOLS = Path(__file__).resolve().parent / "fixtures" / "symbols"


class FakeKicad:
    """Stand-in for ``scripts/kicad-cli`` that answers ERC and netlist exports.

    ``nets`` maps a KiCad net name to its ``REF.PIN`` endpoints; ``erc`` is a
    list of error descriptions to report.
    """

    def __init__(
        self,
        nets: Mapping[str, Sequence[str]] | None = None,
        *,
        erc: Sequence[str] = (),
        fail: bool = False,
    ) -> None:
        self.nets = dict(nets or {"+3V3": ["R1.1"], "GND": ["R1.2"]})
        self.erc = list(erc)
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
        command = list(command)
        self.calls.append(command)
        if self.fail:
            return subprocess.CompletedProcess(command, 3, "", "Failed to load schematic")
        output = Path(command[command.index("--output") + 1])
        if command[1:3] == ["sch", "erc"]:
            violations = [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": text,
                    "items": [{"description": text, "pos": {"x": 1.0, "y": 2.0}}],
                }
                for text in self.erc
            ]
            output.write_text(
                json.dumps({"sheets": [{"path": "/", "violations": violations}]}),
                encoding="utf-8",
            )
        elif command[1:4] == ["sch", "export", "netlist"]:
            lines = ['(export (version "E") (nets']
            for code, (name, endpoints) in enumerate(sorted(self.nets.items()), start=1):
                nodes = " ".join(
                    f'(node (ref "{item.split(".", 1)[0]}") (pin "{item.split(".", 1)[1]}"))'
                    for item in endpoints
                )
                lines.append(f'(net (code "{code}") (name "{name}") {nodes})')
            lines.append("))")
            output.write_text("\n".join(lines), encoding="utf-8")
        elif command[1:4] == ["sch", "export", "svg"]:
            output.mkdir(parents=True, exist_ok=True)
            (output / "circuit.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><text>x</text><path d="M0 0"/></svg>',
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, "", "")


def schematic_text(
    model_hash: str,
    *,
    marker: bool = True,
    value: str = "1k",
    group_title: str = "Reviewed branch",
    purpose: str = "Limits current in the reviewed branch.",
    version: str = SCH_FORMAT_VERSION,
    project_name: str = "garden-logger",
) -> str:
    """A hand-minimal schematic that satisfies the structural gate for MODEL."""
    marker_text = (
        '(text "PCBForge review-only — not PCB input" (exclude_from_sim no) (at 20 20 0) '
        '(effects (font (size 2.54 2.54))) (uuid "t1"))'
        if marker
        else ""
    )
    return f"""(kicad_sch (version {version}) (generator "pcbforge") (generator_version "1")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (title_block (title "Garden logger") (comment 2 "pcbforge_model_sha256={model_hash}"))
  (lib_symbols)
  {marker_text}
  (text "{group_title}" (exclude_from_sim no) (at 20 30 0) (effects (font (size 1.5 1.5))) (uuid "t2"))
  (symbol (lib_id "Device:R") (at 50 50 0) (unit 1) (uuid "s1")
    (property "Reference" "R1" (at 52 50 0) (effects (font (size 1.27 1.27))))
    (property "Value" "{value}" (at 52 52 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 50 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "pcbforge_group" "reviewed-branch" (at 50 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "pcbforge_purpose" "{purpose}" (at 50 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances (project "{project_name}" (path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))))
  )
  (sheet_instances (path "/" (page "1")))
)
"""


def audit_text(
    model_hash: str,
    *,
    warnings: Sequence[Mapping[str, str]] = (),
    bound: Sequence[str] = ("R1",),
    schema: int = SCHEMATIC_AUDIT_SCHEMA,
    schematic_sha256: str = "",
) -> str:
    return json.dumps(
        {
            "schema": schema,
            "model_sha256": model_hash,
            "schematic_sha256": schematic_sha256,
            "bound_component_refs": list(bound),
            "symbol_choices": {
                ref: {"lib_id": "Device:R", "generic": False, "reason": "stock"} for ref in bound
            },
            "nets": {
                "supply": {"display_name": "+3V3", "compiler_name": "+3V3"},
                "ground": {"display_name": "GND", "compiler_name": "GND"},
            },
            "warnings": list(warnings),
        },
        indent=2,
        sort_keys=True,
    ) + "\n"
