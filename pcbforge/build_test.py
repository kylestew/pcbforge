"""Deterministic Step 6 build-and-test acceptance checks."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.initialize import InitInputError, read_spec

BUILD_TEST_SCHEMA = 1
BUILD_TEST_REPORT_SCHEMA = 1
PROJECT_PIN_SCHEMA = 11
BUILD_TEST_FILENAME = "build-test.yaml"
BUILD_TEST_REPORT = Path("docs/build-test.md")

BUILD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
LCSC_RE = re.compile(r"^C[1-9][0-9]*$")
ASSERTION_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ASSERTION_MARKER_RE = re.compile(
    r"^\s*#\s*pcbforge-test:\s*([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\s*$"
)
TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')
HEAD_RE = re.compile(r"^\(\s*([^\s()]+)")
REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"((?:\\.|[^"])*)"')
FOOTPRINT_NAME_RE = re.compile(r'^\(footprint\s+"((?:\\.|[^"])*)"')
AT_RE = re.compile(r"\n\s*\(at\s+([^)]+)\)")
LAYER_RE = re.compile(r'\n\s*\(layer\s+"([^"]+)"\)')
PAD_RE = re.compile(r'^\(pad\s+"?([^"\s)]+)"?')
NET_RE = re.compile(r'\(net\s+(?:"([^"]+)"|([^\s()]+))(?:\s+"((?:\\.|[^"])*)")?\)')

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class BuildTestError(RuntimeError):
    """A build or deterministic acceptance check failed."""


class BuildTestInputError(BuildTestError):
    """The project or Step 6 contract is malformed."""


@dataclass(frozen=True)
class ExpectedComponent:
    lcsc: str
    mpn: str
    footprint: str
    quantity: int


@dataclass(frozen=True)
class BuildTestContract:
    build: str
    bom: tuple[ExpectedComponent, ...]
    board_footprints: int
    assertions: tuple[str, ...]


@dataclass(frozen=True)
class BomComponent:
    lcsc: str
    mpn: str
    footprint: str
    quantity: int
    designators: tuple[str, ...]


@dataclass(frozen=True)
class AssertionLocation:
    identifier: str
    path: Path
    line: int


@dataclass(frozen=True)
class BoardEvidence:
    references: tuple[str, ...]
    footprints: tuple[tuple[str, str], ...]
    footprint_placements: tuple[tuple[str, str, str], ...]
    pads: tuple[tuple[str, str], ...]
    pad_nets: tuple[tuple[str, str, str], ...]
    connectivity_sha256: str
    user_art_count: int
    user_art_sha256: str


@dataclass(frozen=True)
class BuildTestResult:
    build: str
    components: tuple[BomComponent, ...]
    assertions: tuple[AssertionLocation, ...]
    footprint_count: int
    net_count: int
    fingerprint: str
    report: str
    report_path: Path
    wrote_report: bool

    @property
    def summary(self) -> str:
        assertion_label = "assertion" if len(self.assertions) == 1 else "assertions"
        return (
            f"{len(self.components)} BOM lines, {self.footprint_count} footprints, "
            f"{len(self.assertions)} {assertion_label}, and spatial preservation passed"
        )


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError("mapping keys must be scalar values") from exc
        if duplicate:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BuildTestInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError) as exc:
        raise BuildTestInputError(f"cannot read {path}: {exc}") from exc
    try:
        loaded = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise BuildTestInputError(f"invalid {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BuildTestInputError(f"{label} must be a YAML mapping")
    return loaded


def _required_text(
    data: Mapping[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}: expected a non-empty string")
        return ""
    return value.strip()


def read_build_test_contract(project_dir: Path) -> BuildTestContract:
    """Read and strictly validate build-test.yaml schema 1."""
    project_dir = project_dir.expanduser().resolve()
    data = _load_yaml(
        project_dir / BUILD_TEST_FILENAME,
        label=BUILD_TEST_FILENAME,
    )
    errors: list[str] = []
    allowed = {
        "build_test_schema",
        "build",
        "bom",
        "board_footprints",
        "assertions",
    }
    unknown = sorted(set(data) - allowed, key=str)
    if unknown:
        errors.append(f"unknown keys: {', '.join(map(str, unknown))}")
    if data.get("build_test_schema") != BUILD_TEST_SCHEMA:
        errors.append(f"build_test_schema: expected integer {BUILD_TEST_SCHEMA}")

    build = _required_text(data, "build", "build-test", errors)
    if build and BUILD_RE.fullmatch(build) is None:
        errors.append("build: use only letters, digits, underscore, and hyphen")

    bom_raw = data.get("bom")
    components: list[ExpectedComponent] = []
    if not isinstance(bom_raw, list) or not bom_raw:
        errors.append("bom: expected a non-empty list")
    else:
        for index, raw in enumerate(bom_raw):
            prefix = f"bom[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            item_unknown = sorted(
                set(raw) - {"lcsc", "mpn", "footprint", "quantity"},
                key=str,
            )
            if item_unknown:
                errors.append(
                    f"{prefix}: unknown keys: {', '.join(map(str, item_unknown))}"
                )
            lcsc = _required_text(raw, "lcsc", prefix, errors)
            mpn = _required_text(raw, "mpn", prefix, errors)
            footprint = _required_text(raw, "footprint", prefix, errors)
            quantity = raw.get("quantity")
            if lcsc and LCSC_RE.fullmatch(lcsc) is None:
                errors.append(f"{prefix}.lcsc: expected an LCSC ID such as C12345")
            if type(quantity) is not int or quantity <= 0:
                errors.append(f"{prefix}.quantity: expected a positive integer")
                quantity = 0
            components.append(ExpectedComponent(lcsc, mpn, footprint, quantity))
        lcsc_ids = [component.lcsc for component in components if component.lcsc]
        duplicates = sorted(
            identifier for identifier in set(lcsc_ids) if lcsc_ids.count(identifier) > 1
        )
        if duplicates:
            errors.append(f"bom: duplicate LCSC IDs: {', '.join(duplicates)}")

    board_footprints = data.get("board_footprints")
    if type(board_footprints) is not int or board_footprints <= 0:
        errors.append("board_footprints: expected a positive integer")
        board_footprints = 0

    assertions_raw = data.get("assertions")
    assertions: list[str] = []
    if not isinstance(assertions_raw, list) or not assertions_raw:
        errors.append("assertions: expected a non-empty list")
    else:
        for index, raw in enumerate(assertions_raw):
            if not isinstance(raw, str) or ASSERTION_ID_RE.fullmatch(raw) is None:
                errors.append(f"assertions[{index}]: expected a kebab-case test ID")
                continue
            assertions.append(raw)
        duplicates = sorted(
            identifier
            for identifier in set(assertions)
            if assertions.count(identifier) > 1
        )
        if duplicates:
            errors.append(f"assertions: duplicate IDs: {', '.join(duplicates)}")

    ato = _load_yaml(project_dir / "ato.yaml", label="ato.yaml")
    builds = ato.get("builds")
    if build and (
        not isinstance(builds, dict)
        or build not in builds
        or not isinstance(builds[build], dict)
    ):
        errors.append(f"build: {build!r} is not declared in ato.yaml")

    if errors:
        raise BuildTestInputError(
            f"invalid {BUILD_TEST_FILENAME}:\n  - " + "\n  - ".join(errors)
        )
    return BuildTestContract(
        build,
        tuple(components),
        board_footprints,
        tuple(assertions),
    )


def _read_pin_metadata(project_dir: Path) -> Mapping[str, Any]:
    data = _load_yaml(project_dir / ".pcbforge", label=".pcbforge")
    errors = []
    if data.get("schema") != PROJECT_PIN_SCHEMA:
        errors.append(f"schema: expected integer {PROJECT_PIN_SCHEMA}")
    toolchain = data.get("toolchain")
    if not isinstance(toolchain, dict):
        errors.append("toolchain: expected a mapping")
    else:
        for key in ("atopile", "kicad", "uv_lock_sha256"):
            if not isinstance(toolchain.get(key), str) or not toolchain[key].strip():
                errors.append(f"toolchain.{key}: expected a non-empty string")
    guidance = data.get("guidance")
    if not isinstance(guidance, dict):
        errors.append("guidance: expected a mapping")
    elif guidance.get("build_test_schema") != BUILD_TEST_SCHEMA:
        errors.append(
            f"guidance.build_test_schema: expected integer {BUILD_TEST_SCHEMA}"
        )
    elif guidance.get("policy_schema") != 1:
        errors.append("guidance.policy_schema: expected integer 1")
    if errors:
        raise BuildTestInputError(
            "project guidance is not migrated for Step 6:\n  - " + "\n  - ".join(errors)
        )
    return data


def build_test_inputs(project_dir: Path) -> tuple[Path, ...]:
    """Return non-spatial tracked inputs for saved Step 6 evidence."""
    project_dir = project_dir.expanduser().resolve()
    patterns = (
        "spec.md",
        ".pcbforge",
        "ato.yaml",
        BUILD_TEST_FILENAME,
        "fp-lib-table",
        "src/**/*.ato",
        "src/**/*.kicad_mod",
        "src/**/*.kicad_sym",
        "src/**/*.step",
        "src/**/*.wrl",
        "firmware/*.ioc",
    )
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in project_dir.glob(pattern) if path.is_file())
    return tuple(sorted(paths))


def fingerprint_inputs(project_dir: Path) -> str:
    """Fingerprint Step 6 circuit identity and topology, never spatial artwork."""
    project_dir = project_dir.expanduser().resolve()
    digest = hashlib.sha256()
    for path in build_test_inputs(project_dir):
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(b"\0")
        try:
            contents = path.read_bytes()
        except OSError as exc:
            raise BuildTestError(f"cannot fingerprint {path}: {exc}") from exc
        digest.update(hashlib.sha256(contents).digest())
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise BuildTestInputError(str(exc)) from exc
    board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
    digest.update(b"pcb-topology\0")
    digest.update(board_topology_bytes(board))
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_tokens(text: str) -> str:
    return " ".join(TOKEN_RE.findall(text))


def _top_level_blocks(text: str) -> list[tuple[str, str]]:
    first = text.find("(")
    if first < 0:
        raise ValueError("not an s-expression")
    depth = 0
    in_string = False
    escaped = False
    block_start: int | None = None
    blocks: list[tuple[str, str]] = []
    for index, char in enumerate(text[first:], start=first):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            if depth == 1:
                block_start = index
            depth += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 1 and block_start is not None:
                block = text[block_start : index + 1]
                match = HEAD_RE.match(block)
                if match:
                    blocks.append((match.group(1), block))
                block_start = None
            if depth == 0:
                break
    if depth != 0:
        raise ValueError("unbalanced s-expression")
    return blocks


def _aggregate_hash(values: Sequence[str]) -> str:
    return _sha256_bytes("\n".join(sorted(values)).encode())


def read_board_evidence(path: Path) -> BoardEvidence:
    try:
        text = path.read_text(encoding="utf-8")
        blocks = _top_level_blocks(text)
    except FileNotFoundError as exc:
        raise BuildTestInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise BuildTestError(f"cannot inspect {path}: {exc}") from exc

    references: list[str] = []
    footprints: list[tuple[str, str]] = []
    placements: list[tuple[str, str, str]] = []
    pads: list[tuple[str, str]] = []
    pad_nets: list[tuple[str, str, str]] = []
    user_art_hashes: list[str] = []
    user_art_heads = {
        "segment",
        "arc",
        "via",
        "zone",
        "image",
        "target",
        "group",
        "dimension",
    }
    for head, block in blocks:
        if head.startswith("gr_"):
            user_art_heads.add(head)
        if head in user_art_heads:
            user_art_hashes.append(_sha256_bytes(_canonical_tokens(block).encode()))
        if head != "footprint":
            continue
        reference_match = REFERENCE_RE.search(block)
        reference = reference_match.group(1) if reference_match else ""
        footprint_match = FOOTPRINT_NAME_RE.match(block)
        footprint = footprint_match.group(1) if footprint_match else ""
        references.append(reference)
        footprints.append((reference, footprint))
        at = AT_RE.search(block)
        layer = LAYER_RE.search(block)
        placements.append(
            (
                reference,
                at.group(1).strip() if at else "",
                layer.group(1) if layer else "",
            )
        )
        for child_head, child in _top_level_blocks(block):
            if child_head != "pad":
                continue
            pad_match = PAD_RE.match(child)
            net_match = NET_RE.search(child)
            if pad_match:
                pads.append((reference, pad_match.group(1)))
            if pad_match and net_match:
                net = net_match.group(3) or net_match.group(1) or net_match.group(2)
                pad_nets.append((reference, pad_match.group(1), net))

    connectivity = tuple(sorted(pad_nets))
    return BoardEvidence(
        tuple(sorted(references)),
        tuple(sorted(footprints)),
        tuple(sorted(placements)),
        tuple(sorted(pads)),
        connectivity,
        _aggregate_hash(["\0".join(item) for item in connectivity]),
        len(user_art_hashes),
        _aggregate_hash(user_art_hashes),
    )


def board_topology_bytes(board: BoardEvidence) -> bytes:
    """Return canonical circuit-owned PCB identity/connectivity evidence."""
    topology = {
        "references": board.references,
        "footprints": board.footprints,
        "pads": board.pads,
        "pad_nets": board.pad_nets,
    }
    return json.dumps(topology, separators=(",", ":"), sort_keys=True).encode()


def _spatial_errors(before: BoardEvidence, after: BoardEvidence) -> list[str]:
    errors = []
    if before.footprint_placements != after.footprint_placements:
        errors.append("the build changed footprint placement, side, or membership")
    if (
        before.user_art_count != after.user_art_count
        or before.user_art_sha256 != after.user_art_sha256
    ):
        errors.append(
            "the build changed tracks, vias, zones, outline, graphics, or user artwork"
        )
    return errors


def _find_assertions(
    project_dir: Path,
) -> tuple[tuple[AssertionLocation, ...], list[str]]:
    found: list[AssertionLocation] = []
    errors: list[str] = []
    for path in sorted(project_dir.glob("src/**/*.ato")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot read assertion source {path}: {exc}")
            continue
        for index, line in enumerate(lines):
            marker = ASSERTION_MARKER_RE.match(line)
            if marker is None:
                continue
            next_index = index + 1
            if next_index >= len(lines) or not lines[next_index].lstrip().startswith(
                "assert "
            ):
                errors.append(
                    f"{path.relative_to(project_dir)}:{index + 1}: "
                    f"pcbforge-test {marker.group(1)!r} is not followed by assert"
                )
                continue
            found.append(
                AssertionLocation(
                    marker.group(1),
                    path.relative_to(project_dir),
                    next_index + 1,
                )
            )
    return tuple(found), errors


def _validate_assertions(
    expected: Sequence[str],
    found: Sequence[AssertionLocation],
) -> list[str]:
    errors = []
    identifiers = [item.identifier for item in found]
    duplicates = sorted(
        identifier
        for identifier in set(identifiers)
        if identifiers.count(identifier) > 1
    )
    if duplicates:
        errors.append(f"duplicate source assertion IDs: {', '.join(duplicates)}")
    missing = sorted(set(expected) - set(identifiers))
    unexpected = sorted(set(identifiers) - set(expected))
    if missing:
        errors.append(f"missing source assertions: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unlisted source assertions: {', '.join(unexpected)}")
    return errors


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildTestError(f"missing {label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildTestError(f"invalid {label} at {path}: {exc}") from exc


def _read_bom(path: Path) -> tuple[BomComponent, ...]:
    data = _read_json(path, label="BOM JSON")
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        raise BuildTestError("BOM JSON must contain a components list")
    components = []
    for index, raw in enumerate(data["components"]):
        if not isinstance(raw, dict):
            raise BuildTestError(f"BOM component {index} is not a mapping")
        lcsc = raw.get("lcsc")
        mpn = raw.get("mpn")
        footprint = raw.get("package")
        quantity = raw.get("quantity")
        usages = raw.get("usages")
        if (
            not isinstance(lcsc, str)
            or LCSC_RE.fullmatch(lcsc) is None
            or not isinstance(mpn, str)
            or not mpn.strip()
            or not isinstance(footprint, str)
            or not footprint.strip()
            or type(quantity) is not int
            or quantity <= 0
            or not isinstance(usages, list)
        ):
            raise BuildTestError(
                f"BOM component {index} lacks exact LCSC, MPN, footprint, "
                "positive quantity, or usages"
            )
        designators = []
        for usage in usages:
            designator = usage.get("designator") if isinstance(usage, dict) else None
            if not isinstance(designator, str) or not designator.strip():
                raise BuildTestError(
                    f"BOM component {lcsc} contains an invalid designator"
                )
            designators.append(designator.strip())
        components.append(
            BomComponent(
                lcsc,
                mpn.strip(),
                footprint.strip(),
                quantity,
                tuple(sorted(designators)),
            )
        )
    return tuple(sorted(components, key=lambda component: component.lcsc))


def _validate_bom(
    expected: Sequence[ExpectedComponent],
    actual: Sequence[BomComponent],
) -> list[str]:
    errors = []
    actual_ids = [component.lcsc for component in actual]
    duplicates = sorted(
        identifier for identifier in set(actual_ids) if actual_ids.count(identifier) > 1
    )
    if duplicates:
        errors.append(f"BOM has duplicate LCSC lines: {', '.join(duplicates)}")
    expected_by_id = {component.lcsc: component for component in expected}
    actual_by_id = {component.lcsc: component for component in actual}
    missing = sorted(set(expected_by_id) - set(actual_by_id))
    unexpected = sorted(set(actual_by_id) - set(expected_by_id))
    if missing:
        errors.append(f"BOM is missing expected LCSC IDs: {', '.join(missing)}")
    if unexpected:
        errors.append(f"BOM has unexpected LCSC IDs: {', '.join(unexpected)}")
    for lcsc in sorted(set(expected_by_id) & set(actual_by_id)):
        wanted = expected_by_id[lcsc]
        got = actual_by_id[lcsc]
        if wanted.mpn != got.mpn:
            errors.append(f"{lcsc}: MPN {got.mpn!r}, expected {wanted.mpn!r}")
        if wanted.footprint != got.footprint:
            errors.append(
                f"{lcsc}: footprint {got.footprint!r}, expected {wanted.footprint!r}"
            )
        if wanted.quantity != got.quantity:
            errors.append(
                f"{lcsc}: quantity {got.quantity}, expected {wanted.quantity}"
            )
        if len(got.designators) != got.quantity:
            errors.append(
                f"{lcsc}: {len(got.designators)} BOM usages for quantity {got.quantity}"
            )
    return errors


def _validate_bom_csv(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(text))
        headers = set(reader.fieldnames or ())
        rows = list(reader)
    except FileNotFoundError:
        return [f"missing BOM CSV: {path}"]
    except (OSError, UnicodeError, csv.Error) as exc:
        return [f"invalid BOM CSV at {path}: {exc}"]
    required = {
        "Designator",
        "Footprint",
        "Quantity",
        "Manufacturer",
        "Partnumber",
        "LCSC Part #",
    }
    errors = []
    if not required.issubset(headers):
        errors.append("BOM CSV is missing required exact-selection columns")
    if not rows:
        errors.append("BOM CSV has no component rows")
    return errors


def _validate_board(
    contract: BuildTestContract,
    components: Sequence[BomComponent],
    board: BoardEvidence,
) -> list[str]:
    errors = []
    if len(board.references) != contract.board_footprints:
        errors.append(
            f"PCB has {len(board.references)} footprints, expected "
            f"{contract.board_footprints}"
        )
    invalid_references = [reference or "<missing>" for reference in board.references]
    duplicates = sorted(
        reference
        for reference in set(invalid_references)
        if invalid_references.count(reference) > 1
    )
    if "<missing>" in invalid_references:
        errors.append("PCB contains a footprint without a Reference property")
    if duplicates:
        errors.append(f"PCB has duplicate references: {', '.join(duplicates)}")
    bom_references = sorted(
        designator for component in components for designator in component.designators
    )
    missing = sorted(set(bom_references) - set(board.references))
    unexpected = sorted(set(board.references) - set(bom_references))
    if missing:
        errors.append(f"PCB is missing BOM references: {', '.join(missing)}")
    if unexpected:
        errors.append(f"PCB has non-BOM references: {', '.join(unexpected)}")
    if len(bom_references) != len(set(bom_references)):
        errors.append("BOM contains duplicate designators")
    board_footprints = dict(board.footprints)
    for component in components:
        for designator in component.designators:
            actual_footprint = board_footprints.get(designator)
            if actual_footprint is not None and actual_footprint != component.footprint:
                errors.append(
                    f"{designator}: PCB footprint {actual_footprint!r}, expected "
                    f"{component.footprint!r}"
                )
    if not board.pad_nets:
        errors.append("PCB has no resolved pad-to-net connectivity")
    return errors


def _artifact_paths(
    project_dir: Path,
    build: str,
    board_path: Path,
) -> dict[str, Path]:
    build_dir = project_dir / "build" / "builds" / build
    return {
        "Compiler manifest": project_dir / "build" / "manifest.json",
        "BOM JSON": build_dir / f"{build}.bom.json",
        "BOM CSV": build_dir / f"{build}.bom.csv",
        "Resolved PCB": board_path,
    }


def _run_build(
    project_dir: Path,
    tool_root: Path,
    build: str,
    runner: CommandRunner,
) -> str:
    command = [
        str(tool_root / "scripts" / "ato"),
        "build",
        "--build",
        build,
        "--frozen",
        "--verbose",
    ]
    try:
        completed = runner(
            command,
            cwd=project_dir,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise BuildTestError(f"frozen build could not start: {exc}") from exc
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode:
        detail = output.splitlines()[-1] if output else f"exit {completed.returncode}"
        raise BuildTestError(f"frozen build failed: {detail}")
    return output


def _report_frontmatter(text: str) -> Mapping[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise BuildTestError("build-test report has no YAML frontmatter")
    try:
        end = lines.index("---", 1)
        data = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
    except (ValueError, yaml.YAMLError) as exc:
        raise BuildTestError(f"invalid build-test report frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildTestError("build-test report frontmatter must be a mapping")
    return data


def saved_report_status(project_dir: Path, fingerprint: str) -> tuple[bool, str]:
    path = project_dir / BUILD_TEST_REPORT
    try:
        text = path.read_text(encoding="utf-8")
        data = _report_frontmatter(text)
    except FileNotFoundError:
        return False, f"missing {BUILD_TEST_REPORT.as_posix()}"
    except (OSError, UnicodeError, BuildTestError) as exc:
        return False, str(exc)
    if data.get("pcbforge_build_test_report_schema") != BUILD_TEST_REPORT_SCHEMA:
        return False, "build-test report schema is unsupported"
    if data.get("result") != "pass":
        return False, "build-test report does not record a pass"
    if data.get("fingerprint") != fingerprint:
        return False, "build-test report is stale"
    return True, f"{BUILD_TEST_REPORT.as_posix()} is current"


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_report(
    *,
    project_name: str,
    contract: BuildTestContract,
    components: Sequence[BomComponent],
    assertions: Sequence[AssertionLocation],
    board: BoardEvidence,
    pins: Mapping[str, Any],
    fingerprint: str,
    artifacts: Mapping[str, Path],
) -> str:
    metadata = yaml.safe_dump(
        {
            "pcbforge_build_test_report_schema": BUILD_TEST_REPORT_SCHEMA,
            "result": "pass",
            "build": contract.build,
            "fingerprint": fingerprint,
        },
        sort_keys=False,
    ).rstrip()
    toolchain = pins["toolchain"]
    bom_rows = "\n".join(
        "| "
        + " | ".join(
            (
                component.lcsc,
                _escape_table(component.mpn),
                _escape_table(component.footprint),
                str(component.quantity),
                ", ".join(component.designators),
            )
        )
        + " |"
        for component in components
    )
    assertion_rows = "\n".join(
        f"| {item.identifier} | `{item.path.as_posix()}:{item.line}` | Passed |"
        for item in assertions
    )
    project_dir = (artifacts["Compiler manifest"]).parents[1]
    artifact_rows = "\n".join(
        f"| {label} | `{path.relative_to(project_dir).as_posix()}` | "
        f"`{hashlib.sha256(path.read_bytes()).hexdigest()}` |"
        for label, path in artifacts.items()
    )
    input_rows = "\n".join(
        f"| `{path.relative_to(project_dir).as_posix()}` | "
        f"`{hashlib.sha256(path.read_bytes()).hexdigest()}` |"
        for path in build_test_inputs(project_dir)
    )
    net_ids = {net for _, _, net in board.pad_nets}
    return f"""---
{metadata}
---
# {project_name} build + test report

