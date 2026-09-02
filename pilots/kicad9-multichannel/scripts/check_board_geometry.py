#!/usr/bin/env python3
"""Prove `pcbforge.board_geometry` against KiCad's own IPC-D-356 export.

The rotation transform and the "no x mirror on the back side" rule are the
riskiest assumptions under the whole placement-assistance workstream. This
script re-derives both from KiCad itself instead of trusting a comment.

`kicad-cli pcb export ipcd356` writes a netlist carrying every pad's ABSOLUTE
position, so it is an independent oracle for the transform. Its coordinates are
in 0.0001 inch with y pointing up, hence the 0.00254 scale and the negated y.
Its quantum is 0.00254 mm, which is the noise floor for every number below.

The comparison runs twice: once as implemented, and once with an x mirror
applied to back-side pad offsets. Printing both is the point. The contrast is
what stops a future reader from "fixing" the missing mirror.

Not a unit test: it needs KiCad 9 installed, and `tests/` must stay runnable
without it. Run it by hand after a KiCad upgrade, or to regenerate the
provenance block in `tests/test_board_geometry.py`.

    uv run --project toolchain python \\
        pilots/kicad9-multichannel/scripts/check_board_geometry.py

Exits 0 when the worst un-mirrored error is under one IPC quantum.
"""

from __future__ import annotations

import argparse
import datetime
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from pcbforge import sexpr  # noqa: E402
from pcbforge.board_geometry import (  # noqa: E402
    _point,
    _transform,
    read_board_geometry,
)

DEFAULT_BOARD = (
    Path(__file__).resolve().parents[1]
    / "baseline"
    / "source"
    / "multichannel_mixer.kicad_pcb"
)
KICAD_CLI = REPO_ROOT / "scripts" / "kicad-cli"
IPC_SCALE_MM = 0.00254
IPC_QUANTUM_MM = 0.00254

RECORD_RE = re.compile(
    r"^3\d\d.{16} (?P<ref>\S+)\s+-(?P<pad>\d+)\s+\S+"
    r"X(?P<x>[+-]\d+)Y(?P<y>[+-]\d+)"
)


def export_ipcd356(board: Path, into: Path) -> Path:
    output = into / "board.d356"
    result = subprocess.run(
        [str(KICAD_CLI), "pcb", "export", "ipcd356", "-o", str(output), str(board)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise SystemExit(f"kicad-cli failed: {message}")
    return output


def read_oracle(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """Absolute pad positions in mm, keyed by (reference, pad number)."""
    pads: dict[tuple[str, str], tuple[float, float]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = RECORD_RE.match(line)
        if match is None:
            continue
        key = (match.group("ref"), str(int(match.group("pad"))))
        pads.setdefault(
            key,
            (
                int(match.group("x")) * IPC_SCALE_MM,
                -int(match.group("y")) * IPC_SCALE_MM,
            ),
        )
    return pads


def mirrored_positions(board: Path) -> dict[tuple[str, str], tuple[float, float]]:
    """Pad positions recomputed with an x mirror on back-side offsets.

    This is the hypothesis the module rejects. Reading the board again here,
    rather than reusing the module, keeps the failed variant out of shipped
    code.
    """
    root = sexpr.parse(board.read_text(encoding="utf-8"))
    positions: dict[tuple[str, str], tuple[float, float]] = {}
    for node in sexpr.children(root, "footprint"):
        properties = {
            sexpr.atom(item, 1): sexpr.atom(item, 2)
            for item in sexpr.children(node, "property")
        }
        reference = properties.get("Reference", "")
        at = sexpr.child(node, "at")
        if not reference or at is None:
            continue
        back = sexpr.atom(sexpr.child(node, "layer")) == "B.Cu"
        fx, fy = sexpr.number(at, 1), sexpr.number(at, 2)
        rotation = sexpr.number(at, 3)
        for pad in sexpr.children(node, "pad"):
            dx, dy = _point(sexpr.child(pad, "at"))
            if back:
                dx = -dx
            positions[(reference, sexpr.atom(pad, 1))] = _transform(
                fx, fy, rotation, (dx, dy)
            )
    return positions


def compare(
    computed: dict[tuple[str, str], tuple[float, float]],
    oracle: dict[tuple[str, str], tuple[float, float]],
) -> list[tuple[str, str, float, float, float]]:
    rows = []
    for key, (x, y) in computed.items():
        if key not in oracle:
            continue
        ox, oy = oracle[key]
        rows.append((key[0], key[1], x - ox, y - oy, math.hypot(x - ox, y - oy)))
    rows.sort(key=lambda row: row[4], reverse=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", nargs="?", default=DEFAULT_BOARD, type=Path)
    args = parser.parse_args()
    board = args.board.resolve()

    geometry = read_board_geometry(board)
    computed = {
        (footprint.reference, pad.number): (pad.x, pad.y)
        for footprint in geometry.footprints
        for pad in footprint.pads
    }
    sides = {
        footprint.reference: footprint.side for footprint in geometry.footprints
    }

    with tempfile.TemporaryDirectory(prefix="pcbforge-geometry-") as temporary:
        oracle = read_oracle(export_ipcd356(board, Path(temporary)))

    version = subprocess.run(
        [str(KICAD_CLI), "version"], capture_output=True, text=True
    ).stdout.strip()

    rows = compare(computed, oracle)
    front = [row for row in rows if sides.get(row[0]) == "front"]
    back = [row for row in rows if sides.get(row[0]) == "back"]
    mirrored = compare(mirrored_positions(board), oracle)
    matched = sum(1 for row in rows if row[4] <= IPC_QUANTUM_MM)

    print("board:            ", board.relative_to(REPO_ROOT))
    print("kicad-cli:        ", version)
    print("checked:          ", datetime.date.today().isoformat())
    back_side = sum(1 for side in sides.values() if side == "back")
    print(f"footprints:        {len(geometry.footprints)} ({back_side} on the back)")
    # The oracle omits pads with no net, so fewer are compared than computed.
    print(f"pads compared:     {len(rows)} of {len(computed)} computed")
    print(f"within one quantum:{matched} ({IPC_QUANTUM_MM} mm)")
    print(f"worst error:       {rows[0][4]:.4f} mm" if rows else "worst error: n/a")
    print(f"  front-side:      {front[0][4]:.4f} mm" if front else "  front-side: n/a")
    print(f"  back-side:       {back[0][4]:.4f} mm" if back else "  back-side: n/a")
    print()
    print("ten worst pads (reference, pad, dx, dy, error) in mm:")
    for reference, pad, dx, dy, error in rows[:10]:
        print(f"  {reference:6} {pad:4} {dx:+.5f} {dy:+.5f} {error:.5f}")
    print()
    print("rejected hypothesis - x mirror applied to back-side offsets:")
    print(f"  worst error:     {mirrored[0][4]:.4f} mm" if mirrored else "  n/a")

    if not rows:
        print("\nFAIL: no pads compared", file=sys.stderr)
        return 1
    if rows[0][4] > IPC_QUANTUM_MM:
        print(
            f"\nFAIL: worst error {rows[0][4]:.4f} mm exceeds one IPC quantum",
            file=sys.stderr,
        )
        return 1
    print("\nPASS: every pad agrees with KiCad within one IPC quantum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
