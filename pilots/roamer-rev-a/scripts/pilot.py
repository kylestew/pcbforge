#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from board_fingerprint import fingerprint


PILOT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PILOT_ROOT / "baseline" / "source"
BOARD_DIR = SOURCE_ROOT / "hardware" / "driver-board"
IOC_PATH = SOURCE_ROOT / "firmware" / "roamer_rev_a.ioc"
RESULTS_DIR = PILOT_ROOT / "results"
GENERATED_DIR = RESULTS_DIR / "generated"
COMPAT_PROJECT = PILOT_ROOT / "compat" / "project"
COMPAT_BOARD = COMPAT_PROJECT / "layouts" / "default" / "default.kicad_pcb"
KICAD_CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
SOURCE_TAG = "rev-a-jlcpcb-2026-07-18"
SOURCE_COMMIT = "3bc3361573dd21070efe9f76bba473947e2a0c21"


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
        pass
    return payload


def parse_ioc(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key] = value
    return values


def parse_netlist(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    components: dict[str, dict[str, Any]] = {}

    for element in root.findall("./components/comp"):
        reference = element.attrib["ref"]
        fields = {
            field.attrib["name"]: field.text or ""
            for field in element.findall("./fields/field")
        }
        components[reference] = {
            "reference": reference,
            "value": element.findtext("value", default=""),
            "footprint": element.findtext("footprint", default=""),
            "fields": dict(sorted(fields.items())),
            "pins": {},
        }

    nets: list[dict[str, Any]] = []
    for element in root.findall("./nets/net"):
        nodes: list[dict[str, str]] = []
        for node in element.findall("node"):
            data = dict(sorted(node.attrib.items()))
            nodes.append(data)
            reference = data["ref"]
            pin = data["pin"]
            if reference in components:
                components[reference]["pins"][pin] = element.attrib["name"]
        nets.append(
            {
                "code": int(element.attrib["code"]),
                "name": element.attrib["name"],
                "class": element.attrib.get("class", ""),
                "nodes": sorted(
                    nodes,
                    key=lambda item: (item.get("ref", ""), item.get("pin", "")),
                ),
            }
        )

    for component in components.values():
        component["pins"] = dict(
            sorted(component["pins"].items(), key=lambda item: natural_pin_key(item[0]))
        )

    return {
        "components": [
            components[key] for key in sorted(components, key=natural_reference_key)
        ],
        "nets": sorted(nets, key=lambda item: item["code"]),
        "statistics": {
            "components": len(components),
            "nets": len(nets),
            "connected_nets": sum(
                not item["name"].startswith("unconnected-") for item in nets
            ),
            "explicit_no_connect_nets": sum(
                item["name"].startswith("unconnected-") for item in nets
            ),
        },
    }


def natural_reference_key(reference: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([^0-9]+)([0-9]+)(.*)", reference)
    if not match:
        return (reference, -1, "")
    return (match.group(1), int(match.group(2)), match.group(3))


def natural_pin_key(pin: str) -> tuple[int, int | str, str]:
    match = re.fullmatch(r"([A-Za-z]*)([0-9]+)(.*)", pin)
    if not match:
        return (1, pin, "")
    return (0, int(match.group(2)), f"{match.group(1)}{match.group(3)}")


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return [dict(row) for row in csv.DictReader(stream)]


def baseline() -> None:
    if not KICAD_CLI.exists():
        raise PilotFailure(f"KiCad CLI not found at {KICAD_CLI}")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    netlist_path = GENERATED_DIR / "rev-a.netlist.xml"
    erc_path = GENERATED_DIR / "rev-a.erc.json"
    board_path = BOARD_DIR / "driver-board.kicad_pcb"
    with tempfile.TemporaryDirectory(
        prefix="baseline-kicad-", dir=GENERATED_DIR
    ) as temporary:
        working_board_dir = Path(temporary) / "driver-board"
        shutil.copytree(BOARD_DIR, working_board_dir)
        schematic_path = working_board_dir / "driver-board.kicad_sch"

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
                schematic_path,
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
                schematic_path,
            ]
        )
    graph = parse_netlist(netlist_path)
    write_json(RESULTS_DIR / "baseline-graph.json", graph)
    write_json(RESULTS_DIR / "baseline-board.json", fingerprint_board(board_path))

    files = {
        str(path.relative_to(SOURCE_ROOT)): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(SOURCE_ROOT.rglob("*"))
        if path.is_file()
    }
    ioc = parse_ioc(IOC_PATH)
    component_map = {
        component["reference"]: component for component in graph["components"]
    }
    mcu = component_map["U1"]
    expected_ioc_mismatches = {
        "PB10": {
            "ioc_signal": ioc.get("PB10.Signal"),
            "ioc_label": ioc.get("PB10.GPIO_Label"),
            "released_net": mcu["pins"].get("21"),
        },
        "PB11": {
            "ioc_signal": ioc.get("PB11.Signal"),
            "ioc_label": ioc.get("PB11.GPIO_Label"),
            "released_net": mcu["pins"].get("22"),
        },
    }
    erc_report = json.loads(erc_path.read_text(encoding="utf-8"))
    erc_report.pop("date", None)
    canonical_erc = json.dumps(
        erc_report, sort_keys=True, separators=(",", ":")
    ).encode()

    manifest = {
        "source": {
            "repository": "/Users/kylestewart/Projects/roamer-bot",
            "tag": SOURCE_TAG,
            "commit": SOURCE_COMMIT,
        },
        "toolchain": {
            "kicad": run([KICAD_CLI, "--version"]).stdout.strip(),
            "board_format": re.search(
                r"\(version ([0-9]+)\)", board_path.read_text(encoding="utf-8")
            ).group(1),
        },
        "statistics": graph["statistics"],
        "manufacturing": {
            "bom_rows": len(
                csv_rows(BOARD_DIR / "mfr" / "driver-board-jlc-bom.csv")
            ),
            "cpl_rows": len(
                csv_rows(BOARD_DIR / "mfr" / "driver-board-jlc-cpl.csv")
            ),
        },
        "expected_ioc_mismatches": expected_ioc_mismatches,
        "erc": {
            "returncode": erc_result.returncode,
            "stdout": erc_result.stdout.strip(),
            "violations": len(erc_report.get("violations", [])),
            "ignored_checks": len(erc_report.get("ignored_checks", [])),
            "canonical_report_sha256": hashlib.sha256(canonical_erc).hexdigest(),
        },
        "files": files,
    }
    write_json(RESULTS_DIR / "baseline-manifest.json", manifest)

    if graph["statistics"]["components"] != 69:
        raise PilotFailure("expected 69 released schematic components")
    if graph["statistics"]["nets"] != 67:
        raise PilotFailure("expected 67 released nets")
    for pin, mismatch in expected_ioc_mismatches.items():
        if not str(mismatch["released_net"]).startswith("unconnected-"):
            raise PilotFailure(f"expected released {pin} to be explicitly unconnected")
        if not str(mismatch["ioc_signal"]).startswith("I2C2_"):
            raise PilotFailure(f"expected tagged .ioc {pin} to carry stale I2C2")

    print(
        "baseline captured: "
        f"{graph['statistics']['components']} components, "
        f"{graph['statistics']['nets']} nets; "
        "PB10/PB11 .ioc mismatch confirmed"
    )