> Generated by PCBForge. Change `build-test.yaml` or circuit source, then rerun
> `pcbforge status --check --write`; do not edit this report manually.

## Result

**PASS** — frozen build, exact BOM, source assertions, resolved PCB evidence,
and no-op spatial preservation all passed.

| Evidence | Result |
|---|---|
| Build target | `{contract.build}` |
| Atopile | `{toolchain["atopile"]}` |
| KiCad | `{toolchain["kicad"]}` |
| Input fingerprint | `{fingerprint}` |
| BOM lines | {len(components)} |
| PCB footprints | {len(board.references)} |
| Resolved nets | {len(net_ids)} |
| Source assertions | {len(assertions)} |

## Exact BOM

| LCSC | MPN | Footprint | Qty | Designators |
|---|---|---|---:|---|
{bom_rows}

## Assertions

| Test ID | Source | Compiler result |
|---|---|---|
{assertion_rows}

## Board evidence

- Every BOM designator appears exactly once on the PCB.
- Footprint count matches `build-test.yaml`.
- {len(board.pad_nets)} pad-to-net assignments resolve across {len(net_ids)} nets.
- Connectivity SHA-256: `{board.connectivity_sha256}`.
- Footprint placement, tracks, vias, zones, board outline, graphics, and user
  artwork were unchanged by the no-op frozen build.

