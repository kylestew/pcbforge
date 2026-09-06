"""Native schematic acceptance, evidence and previews."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import io
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from pcbforge import sexpr as sx
from pcbforge.artifact_hash import evidence_bytes
from pcbforge.electrical import ElectricalError, read_contract, read_facts, read_yaml, run_tests
from pcbforge.fsutil import commit_outputs
from pcbforge.kicad_fp import footprint_pads, footprint_path, footprints_dir
from pcbforge.kicad_sym import lib_symbol, symbols_dir
from pcbforge.kicad_tools import VERSION, check_version, command
from pcbforge.schematic import CircuitGraph, SchematicError, canonical, digest, extract, hierarchy, project_schematic, source_files, semantic_diff, from_payload
from pcbforge.schematic_lint import Finding, lint_saved

REPORT_PATH = Path("docs/circuit-check.md")
EVIDENCE_PATH = Path("review/circuit/evidence.json")
GRAPH_PATH = Path("build/circuit/graph.json")
BOM_PATH = Path("build/circuit/bom.json")
BOM_CSV_PATH = Path("build/circuit/bom.csv")
CONTRACT_PATH = Path("circuit-tests.yaml")
_GRAPH_CACHE: dict[tuple, CircuitGraph] = {}


@dataclass(frozen=True)
class CircuitCheckResult:
    graph: CircuitGraph
    fingerprint: str
    presentation_fingerprint: str
    findings: tuple[Finding, ...]
    tests: tuple[dict, ...]
    report: str
    wrote: bool = False

    @property
    def summary(self):
        return f"{len(self.graph.components)} components, {len(self.graph.nets)} nets, {len(self.tests)} electrical tests passed"


def circuit_inputs(project_dir: Path) -> tuple[Path, ...]:
    project_dir = project_dir.resolve()
    paths = set(source_files(project_schematic(project_dir)))
    for pattern in ("spec.md", ".pcbforge", "circuit-tests.yaml", "electrical-facts.yaml", "circuit-review.yaml",
                    "firmware/*.ioc", "*.kicad_pro", "*-lib-table", "parts/**/*"):
        paths.update(p for p in project_dir.glob(pattern) if p.is_file())
    # Bind local Python helpers as well as the explicitly listed entry points.
    ignored = {".git", ".venv", "venv", "__pycache__", "build", "pcb-update-backups"}
    paths.update(p for p in project_dir.rglob("*.py") if not ignored.intersection(p.relative_to(project_dir).parts))
    # Include every listed callable even if the project uses another directory.
    if (project_dir / CONTRACT_PATH).is_file():
        from pcbforge.electrical import resolve_callable
        contract = read_contract(project_dir)
        for relative in contract.get("inputs", []):
            paths.add(project_dir / relative)
        for test in contract["tests"]:
            paths.add(resolve_callable(project_dir, test["callable"])[0])
    if any(not p.resolve().is_relative_to(project_dir) for p in paths):
        raise SchematicError("circuit inputs must be self-contained within the project")
    return tuple(sorted(paths))


def source_digest(project_dir: Path) -> str:
    project_dir = project_dir.resolve()
    return digest([(p.relative_to(project_dir).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
                   for p in circuit_inputs(project_dir)])


def load_graph(project_dir: Path, *, tool_root=None, runner=subprocess.run) -> CircuitGraph:
    project_dir = project_dir.resolve()
    schematic = project_schematic(project_dir)
    paths = (*source_files(schematic), *project_dir.glob("*.kicad_pro"))
    key = (str(project_dir.resolve()), str(tool_root), id(runner),
           tuple((str(p), hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths))
    if key not in _GRAPH_CACHE:
        if len(_GRAPH_CACHE) > 32:
            _GRAPH_CACHE.clear()
        _GRAPH_CACHE[key] = extract(schematic, tool_root=tool_root, runner=runner)
    return _GRAPH_CACHE[key]


def _project_electrical(path: Path) -> bytes:
    data = json.loads(path.read_text())
    if data.get("variants"):
        raise SchematicError("assembly variants are unsupported; use one explicit native configuration")
    # Board net classes and editor view state are owned by later phases.
    return canonical({k: data.get(k) for k in ("erc", "text_variables", "variants")})


def fingerprint_inputs(project_dir: Path, *, graph=None, tool_root=None, runner=subprocess.run, include_spec=True) -> str:
    project_dir = project_dir.resolve()
    graph = graph or load_graph(project_dir, tool_root=tool_root, runner=runner)
    values = [("circuit", graph.fingerprint), ("kicad", VERSION)]
    # The installed version alone cannot detect a locally modified library.
    root = tool_root or Path(__file__).resolve().parents[1]
    fp_dir, sym_dir = footprints_dir(root), symbols_dir(root)
    libraries = {}
    for component in graph.components:
        path = footprint_path(component.footprint, fp_dir, project_dir)
        if path:
            libraries["footprint:" + component.footprint] = hashlib.sha256(path.read_bytes()).hexdigest()
        path = sym_dir / (component.symbol.split(":", 1)[0] + ".kicad_sym")
        if path.is_file():
            libraries["symbol:" + path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()
    values.append(("libraries", digest(libraries)))
    for path in circuit_inputs(project_dir):
        if path.suffix == ".kicad_sch":
            continue
        if path.name == "spec.md":
            if not include_spec:
                continue
            from pcbforge.status import spec_contract_digest
            value = spec_contract_digest(project_dir).encode()
        elif path.suffix == ".kicad_pro":
            value = _project_electrical(path)
        elif path.name == "circuit-review.yaml":
            # Readability exclusions affect visual evidence, not electrical intent.
            data = read_yaml(path)
            value = canonical({k: v for k, v in data.items() if k != "readability_exclusions"})
        else:
            value = evidence_bytes(path)
        values.append((path.relative_to(project_dir).as_posix(), hashlib.sha256(value).hexdigest()))
    return digest(values)


def presentation_fingerprint(project_dir: Path) -> str:
    project_dir = project_dir.resolve()
    files = source_files(project_schematic(project_dir))
    values = [(p.relative_to(project_dir).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    contract = project_dir / "circuit-review.yaml"
    if contract.is_file():
        values.append((contract.name, hashlib.sha256(contract.read_bytes()).hexdigest()))
    return digest(values)


def _review_contract(project_dir: Path) -> dict:
    path = project_dir / "circuit-review.yaml"
    data = read_yaml(path)
    if data.get("circuit_review_schema") != 4 or set(data) - {"circuit_review_schema", "erc_exclusions", "readability_exclusions"}:
        raise SchematicError("circuit-review.yaml: expected native schema 4 and explicit exclusions")
    for name in ("erc_exclusions", "readability_exclusions"):
        values = data.get(name, [])
        if not isinstance(values, list) or any(not isinstance(v, dict) or set(v) != {"id", "rationale"}
                                               or not v["id"] or not v["rationale"] for v in values):
            raise SchematicError(f"{name}: each exclusion needs an exact finding ID and rationale")
        if len({v["id"] for v in values}) != len(values):
            raise SchematicError(f"{name}: duplicate exclusions")
    return data


def erc_findings(path: Path, *, tool_root=None, runner=subprocess.run) -> tuple[Finding, ...]:
    with tempfile.TemporaryDirectory(prefix="pcbforge-erc-") as temp:
        output = Path(temp) / "erc.json"
        result = runner([command(tool_root), "sch", "erc", "--format", "json", "--severity-all", "--output", str(output), str(path)],
                        capture_output=True, text=True, check=False)
        if result.returncode or not output.is_file():
            raise SchematicError(f"KiCad ERC failed: {(result.stderr or result.stdout)[-1500:]}")
        data = json.loads(output.read_text())
    findings = []
    if not isinstance(data.get("sheets"), list):
        raise SchematicError("KiCad ERC report is incomplete")
    for sheet in data["sheets"]:
        for violation in sheet.get("violations", []):
            # Include excluded violations: project settings alone cannot waive them.
            items = [{k: v for k, v in item.items() if k in {"uuid", "description"}}
                     for item in violation.get("items", [])]
            description = violation.get("description", "") + " " + json.dumps(items, sort_keys=True)
            findings.append(Finding("erc:" + violation.get("type", "unknown"), description,
                                    sheet.get("path", "/"), violation.get("severity", "error")))
    return tuple(findings)


def check_component_parts(graph: CircuitGraph, facts: dict, project_dir: Path, tool_root: Path) -> tuple[Finding, ...]:
    findings = []
    fp_dir, sym_dir = footprints_dir(tool_root), symbols_dir(tool_root)
    for component in graph.components:
        ref = component.reference
        if component.exclude_board:
            continue
        if not component.footprint:
            findings.append(Finding("part-footprint", f"{ref}: footprint is missing", "parts"))
            continue
        pads = footprint_pads(component.footprint, fp_dir, project_dir)
        if pads is None:
            findings.append(Finding("part-footprint", f"{ref}: footprint cannot be resolved", "parts"))
            continue
        symbol_pins = {p.number for p in component.pins if p.electrical != "no_connect"}
        # A symbol may document a missing key position with electrical no_connect.
        if symbol_pins - pads[0] or pads[0] - {p.number for p in component.pins}:
            findings.append(Finding("part-pad-map", f"{ref}: symbol pins and footprint pads disagree", "parts"))
        try:
            official = lib_symbol(component.symbol, sym_dir)
            actual = {(p.number, p.name, p.electrical) for p in component.pins}
            expected = {(p.number, p.name, p.electrical) for p in official.pins}
            if actual != expected:
                findings.append(Finding("part-symbol", f"{ref}: embedded pin definitions differ from the official symbol", "parts"))
        except Exception:
            official = None
        entry = facts.get("parts", {}).get(ref, {})
        if component.fitted:
            import re
            if not component.mpn or not re.fullmatch(r"C[1-9][0-9]*", component.lcsc):
                findings.append(Finding("part-identity", f"{ref}: exact MPN and LCSC fields are required", "parts"))
            if not component.fields.get("Datasheet") or not component.purpose:
                findings.append(Finding("part-evidence", f"{ref}: Datasheet and pcbforge_purpose fields are required", "parts"))
            if not isinstance(entry, dict) or entry.get("mpn") != component.mpn or not entry.get("source"):
                findings.append(Finding("part-evidence", f"{ref}: reviewed exact-part facts and source are required", "parts"))
            else:
                expected_map = entry.get("pins")
                if not isinstance(expected_map, dict) or {str(k): v for k, v in expected_map.items()} != {p.number: p.name for p in component.pins}:
                    findings.append(Finding("part-pin-functions", f"{ref}: sourced package pin functions do not match the symbol", "parts"))
                if entry.get("footprint") != component.footprint:
                    findings.append(Finding("part-package", f"{ref}: sourced package does not match the selected footprint", "parts"))
        if official is None and not entry.get("library_rationale"):
            findings.append(Finding("part-library", f"{ref}: custom symbol requires official-library search rationale", "parts"))
        # Ordinary two-pin passives keep official drawing and package assets.
        import re
        prefix = re.sub(r"[0-9].*", "", ref)
        commodity = {"R": ("Device:R", "Resistor_SMD:"), "C": ("Device:C", "Capacitor_SMD:")}
        if prefix == "D" and ("LED" in component.symbol.upper() or "LED" in component.footprint.upper()):
            commodity["D"] = ("Device:LED", "LED_SMD:")
        if prefix in commodity and len(component.pins) == 2:
            symbol, library = commodity[prefix]
            allowed_symbols = {symbol, "Device:R_Small" if prefix == "R" else "Device:C_Small" if prefix == "C" else symbol}
            if component.symbol not in allowed_symbols or not component.footprint.startswith(library) or not pads[1].is_relative_to(fp_dir):
                findings.append(Finding("part-commodity", f"{ref}: use the official commodity symbol and footprint", "parts"))
    return tuple(findings)


def check_mcu(graph: CircuitGraph, facts: dict, project_dir: Path, *, tool_root=None, runner=subprocess.run) -> None:
    from pcbforge.ioc import check_ioc
    result = check_ioc(project_dir, tool_root=tool_root, runner=runner)
    mcu = facts.get("mcu")
    if not isinstance(mcu, dict) or not mcu.get("reference") or not isinstance(mcu.get("assignments"), dict):
        raise SchematicError("electrical-facts.yaml needs mcu.reference and mcu.assignments")
    component = graph.component(mcu["reference"])
    if component.mpn != result.part_number:
        raise SchematicError("schematic MCU MPN differs from checked IOC")
    actual = {p.pin: p for p in result.pins}
    if set(mcu["assignments"]) != set(actual):
        raise SchematicError("MCU facts must cover every physical IOC assignment exactly")
    for name, assignment in mcu["assignments"].items():
        if not isinstance(assignment, dict) or set(assignment) != {"pin", "signal", "net"}:
            raise SchematicError(f"{name}: MCU assignment needs pin, signal and net")
        pin = graph.pin(component.reference + "." + str(assignment["pin"]))
        # CubeMX decorates some GPIO names with oscillator/RTC functions.
        import re
        physical = re.match(r"^(P[A-K][0-9]+)(?:-|$)", name)
        aliases = {name, physical.group(1)} if physical else {name}
        if not aliases.intersection(pin.name.split("/")) or assignment["signal"] != actual[name].signal or assignment["net"] != pin.net or pin.no_connect:
            raise SchematicError(f"{name}: schematic pin function, signal or net differs from the checked IOC assignment")


def export_preview(project_dir: Path, *, tool_root=None, runner=subprocess.run) -> Path:
    from pcbforge.schematic import run_kicad
    output = project_dir / "review/circuit/preview"
    with tempfile.TemporaryDirectory(prefix="pcbforge-preview-") as tmp:
        staging = Path(tmp)
        run_kicad(["sch", "export", "svg", "--output", str(staging), str(project_schematic(project_dir))], tool_root=tool_root, runner=runner)
        files = list(staging.glob("*.svg"))
        if not files:
            raise SchematicError("KiCad produced no schematic previews")
        output.mkdir(parents=True, exist_ok=True)
        commit_outputs([(output / p.name, p.read_bytes()) for p in files], label="schematic previews")
        for stale in output.glob("*.svg"):
            if stale.name not in {p.name for p in files}:
                stale.unlink()
    return output


def check_circuit(project_dir: Path, *, tool_root=None, runner=subprocess.run, write_report=False) -> CircuitCheckResult:
    project_dir = project_dir.resolve()
    tool_root = tool_root or Path(__file__).resolve().parent.parent
    check_version(tool_root, runner)
    before = source_digest(project_dir)
    contract = _review_contract(project_dir)
    facts = read_facts(project_dir)
    graph = load_graph(project_dir, tool_root=tool_root, runner=runner)
    if not graph.components:
        raise SchematicError("the circuit contains no components")
    erc = erc_findings(project_schematic(project_dir), tool_root=tool_root, runner=runner)
    readability = lint_saved(project_schematic(project_dir))
    parts = check_component_parts(graph, facts, project_dir, tool_root)
    findings = erc + readability + parts
    excluded = {v["id"] for key in ("erc_exclusions", "readability_exclusions") for v in contract.get(key, [])}
    available = {f.identifier for f in erc + readability}
    if excluded - available:
        raise SchematicError("stale or unknown finding exclusions: " + ", ".join(sorted(excluded-available)))
    blockers = [f for f in findings if f.identifier not in excluded and
                (f.severity == "error" or f.code.startswith("erc:"))]
    if blockers:
        raise SchematicError("circuit checks failed:\n" + "\n".join(f"- [{f.identifier}] {f.code}: {f.message}" for f in blockers))
    check_mcu(graph, facts, project_dir, tool_root=tool_root, runner=runner)
    tests = run_tests(project_dir, graph, facts)
    failures = [t for t in tests if t["outcome"] != "pass"]
    if failures:
        raise SchematicError("electrical acceptance failed:\n" + "\n".join(f"- {t['id']}: {t.get('error', 'failed')}" for t in failures))
    if source_digest(project_dir) != before:
        raise SchematicError("circuit inputs changed during validation; retry")
    fingerprint = fingerprint_inputs(project_dir, graph=graph)
    presentation = presentation_fingerprint(project_dir)
    evidence = {"schema": 2, "result": "pass", "fingerprint": fingerprint, "presentation_fingerprint": presentation,
                "source_digest": before, "graph_hash": graph.fingerprint, "bom_hash": digest(graph.bom()),
                "kicad": VERSION, "tests": tests, "findings": [dict(asdict(f), id=f.identifier, excluded=f.identifier in excluded) for f in findings]}
    lines = ["# Circuit acceptance", "", "PASS — saved schematic, ERC, exact parts, IOC and electrical tests.", "",
             f"KiCad: {VERSION}. Components: {len(graph.components)}. Nets: {len(graph.nets)}.", "",
             "| Test | Result | Measurements |", "|---|---|---|"]
    for test in tests:
        lines.append(f"| {test['id']} | {test['outcome']} | {json.dumps(test['measurements']).replace('|', '/')} |")
    lines += ["", "## Drawing review", "", "Review every changed sheet in review/circuit/preview before approval.", ""]
    lines += [f"- [{f.identifier}] {f.code}: {f.message}" for f in findings]
    lines += ["", f"Electrical fingerprint: `{fingerprint}`", f"Presentation fingerprint: `{presentation}`", ""]
    report = "\n".join(lines)
    bom_csv = io.StringIO()
    writer = csv.writer(bom_csv, lineterminator="\n")
    writer.writerow(("Designators", "Quantity", "MPN", "LCSC", "Footprint"))
    for item in graph.bom():
        writer.writerow((",".join(item["designators"]), item["quantity"], item["mpn"], item["lcsc"], item["footprint"]))
    evidence["report_hash"] = hashlib.sha256(report.encode()).hexdigest()
    evidence["bom_csv_hash"] = hashlib.sha256(bom_csv.getvalue().encode()).hexdigest()
    previous = project_dir / "review/circuit/pcb-sync.json"
    if previous.is_file():
        old_graph = from_payload(json.loads(previous.read_text())["graph"])
        evidence["changes"] = semantic_diff(old_graph, graph)
    else:
        evidence["changes"] = [f"Add {c.reference}" for c in graph.components]

    if write_report:
        preview = export_preview(project_dir, tool_root=tool_root, runner=runner)
        evidence["previews"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in preview.glob("*.svg")}
        outputs = [(project_dir / EVIDENCE_PATH, canonical(evidence)), (project_dir / GRAPH_PATH, canonical(graph.payload())),
                   (project_dir / BOM_PATH, canonical({"schema": 1, "components": graph.bom()})), (project_dir / REPORT_PATH, report.encode()),
                   (project_dir / BOM_CSV_PATH, bom_csv.getvalue().encode())]
        for path, _ in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
        if source_digest(project_dir) != before:
            raise SchematicError("circuit inputs changed during preview export; retry")
        commit_outputs(outputs, label="circuit evidence")
    return CircuitCheckResult(graph, fingerprint, presentation, findings, tests, report, write_report)


def read_evidence(project_dir: Path, *, require_presentation: bool = False) -> dict:
    project_dir = project_dir.resolve()
    try:
        data = json.loads((project_dir / EVIDENCE_PATH).read_text())
        if data.get("schema") != 2 or data.get("result") != "pass":
            raise SchematicError("missing passing native circuit evidence")
        if data["fingerprint"] != fingerprint_inputs(project_dir):
            raise SchematicError("circuit evidence is electrically stale; run check-circuit --write-report")
        if require_presentation and data["presentation_fingerprint"] != presentation_fingerprint(project_dir):
            raise SchematicError("schematic presentation changed; run check-circuit --write-report and review the changed sheets")
        if require_presentation:
            previews = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (project_dir / "review/circuit/preview").glob("*.svg")}
            if not previews or data.get("previews") != previews:
                raise SchematicError("schematic previews are missing or changed; rerun check-circuit --write-report")
        graph = from_payload(json.loads((project_dir / GRAPH_PATH).read_text()))
        bom = json.loads((project_dir / BOM_PATH).read_text())
        if graph.fingerprint != load_graph(project_dir).fingerprint or graph.fingerprint != data["graph_hash"] or digest(bom["components"]) != digest(graph.bom()) or digest(bom["components"]) != data["bom_hash"]:
            raise SchematicError("derived circuit artifacts changed; rerun check-circuit --write-report")
        for path, key in ((REPORT_PATH, "report_hash"), (BOM_CSV_PATH, "bom_csv_hash")):
            if hashlib.sha256((project_dir / path).read_bytes()).hexdigest() != data.get(key):
                raise SchematicError("derived report or BOM changed; rerun check-circuit --write-report")
        return data
    except (OSError, KeyError, ValueError) as exc:
        raise SchematicError(f"circuit acceptance is not current: {exc}") from exc
