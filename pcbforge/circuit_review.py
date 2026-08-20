"""Human-readable circuit review and compiled parity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge import sexpr
from pcbforge.artifact_hash import (
    ArtifactHashError,
    evidence_bytes,
    semantic_bom_bytes,
)
from pcbforge.build_test import (
    BuildTestError,
    ato_source_semantic_bytes,
    board_topology_bytes,
    read_board_evidence,
)
from pcbforge.initialize import InitInputError, read_spec

CIRCUIT_REVIEW_SCHEMA = 2
CIRCUIT_MODEL_SCHEMA = 1
PROJECT_PIN_SCHEMA = 1
CONTRACT_FILENAME = "circuit-review.yaml"
BASELINE_PATH = Path("review/circuit/source-baseline.json")
STAGES = {"proposal", "final"}
SCHEMATIC_AUDIT_SCHEMA = 2
SCH_FORMAT_VERSION = "20250114"
REVIEW_MARKER_TEXT = "PCBForge review-only"
CommandRunner = Callable[..., subprocess.CompletedProcess]
# Tests inject a fake kicad-cli here; production always shells out.
KICAD_RUNNER: CommandRunner | None = None

_COMPONENT_KINDS = {
    "battery",
    "capacitor",
    "connector",
    "crystal",
    "diode",
    "fuse",
    "ic",
    "inductor",
    "led",
    "mechanical",
    "mosfet",
    "resistor",
    "switch",
    "test-point",
    "transistor",
    "other",
}
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_NODE_RE = re.compile(r"^([A-Z][A-Z0-9_-]*)\.([^.\s]+)$")


class CircuitReviewError(RuntimeError):
    """A circuit review validation or comparison failed."""


class CircuitReviewInputError(CircuitReviewError):
    """The circuit review contract or one of its authored inputs is malformed."""


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


@dataclass(frozen=True)
class CircuitReviewContract:
    build: str
    model: Path
    schematic: Path
    proposal_narrative: Path
    final_narrative: Path


@dataclass(frozen=True)
class CircuitComponent:
    reference: str
    kind: str
    value: str
    footprint: str
    mpn: str
    lcsc: str
    purpose: str


@dataclass(frozen=True)
class CircuitNet:
    identifier: str
    display_name: str
    compiler_name: str
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class CircuitGroup:
    identifier: str
    title: str
    purpose: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class CircuitPath:
    identifier: str
    title: str
    purpose: str
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class CircuitModel:
    components: tuple[CircuitComponent, ...]
    nets: tuple[CircuitNet, ...]
    groups: tuple[CircuitGroup, ...]
    paths: tuple[CircuitPath, ...]


@dataclass(frozen=True)
class CircuitReviewResult:
    stage: str
    components: int
    nets: int
    connected_pins: int
    groups: int
    paths: int
    fingerprint: str
    evidence_path: Path
    wrote: bool
    diagram_warnings: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        summary = (
            f"{self.components} components, {self.nets} nets, "
            f"{self.connected_pins} connected pins, {self.groups} groups, "
            f"and {self.paths} review paths passed"
        )
        if self.diagram_warnings:
            summary += f" with {len(self.diagram_warnings)} diagram warning(s)"
        return summary


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except FileNotFoundError as exc:
        raise CircuitReviewInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CircuitReviewInputError(f"invalid {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CircuitReviewInputError(f"{path.name} must be a YAML mapping")
    return loaded


def _strict_keys(
    data: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    field: str,
) -> None:
    unknown = sorted(set(data) - allowed, key=str)
    missing = sorted(required - set(data))
    errors = []
    if unknown:
        errors.append("unknown keys: " + ", ".join(map(str, unknown)))
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if errors:
        raise CircuitReviewInputError(f"{field}: {'; '.join(errors)}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CircuitReviewInputError(f"{field}: expected a non-empty string")
    return value.strip()


def _identifier(value: Any, field: str) -> str:
    identifier = _text(value, field)
    if not _ID_RE.fullmatch(identifier):
        raise CircuitReviewInputError(f"{field}: expected a kebab-case identifier")
    return identifier


def _string_list(value: Any, field: str, *, minimum: int = 1) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CircuitReviewInputError(f"{field}: expected a list")
    items = tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(value))
    if len(items) < minimum:
        raise CircuitReviewInputError(f"{field}: expected at least {minimum} item(s)")
    if len(set(items)) != len(items):
        raise CircuitReviewInputError(f"{field}: duplicate items are not allowed")
    return items


def _safe_path(value: Any, field: str, *, suffix: str, prefix: Path) -> Path:
    path = Path(_text(value, field))
    if path.is_absolute() or ".." in path.parts:
        raise CircuitReviewInputError(f"{field}: path must stay inside the project")
    try:
        path.relative_to(prefix)
    except ValueError as exc:
        raise CircuitReviewInputError(
            f"{field}: path must be under {prefix.as_posix()}/"
        ) from exc
    if path.suffix != suffix:
        raise CircuitReviewInputError(f"{field}: expected a {suffix} file")
    return path


def _read_pins(project_dir: Path) -> None:
    pins = _load_yaml(project_dir / ".pcbforge")
    if type(pins.get("schema")) is not int or pins.get("schema") != PROJECT_PIN_SCHEMA:
        raise CircuitReviewInputError("unsupported version — restart the project")
    guidance = pins.get("guidance")
    if (
        not isinstance(guidance, dict)
        or type(guidance.get("circuit_review_schema")) is not int
        or guidance.get("circuit_review_schema") != CIRCUIT_REVIEW_SCHEMA
    ):
        raise CircuitReviewInputError(
            f"project guidance does not pin circuit review schema "
            f"{CIRCUIT_REVIEW_SCHEMA}"
        )


def _source_baseline_payload(project_dir: Path) -> dict[str, Any]:
    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise CircuitReviewInputError(str(exc)) from exc
    sources = [
        {
            "path": path.relative_to(project_dir).as_posix(),
            "sha256": hashlib.sha256(ato_source_semantic_bytes(path)).hexdigest(),
        }
        for path in sorted(project_dir.glob("src/**/*.ato"))
    ]
    board = project_dir / f"{spec.name}.kicad_pcb"
    try:
        board_hash = hashlib.sha256(
            board_topology_bytes(read_board_evidence(board))
        ).hexdigest()
    except BuildTestError as exc:
        raise CircuitReviewInputError(str(exc)) from exc
    payload: dict[str, Any] = {
        "source_baseline_schema": 1,
        "sources": sources,
        "board_topology_sha256": board_hash,
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return payload


def capture_implementation_baseline(project_dir: Path) -> Path:
    """Capture the source/board handoff at checked ARCHITECT finalization."""
    project_dir = project_dir.expanduser().resolve()
    _read_pins(project_dir)
    path = project_dir / BASELINE_PATH
    _atomic_write(
        path,
        json.dumps(_source_baseline_payload(project_dir), indent=2, sort_keys=True)
        + "\n",
    )
    return BASELINE_PATH


def baseline_is_current(project_dir: Path) -> tuple[bool, str]:
    """Check that physical circuit source did not change before approval."""
    project_dir = project_dir.expanduser().resolve()
    path = project_dir / BASELINE_PATH
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            False,
            f"missing {BASELINE_PATH.as_posix()}; rerun finish-architect",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"invalid {BASELINE_PATH.as_posix()}: {exc}"
    current = _source_baseline_payload(project_dir)
    if not isinstance(saved, dict) or saved.get("fingerprint") != current["fingerprint"]:
        return (
            False,
            "physical source or board topology changed before proposal approval",
        )
    return True, "pre-CIRCUIT source baseline is unchanged"


def read_circuit_review_contract(project_dir: Path) -> CircuitReviewContract:
    """Read the strict circuit review contract."""
    project_dir = project_dir.expanduser().resolve()
    data = _load_yaml(project_dir / CONTRACT_FILENAME)
    keys = {
        "circuit_review_schema",
        "build",
        "model",
        "schematic",
        "proposal_narrative",
        "final_narrative",
    }
    if data.get("circuit_review_schema") == 1 and "diagram" in data:
        raise CircuitReviewInputError(
            "circuit-review.yaml schema 1 (SVG diagram) is no longer supported; "
            "set circuit_review_schema: 2, replace `diagram:` with "
            "`schematic: review/circuit/circuit.kicad_sch`, and author "
            "review/circuit/circuit_schematic.py per agent/circuit-kicad.md"
        )
    _strict_keys(data, allowed=keys, required=keys, field=CONTRACT_FILENAME)
    if type(data.get("circuit_review_schema")) is not int or data.get(
        "circuit_review_schema"
    ) != CIRCUIT_REVIEW_SCHEMA:
        raise CircuitReviewInputError("unsupported version — restart the project")
    build = _text(data.get("build"), "build")
    model = _safe_path(
        data.get("model"),
        "model",
        suffix=".yaml",
        prefix=Path("review/circuit"),
    )
    schematic = _safe_path(
        data.get("schematic"),
        "schematic",
        suffix=".kicad_sch",
        prefix=Path("review/circuit"),
    )
    proposal_narrative = _safe_path(
        data.get("proposal_narrative"),
        "proposal_narrative",
        suffix=".md",
        prefix=Path("docs"),
    )
    final_narrative = _safe_path(
        data.get("final_narrative"),
        "final_narrative",
        suffix=".md",
        prefix=Path("docs"),
    )
    ato = _load_yaml(project_dir / "ato.yaml")
    builds = ato.get("builds")
    if not isinstance(builds, dict) or not isinstance(builds.get(build), dict):
        raise CircuitReviewInputError(f"build {build!r} is not declared in ato.yaml")
    return CircuitReviewContract(
        build,
        model,
        schematic,
        proposal_narrative,
        final_narrative,
    )


def _records(value: Any, field: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CircuitReviewInputError(f"{field}: expected a non-empty list")
    records = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise CircuitReviewInputError(f"{field}[{index}]: expected a mapping")
        records.append(item)
    return records


def read_circuit_model(path: Path) -> CircuitModel:
    """Parse and validate the exact pre-source electrical proposal model."""
    data = _load_yaml(path)
    keys = {"circuit_model_schema", "components", "nets", "groups", "paths"}
    _strict_keys(data, allowed=keys, required=keys, field=path.name)
    if type(data.get("circuit_model_schema")) is not int or data.get(
        "circuit_model_schema"
    ) != CIRCUIT_MODEL_SCHEMA:
        raise CircuitReviewInputError("unsupported version — restart the project")

    components = []
    component_keys = {
        "reference",
        "kind",
        "value",
        "footprint",
        "mpn",
        "lcsc",
        "purpose",
    }
    for index, raw in enumerate(_records(data.get("components"), "components")):
        field = f"components[{index}]"
        _strict_keys(
            raw,
            allowed=component_keys,
            required=component_keys,
            field=field,
        )
        kind = _text(raw.get("kind"), f"{field}.kind")
        if kind not in _COMPONENT_KINDS:
            raise CircuitReviewInputError(
                f"{field}.kind: choose from {', '.join(sorted(_COMPONENT_KINDS))}"
            )
        reference = _text(raw.get("reference"), f"{field}.reference")
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", reference):
            raise CircuitReviewInputError(
                f"{field}.reference: expected an uppercase reference"
            )
        components.append(
            CircuitComponent(
                reference,
                kind,
                _text(raw.get("value"), f"{field}.value"),
                _text(raw.get("footprint"), f"{field}.footprint"),
                _text(raw.get("mpn"), f"{field}.mpn"),
                _text(raw.get("lcsc"), f"{field}.lcsc"),
                _text(raw.get("purpose"), f"{field}.purpose"),
            )
        )
    references = [item.reference for item in components]
    duplicate_references = sorted(
        reference for reference in set(references) if references.count(reference) > 1
    )
    if duplicate_references:
        raise CircuitReviewInputError(
            "components: duplicate references: " + ", ".join(duplicate_references)
        )
    reference_set = set(references)

    nets = []
    net_keys = {"id", "display_name", "compiler_name", "nodes"}
    assigned_nodes: dict[str, str] = {}
    for index, raw in enumerate(_records(data.get("nets"), "nets")):
        field = f"nets[{index}]"
        _strict_keys(
            raw,
            allowed=net_keys,
            required={"id", "display_name", "nodes"},
            field=field,
        )
        identifier = _identifier(raw.get("id"), f"{field}.id")
        compiler_name_raw = raw.get("compiler_name", "")
        if compiler_name_raw is None:
            compiler_name_raw = ""
        if not isinstance(compiler_name_raw, str):
            raise CircuitReviewInputError(f"{field}.compiler_name: expected a string")
        nodes = _string_list(raw.get("nodes"), f"{field}.nodes")
        for node in nodes:
            match = _NODE_RE.fullmatch(node)
            if match is None or match.group(1) not in reference_set:
                raise CircuitReviewInputError(
                    f"{field}.nodes: unknown or invalid endpoint {node!r}"
                )
            if node in assigned_nodes:
                raise CircuitReviewInputError(
                    f"{field}.nodes: {node} is already assigned to "
                    f"{assigned_nodes[node]}"
                )
            assigned_nodes[node] = identifier
        nets.append(
            CircuitNet(
                identifier,
                _text(raw.get("display_name"), f"{field}.display_name"),
                compiler_name_raw.strip(),
                tuple(sorted(nodes)),
            )
        )
    net_ids = [item.identifier for item in nets]
    duplicate_nets = sorted(
        identifier for identifier in set(net_ids) if net_ids.count(identifier) > 1
    )
    if duplicate_nets:
        raise CircuitReviewInputError(
            "nets: duplicate ids: " + ", ".join(duplicate_nets)
        )

    groups = []
    group_keys = {"id", "title", "purpose", "references"}
    grouped: dict[str, str] = {}
    for index, raw in enumerate(_records(data.get("groups"), "groups")):
        field = f"groups[{index}]"
        _strict_keys(
            raw,
            allowed=group_keys,
            required=group_keys,
            field=field,
        )
        identifier = _identifier(raw.get("id"), f"{field}.id")
        group_references = _string_list(raw.get("references"), f"{field}.references")
        for reference in group_references:
            if reference not in reference_set:
                raise CircuitReviewInputError(
                    f"{field}.references: unknown reference {reference!r}"
                )
            if reference in grouped:
                raise CircuitReviewInputError(
                    f"{field}.references: {reference} is already in {grouped[reference]}"
                )
            grouped[reference] = identifier
        groups.append(
            CircuitGroup(
                identifier,
                _text(raw.get("title"), f"{field}.title"),
                _text(raw.get("purpose"), f"{field}.purpose"),
                tuple(sorted(group_references)),
            )
        )
    group_ids = [item.identifier for item in groups]
    if len(set(group_ids)) != len(group_ids):
        raise CircuitReviewInputError("groups: duplicate ids are not allowed")
    ungrouped = sorted(reference_set - set(grouped))
    if ungrouped:
        raise CircuitReviewInputError(
            "groups: every component must appear exactly once; missing "
            + ", ".join(ungrouped)
        )

    paths = []
    path_keys = {"id", "title", "purpose", "nodes"}
    for index, raw in enumerate(_records(data.get("paths"), "paths")):
        field = f"paths[{index}]"
        _strict_keys(
            raw,
            allowed=path_keys,
            required=path_keys,
            field=field,
        )
        nodes = _string_list(raw.get("nodes"), f"{field}.nodes", minimum=2)
        for node in nodes:
            if node not in assigned_nodes:
                raise CircuitReviewInputError(
                    f"{field}.nodes: endpoint {node!r} is not connected by a net"
                )
        for left, right in zip(nodes, nodes[1:]):
            left_ref = left.split(".", 1)[0]
            right_ref = right.split(".", 1)[0]
            if left_ref != right_ref and assigned_nodes[left] != assigned_nodes[right]:
                raise CircuitReviewInputError(
                    f"{field}.nodes: broken path between {left} and {right}"
                )
        paths.append(
            CircuitPath(
                _identifier(raw.get("id"), f"{field}.id"),
                _text(raw.get("title"), f"{field}.title"),
                _text(raw.get("purpose"), f"{field}.purpose"),
                nodes,
            )
        )
    path_ids = [item.identifier for item in paths]
    if len(set(path_ids)) != len(path_ids):
        raise CircuitReviewInputError("paths: duplicate ids are not allowed")

    return CircuitModel(
        tuple(sorted(components, key=lambda item: item.reference)),
        tuple(sorted(nets, key=lambda item: item.identifier)),
        tuple(sorted(groups, key=lambda item: item.identifier)),
        tuple(sorted(paths, key=lambda item: item.identifier)),
    )


def _model_payload(model: CircuitModel) -> dict[str, Any]:
    return {
        "circuit_model_schema": CIRCUIT_MODEL_SCHEMA,
        "components": [
            {
                "reference": item.reference,
                "kind": item.kind,
                "value": item.value,
                "footprint": item.footprint,
                "mpn": item.mpn,
                "lcsc": item.lcsc,
                "purpose": item.purpose,
            }
            for item in model.components
        ],
        "nets": [
            {
                "id": item.identifier,
                "display_name": item.display_name,
                "compiler_name": item.compiler_name,
                "nodes": list(item.nodes),
            }
            for item in model.nets
        ],
        "groups": [
            {
                "id": item.identifier,
                "title": item.title,
                "purpose": item.purpose,
                "references": list(item.references),
            }
            for item in model.groups
        ],
        "paths": [
            {
                "id": item.identifier,
                "title": item.title,
                "purpose": item.purpose,
                "nodes": list(item.nodes),
            }
            for item in model.paths
        ],
    }


def circuit_model_fingerprint(model: CircuitModel) -> str:
    encoded = json.dumps(
        _model_payload(model),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def audit_path_for(schematic_path: Path) -> Path:
    """Sidecar audit record next to the schematic (``circuit.audit.json``)."""
    schematic_path = Path(schematic_path)
    return schematic_path.with_name(schematic_path.stem + ".audit.json")


def _read_audit(path: Path, model: CircuitModel) -> dict[str, Any]:
    label = path.name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CircuitReviewInputError(
            f"missing {label}; run `pcbforge render-circuit`"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CircuitReviewInputError(f"{label}: invalid JSON: {exc}") from exc
    required = {"schema", "model_sha256", "bound_component_refs", "symbol_choices", "nets", "warnings"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise CircuitReviewInputError(f"{label}: invalid audit fields")
    if payload.get("schema") != SCHEMATIC_AUDIT_SCHEMA:
        raise CircuitReviewInputError(f"{label}: unsupported audit schema")
    if payload.get("model_sha256") != circuit_model_fingerprint(model):
        raise CircuitReviewInputError(
            f"{label}: audit is stale for the current model; rerun `pcbforge render-circuit`"
        )
    bound_value = payload.get("bound_component_refs")
    warning_value = payload.get("warnings")
    if (
        not isinstance(bound_value, list)
        or any(not isinstance(item, str) for item in bound_value)
        or len(bound_value) != len(set(bound_value))
    ):
        raise CircuitReviewInputError(f"{label}: bound_component_refs must contain unique strings")
    if not isinstance(warning_value, list):
        raise CircuitReviewInputError(f"{label}: warnings must be a list")
    warnings: list[dict[str, str]] = []
    for index, item in enumerate(warning_value):
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "message"}
            or not isinstance(item.get("code"), str)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", item["code"])
            or not isinstance(item.get("message"), str)
            or not item["message"].strip()
        ):
            raise CircuitReviewInputError(f"{label}: invalid warning at index {index}")
        warnings.append({"code": item["code"], "message": item["message"].strip()})
    choices = payload.get("symbol_choices")
    if not isinstance(choices, dict) or any(
        not isinstance(v, dict) or set(v) != {"lib_id", "generic", "reason"} for v in choices.values()
    ):
        raise CircuitReviewInputError(f"{label}: invalid symbol_choices")
    component_refs = {item.reference for item in model.components}
    unknown = sorted((set(bound_value) | set(choices)) - component_refs)
    if unknown:
        raise CircuitReviewInputError(
            f"{label}: unknown component references: " + ", ".join(unknown)
        )
    return {
        "schema": SCHEMATIC_AUDIT_SCHEMA,
        "bound_component_refs": sorted(bound_value),
        "symbol_choices": {key: choices[key] for key in sorted(choices)},
        "warnings": warnings,
    }


def _kicad_cli(tool_root: Path | None) -> str:
    root = (tool_root or Path(__file__).resolve().parents[1]).resolve()
    return str(root / "scripts" / "kicad-cli")


def _run_kicad(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    label: str,
) -> subprocess.CompletedProcess:
    try:
        completed = runner(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise CircuitReviewInputError(f"cannot run kicad-cli for {label}: {exc}") from exc
    return completed


def _erc_violations(report_path: Path) -> list[str]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CircuitReviewInputError(f"kicad-cli produced no ERC report: {exc}") from exc
    violations: list[str] = []
    for sheet in report.get("sheets", []) if isinstance(report, dict) else []:
        for item in sheet.get("violations", []):
            if item.get("severity") != "error":
                continue
            where = ""
            for entry in item.get("items", []):
                pos = entry.get("pos") or {}
                if pos:
                    where = f" @({pos.get('x', 0):.2f}, {pos.get('y', 0):.2f})"
                    break
            detail = "; ".join(
                str(entry.get("description", "")).strip()
                for entry in item.get("items", [])
                if entry.get("description")
            )
            violations.append(
                f"[{item.get('type', 'erc')}] {item.get('description', '').strip()}"
                f"{where}" + (f": {detail}" if detail else "")
            )
    return violations


def _parse_netlist(path: Path) -> dict[frozenset[str], str]:
    """Endpoint sets (``REF.PIN``) keyed to the KiCad net name, power symbols dropped."""
    try:
        root = sexpr.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, sexpr.SExprError) as exc:
        raise CircuitReviewInputError(f"kicad-cli produced no netlist: {exc}") from exc
    nets: dict[frozenset[str], str] = {}
    nets_node = sexpr.child(root, "nets")
    for net in sexpr.children(nets_node or [], "net"):
        name = sexpr.atom(sexpr.child(net, "name"))
        # local labels on the root sheet export as "/NAME"; power nets are bare
        if name.startswith("/") and name.count("/") == 1:
            name = name[1:]
        endpoints = set()
        for node in sexpr.children(net, "node"):
            ref = sexpr.atom(sexpr.child(node, "ref"))
            pin = sexpr.atom(sexpr.child(node, "pin"))
            if ref.startswith("#"):
                continue
            endpoints.add(f"{ref}.{pin}")
        if endpoints:
            nets[frozenset(endpoints)] = name
    return nets


def _symbol_properties(symbol: sexpr.Node) -> dict[str, str]:
    return {
        sexpr.atom(prop, 1): sexpr.atom(prop, 2)
        for prop in sexpr.children(symbol, "property")
    }


def _sheet_texts(root: sexpr.Node) -> list[str]:
    texts = [sexpr.atom(node) for node in sexpr.children(root, "text")]
    block = sexpr.child(root, "title_block")
    if block is not None:
        texts += [sexpr.atom(node) for node in sexpr.children(block, "title")]
        texts += [sexpr.atom(node, 2) for node in sexpr.children(block, "comment")]
    return texts


def validate_circuit_schematic(
    path: Path,
    model: CircuitModel,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Validate structure, model binding, ERC and pin-exact netlist parity.

    Returns the audit record (schema, bound references, symbol choices,
    readability warnings) that the evidence file embeds.
    """
    path = Path(path)
    label = path.name
    runner = runner or KICAD_RUNNER or subprocess.run
    try:
        root = sexpr.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CircuitReviewInputError(f"missing {label}; run `pcbforge render-circuit`") from exc
    except (OSError, UnicodeError, sexpr.SExprError) as exc:
        raise CircuitReviewInputError(f"{label}: not a KiCad schematic: {exc}") from exc
    if sexpr.head(root) != "kicad_sch":
        raise CircuitReviewInputError(f"{label}: not a KiCad schematic")
    if sexpr.atom(sexpr.child(root, "version")) != SCH_FORMAT_VERSION:
        raise CircuitReviewInputError(
            f"{label}: expected schematic format version {SCH_FORMAT_VERSION}"
        )
    if sexpr.atom(sexpr.child(root, "generator")) != "pcbforge":
        raise CircuitReviewInputError(f"{label}: must be generated by pcbforge render-circuit")
    forbidden = {"sheet", "image", "embedded_files", "bus", "bus_entry"}
    present = sorted({sexpr.head(node) for node in root if isinstance(node, list)} & forbidden)
    if present:
        raise CircuitReviewInputError(
            f"{label}: unsupported elements in a review schematic: " + ", ".join(present)
        )
    texts = _sheet_texts(root)
    if not any(REVIEW_MARKER_TEXT in text for text in texts):
        raise CircuitReviewInputError(
            f"{label}: missing the visible `{REVIEW_MARKER_TEXT}` marker"
        )
    expected = circuit_model_fingerprint(model)
    if not any(f"pcbforge_model_sha256={expected}" in text for text in texts):
        raise CircuitReviewInputError(
            f"{label}: model fingerprint is missing or stale; expected "
            f"pcbforge_model_sha256={expected} (rerun `pcbforge render-circuit`)"
        )

    components = {item.reference: item for item in model.components}
    group_of = {ref: group.identifier for group in model.groups for ref in group.references}
    seen: dict[str, int] = {}
    errors: list[str] = []
    for symbol in sexpr.children(root, "symbol"):
        props = _symbol_properties(symbol)
        reference = props.get("Reference", "")
        if reference.startswith("#"):
            continue
        component = components.get(reference)
        if component is None:
            errors.append(f"symbol {reference!r} is not in the model")
            continue
        seen[reference] = seen.get(reference, 0) + 1
        if props.get("Value") != component.value:
            errors.append(f"{reference}: value {props.get('Value')!r} != model {component.value!r}")
        if _normalize_footprint(props.get("Footprint", "")) != _normalize_footprint(component.footprint):
            errors.append(
                f"{reference}: footprint {props.get('Footprint')!r} != model {component.footprint!r}"
            )
        if props.get("pcbforge_group") != group_of.get(reference):
            errors.append(f"{reference}: pcbforge_group {props.get('pcbforge_group')!r} != {group_of.get(reference)!r}")
        if props.get("pcbforge_purpose") != component.purpose:
            errors.append(f"{reference}: pcbforge_purpose does not match the model")
    missing = sorted(set(components) - set(seen))
    if missing:
        errors.append("model components missing from the schematic: " + ", ".join(missing))
    duplicated = sorted(ref for ref, count in seen.items() if count > 1)
    multi_unit_ok = set()
    for ref in duplicated:
        units = {
            sexpr.atom(sexpr.child(symbol, "unit"))
            for symbol in sexpr.children(root, "symbol")
            if _symbol_properties(symbol).get("Reference") == ref
        }
        if len(units) == seen[ref]:
            multi_unit_ok.add(ref)
    duplicated = [ref for ref in duplicated if ref not in multi_unit_ok]
    if duplicated:
        errors.append("components placed more than once: " + ", ".join(duplicated))
    group_titles = {group.title for group in model.groups}
    shown = set(texts)
    missing_groups = sorted(title for title in group_titles if title not in shown)
    if missing_groups:
        errors.append("group titles missing from the sheet: " + ", ".join(missing_groups))
    if errors:
        raise CircuitReviewError(f"{label}: structural checks failed:\n  - " + "\n  - ".join(errors))

    audit = _read_audit(audit_path_for(path), model)
    placed = set(audit["bound_component_refs"])
    if placed != set(components):
        raise CircuitReviewInputError(
            f"{audit_path_for(path).name}: bound_component_refs do not match the model"
        )

    kicad = _kicad_cli(tool_root)
    with tempfile.TemporaryDirectory(prefix="pcbforge-sch-") as temporary:
        staging = Path(temporary)
        erc_path = staging / "erc.json"
        completed = _run_kicad(
            runner,
            (kicad, "sch", "erc", "--format", "json", "--severity-error", "--output", str(erc_path), str(path)),
            label="ERC",
        )
        if not erc_path.is_file():
            tail = (completed.stdout or "").strip().splitlines()[-1:] or [""]
            raise CircuitReviewInputError(
                f"kicad-cli could not run ERC on {label} (exit {completed.returncode}) {tail[0]}".strip()
            )
        violations = _erc_violations(erc_path)
        if violations:
            raise CircuitReviewError(f"{label}: ERC errors:\n  - " + "\n  - ".join(violations))
        net_path = staging / "review.net"
        completed = _run_kicad(
            runner,
            (kicad, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(net_path), str(path)),
            label="netlist export",
        )
        if not net_path.is_file():
            raise CircuitReviewInputError(
                f"kicad-cli could not export a netlist from {label} (exit {completed.returncode})"
            )
        schematic_nets = _parse_netlist(net_path)

    model_nets = {frozenset(item.nodes): item for item in model.nets}
    missing_topology = sorted(
        model_nets[endpoints].identifier for endpoints in set(model_nets) - set(schematic_nets)
    )
    extra_topology = sorted(
        schematic_nets[endpoints] + " {" + ", ".join(sorted(endpoints)) + "}"
        for endpoints in set(schematic_nets) - set(model_nets)
    )
    parity: list[str] = []
    if missing_topology:
        parity.append("schematic is missing proposed endpoint sets: " + ", ".join(missing_topology))
    if extra_topology:
        parity.append("schematic has unproposed endpoint sets: " + ", ".join(extra_topology))
    for endpoints in set(model_nets) & set(schematic_nets):
        net = model_nets[endpoints]
        actual = schematic_nets[endpoints]
        if len(endpoints) < 2:
            continue
        if actual != net.display_name and not actual.startswith("Net-("):
            parity.append(
                f"{net.identifier}: schematic net is named {actual!r}, expected {net.display_name!r}"
            )
    if parity:
        raise CircuitReviewError(f"{label}: netlist parity failed:\n  - " + "\n  - ".join(parity))

    unlabeled = sorted(
        model_nets[endpoints].identifier
        for endpoints in set(model_nets) & set(schematic_nets)
        if len(endpoints) >= 2 and schematic_nets[endpoints].startswith("Net-(")
    )
    warnings = list(audit["warnings"])
    for identifier in unlabeled:
        warnings.append(
            {
                "code": "unlabeled-net",
                "message": f"net {identifier} has no label or power symbol showing its name",
            }
        )
    unique = {(item["code"], item["message"]): item for item in warnings}
    audit["warnings"] = [unique[key] for key in sorted(unique)]
    return audit


