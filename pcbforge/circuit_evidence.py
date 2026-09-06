"""Shared board and accepted schematic evidence for downstream tools."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from pcbforge.schematic import SchematicError
from pcbforge.electrical import ElectricalError

class CircuitEvidenceError(SchematicError):
    pass
class CircuitEvidenceInputError(CircuitEvidenceError):
    pass

ELECTRICAL_TEST_FILENAME = "circuit-tests.yaml"
CIRCUIT_REPORT_PATH = Path("docs/circuit-check.md")
ELECTRICAL_TEST_SCHEMA = 1
CIRCUIT_REPORT_SCHEMA = 2
PROJECT_PIN_SCHEMA = 2

TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')
HEAD_RE = re.compile(r"^\(\s*([^\s()]+)")
REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"((?:\\.|[^"])*)"')
FOOTPRINT_NAME_RE = re.compile(r'^\(footprint\s+"((?:\\.|[^"])*)"')
AT_RE = re.compile(r"\n\s*\(at\s+([^)]+)\)")
LAYER_RE = re.compile(r'\n\s*\(layer\s+"([^"]+)"\)')
PAD_RE = re.compile(r'^\(pad\s+"?([^"\s)]+)"?')
NET_RE = re.compile(r'\(net\s+(?:"([^"]+)"|([^\s()]+))(?:\s+"((?:\\.|[^"])*)")?\)')
SCHEMATIC_LINK_RE = re.compile(
    r'\n\s*\(path\s+"|\(property\s+"(?:Sheetname|Sheetfile)"'
)


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
    schematic_links: tuple[str, ...] = ()


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
        raise CircuitEvidenceInputError(f"missing {path.name}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise CircuitEvidenceError(f"cannot inspect {path}: {exc}") from exc

    references: list[str] = []
    footprints: list[tuple[str, str]] = []
    placements: list[tuple[str, str, str]] = []
    pads: list[tuple[str, str]] = []
    pad_nets: list[tuple[str, str, str]] = []
    schematic_links: list[str] = []
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
        if SCHEMATIC_LINK_RE.search(block):
            schematic_links.append(reference)
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
        tuple(sorted(schematic_links)),
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
        errors.append("the transition changed footprint placement, side, or membership")
    if (
        before.user_art_count != after.user_art_count
        or before.user_art_sha256 != after.user_art_sha256
    ):
        errors.append(
            "the transition changed tracks, vias, zones, outline, graphics, or user artwork"
        )
    return errors


@dataclass(frozen=True)
class BomComponent:
    lcsc: str
    mpn: str
    footprint: str
    quantity: int
    designators: tuple[str, ...]


def read_bom_components(path: Path) -> tuple[BomComponent, ...]:
    try:
        data = json.loads(path.read_text())
        if data.get("schema") != 1 or not isinstance(data.get("components"), list):
            raise ValueError("expected native BOM schema 1")
        components = []
        seen = set()
        for raw in data["components"]:
            item = dict(raw)
            item["designators"] = tuple(item["designators"])
            component = BomComponent(**item)
            if component.quantity != len(component.designators) or component.quantity < 1 or not component.lcsc or not component.mpn:
                raise ValueError("invalid BOM identity or quantity")
            if seen.intersection(component.designators):
                raise ValueError("duplicate BOM reference")
            seen.update(component.designators)
            components.append(component)
        return tuple(components)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CircuitEvidenceInputError(f"cannot read schematic BOM: {exc}") from exc


def read_circuit_inventory(project_dir: Path):
    """Derived inventory view for layout, policy and fabrication consumers."""
    from pcbforge.circuit import load_graph
    from pcbforge.electrical import read_contract
    try:
        graph = load_graph(project_dir)
        contract = read_contract(project_dir)
        return SimpleNamespace(build="default", bom=tuple(BomComponent(**dict(row, designators=tuple(row["designators"]))) for row in graph.bom()),
                               board_footprints=sum(not c.exclude_board for c in graph.components),
                               assertions=tuple(t["id"] for t in contract["tests"]))
    except (SchematicError, ElectricalError) as exc:
        raise CircuitEvidenceInputError(str(exc)) from exc


def fingerprint_inputs(project_dir: Path) -> str:
    from pcbforge.circuit import fingerprint_inputs as native_fingerprint
    try:
        return native_fingerprint(project_dir)
    except (SchematicError, ElectricalError, OSError) as exc:
        raise CircuitEvidenceInputError(str(exc)) from exc


def circuit_evidence_inputs(project_dir: Path) -> tuple[Path, ...]:
    from pcbforge.circuit import circuit_inputs
    try:
        return circuit_inputs(project_dir)
    except (SchematicError, ElectricalError, OSError) as exc:
        raise CircuitEvidenceInputError(str(exc)) from exc


def saved_report_status(project_dir: Path, fingerprint: str) -> tuple[bool, str]:
    from pcbforge.circuit import read_evidence
    try:
        evidence = read_evidence(project_dir)
        return evidence["fingerprint"] == fingerprint, "native circuit acceptance is current"
    except (SchematicError, ElectricalError, OSError) as exc:
        return False, str(exc)


def require_current_acceptance(project_dir: Path) -> None:
    from pcbforge.circuit import read_evidence
    try:
        read_evidence(project_dir)
    except (SchematicError, ElectricalError, OSError) as exc:
        raise CircuitEvidenceInputError(str(exc)) from exc

