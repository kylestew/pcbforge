#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from board_fingerprint import fingerprint


PILOT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PILOT_ROOT / "baseline" / "source"
BOARD_PATH = SOURCE_ROOT / "multichannel_mixer.kicad_pcb"
SCHEMATIC_PATH = SOURCE_ROOT / "multichannel_mixer.kicad_sch"
RESULTS_DIR = PILOT_ROOT / "results"
GENERATED_DIR = RESULTS_DIR / "generated"
KICAD_CLI = Path("/Applications/KiCad 9/KiCad.app/Contents/MacOS/kicad-cli")
SOURCE_DISTRIBUTION = "KiCad 9.0.9 universal macOS image, multichannel demo"
SOURCE_URL = (
    "https://downloads.kicad.org/kicad/macos/explore/stable/download/"
    "kicad-unified-universal-9.0.9.dmg"
)
SOURCE_BOARD_SHA256 = (
    "44b7c119a0d05c98e9f294659879184038a5f6c30073c04d9536990429ca413e"
)
KICAD_9_BOARD_FORMAT = "20241229"
TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')
NUMBER_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
BARE_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.~/-]*$")


class PilotFailure(RuntimeError):
    pass


def run(
    args: list[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise PilotFailure(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fingerprint_board(path: Path) -> dict[str, Any]:
    payload = fingerprint(path)
    try:
        payload["path"] = str(path.relative_to(PILOT_ROOT))
    except ValueError:
        payload["path"] = str(path)
    return payload


def normalized_token_signature(path: Path) -> dict[str, Any]:
    def normalize(token: str) -> str:
        if NUMBER_RE.fullmatch(token):
            return str(Decimal(token).normalize())
        if (
            len(token) > 1
            and token.startswith('"')
            and token.endswith('"')
            and BARE_SYMBOL_RE.fullmatch(token[1:-1])
        ):
            return token[1:-1]
        return token

    counts = Counter(
        normalize(token)
        for token in TOKEN_RE.findall(path.read_text(encoding="utf-8"))
    )
    encoded = json.dumps(
        sorted(counts.items()),
        separators=(",", ":"),
    ).encode()
    return {
        "tokens": sum(counts.values()),
        "distinct_tokens": len(counts),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def parse_netlist(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    components = [
        {
            "reference": element.attrib["ref"],
            "value": element.findtext("value", default=""),
            "footprint": element.findtext("footprint", default=""),
        }
        for element in root.findall("./components/comp")
    ]
    nets = [
        {
            "code": int(element.attrib["code"]),
            "name": element.attrib["name"],
            "nodes": sorted(
                (dict(sorted(node.attrib.items())) for node in element.findall("node")),
                key=lambda item: (item.get("ref", ""), item.get("pin", "")),
            ),
        }
        for element in root.findall("./nets/net")
    ]
    return {
        "components": sorted(components, key=lambda item: item["reference"]),
        "nets": sorted(nets, key=lambda item: item["code"]),
        "statistics": {
            "components": len(components),
            "nets": len(nets),
            "connected_pins": sum(len(net["nodes"]) for net in nets),
        },
    }


def canonical_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    report = json.loads(path.read_text(encoding="utf-8"))
    report.pop("date", None)
    violations = report.get("violations")
    if violations is None:
        violations = [
            violation
            for sheet in report.get("sheets", [])
            for violation in sheet.get("violations", [])
        ]
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    return {
        "exists": True,
        "violations": len(violations),
        "unconnected_items": len(report.get("unconnected_items", [])),
        "ignored_checks": len(report.get("ignored_checks", [])),
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def relevant_stderr(output: str) -> str:
    return "\n".join(
        line
        for line in output.splitlines()
        if not line.startswith("Fontconfig warning:")
    ).strip()


def verify_inputs() -> None:
    if not BOARD_PATH.exists() or not SCHEMATIC_PATH.exists():
        raise PilotFailure("captured KiCad source fixture is incomplete")
    if sha256_file(BOARD_PATH) != SOURCE_BOARD_SHA256:
        raise PilotFailure("captured board differs from the selected KiCad demo")
    board_text = BOARD_PATH.read_text(encoding="utf-8")
    if f"(version {KICAD_9_BOARD_FORMAT})" not in board_text:
        raise PilotFailure("captured board is not KiCad 9 format")
    if '(generator_version "9.0")' not in board_text:
        raise PilotFailure("captured board was not generated by KiCad 9")


def verify_kicad() -> str:
    if not KICAD_CLI.exists():
        raise PilotFailure(f"KiCad 9 CLI not found at {KICAD_CLI}")
    version = run([KICAD_CLI, "--version"]).stdout.strip()
    match = re.search(r"\b9\.0(?:\.\d+)?\b", version)
    if not match:
        raise PilotFailure(f"expected KiCad 9.0.x, got {version!r}")
    return version


def bootstrap_check() -> None:
    ato = PILOT_ROOT / "scripts" / "ato"
    result = run([ato, "--version"])
    version = result.stdout.strip().splitlines()[-1]
    if version != "0.15.7":
        raise PilotFailure(f"expected atopile 0.15.7, got {version}")
    kicad_version = verify_kicad()
    print(f"atopile {version}; KiCad {kicad_version}; isolated wrappers operational")


def baseline() -> None:
    verify_inputs()
    kicad_version = verify_kicad()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    netlist_path = GENERATED_DIR / "multichannel.netlist.xml"
    erc_path = GENERATED_DIR / "multichannel.erc.json"
    drc_path = GENERATED_DIR / "multichannel.drc.json"
    render_path = GENERATED_DIR / "multichannel.png"

    with tempfile.TemporaryDirectory(
        prefix="baseline-kicad9-", dir=GENERATED_DIR
    ) as temporary:
        working_source = Path(temporary) / "source"
        shutil.copytree(SOURCE_ROOT, working_source)
        working_schematic = working_source / SCHEMATIC_PATH.name
        working_board = working_source / BOARD_PATH.name

        run(
            [
                KICAD_CLI,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                netlist_path,
                working_schematic,
            ]
        )
        erc_result = run(
            [
                KICAD_CLI,
                "sch",
                "erc",
                "--severity-all",
                "--format",
                "json",
                "--output",
                erc_path,
                working_schematic,
            ],
            check=False,
        )
        drc_result = run(
            [
                KICAD_CLI,
                "pcb",
                "drc",
                "--severity-all",
                "--format",
                "json",
                "--output",
                drc_path,
                working_board,
            ],
            check=False,
        )
        render_result = run(
            [
                KICAD_CLI,
                "pcb",
                "render",
                "--output",
                render_path,
                working_board,
            ],
            check=False,
        )

    graph = parse_netlist(netlist_path)
    board = fingerprint_board(BOARD_PATH)
    write_json(RESULTS_DIR / "baseline-graph.json", graph)
    write_json(RESULTS_DIR / "baseline-board.json", board)

    files = {
        str(path.relative_to(SOURCE_ROOT)): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(SOURCE_ROOT.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "source": {
            "distribution": SOURCE_DISTRIBUTION,
            "url": SOURCE_URL,
            "project": "multichannel_mixer",
        },
        "toolchain": {
            "kicad": kicad_version,
            "board_format": KICAD_9_BOARD_FORMAT,
        },
        "statistics": {
            **graph["statistics"],
            "board_footprints": board["categories"]["footprint"]["count"],
            "board_nets": board["categories"]["net"]["count"],
            "segments": board["categories"]["segment"]["count"],
            "vias": board["categories"]["via"]["count"],
            "zones": board["categories"]["zone"]["count"],
        },
        "erc": {
            "returncode": erc_result.returncode,
            "stdout": erc_result.stdout.strip(),
            "stderr": relevant_stderr(erc_result.stderr),
            **canonical_report(erc_path),
        },
        "drc": {
            "returncode": drc_result.returncode,
            "stdout": drc_result.stdout.strip(),
            "stderr": relevant_stderr(drc_result.stderr),
            **canonical_report(drc_path),
        },
        "render": {
            "returncode": render_result.returncode,
            "stdout": render_result.stdout.strip(),
            "stderr": render_result.stderr.strip(),
            "exists": render_path.exists(),
        },
        "files": files,
    }
    write_json(RESULTS_DIR / "baseline-manifest.json", manifest)

    expected_board_counts = {
        "footprint": 114,
        "net": 81,
        "segment": 576,
        "via": 29,
        "zone": 6,
    }
    actual_board_counts = {
        name: board["categories"][name]["count"] for name in expected_board_counts
    }
    if actual_board_counts != expected_board_counts:
        raise PilotFailure(
            f"board fixture counts changed: expected {expected_board_counts}, "
            f"got {actual_board_counts}"
        )
    if not render_path.exists() or render_result.returncode:
        raise PilotFailure("KiCad 9 could not render the captured board")

    print(
        "baseline captured: "
        f"{graph['statistics']['components']} schematic components, "
        f"{graph['statistics']['nets']} schematic nets; "
        "114 footprints, 576 segments, 29 vias, 6 zones"
    )


def compatibility() -> None:
    verify_inputs()
    kicad_version = verify_kicad()

    from faebryk.libs.eda.hl.convert.pcb_netlist import convert_pcb_to_netlist
    from faebryk.libs.eda.kicad.convert.pcb.il_hl import convert_pcb_il_to_hl
    from faebryk.libs.kicad.fileformats import kicad

    before = fingerprint_board(BOARD_PATH)
    parsed_file = kicad.loads(kicad.pcb.PcbFile, BOARD_PATH)
    parsed = parsed_file.kicad_pcb
    high_level = convert_pcb_il_to_hl(parsed)
    reconstructed_netlist = convert_pcb_to_netlist(high_level)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    roundtrip_path = GENERATED_DIR / "multichannel.atopile-roundtrip.kicad_pcb"
    kicad.dumps(parsed_file, roundtrip_path)
    after = fingerprint_board(roundtrip_path)

    reparsed_file = kicad.loads(kicad.pcb.PcbFile, roundtrip_path)
    stable_path = GENERATED_DIR / "multichannel.atopile-roundtrip-2.kicad_pcb"
    kicad.dumps(reparsed_file, stable_path)

    source_token_signature = normalized_token_signature(BOARD_PATH)
    serialized_token_signature = normalized_token_signature(roundtrip_path)
    render_path = GENERATED_DIR / "multichannel-atopile-roundtrip.png"
    render_result = run(
        [
            KICAD_CLI,
            "pcb",
            "render",
            "--output",
            render_path,
            roundtrip_path,
        ],
        check=False,
    )

    preservation = {
        "whole_file_byte_identical": before["sha256"] == after["sha256"],
        "normalized_token_multiset_identical": (
            source_token_signature == serialized_token_signature
        ),
        "serializer_canonical_form_byte_stable": (
            sha256_file(roundtrip_path) == sha256_file(stable_path)
        ),
        "footprint_placements_identical": (
            before["footprint_placements"] == after["footprint_placements"]
        ),
        "user_art_identical": before["user_art"] == after["user_art"],
        "category_counts_identical": {
            name: before["categories"].get(name, {}).get("count")
            == after["categories"].get(name, {}).get("count")
            for name in sorted(set(before["categories"]) | set(after["categories"]))
        },
        "category_content_identical": {
            name: before["categories"].get(name)
            == after["categories"].get(name)
            for name in sorted(set(before["categories"]) | set(after["categories"]))
        },
    }
    result = {
        "status": "passed",
        "toolchain": {
            "atopile": "0.15.7",
            "kicad": kicad_version,
            "board_format": KICAD_9_BOARD_FORMAT,
        },
        "parser": {
            "footprints": len(parsed.footprints),
            "nets": len(parsed.nets),
            "segments": len(parsed.segments),
            "vias": len(parsed.vias),
            "zones": len(parsed.zones),
        },
        "projection": {
            "high_level_collections": len(high_level.collections),
            "reconstructed_nets": len(reconstructed_netlist.nets),
        },
        "roundtrip": {
            "render_returncode": render_result.returncode,
            "render_exists": render_path.exists(),
            "source_sha256": before["sha256"],
            "serialized_sha256": after["sha256"],
            "stable_reserialization_sha256": sha256_file(stable_path),
            "source_token_signature": source_token_signature,
            "serialized_token_signature": serialized_token_signature,
            "preservation": preservation,
        },
        "scope": {
            "reader_writer_gate": True,
            "atopile_managed_sync": False,
            "reason": (
                "The legacy board has no atopile ownership metadata; managed "
                "no-op sync requires the circuit port."
            ),
        },
    }

    critical_categories = ("segment", "via", "zone")
    failures: list[str] = []
    if len(parsed.footprints) != 114:
        failures.append("parser did not load all 114 footprints")
    if len(parsed.nets) != 81:
        failures.append("parser did not load all 81 PCB nets")
    if (
        len(parsed.segments) != 576
        or len(parsed.vias) != 29
        or len(parsed.zones) != 6
    ):
        failures.append("parser did not retain all routed board objects")
    if len(high_level.collections) != 114:
        failures.append("high-level projection did not retain all footprints")
    if not reconstructed_netlist.nets:
        failures.append("high-level projection reconstructed no nets")
    if render_result.returncode or not render_path.exists():
        failures.append("KiCad 9 rejected atopile's serialized board")
    if not preservation["footprint_placements_identical"]:
        failures.append("footprint placements changed during round trip")
    if not preservation["normalized_token_multiset_identical"]:
        failures.append("normalized PCB tokens changed during round trip")
    if not preservation["serializer_canonical_form_byte_stable"]:
        failures.append("atopile serializer did not reach a stable canonical form")
    for category in critical_categories:
        if not preservation["category_content_identical"].get(category, False):
            failures.append(f"{category} content changed during round trip")

    if failures:
        result["status"] = "failed"
        result["failures"] = failures
    write_json(RESULTS_DIR / "compatibility-result.json", result)
    write_json(RESULTS_DIR / "compatibility-before.json", before)
    write_json(RESULTS_DIR / "compatibility-after.json", after)

    if failures:
        raise PilotFailure("; ".join(failures))
    print(
        "compatibility gate passed: atopile parsed, projected, reconstructed, "
        "serialized, and preserved the complete KiCad 9 board"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the KiCad 9 mixer pilot")
    parser.add_argument(
        "command",
        choices=["bootstrap-check", "baseline", "compatibility", "all"],
    )
    args = parser.parse_args()

    try:
        if args.command == "bootstrap-check":
            bootstrap_check()
        elif args.command == "baseline":
            baseline()
        elif args.command == "compatibility":
            compatibility()
        elif args.command == "all":
            bootstrap_check()
            baseline()
            compatibility()
    except PilotFailure as error:
        print(f"PILOT FAILURE: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