def _normalize_footprint(value: str) -> str:
    return value.replace(".pretty:", ":").strip()


def _compiled_components(
    project_dir: Path,
    contract: CircuitReviewContract,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    path = (
        project_dir / "build" / "builds" / contract.build / f"{contract.build}.bom.json"
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
    errors = []
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict) or not isinstance(raw.get("usages"), list):
            errors.append(f"compiled BOM component {index} is invalid")
            continue
        identity = {
            "value": str(raw.get("value") or "").strip(),
            "footprint": _normalize_footprint(str(raw.get("package") or "")),
            "mpn": str(raw.get("mpn") or "").strip(),
            "lcsc": str(raw.get("lcsc") or "").strip(),
        }
        for usage in raw["usages"]:
            reference = usage.get("designator") if isinstance(usage, dict) else None
            if not isinstance(reference, str) or not reference.strip():
                errors.append(
                    f"compiled BOM component {index} has an invalid designator"
                )
                continue
            if reference.strip() in components:
                errors.append(
                    f"compiled BOM has duplicate designator {reference.strip()}"
                )
            components[reference.strip()] = identity
    return components, errors


def _compare_to_compiled(
    project_dir: Path,
    contract: CircuitReviewContract,
    model: CircuitModel,
) -> tuple[list[str], dict[str, str]]:
    errors = []
    compiled, component_errors = _compiled_components(project_dir, contract)
    errors.extend(component_errors)
    try:
        spec = read_spec(project_dir / "spec.md")
        board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
    except (InitInputError, BuildTestError) as exc:
        errors.append(str(exc))
        return errors, {}
    proposed = {
        item.reference: {
            "kind": item.kind,
            "value": item.value,
            "footprint": _normalize_footprint(item.footprint),
            "mpn": item.mpn,
            "lcsc": item.lcsc,
        }
        for item in model.components
    }
    unfitted = {
        reference
        for reference, identity in proposed.items()
        if identity["lcsc"].casefold() == "n/a"
    }
    invalid_unfitted = sorted(
        reference
        for reference in unfitted
        if proposed[reference]["kind"] not in {"mechanical", "test-point"}
    )
    if invalid_unfitted:
        errors.append(
            "only mechanical and test-point PCB features may use LCSC N/A: "
            + ", ".join(invalid_unfitted)
        )
    fitted = set(proposed) - unfitted
    missing = sorted(set(compiled) - set(proposed))
    unexpected = sorted(fitted - set(compiled))
    if missing:
        errors.append("model is missing compiled references: " + ", ".join(missing))
    if unexpected:
        errors.append("model has non-compiled references: " + ", ".join(unexpected))
    for reference in sorted(set(compiled) & set(proposed)):
        for field in ("footprint", "mpn", "lcsc"):
            if proposed[reference][field] != compiled[reference][field]:
                errors.append(
                    f"{reference}: proposed {field} {proposed[reference][field]!r}, "
                    f"compiled {compiled[reference][field]!r}"
                )
        compiled_value = compiled[reference]["value"]
        if compiled_value and proposed[reference]["value"] != compiled_value:
            errors.append(
                f"{reference}: proposed value {proposed[reference]['value']!r}, "
                f"compiled {compiled_value!r}"
            )
    board_footprints = {
        reference: _normalize_footprint(footprint)
        for reference, footprint in board.footprints
    }
    missing_from_model = sorted(set(board_footprints) - set(proposed))
    missing_from_board = sorted(set(proposed) - set(board_footprints))
    if missing_from_model:
        errors.append(
            "model is missing PCB references: " + ", ".join(missing_from_model)
        )
    if missing_from_board:
        errors.append(
            "compiled PCB is missing model references: " + ", ".join(missing_from_board)
        )
    for reference in sorted(set(proposed) & set(board_footprints)):
        if proposed[reference]["footprint"] != board_footprints[reference]:
            errors.append(
                f"{reference}: proposed footprint "
                f"{proposed[reference]['footprint']!r}, compiled PCB "
                f"{board_footprints[reference]!r}"
            )
    board_nets: dict[frozenset[str], str] = {}
    for reference, pin, name in board.pad_nets:
        endpoint_set = frozenset(
            f"{item_reference}.{item_pin}"
            for item_reference, item_pin, item_name in board.pad_nets
            if item_name == name
        )
        board_nets[endpoint_set] = name
    model_nets = {frozenset(item.nodes): item for item in model.nets}
    missing_topology = sorted(
        model_nets[endpoints].identifier
        for endpoints in set(model_nets) - set(board_nets)
    )
    extra_topology = sorted(
        board_nets[endpoints] for endpoints in set(board_nets) - set(model_nets)
    )
    if missing_topology:
        errors.append(
            "compiled PCB is missing proposed endpoint sets: "
            + ", ".join(missing_topology)
        )
    if extra_topology:
        errors.append(
            "compiled PCB has unproposed endpoint sets: " + ", ".join(extra_topology)
        )
    compiler_names = {}
    for endpoints in set(model_nets) & set(board_nets):
        proposed_net = model_nets[endpoints]
        actual_name = board_nets[endpoints]
        compiler_names[proposed_net.identifier] = actual_name
        if proposed_net.compiler_name and proposed_net.compiler_name != actual_name:
            errors.append(
                f"{proposed_net.identifier}: expected compiler net "
                f"{proposed_net.compiler_name!r}, got {actual_name!r}"
            )
    return errors, dict(sorted(compiler_names.items()))


def _evidence_path(stage: str) -> Path:
    return Path("review") / "circuit" / stage / "evidence.json"


def circuit_review_inputs(project_dir: Path, stage: str) -> tuple[Path, ...]:
    """Return tracked inputs whose changes stale a circuit review check."""
    if stage not in STAGES:
        raise CircuitReviewInputError("stage must be proposal or final")
    project_dir = project_dir.expanduser().resolve()
    contract = read_circuit_review_contract(project_dir)
    narrative = (
        contract.proposal_narrative if stage == "proposal" else contract.final_narrative
    )
    candidates = {
        project_dir / ".pcbforge",
        project_dir / CONTRACT_FILENAME,
        project_dir / "spec.md",
        project_dir / "docs" / "architecture.md",
        project_dir / BASELINE_PATH,
        project_dir / contract.model,
        project_dir / contract.schematic,
        audit_path_for(project_dir / contract.schematic),
        project_dir / narrative,
    }
    if stage == "final":
        candidates.update(project_dir.glob("src/**/*.ato"))
        candidates.add(project_dir / "ato.yaml")
        candidates.add(project_dir / _evidence_path("proposal"))
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
    return tuple(sorted(path for path in candidates if path.is_file()))


def circuit_review_status_fingerprint(project_dir: Path, stage: str) -> str:
    digest = hashlib.sha256()
    for path in circuit_review_inputs(project_dir, stage):
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(b"\0")
        if path.suffix == ".kicad_pcb":
            try:
                digest.update(board_topology_bytes(read_board_evidence(path)))
            except BuildTestError:
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        elif path.name.endswith(".bom.json"):
            try:
                digest.update(semantic_bom_bytes(path))
            except ArtifactHashError as exc:
                raise CircuitReviewInputError(str(exc)) from exc
        elif path.suffix == ".ato":
            digest.update(hashlib.sha256(ato_source_semantic_bytes(path)).digest())
        else:
            digest.update(hashlib.sha256(evidence_bytes(path)).digest())
    return digest.hexdigest()


def _atomic_write(path: Path, contents: str) -> bool:
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


def check_circuit_review(
    project_dir: Path,
    stage: str,
    *,
    write: bool = False,
    tool_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> CircuitReviewResult:
    """Validate proposal comprehension evidence or final compiled parity."""
    project_dir = project_dir.expanduser().resolve()
    if stage not in STAGES:
        raise CircuitReviewInputError("stage must be proposal or final")
    _read_pins(project_dir)
    contract = read_circuit_review_contract(project_dir)
    model_path = project_dir / contract.model
    schematic_path = project_dir / contract.schematic
    narrative_rel = (
        contract.proposal_narrative if stage == "proposal" else contract.final_narrative
    )
    narrative_path = project_dir / narrative_rel
    if not narrative_path.is_file():
        raise CircuitReviewInputError(f"missing {narrative_rel.as_posix()}")
    narrative = narrative_path.read_text(encoding="utf-8")
    if "PCBForge review-only" not in narrative:
        raise CircuitReviewInputError(
            f"{narrative_rel.as_posix()} must contain `PCBForge review-only`"
        )
    if stage == "proposal" and "proposal" not in narrative.casefold():
        raise CircuitReviewInputError("proposal narrative must identify its proposal")
    review_root = project_dir / "review" / "circuit"
    if any(review_root.rglob("*.kicad_pcb")):
        raise CircuitReviewInputError(
            "review-only circuit directories must not contain a KiCad PCB"
        )
    if any(review_root.rglob("*.kicad_pro")):
        raise CircuitReviewInputError(
            "review-only circuit directories must not contain a KiCad project; "
            "the review schematic opens standalone and never drives a board"
        )

    model = read_circuit_model(model_path)
    schematic_audit = validate_circuit_schematic(
        schematic_path, model, tool_root=tool_root, runner=runner
    )
    model_fingerprint = circuit_model_fingerprint(model)
    if stage == "proposal":
        baseline_ok, detail = baseline_is_current(project_dir)
        if not baseline_ok:
            raise CircuitReviewError(detail)
        compiler_names: dict[str, str] = {}
    else:
        proposal_path = project_dir / _evidence_path("proposal")
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CircuitReviewError("missing current proposal evidence") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CircuitReviewError(f"invalid proposal evidence: {exc}") from exc
        if (
            not isinstance(proposal, dict)
            or proposal.get("model_semantic_fingerprint") != model_fingerprint
        ):
            raise CircuitReviewError(
                "circuit model differs from the approved proposal; "
                "update and reapprove the proposal"
            )
        errors, compiler_names = _compare_to_compiled(
            project_dir,
            contract,
            model,
        )
        if errors:
            raise CircuitReviewError(
                "compiled circuit parity failed:\n  - " + "\n  - ".join(errors)
            )

    try:
        spec = read_spec(project_dir / "spec.md")
        board = read_board_evidence(project_dir / f"{spec.name}.kicad_pcb")
    except (InitInputError, BuildTestError) as exc:
        raise CircuitReviewInputError(str(exc)) from exc
    board_hash = hashlib.sha256(board_topology_bytes(board)).hexdigest()
    payload = {
        "circuit_review_schema": CIRCUIT_REVIEW_SCHEMA,
        "stage": stage,
        "model_path": contract.model.as_posix(),
        "schematic": contract.schematic.as_posix(),
        "narrative": narrative_rel.as_posix(),
        "model_semantic_fingerprint": model_fingerprint,
        "model": _model_payload(model),
        "schematic_sha256": hashlib.sha256(schematic_path.read_bytes()).hexdigest(),
        "schematic_audit": schematic_audit,
        "narrative_sha256": hashlib.sha256(narrative_path.read_bytes()).hexdigest(),
        "board_topology_sha256": board_hash,
        "compiler_net_names": compiler_names,
        "material_differences": [],
        "source_baseline_sha256": hashlib.sha256(
            (project_dir / BASELINE_PATH).read_bytes()
        ).hexdigest(),
    }
    if stage == "final":
        bom_path = (
            project_dir
            / "build"
            / "builds"
            / contract.build
            / f"{contract.build}.bom.json"
        )
        payload["proposal_evidence_sha256"] = hashlib.sha256(
            (project_dir / _evidence_path("proposal")).read_bytes()
        ).hexdigest()
        try:
            payload["compiled_bom_sha256"] = hashlib.sha256(
                semantic_bom_bytes(bom_path)
            ).hexdigest()
        except ArtifactHashError as exc:
            raise CircuitReviewInputError(str(exc)) from exc
        payload["source_sha256"] = {
            path.relative_to(project_dir).as_posix(): hashlib.sha256(
                ato_source_semantic_bytes(path)
            ).hexdigest()
            for path in sorted(project_dir.glob("src/**/*.ato"))
        }
    evidence_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    evidence_rel = _evidence_path(stage)
    evidence_path = project_dir / evidence_rel
    wrote = False
    if write:
        wrote = _atomic_write(evidence_path, evidence_text)
    else:
        try:
            if evidence_path.read_text(encoding="utf-8") != evidence_text:
                raise CircuitReviewError(
                    f"{evidence_rel.as_posix()} is missing or stale; rerun with --write"
                )
        except FileNotFoundError as exc:
            raise CircuitReviewError(
                f"{evidence_rel.as_posix()} is missing; rerun with --write"
            ) from exc
    diagram_warnings = tuple(
        f"[{item['code']}] {item['message']}" for item in schematic_audit["warnings"]
    )
    return CircuitReviewResult(
        stage,
        len(model.components),
        len(model.nets),
        sum(len(net.nodes) for net in model.nets),
        len(model.groups),
        len(model.paths),
        circuit_review_status_fingerprint(project_dir, stage),
        evidence_rel,
        wrote,
        diagram_warnings,
    )
