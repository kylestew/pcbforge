#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from board_fingerprint import fingerprint
from faebryk.libs.kicad.fileformats import kicad


VERSION_RE = re.compile(r"\(version\s+(\d+)\)")
FEATURES = (
    "teardrops",
    "aux_axis_origin",
    "grid_origin",
    "solder_mask_min_width",
    "solder_mask_margin",
    "clearance",
)


def feature_counts(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    return {feature: text.count(f"({feature}") for feature in FEATURES}


def evaluate(path: Path, temporary: Path, root: Path) -> dict[str, object]:
    before = fingerprint(path)
    parsed_file = kicad.loads(kicad.pcb.PcbFile, path)
    parsed = parsed_file.kicad_pcb
    output = temporary / f"{len(list(temporary.iterdir())):03d}.kicad_pcb"
    kicad.dumps(parsed_file, output)
    after = fingerprint(output)
    before_features = feature_counts(path)
    after_features = feature_counts(output)
    categories = sorted(set(before["categories"]) | set(after["categories"]))
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "footprints": len(parsed.footprints),
        "nets": len(parsed.nets),
        "segments": len(parsed.segments),
        "vias": len(parsed.vias),
        "zones": len(parsed.zones),
        "byte_identical": before["sha256"] == after["sha256"],
        "placements_identical": (
            before["footprint_placements"] == after["footprint_placements"]
        ),
        "changed_categories": [
            name
            for name in categories
            if before["categories"].get(name) != after["categories"].get(name)
        ],
        "lost_feature_counts": {
            feature: before_features[feature] - after_features[feature]
            for feature in FEATURES
            if before_features[feature] != after_features[feature]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="atopile-kicad9-candidates-") as raw:
        temporary = Path(raw)
        for path in sorted(args.root.rglob("*.kicad_pcb")):
            match = VERSION_RE.search(path.read_text(encoding="utf-8"))
            if match is None or match.group(1) != "20241229":
                continue
            try:
                results.append(evaluate(path, temporary, args.root))
            except Exception as error:
                results.append(
                    {
                        "path": str(path.relative_to(args.root)),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"evaluated {len(results)} KiCad 9 boards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
