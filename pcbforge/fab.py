"""VERIFY-to-ORDER fabrication packet generation and validation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.artifact_hash import ArtifactHashError, semantic_bom_sha256
from pcbforge.build_test import (
    BuildTestError,
    BuildTestInputError,
    BoardEvidence,
    read_board_evidence,
    schematic_tamper_message,
    read_bom_components,
    read_build_test_contract,
    require_current_acceptance,
)
from pcbforge.initialize import InitInputError, read_spec
from pcbforge.fsutil import AtomicWriteError, commit_outputs, remove_paths
from pcbforge.placement import PlacementError, read_rules_profile

FAB_SCHEMA = 1
FAB_DIRNAME = "fab"
MANIFEST_FILENAME = "manifest.json"
JLC_BOM_FILENAME = "jlc-bom.csv"
JLC_CPL_FILENAME = "jlc-cpl.csv"
DRC_REPORT_FILENAME = "drc-report.json"
POS_FILENAME = "position.csv"
BOM_CSV_FILENAME = "compiler-bom.csv"
ARCHIVE_SUFFIX = "-fab.zip"
KEEP_FILENAME = ".gitkeep"

LAYERS_BY_COUNT = {
    2: (
        "F.Cu",
        "B.Cu",
        "F.Paste",
        "B.Paste",
        "F.Silkscreen",
        "B.Silkscreen",
        "F.Mask",
        "B.Mask",
        "Edge.Cuts",
    ),
    4: (
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
        "F.Paste",
        "B.Paste",
        "F.Silkscreen",
        "B.Silkscreen",
        "F.Mask",
        "B.Mask",
        "Edge.Cuts",
    ),
}

# KiCad stamps wall-clock time into every plot; SOURCE_DATE_EPOCH is ignored
# (verified against 9.0.9). These markers are dropped before hashing so that
# regenerating an unchanged board is provably the same fabrication data.
TIMESTAMP_MARKERS = (
    "TF.CreationDate",
    "Created by KiCad",
    "DRILL file {KiCad",
)
DESIGNATOR_RE = re.compile(r"^([A-Za-z_]+)(\d*)$")

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class FabError(RuntimeError):
    """Fabrication packet generation or validation failed."""


class FabInputError(FabError):
    """The project is not in a state that can produce a packet."""


@dataclass(frozen=True)
class FabFile:
    name: str
    sha256: str
    normalized_sha256: str
    size: int


@dataclass(frozen=True)
class FabResult:
    project_dir: Path
    project_name: str
    files: tuple[FabFile, ...]
    archive_name: str
    layers: tuple[str, ...]
    component_count: int
    placement_count: int
    recorded: bool

    @property
    def summary(self) -> str:
        return (
            f"{len(self.files)} packet files, {len(self.layers)} plotted layers, "
            f"{self.component_count} BOM lines, "
            f"{self.placement_count} placements"
        )


def fab_dir(project_dir: Path) -> Path:
    return project_dir / FAB_DIRNAME


def archive_name(project_name: str) -> str:
    return f"{project_name}{ARCHIVE_SUFFIX}"


def _tool_root(tool_root: Path | None) -> Path:
    return (tool_root or Path(__file__).resolve().parents[1]).resolve()


def _read_pins(project_dir: Path) -> Mapping[str, Any]:
    path = project_dir / ".pcbforge"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FabInputError("missing .pcbforge project pin") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise FabInputError(f"invalid .pcbforge: {exc}") from exc
    if not isinstance(data, dict):
        raise FabInputError(".pcbforge must be a mapping")
    for key in ("toolchain", "rules"):
        if not isinstance(data.get(key), dict):
            raise FabInputError(f".pcbforge {key}: expected a mapping")
    return data


def _spec(project_dir: Path) -> Any:
    try:
        return read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise FabInputError(str(exc)) from exc


def _board_evidence(path: Path) -> BoardEvidence:
    try:
        board = read_board_evidence(path)
    except (BuildTestInputError, BuildTestError) as exc:
        raise FabInputError(str(exc)) from exc
    tamper = schematic_tamper_message(board, path.name)
    if tamper:
        raise FabInputError(tamper)
    return board


def _layers(layer_count: int) -> tuple[str, ...]:
    try:
        return LAYERS_BY_COUNT[layer_count]
    except KeyError as exc:
        raise FabInputError(
            f"unsupported layer count {layer_count}; expected 2 or 4"
        ) from exc


def _require_verify_complete(project_dir: Path) -> None:
    from pcbforge.status import (
        StatusError,
        by_phase_complete,
        inspect_status,
        read_status_document,
    )

    try:
        document = read_status_document(project_dir)
        report = inspect_status(project_dir, document=document)
    except StatusError as exc:
        raise FabInputError(f"cannot read project status: {exc}") from exc
    if not by_phase_complete(report.phases, "verify"):
        raise FabInputError(
            "cannot generate the fabrication packet before VERIFY is approved "
            "and current"
        )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: CommandRunner,
    purpose: str,
) -> None:
    try:
        completed = runner(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise FabError(f"{purpose} could not start: {exc}") from exc
    if completed.returncode:
        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        detail = output.splitlines()[-1] if output else f"exit {completed.returncode}"
        raise FabError(f"{purpose} failed: {detail}")


def _normalized_bytes(name: str, data: bytes) -> bytes:
    """Strip KiCad's wall-clock stamps so equal boards hash equal."""
    if name.endswith(".gbrjob") or name.endswith(".json"):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return data
        if isinstance(payload, dict):
            payload.pop("date", None)
            general = payload.get("GeneralSpecs")
            if isinstance(general, dict):
                general.pop("CreationDate", None)
        return json.dumps(payload, indent=2, sort_keys=True).encode()
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return data
    kept = [
        line
        for line in text.splitlines(keepends=True)
        if not any(marker in line for marker in TIMESTAMP_MARKERS)
    ]
    return "".join(kept).encode()