def bootstrap_check() -> None:
    ato = PILOT_ROOT / "scripts" / "ato"
    result = run([ato, "--version"])
    version = result.stdout.strip().splitlines()[-1]
    if version != "0.15.7":
        raise PilotFailure(f"expected atopile 0.15.7, got {version}")
    print(f"atopile {version}; pilot-local wrapper operational")


def compatibility() -> None:
    if not COMPAT_BOARD.exists():
        raise PilotFailure(f"compatibility board missing: {COMPAT_BOARD}")

    board_text = COMPAT_BOARD.read_text(encoding="utf-8")
    if "(version 20260206)" not in board_text:
        raise PilotFailure("compatibility board is not a KiCad 10 format fixture")
    if '(net "2")' not in board_text:
        raise PilotFailure("compatibility board lacks the KiCad 10 named-net sentinel")

    before = fingerprint_board(COMPAT_BOARD)
    write_json(RESULTS_DIR / "compat-before.json", before)

    render_path = GENERATED_DIR / "compat-kicad10-before.png"
    render_result = run(
        [
            KICAD_CLI,
            "pcb",
            "render",
            "--output",
            render_path,
            COMPAT_BOARD,
        ],
        check=False,
    )
    if render_result.returncode:
        raise PilotFailure(
            "KiCad 10 rejected the compatibility fixture:\n"
            f"{render_result.stdout}{render_result.stderr}"
        )

    ato = PILOT_ROOT / "scripts" / "ato"
    command = [
        ato,
        "build",
        COMPAT_PROJECT,
        "--keep-picked-parts",
        "--keep-net-names",
        "--keep-designators",
    ]
    build_result = run(command, check=False)
    combined_output = build_result.stdout + build_result.stderr

    after = fingerprint_board(COMPAT_BOARD)
    after_result = (
        RESULTS_DIR / "compat-after-failed-sync.json"
        if build_result.returncode
        else RESULTS_DIR / "compat-after-sync.json"
    )
    write_json(after_result, after)

    expected_error = (
        "UnexpectedType in kicad.pcb.Pad field 'number'"
        in combined_output
        and 'got string "2" but expected unquoted number' in combined_output
    )
    if expected_error:
        captured_output = (
            "command: scripts/ato build compat/project "
            "--keep-picked-parts --keep-net-names --keep-designators\n"
            f"returncode: {build_result.returncode}\n"
            "stage: Loading PCB\n"
            "error: UnexpectedType in kicad.pcb.Pad field 'number': "
            'got string "2" but expected unquoted number\n'
            'source: (net "2")\n'
        )
    else:
        captured_output = combined_output
    (RESULTS_DIR / "compatibility-build.txt").write_text(
        captured_output,
        encoding="utf-8",
    )
    unchanged = {
        "whole_file": before["sha256"] == after["sha256"],
        "user_art": before["user_art"] == after["user_art"],
        "footprint_placements": (
            before["footprint_placements"] == after["footprint_placements"]
        ),
    }
    after_render_returncode: int | None = None
    if not build_result.returncode:
        after_render = run(
            [
                KICAD_CLI,
                "pcb",
                "render",
                "--output",
                GENERATED_DIR / "compat-kicad10-after.png",
                COMPAT_BOARD,
            ],
            check=False,
        )
        after_render_returncode = after_render.returncode
    result = {
        "status": "blocked" if build_result.returncode else "passed",
        "decision": (
            "stop-before-full-port"
            if build_result.returncode
            else "continue-to-full-port"
        ),
        "toolchain": {
            "atopile": "0.15.7",
            "kicad": run([KICAD_CLI, "--version"]).stdout.strip(),
            "fixture_board_format": "20260206",
        },
        "fixture": {
            "kicad_10_rendered_before_sync": True,
            "kicad_10_rendered_after_sync": (
                after_render_returncode == 0
                if after_render_returncode is not None
                else None
            ),
            "sha256_before": before["sha256"],
            "sha256_after": after["sha256"],
            "user_art_items": before["user_art"]["count"],
            "footprints": len(before["footprint_placements"]),
        },
        "sync": {
            "returncode": build_result.returncode,
            "expected_parser_failure_matched": expected_error,
            "input_unchanged_after_failure": unchanged,
        },
    }
    write_json(RESULTS_DIR / "compatibility-result.json", result)

    if build_result.returncode:
        if not expected_error:
            raise PilotFailure(
                "atopile rejected the KiCad 10 fixture for an unexpected reason; "
                "see results/compatibility-build.txt"
            )
        if not all(unchanged.values()):
            raise PilotFailure(
                "atopile rejected the KiCad 10 fixture and mutated it before failure"
            )
        raise PilotFailure(
            "compatibility gate blocked: atopile 0.15.7 expects KiCad 9 numeric "
            "net IDs and cannot parse KiCad 10 named-net pads; input remained "
            "byte-identical"
        )

    if after_render_returncode:
        raise PilotFailure("KiCad 10 rejected the board after synchronization")
    if not all(unchanged.values()):
        raise PilotFailure("no-op synchronization changed user-owned PCB data")
    print("compatibility gate passed: KiCad 10 layout survived no-op synchronization")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Roamer Rev A pilot")
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
