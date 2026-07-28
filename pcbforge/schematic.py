"""Native KiCad schematic review checks for the Step 5 IMPLEMENT gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.build_test import (
    BuildTestError,
    board_topology_bytes,
    read_board_evidence,
)
from pcbforge.initialize import InitInputError, read_spec

SCHEMATIC_REVIEW_SCHEMA = 1
PROJECT_PIN_SCHEMA = 12
CONTRACT_FILENAME = "schematic-review.yaml"
BASELINE_PATH = Path("review/implement/source-baseline.json")
STAGES = {"proposal", "final"}

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class SchematicError(RuntimeError):
    """A KiCad schematic check or comparison failed."""


class SchematicInputError(SchematicError):
    """The schematic review contract or project is malformed."""


@dataclass(frozen=True)
class SchematicContract:
    build: str
    proposal_root: Path
    final_root: Path
    proposal_narrative: Path
    final_narrative: Path


@dataclass(frozen=True)
class SchematicComponent:
    reference: str
    value: str
    footprint: str
    mpn: str
    lcsc: str


@dataclass(frozen=True)
class SchematicGraph:
    components: tuple[SchematicComponent, ...]
    nets: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]


@dataclass(frozen=True)
class SchematicResult:
    stage: str
    components: int
    nets: int
    connected_pins: int
    fingerprint: str
    evidence_path: Path
    render_paths: tuple[Path, ...]
    wrote: bool

    @property
    def summary(self) -> str:
        return (
            f"{self.components} components, {self.nets} nets, "
            f"{self.connected_pins} connected pins; ERC and review checks passed"
        )


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchematicInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SchematicInputError(f"invalid {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SchematicInputError(f"{path.name} must be a YAML mapping")
    return loaded


def _safe_relative_path(value: Any, field: str, required_prefix: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SchematicInputError(f"{field}: expected a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SchematicInputError(f"{field}: path must stay inside the project")
    try:
        path.relative_to(required_prefix)
    except ValueError as exc:
        raise SchematicInputError(
            f"{field}: path must be under {required_prefix.as_posix()}/"
        ) from exc
    return path


def read_schematic_contract(project_dir: Path) -> SchematicContract:
    """Read the strict, tracked Step 5 schematic review contract."""
    project_dir = project_dir.expanduser().resolve()
    data = _load_yaml(project_dir / CONTRACT_FILENAME)
    allowed = {
        "schematic_review_schema",
        "build",
        "proposal_root",
        "final_root",
        "proposal_narrative",
        "final_narrative",
    }
    unknown = sorted(set(data) - allowed, key=str)
    if unknown:
        raise SchematicInputError(
            f"{CONTRACT_FILENAME}: unknown keys: {', '.join(map(str, unknown))}"
        )
    if data.get("schematic_review_schema") != SCHEMATIC_REVIEW_SCHEMA:
        raise SchematicInputError(
            f"schematic_review_schema: expected integer {SCHEMATIC_REVIEW_SCHEMA}"
        )
    build = data.get("build")
    if not isinstance(build, str) or not build.strip():
        raise SchematicInputError("build: expected a non-empty build name")
    proposal_root = _safe_relative_path(
        data.get("proposal_root"),
        "proposal_root",
        Path("review/implement/proposal"),
    )
    final_root = _safe_relative_path(
        data.get("final_root"),
        "final_root",
        Path("review/implement/final"),
    )
    proposal_narrative = _safe_relative_path(
        data.get("proposal_narrative"),
        "proposal_narrative",
        Path("docs"),
    )
    final_narrative = _safe_relative_path(
        data.get("final_narrative"),
        "final_narrative",
        Path("docs"),
    )
    if proposal_root.suffix != ".kicad_sch" or final_root.suffix != ".kicad_sch":
        raise SchematicInputError("proposal_root and final_root must be .kicad_sch files")
    ato = _load_yaml(project_dir / "ato.yaml")
    builds = ato.get("builds")
    if (
        not isinstance(builds, dict)
        or build not in builds
        or not isinstance(builds[build], dict)
    ):
        raise SchematicInputError(f"build {build!r} is not declared in ato.yaml")
    return SchematicContract(
        build.strip(),
        proposal_root,
        final_root,
        proposal_narrative,
        final_narrative,
    )


def _read_pins(project_dir: Path) -> Mapping[str, Any]:
    data = _load_yaml(project_dir / ".pcbforge")
    if data.get("schema") != PROJECT_PIN_SCHEMA:
        raise SchematicInputError(
            "project is not migrated for schematic review; run "
            "`pcbforge migrate-schematic-review`"
        )
    guidance = data.get("guidance")
    if (
        not isinstance(guidance, dict)
        or guidance.get("schematic_review_schema") != SCHEMATIC_REVIEW_SCHEMA
    ):
        raise SchematicInputError(
            "project guidance does not pin schematic review schema 1"
        )
    return data


def _normalize_footprint(value: str) -> str:
    return value.replace(".pretty:", ":").strip()


def _field_value(component: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for element in (
        *component.findall("./fields/field"),
        *component.findall("./property"),
    ):
        name = element.attrib.get("name", "").casefold()
        if name not in wanted:
            continue
        value = element.attrib.get("value")
        if value is None:
            value = element.text or ""
        if value.strip():
            return value.strip()
    return ""


def parse_kicad_netlist(path: Path) -> SchematicGraph:
    """Parse canonical identity and physical-pin connectivity from KiCad XML."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise SchematicError(f"cannot parse KiCad netlist: {exc}") from exc
    components: list[SchematicComponent] = []
    for element in root.findall("./components/comp"):
        reference = element.attrib.get("ref", "").strip()
        if not reference:
            raise SchematicError("schematic contains a component without a reference")
        components.append(
            SchematicComponent(
                reference,
                element.findtext("value", default="").strip(),
                _normalize_footprint(
                    element.findtext("footprint", default="").strip()
                ),
                _field_value(
                    element,
                    "MPN",
                    "Manufacturer Part Number",
                    "Manufacturer Part",
                ),
                _field_value(element, "LCSC", "LCSC Part #", "LCSC Part"),
            )
        )
    references = [component.reference for component in components]
    duplicates = sorted(
        reference
        for reference in set(references)
        if references.count(reference) > 1
    )
    if duplicates:
        raise SchematicError(
            f"schematic has duplicate references: {', '.join(duplicates)}"
        )
    nets: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for element in root.findall("./nets/net"):
        name = element.attrib.get("name", "").strip()
        nodes = tuple(
            sorted(
                (
                    node.attrib.get("ref", "").strip(),
                    node.attrib.get("pin", "").strip(),
                )
                for node in element.findall("node")
            )
        )
        if name and nodes:
            nets.append((name, nodes))
    return SchematicGraph(
        tuple(sorted(components, key=lambda component: component.reference)),
        tuple(sorted(nets)),
    )