## Compiler artifacts

| Artifact | Path | SHA-256 |
|---|---|---|
{artifact_rows}

## Inputs

| Path | SHA-256 |
|---|---|
{input_rows}
"""


def _atomic_write(path: Path, contents: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == contents:
            return False
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        raise BuildTestError(f"cannot read existing report {path}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(contents)
        os.replace(temporary_name, path)
    except OSError as exc:
        try:
            if "temporary_name" in locals():
                Path(temporary_name).unlink(missing_ok=True)
        finally:
            raise BuildTestError(f"cannot write report {path}: {exc}") from exc
    return True


def check_build_test(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    write_report: bool = False,
) -> BuildTestResult:
    """Run the complete deterministic Step 6 gate."""
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise BuildTestInputError(f"project directory does not exist: {project_dir}")
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise BuildTestInputError(str(exc)) from exc
    contract = read_build_test_contract(project_dir)
    pins = _read_pin_metadata(project_dir)
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    before = read_board_evidence(board_path)
    assertions, assertion_source_errors = _find_assertions(project_dir)
    assertion_errors = assertion_source_errors + _validate_assertions(
        contract.assertions,
        assertions,
    )

    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    build_failure: BuildTestError | None = None
    try:
        _run_build(project_dir, tool_root, contract.build, runner)
    except BuildTestError as exc:
        build_failure = exc
    after = read_board_evidence(board_path)
    errors = _spatial_errors(before, after)
    if build_failure is not None:
        errors.insert(0, str(build_failure))
    errors.extend(assertion_errors)

    artifacts = _artifact_paths(project_dir, contract.build, board_path)
    components: tuple[BomComponent, ...] = ()
    try:
        manifest = _read_json(
            artifacts["Compiler manifest"],
            label="compiler manifest",
        )
        if not isinstance(manifest, dict):
            errors.append("compiler manifest must be a JSON mapping")
        components = _read_bom(artifacts["BOM JSON"])
    except BuildTestError as exc:
        errors.append(str(exc))
    errors.extend(_validate_bom_csv(artifacts["BOM CSV"]))
    if components:
        errors.extend(_validate_bom(contract.bom, components))
        errors.extend(_validate_board(contract, components, after))

    if errors:
        raise BuildTestError(
            f"build-test failed: {errors[0]}\n  - " + "\n  - ".join(errors[1:])
            if len(errors) > 1
            else f"build-test failed: {errors[0]}"
        )

    fingerprint = fingerprint_inputs(project_dir)
    report_path = project_dir / BUILD_TEST_REPORT
    report = _render_report(
        project_name=spec.name,
        contract=contract,
        components=components,
        assertions=assertions,
        board=after,
        pins=pins,
        fingerprint=fingerprint,
        artifacts=artifacts,
    )
    wrote = _atomic_write(report_path, report) if write_report else False
    return BuildTestResult(
        contract.build,
        components,
        assertions,
        len(after.references),
        len({net for _, _, net in after.pad_nets}),
        fingerprint,
        report,
        BUILD_TEST_REPORT,
        wrote,
    )