def _fab_file(name: str, data: bytes) -> FabFile:
    return FabFile(
        name,
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(_normalized_bytes(name, data)).hexdigest(),
        len(data),
    )


def _designator_key(designator: str) -> tuple[str, int, str]:
    match = DESIGNATOR_RE.match(designator)
    if match is None:
        return (designator, 0, designator)
    prefix, number = match.groups()
    return (prefix, int(number) if number else 0, designator)


def _csv_text(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _short_footprint(footprint: str) -> str:
    return footprint.split(":", 1)[-1]


def render_jlc_bom(components: Sequence[Any]) -> str:
    """Render the JLC assembly BOM from validated compiler components."""
    rows = [
        (
            component.mpn,
            ",".join(sorted(component.designators, key=_designator_key)),
            _short_footprint(component.footprint),
            component.lcsc,
        )
        for component in sorted(components, key=lambda item: item.lcsc)
    ]
    return _csv_text(
        ("Comment", "Designator", "Footprint", "LCSC Part #"),
        rows,
    )


def _read_position_rows(path: Path) -> tuple[dict[str, str], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FabError(f"cannot read the exported position file: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"Ref", "PosX", "PosY", "Rot", "Side"}
    missing = sorted(required - set(reader.fieldnames or ()))
    if missing:
        raise FabError(
            f"position file is missing required columns: {', '.join(missing)}"
        )
    return tuple(dict(row) for row in reader)


def render_jlc_cpl(rows: Sequence[Mapping[str, str]]) -> str:
    """Render the JLC placement file from the exported position rows."""
    output = []
    for row in sorted(rows, key=lambda item: _designator_key(item["Ref"])):
        side = (row.get("Side") or "").strip().lower()
        if side not in {"top", "bottom"}:
            raise FabError(
                f"{row.get('Ref', '?')}: unsupported board side {side!r}"
            )
        try:
            values = tuple(
                f"{float(row[column]):.4f}" for column in ("PosX", "PosY", "Rot")
            )
        except (TypeError, ValueError) as exc:
            raise FabError(
                f"{row.get('Ref', '?')}: invalid placement coordinates"
            ) from exc
        output.append((row["Ref"], values[0], values[1], side, values[2]))
    return _csv_text(
        ("Designator", "Mid X", "Mid Y", "Layer", "Rotation"),
        output,
    )


def _gerber_name(stem: str, layer: str) -> str:
    return f"{stem}-{layer.replace('.', '_')}"


def _validate_packet(
    *,
    project_name: str,
    layers: Sequence[str],
    staged: Mapping[str, bytes],
    components: Sequence[Any],
    placements: Sequence[Mapping[str, str]],
    board: BoardEvidence,
) -> None:
    errors: list[str] = []
    for layer in layers:
        prefix = _gerber_name(project_name, layer) + "."
        matches = [name for name in staged if name.startswith(prefix)]
        if not matches:
            errors.append(f"missing plotted layer {layer}")
        elif not any(staged[name].strip() for name in matches):
            errors.append(f"plotted layer {layer} is empty")
    if not any(name.endswith(".drl") for name in staged):
        errors.append("no Excellon drill file was generated")

    expected_designators = {
        designator
        for component in components
        for designator in component.designators
    }
    board_references = set(board.references)
    unknown = sorted(expected_designators - board_references)
    if unknown:
        errors.append(
            f"BOM designators missing from the board: {', '.join(unknown)}"
        )
    for component in components:
        if len(component.designators) != component.quantity:
            errors.append(
                f"{component.lcsc}: {len(component.designators)} designators for "
                f"quantity {component.quantity}"
            )

    placed = {row["Ref"] for row in placements}
    stray = sorted(placed - board_references)
    if stray:
        errors.append(
            f"placement file references unknown parts: {', '.join(stray)}"
        )
    unplaced = sorted(expected_designators - placed)
    if unplaced:
        errors.append(
            "assembly parts missing from the placement file: "
            f"{', '.join(unplaced)}"
        )
    if errors:
        raise FabError(
            "fabrication packet failed validation; nothing was written:\n  - "
            + "\n  - ".join(errors)
        )


def _require_clean_drc(data: bytes) -> None:
    from pcbforge.status import _drc_report_status

    with tempfile.TemporaryDirectory(prefix="pcbforge-fab-drc-") as temporary:
        report = Path(temporary) / DRC_REPORT_FILENAME
        report.write_bytes(data)
        ok, summary = _drc_report_status(report)
    if not ok:
        raise FabError(
            f"the board no longer passes DRC ({summary}); VERIFY approval no "
            "longer describes this board — reopen VERIFY before ordering"
        )


def _archive_bytes(entries: Sequence[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _manifest_bytes(
    *,
    project_name: str,
    build: str,
    pins: Mapping[str, Any],
    rules_profile: str,
    layers: Sequence[str],
    board_sha256: str,
    bom_sha256: str,
    commands: Sequence[Sequence[str]],
    files: Sequence[FabFile],
) -> bytes:
    payload = {
        "pcbforge_fab_schema": FAB_SCHEMA,
        "project": project_name,
        "build": build,
        "toolchain": {
            "atopile": pins["toolchain"].get("atopile"),
            "kicad": pins["toolchain"].get("kicad"),
        },
        "rules_profile": rules_profile,
        "layers": list(layers),
        "sources": {
            "board_sha256": board_sha256,
            "bom_semantic_sha256": bom_sha256,
        },
        "commands": [list(command) for command in commands],
        "files": [
            {
                "name": item.name,
                "sha256": item.sha256,
                "normalized_sha256": item.normalized_sha256,
                "size": item.size,
            }
            for item in sorted(files, key=lambda item: item.name)
        ],
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _context(
    project_dir: Path,
    tool_root: Path | None,
) -> tuple[Any, Mapping[str, Any], str, tuple[str, ...], Path]:
    tool_root = _tool_root(tool_root)
    spec = _spec(project_dir)
    pins = _read_pins(project_dir)
    try:
        read_rules_profile(tool_root, spec.layers, pins)
    except PlacementError as exc:
        raise FabInputError(str(exc)) from exc
    rules_profile = str(pins["rules"].get("profile", ""))
    return spec, pins, rules_profile, _layers(spec.layers), tool_root


def _compiler_paths(project_dir: Path, build: str) -> tuple[Path, Path]:
    build_dir = project_dir / "build" / "builds" / build
    return build_dir / f"{build}.bom.json", build_dir / f"{build}.bom.csv"


def _read_sources(
    project_dir: Path,
) -> tuple[Any, tuple[Any, ...], Path, Path, str]:
    try:
        contract = read_build_test_contract(project_dir)
    except (BuildTestInputError, BuildTestError) as exc:
        raise FabInputError(str(exc)) from exc
    bom_json, bom_csv = _compiler_paths(project_dir, contract.build)
    try:
        components = read_bom_components(bom_json)
        bom_sha256 = semantic_bom_sha256(bom_json)
    except (BuildTestError, ArtifactHashError) as exc:
        raise FabInputError(f"cannot read the compiler BOM: {exc}") from exc
    expected = {item.lcsc for item in contract.bom}
    actual = {item.lcsc for item in components}
    if expected != actual:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise FabInputError(
            "compiler BOM does not match the approved build-test contract "
            f"(missing: {missing}; unexpected: {extra})"
        )
    return contract, components, bom_json, bom_csv, bom_sha256


def generate_fab(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> FabResult:
    """Generate, validate, and record the VERIFY-to-ORDER fabrication packet."""
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise FabInputError(f"project directory does not exist: {project_dir}")
    _require_verify_complete(project_dir)
    try:
        require_current_acceptance(project_dir)
    except BuildTestInputError as exc:
        raise FabInputError(str(exc)) from exc

    spec, pins, rules_profile, layers, tool_root = _context(project_dir, tool_root)
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    board = _board_evidence(board_path)
    try:
        board_before = board_path.read_bytes()
    except OSError as exc:
        raise FabInputError(f"cannot read {board_path.name}: {exc}") from exc
    contract, components, bom_json, bom_csv, bom_sha256 = _read_sources(project_dir)

    kicad = str(tool_root / "scripts" / "kicad-cli")
    with tempfile.TemporaryDirectory(prefix="pcbforge-fab-") as temporary:
        staging = Path(temporary)
        position_path = staging / POS_FILENAME
        drc_path = staging / DRC_REPORT_FILENAME
        commands = (
            (
                kicad, "pcb", "export", "gerbers",
                "--output", str(staging),
                "--layers", ",".join(layers),
                "--no-x2",
                "--subtract-soldermask",
                "--precision", "6",
                str(board_path),
            ),
            (
                kicad, "pcb", "export", "drill",
                "--output", f"{staging}/",
                "--format", "excellon",
                "--excellon-units", "mm",
                "--excellon-separate-th",
                "--excellon-zeros-format", "decimal",
                "--drill-origin", "absolute",
                "--generate-map",
                "--map-format", "gerberx2",
                str(board_path),
            ),
            (
                kicad, "pcb", "export", "pos",
                "--output", str(position_path),
                "--format", "csv",
                "--units", "mm",
                "--side", "both",
                "--exclude-dnp",
                str(board_path),
            ),
            (
                kicad, "pcb", "drc",
                "--format", "json",
                "--output", str(drc_path),
                "--severity-all",
                str(board_path),
            ),
        )
        purposes = (
            "gerber export",
            "drill export",
            "placement export",
            "DRC export",
        )
        for command, purpose in zip(commands, purposes, strict=True):
            _run(command, cwd=project_dir, runner=runner, purpose=purpose)

        placements = _read_position_rows(position_path)
        staged: dict[str, bytes] = {}
        for path in sorted(staging.iterdir()):
            if path.is_file():
                staged[path.name] = path.read_bytes()

    if DRC_REPORT_FILENAME not in staged:
        raise FabError("the DRC export produced no report")
    _require_clean_drc(staged[DRC_REPORT_FILENAME])

    try:
        staged[BOM_CSV_FILENAME] = bom_csv.read_bytes()
    except OSError as exc:
        raise FabInputError(f"cannot read the compiler BOM CSV: {exc}") from exc
    staged[JLC_BOM_FILENAME] = render_jlc_bom(components).encode()
    staged[JLC_CPL_FILENAME] = render_jlc_cpl(placements).encode()

    _validate_packet(
        project_name=spec.name,
        layers=layers,
        staged=staged,
        components=components,
        placements=placements,
        board=board,
    )

    files = tuple(_fab_file(name, data) for name, data in sorted(staged.items()))
    staged[MANIFEST_FILENAME] = _manifest_bytes(
        project_name=spec.name,
        build=contract.build,
        pins=pins,
        rules_profile=rules_profile,
        layers=layers,
        board_sha256=hashlib.sha256(board_before).hexdigest(),
        bom_sha256=bom_sha256,
        commands=_portable_commands(commands, tool_root, staging, board_path),
        files=files,
    )
    archive = archive_name(spec.name)
    staged[archive] = _archive_bytes(
        [
            *((name, data) for name, data in staged.items()),
            (board_path.name, board_before),
        ]
    )

    target = fab_dir(project_dir)
    target.mkdir(parents=True, exist_ok=True)
    outputs = [(target / name, data) for name, data in sorted(staged.items())]
    try:
        commit_outputs(outputs, label="fabrication packet")
    except AtomicWriteError as exc:
        raise FabError(str(exc)) from exc
    remove_paths(
        [
            path
            for path in sorted(target.iterdir())
            if path.is_file()
            and path.name != KEEP_FILENAME
            and path.name not in staged
        ]
    )

    if board_path.read_bytes() != board_before:
        raise FabError(
            f"safety invariant failed: {board_path.name} changed during "
            "fabrication packet generation"
        )
    recorded = _record_transition(project_dir, spec.name, len(layers))
    return FabResult(
        project_dir,
        spec.name,
        files,
        archive,
        layers,
        len(components),
        len(placements),
        recorded,
    )


def _portable_commands(
    commands: Sequence[Sequence[str]],
    tool_root: Path,
    staging: Path,
    board_path: Path,
) -> tuple[tuple[str, ...], ...]:
    """Strip machine-specific paths so the manifest is comparable anywhere."""
    replacements = (
        (str(tool_root / "scripts" / "kicad-cli"), "kicad-cli"),
        (f"{staging}/", "<output>/"),
        (str(staging), "<output>"),
        (str(board_path), f"<project>/{board_path.name}"),
    )
    portable = []
    for command in commands:
        rendered = []
        for argument in command:
            for needle, replacement in replacements:
                if argument == needle:
                    argument = replacement
                    break
            rendered.append(argument)
        portable.append(tuple(rendered))
    return tuple(portable)


def _record_transition(project_dir: Path, project_name: str, layer_count: int) -> bool:
    from pcbforge.status import StatusInputError, record_fab_out_transition

    try:
        record_fab_out_transition(
            project_dir,
            note=(
                f"Generated and validated the {layer_count}-layer {project_name} "
                "fabrication packet"
            ),
        )
    except StatusInputError as exc:
        if "already current" in str(exc):
            return False
        raise FabInputError(str(exc)) from exc
    return True


def read_manifest(project_dir: Path) -> Mapping[str, Any]:
    """Read and schema-check the packet manifest."""
    path = fab_dir(project_dir) / MANIFEST_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FabInputError(
            "missing fab/manifest.json; run `pcbforge fab-out`"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FabError(f"invalid fab/manifest.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise FabError("fab/manifest.json must be a JSON object")
    if payload.get("pcbforge_fab_schema") != FAB_SCHEMA:
        raise FabError(
            "fab/manifest.json schema is unsupported; regenerate the packet"
        )
    if not isinstance(payload.get("files"), list):
        raise FabError("fab/manifest.json has no file list")
    return payload


def check_fab(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> FabResult:
    """Validate the recorded packet against the project without regenerating."""
    project_dir = project_dir.expanduser().resolve()
    spec, _pins, _profile, layers, _tool_root = _context(project_dir, tool_root)
    manifest = read_manifest(project_dir)
    target = fab_dir(project_dir)
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    board = _board_evidence(board_path)
    contract, components, _bom_json, _bom_csv, bom_sha256 = _read_sources(project_dir)

    errors: list[str] = []
    sources = manifest.get("sources")
    sources = sources if isinstance(sources, dict) else {}
    try:
        board_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise FabInputError(f"cannot read {board_path.name}: {exc}") from exc
    if sources.get("board_sha256") != board_sha256:
        errors.append("the board changed after the packet was generated")
    if sources.get("bom_semantic_sha256") != bom_sha256:
        errors.append("the compiler BOM changed after the packet was generated")
    if manifest.get("build") != contract.build:
        errors.append("the packet was generated for a different compiler build")
    if tuple(manifest.get("layers") or ()) != layers:
        errors.append("the packet layer set does not match the project stackup")

    staged: dict[str, bytes] = {}
    files: list[FabFile] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise FabError("fab/manifest.json contains an invalid file entry")
        name = entry["name"]
        path = target / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            errors.append(f"missing packet file {name}")
            continue
        except OSError as exc:
            raise FabError(f"cannot read fab/{name}: {exc}") from exc
        staged[name] = data
        item = _fab_file(name, data)
        files.append(item)
        if item.sha256 != entry.get("sha256"):
            errors.append(f"fab/{name} was modified after generation")

    expected_bom = render_jlc_bom(components).encode()
    if staged.get(JLC_BOM_FILENAME) not in (None, expected_bom):
        errors.append(f"fab/{JLC_BOM_FILENAME} no longer matches the compiler BOM")
    if POS_FILENAME in staged:
        placements = _read_position_rows(target / POS_FILENAME)
        expected_cpl = render_jlc_cpl(placements).encode()
        if staged.get(JLC_CPL_FILENAME) not in (None, expected_cpl):
            errors.append(
                f"fab/{JLC_CPL_FILENAME} no longer matches the exported placements"
            )
    else:
        placements = ()
        errors.append(f"missing packet file {POS_FILENAME}")

    archive = archive_name(spec.name)
    archive_path = target / archive
    if not archive_path.is_file():
        errors.append(f"missing packet archive {archive}")
    else:
        errors.extend(_archive_errors(archive_path, staged, board_path))

    if DRC_REPORT_FILENAME in staged:
        try:
            _require_clean_drc(staged[DRC_REPORT_FILENAME])
        except FabError as exc:
            errors.append(str(exc))

    if not errors:
        _validate_packet(
            project_name=spec.name,
            layers=layers,
            staged=staged,
            components=components,
            placements=placements,
            board=board,
        )
    if errors:
        raise FabError(
            "fabrication packet is not current:\n  - " + "\n  - ".join(errors)
        )
    return FabResult(
        project_dir,
        spec.name,
        tuple(files),
        archive,
        layers,
        len(components),
        len(placements),
        False,
    )


def _archive_errors(
    archive_path: Path,
    staged: Mapping[str, bytes],
    board_path: Path,
) -> list[str]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"cannot read {archive_path.name}: {exc}"]
    errors = []
    for name, data in staged.items():
        if name == archive_path.name:
            continue
        if entries.get(name) != data:
            errors.append(f"{archive_path.name} does not contain the current {name}")
    try:
        if entries.get(board_path.name) != board_path.read_bytes():
            errors.append(
                f"{archive_path.name} does not contain the current "
                f"{board_path.name}"
            )
    except OSError as exc:
        errors.append(f"cannot read {board_path.name}: {exc}")
    return errors


def render_fab_result(result: FabResult) -> str:
    """Render the human-facing packet summary."""
    lines = [
        f"pcbforge fabrication packet: {result.project_name}",
        f"summary: {result.summary}",
        f"archive: {FAB_DIRNAME}/{result.archive_name}",
        "files:",
    ]
    lines.extend(
        f"  - {FAB_DIRNAME}/{item.name} [{item.sha256[:12]}] {item.size} bytes"
        for item in result.files
    )
    return "\n".join(lines)