def _graph_payload(graph: SchematicGraph) -> dict[str, Any]:
    return {
        "components": [
            {
                "reference": component.reference,
                "value": component.value,
                "footprint": component.footprint,
                "mpn": component.mpn,
                "lcsc": component.lcsc,
            }
            for component in graph.components
        ],
        "nets": [
            {
                "name": name,
                "nodes": [
                    {"reference": reference, "pin": pin}
                    for reference, pin in nodes
                ],
            }
            for name, nodes in graph.nets
        ],
    }


def _semantic_fingerprint(graph: SchematicGraph) -> str:
    encoded = json.dumps(
        _graph_payload(graph),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_baseline_payload(project_dir: Path) -> dict[str, Any]:
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise SchematicInputError(str(exc)) from exc
    sources = []
    for path in sorted(project_dir.glob("src/**/*.ato")):
        sources.append(
            {
                "path": path.relative_to(project_dir).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    board = project_dir / f"{spec.name}.kicad_pcb"
    try:
        board_hash = hashlib.sha256(
            board_topology_bytes(read_board_evidence(board))
        ).hexdigest()
    except BuildTestError as exc:
        raise SchematicInputError(str(exc)) from exc
    payload: dict[str, Any] = {
        "schematic_review_schema": SCHEMATIC_REVIEW_SCHEMA,
        "sources": sources,
        "board_topology_sha256": board_hash,
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return payload


def capture_implementation_baseline(project_dir: Path) -> Path:
    """Capture the source/board handoff immediately after MCU approval."""
    project_dir = project_dir.expanduser().resolve()
    _read_pins(project_dir)
    payload = _source_baseline_payload(project_dir)
    path = project_dir / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return BASELINE_PATH


def baseline_is_current(project_dir: Path) -> tuple[bool, str]:
    """Check that physical circuit source did not change before proposal approval."""
    project_dir = project_dir.expanduser().resolve()
    path = project_dir / BASELINE_PATH
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"missing {BASELINE_PATH.as_posix()}; reapprove MCU"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"invalid {BASELINE_PATH.as_posix()}: {exc}"
    current = _source_baseline_payload(project_dir)
    if not isinstance(saved, dict) or saved.get("fingerprint") != current["fingerprint"]:
        return (
            False,
            "physical source or board topology changed before proposal approval",
        )
    return True, "pre-IMPLEMENT source baseline is unchanged"


def schematic_inputs(project_dir: Path, stage: str) -> tuple[Path, ...]:
    """Return tracked inputs whose changes stale a schematic check."""
    if stage not in STAGES:
        raise SchematicInputError("stage must be proposal or final")
    project_dir = project_dir.expanduser().resolve()
    contract = read_schematic_contract(project_dir)
    root = contract.proposal_root if stage == "proposal" else contract.final_root
    narrative = (
        contract.proposal_narrative
        if stage == "proposal"
        else contract.final_narrative
    )
    stage_dir = root.parent
    candidates: set[Path] = {
        project_dir / ".pcbforge",
        project_dir / CONTRACT_FILENAME,
        project_dir / "spec.md",
        project_dir / "docs" / "architecture.md",
        project_dir / BASELINE_PATH,
        project_dir / root,
        project_dir / narrative,
    }
    candidates.update(
        path
        for path in stage_dir.glob("*.kicad_sch")
        if path.is_file()
    )
    if stage == "final":
        candidates.update(project_dir.glob("src/**/*.ato"))
        candidates.add(project_dir / "ato.yaml")
        try:
            spec = read_spec(project_dir / "spec.md")
            candidates.add(project_dir / f"{spec.name}.kicad_pcb")
        except InitInputError:
            pass
        candidates.add(
            project_dir
            / "build"
            / "builds"
            / contract.build
            / f"{contract.build}.bom.json"
        )
        candidates.add(
            project_dir
            / "review"
            / "implement"
            / "proposal"
            / "evidence.json"
        )
    return tuple(sorted(path for path in candidates if path.is_file()))


def schematic_status_fingerprint(project_dir: Path, stage: str) -> str:
    digest = hashlib.sha256()
    for path in schematic_inputs(project_dir, stage):
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise SchematicError(f"cannot run {' '.join(command)}: {exc}") from exc


def _erc_violations(path: Path) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchematicError(f"invalid KiCad ERC JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchematicError("KiCad ERC JSON must be a mapping")
    violations = payload.get("violations")
    if isinstance(violations, list):
        return [item for item in violations if isinstance(item, dict)]
    sheets = payload.get("sheets")
    if isinstance(sheets, list):
        return [
            violation
            for sheet in sheets
            if isinstance(sheet, dict)
            for violation in sheet.get("violations", [])
            if isinstance(violation, dict)
        ]
    return []


def _read_compiled_components(
    project_dir: Path,
    contract: SchematicContract,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    path = (
        project_dir
        / "build"
        / "builds"
        / contract.build
        / f"{contract.build}.bom.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [f"missing compiled BOM: {path.relative_to(project_dir)}"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [f"invalid compiled BOM: {exc}"]
    raw_components = data.get("components") if isinstance(data, dict) else None
    if not isinstance(raw_components, list):
        return {}, ["compiled BOM must contain a components list"]
    components: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            errors.append(f"compiled BOM component {index} is invalid")
            continue
        usages = raw.get("usages")
        if not isinstance(usages, list):
            errors.append(f"compiled BOM component {index} lacks usages")
            continue
        identity = {
            "value": str(raw.get("value") or "").strip(),
            "footprint": _normalize_footprint(str(raw.get("package") or "")),
            "mpn": str(raw.get("mpn") or "").strip(),
            "lcsc": str(raw.get("lcsc") or "").strip(),
        }
        for usage in usages:
            reference = usage.get("designator") if isinstance(usage, dict) else None
            if not isinstance(reference, str) or not reference.strip():
                errors.append(f"compiled BOM component {index} has an invalid designator")
                continue
            components[reference.strip()] = identity
    return components, errors


def _compare_final_to_compiled(
    project_dir: Path,
    contract: SchematicContract,
    graph: SchematicGraph,
) -> list[str]:
    errors: list[str] = []
    compiled, bom_errors = _read_compiled_components(project_dir, contract)
    errors.extend(bom_errors)
    schematic = {
        component.reference: {
            "value": component.value,
            "footprint": _normalize_footprint(component.footprint),
            "mpn": component.mpn,
            "lcsc": component.lcsc,
        }
        for component in graph.components
    }
    missing = sorted(set(compiled) - set(schematic))
    unexpected = sorted(set(schematic) - set(compiled))
    if missing:
        errors.append(f"schematic is missing compiled references: {', '.join(missing)}")
    if unexpected:
        errors.append(
            f"schematic has non-compiled references: {', '.join(unexpected)}"
        )
    for reference in sorted(set(compiled) & set(schematic)):
        for field in ("value", "footprint", "mpn", "lcsc"):
            if schematic[reference][field] != compiled[reference][field]:
                errors.append(
                    f"{reference}: schematic {field} "
                    f"{schematic[reference][field]!r}, compiled "
                    f"{compiled[reference][field]!r}"
                )
    try:
        spec = read_spec(project_dir / "spec.md")
        board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
    except (InitInputError, BuildTestError) as exc:
        errors.append(str(exc))
        return errors
    board_nodes = {
        (reference, pin, net)
        for reference, pin, net in board.pad_nets
    }
    schematic_nodes = {
        (reference, pin, net)
        for net, nodes in graph.nets
        for reference, pin in nodes
        if reference in schematic
    }
    missing_nodes = sorted(board_nodes - schematic_nodes)
    extra_nodes = sorted(schematic_nodes - board_nodes)
    if missing_nodes:
        errors.append(
            "schematic is missing compiled pin/net endpoints: "
            + ", ".join(f"{ref}.{pin}={net}" for ref, pin, net in missing_nodes[:12])
        )
    if extra_nodes:
        errors.append(
            "schematic has non-compiled pin/net endpoints: "
            + ", ".join(f"{ref}.{pin}={net}" for ref, pin, net in extra_nodes[:12])
        )
    return errors


def _load_saved_graph(path: Path) -> SchematicGraph:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchematicError(f"invalid proposal evidence: {exc}") from exc
    graph = payload.get("graph") if isinstance(payload, dict) else None
    if not isinstance(graph, dict):
        raise SchematicError("proposal evidence has no canonical graph")
    components = tuple(
        SchematicComponent(
            str(item.get("reference", "")),
            str(item.get("value", "")),
            str(item.get("footprint", "")),
            str(item.get("mpn", "")),
            str(item.get("lcsc", "")),
        )
        for item in graph.get("components", [])
        if isinstance(item, dict)
    )
    nets = tuple(
        (
            str(item.get("name", "")),
            tuple(
                sorted(
                    (
                        str(node.get("reference", "")),
                        str(node.get("pin", "")),
                    )
                    for node in item.get("nodes", [])
                    if isinstance(node, dict)
                )
            ),
        )
        for item in graph.get("nets", [])
        if isinstance(item, dict)
    )
    return SchematicGraph(tuple(sorted(components, key=lambda item: item.reference)), tuple(sorted(nets)))


def _atomic_write_text(path: Path, contents: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == contents:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
        os.replace(temporary_name, path)
    except OSError:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return True


def check_schematic(
    project_dir: Path,
    stage: str,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    write: bool = False,
) -> SchematicResult:
    """Run KiCad ERC/rendering and optional final compiled-design parity."""
    project_dir = project_dir.expanduser().resolve()
    if stage not in STAGES:
        raise SchematicInputError("stage must be proposal or final")
    _read_pins(project_dir)
    contract = read_schematic_contract(project_dir)
    root_rel = contract.proposal_root if stage == "proposal" else contract.final_root
    narrative_rel = (
        contract.proposal_narrative
        if stage == "proposal"
        else contract.final_narrative
    )
    root = project_dir / root_rel
    narrative = project_dir / narrative_rel
    if not root.is_file():
        raise SchematicInputError(f"missing {root_rel.as_posix()}")
    if not narrative.is_file():
        raise SchematicInputError(f"missing {narrative_rel.as_posix()}")
    narrative_text = narrative.read_text(encoding="utf-8")
    if "PCBForge review-only" not in narrative_text:
        raise SchematicInputError(
            f"{narrative_rel.as_posix()} must contain `PCBForge review-only`"
        )
    if stage == "proposal" and "proposal" not in narrative_text.casefold():
        raise SchematicInputError("proposal narrative must identify itself as a proposal")
    if list(root.parent.glob("*.kicad_pcb")):
        raise SchematicInputError(
            "review-only schematic directories must not contain a KiCad PCB"
        )
    if stage == "proposal":
        baseline_ok, baseline_detail = baseline_is_current(project_dir)
        if not baseline_ok:
            raise SchematicError(baseline_detail)

    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise SchematicInputError(str(exc)) from exc
    board_path = project_dir / f"{spec.name}.kicad_pcb"
    board_before = hashlib.sha256(board_path.read_bytes()).hexdigest()
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    kicad = str(tool_root / "scripts" / "kicad-cli")
    with tempfile.TemporaryDirectory(prefix="pcbforge-schematic-") as temporary:
        temporary_dir = Path(temporary)
        netlist_path = temporary_dir / "netlist.xml"
        erc_path = temporary_dir / "erc.json"
        render_dir = temporary_dir / "render"
        render_dir.mkdir()
        netlist = _run(
            runner,
            [
                kicad,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(netlist_path),
                str(root),
            ],
            cwd=project_dir,
        )
        if netlist.returncode != 0:
            raise SchematicError(
                "KiCad netlist export failed: "
                + (netlist.stderr or netlist.stdout).strip()
            )
        erc = _run(
            runner,
            [
                kicad,
                "sch",
                "erc",
                "--severity-all",
                "--format",
                "json",
                "--output",
                str(erc_path),
                "--exit-code-violations",
                str(root),
            ],
            cwd=project_dir,
        )
        violations = _erc_violations(erc_path)
        if erc.returncode != 0 or violations:
            first = violations[0] if violations else {}
            description = str(
                first.get("description")
                or first.get("message")
                or (erc.stderr or erc.stdout).strip()
                or "unknown violation"
            )
            raise SchematicError(
                f"KiCad ERC has {len(violations)} violation(s): {description}"
            )
        rendered = _run(
            runner,
            [
                kicad,
                "sch",
                "export",
                "svg",
                "--output",
                str(render_dir),
                str(root),
            ],
            cwd=project_dir,
        )
        if rendered.returncode != 0:
            raise SchematicError(
                "KiCad SVG export failed: "
                + (rendered.stderr or rendered.stdout).strip()
            )
        svg_paths = tuple(sorted(render_dir.glob("*.svg")))
        if not svg_paths:
            raise SchematicError("KiCad SVG export produced no pages")
        graph = parse_kicad_netlist(netlist_path)
        if not graph.components or not graph.nets:
            raise SchematicError("schematic must contain components and connected nets")
        errors: list[str] = []
        incomplete = [
            component.reference
            for component in graph.components
            if not component.footprint or not component.mpn or not component.lcsc
        ]
        if incomplete:
            errors.append(
                "components missing footprint, MPN, or LCSC identity: "
                + ", ".join(incomplete)
            )
        if stage == "final":
            proposal_graph = _load_saved_graph(
                project_dir
                / contract.proposal_root.parent
                / "evidence.json"
            )
            if _semantic_fingerprint(proposal_graph) != _semantic_fingerprint(graph):
                errors.append(
                    "implemented schematic differs electrically or by part identity "
                    "from the approved proposal; update and reapprove the proposal"
                )
            errors.extend(_compare_final_to_compiled(project_dir, contract, graph))
        if errors:
            raise SchematicError(
                "schematic parity failed:\n  - " + "\n  - ".join(errors)
            )
        board_after = hashlib.sha256(board_path.read_bytes()).hexdigest()
        if board_before != board_after:
            raise SchematicError("schematic checking changed the Atopile-owned PCB")

        evidence_rel = root_rel.parent / "evidence.json"
        render_rel = root_rel.parent / "render"
        payload = {
            "schematic_review_schema": SCHEMATIC_REVIEW_SCHEMA,
            "stage": stage,
            "root": root_rel.as_posix(),
            "narrative": narrative_rel.as_posix(),
            "graph": _graph_payload(graph),
            "semantic_fingerprint": _semantic_fingerprint(graph),
            "erc": {"violations": 0},
            "board_preserved_sha256": board_after,
            "renders": [path.name for path in svg_paths],
        }
        evidence_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        wrote = False
        if write:
            evidence_path = project_dir / evidence_rel
            target_render = project_dir / render_rel
            target_render.parent.mkdir(parents=True, exist_ok=True)
            staged_render = target_render.parent / f".{target_render.name}.stage"
            backup_render = target_render.parent / f".{target_render.name}.backup"
            if staged_render.exists():
                shutil.rmtree(staged_render)
            if backup_render.exists():
                shutil.rmtree(backup_render)
            shutil.copytree(render_dir, staged_render)
            if target_render.exists():
                os.replace(target_render, backup_render)
            try:
                os.replace(staged_render, target_render)
            except OSError:
                if backup_render.exists():
                    os.replace(backup_render, target_render)
                raise
            if backup_render.exists():
                shutil.rmtree(backup_render)
            wrote = _atomic_write_text(evidence_path, evidence_text) or True
        else:
            evidence_path = project_dir / evidence_rel
            try:
                if evidence_path.read_text(encoding="utf-8") != evidence_text:
                    raise SchematicError(
                        f"{evidence_rel.as_posix()} is missing or stale; rerun with --write"
                    )
                existing_renders = tuple(sorted((project_dir / render_rel).glob("*.svg")))
                if [path.name for path in existing_renders] != [
                    path.name for path in svg_paths
                ]:
                    raise SchematicError(
                        f"{render_rel.as_posix()} is missing or stale; rerun with --write"
                    )
                for expected, existing in zip(svg_paths, existing_renders):
                    if expected.read_bytes() != existing.read_bytes():
                        raise SchematicError(
                            f"{render_rel.as_posix()} is stale; rerun with --write"
                        )
            except FileNotFoundError as exc:
                raise SchematicError(
                    f"{evidence_rel.as_posix()} is missing; rerun with --write"
                ) from exc
        fingerprint = schematic_status_fingerprint(project_dir, stage)
        return SchematicResult(
            stage,
            len(graph.components),
            len(graph.nets),
            sum(len(nodes) for _, nodes in graph.nets),
            fingerprint,
            evidence_rel,
            tuple(render_rel / path.name for path in svg_paths),
            wrote,
        )
