"""Explicit electrical acceptance tests and unit-aware calculation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Mapping
import yaml

from pcbforge.schematic import CircuitGraph, SchematicError, canonical, from_payload


class ElectricalError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for k, v in node.value:
        key = loader.construct_object(k, deep=True)
        if key in result:
            raise ElectricalError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(v, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.load(path.read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ElectricalError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ElectricalError(f"{path}: expected a mapping")
    return data


_SCALE = {"": 1, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
          "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_UNITS = {"ohm": "ohm", "Ω": "ohm", "R": "ohm", "V": "V", "A": "A", "F": "F", "H": "H", "Hz": "Hz", "s": "s", "W": "W"}


def quantity(value: str, unit: str) -> float:
    """Read a value in the declared SI unit, including KiCad 4k7 notation."""
    if not isinstance(value, str) or unit not in set(_UNITS.values()):
        raise ElectricalError("quantity requires a text value and a supported SI unit")
    text = value.strip().replace(" ", "")
    embedded = re.fullmatch(r"(\d+)([RrkpnumKMµ])(\d+)", text)
    if embedded:
        integer, scale, fraction = embedded.groups()
        factor = 1 if scale in "Rr" else _SCALE[scale]
        return float(integer + "." + fraction) * factor
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([pnuµmkKMG]?)(ohm|Ω|R|V|A|F|H|Hz|s|W)?", text)
    if not match:
        raise ElectricalError(f"cannot interpret {value!r} as {unit}")
    number, scale, suffix = match.groups()
    if suffix and _UNITS[suffix] != unit:
        raise ElectricalError(f"{value!r} is not in {unit}")
    result = float(number) * _SCALE[scale]
    if not math.isfinite(result):
        raise ElectricalError("quantity must be finite")
    return result


def interval(nominal: float, tolerance: float) -> tuple[float, float]:
    if not math.isfinite(nominal) or not 0 <= tolerance < 1:
        raise ElectricalError("expected a finite nominal and fractional tolerance in [0, 1)")
    bounds = nominal * (1-tolerance), nominal * (1+tolerance)
    return min(bounds), max(bounds)


def divider(vin: tuple[float, float], upper: tuple[float, float], lower: tuple[float, float]) -> tuple[float, float]:
    if min(*upper, *lower) <= 0:
        raise ElectricalError("divider resistances must be positive")
    return vin[0]*lower[0]/(upper[1]+lower[0]), vin[1]*lower[1]/(upper[0]+lower[1])


def rc_time(resistance: tuple[float, float], capacitance: tuple[float, float]) -> tuple[float, float]:
    if min(*resistance, *capacitance) <= 0:
        raise ElectricalError("RC values must be positive")
    return resistance[0]*capacitance[0], resistance[1]*capacitance[1]


@dataclass
class CircuitTestContext:
    graph: CircuitGraph
    facts: Mapping[str, Any]
    measurements: list[dict[str, Any]] = field(default_factory=list)

    def require(self, condition: bool, message: str) -> None:
        if type(condition) is not bool:
            raise ElectricalError("require expects a boolean result")
        self.measurements.append({"check": message, "passed": condition})
        if not condition:
            raise AssertionError(message)

    def within(self, name: str, measured: float, minimum: float, maximum: float, unit: str) -> None:
        if not all(math.isfinite(x) for x in (measured, minimum, maximum)) or minimum > maximum:
            raise ElectricalError(f"{name}: invalid measurement bounds")
        ok = minimum <= measured <= maximum
        self.measurements.append({"check": name, "measured": measured, "minimum": minimum,
                                  "maximum": maximum, "unit": unit, "passed": ok})
        if not ok:
            raise AssertionError(f"{name}: {measured:g} {unit} outside [{minimum:g}, {maximum:g}]")

    def connected(self, *endpoints: str) -> None:
        self.require(self.graph.connected(*endpoints), "Required connection: " + ", ".join(endpoints))

    def isolated(self, first: str, second: str) -> None:
        self.require(not self.graph.connected(first, second), f"Forbidden connection: {first}, {second}")

    def value(self, reference: str, unit: str) -> float:
        return quantity(self.graph.component(reference).value, unit)

    def fact(self, name: str, unit: str) -> float:
        item = self.facts.get("values", {}).get(name)
        if not isinstance(item, dict) or not item.get("source"):
            raise ElectricalError(f"missing reviewed fact/source: {name}")
        return quantity(item["value"], unit)

    def rail(self, endpoint: str, expected_net: str) -> None:
        self.require(bool(expected_net) and not self.graph.pin(endpoint).no_connect and self.graph.pin(endpoint).net == expected_net, f"{endpoint} must use rail {expected_net}")

    def resistor_between(self, first: str, second: str, *, minimum: float, maximum: float) -> None:
        a, b = self.graph.pin(first).net, self.graph.pin(second).net
        self.require(bool(a and b) and a != b, "resistor endpoints use distinct connected nets")
        candidates = [c for c in self.graph.components if c.symbol in {"Device:R", "Device:R_Small"}
                      and c.fitted and len(c.pins) == 2 and {p.net for p in c.pins} == {a, b}]
        self.require(len(candidates) == 1, f"exactly one resistor between {first} and {second}")
        self.within(candidates[0].reference, self.value(candidates[0].reference, "ohm"), minimum, maximum, "ohm")

    def decoupling(self, supply: str, ground: str, *, minimum: float, maximum: float) -> None:
        a, b = self.graph.pin(supply).net, self.graph.pin(ground).net
        self.require(bool(a and b) and a != b, "supply and ground use distinct connected nets")
        capacitors = [c for c in self.graph.components if c.symbol in {"Device:C", "Device:C_Small"}
                      and c.fitted and {p.net for p in c.pins} == {a, b}]
        self.require(any(minimum <= self.value(c.reference, "F") <= maximum for c in capacitors),
                     f"decoupling on {supply}, {ground} within [{minimum:g}, {maximum:g}] F")

    def regulator(self, *, input_min: float, output_max: float, dropout_max: float,
                  load_max: float, rated_current: float, fraction: float = 0.7) -> None:
        self.require(input_min >= output_max + dropout_max, "regulator worst-case headroom")
        self.within("regulator maximum load", load_max, 0, rated_current*fraction, "A")


def read_contract(project_dir: Path) -> dict:
    data = read_yaml(project_dir / "circuit-tests.yaml")
    if data.get("circuit_tests_schema") != 1 or set(data) - {"circuit_tests_schema", "requirements", "tests", "inputs"} or not {"requirements", "tests"} <= set(data):
        raise ElectricalError("circuit-tests.yaml: expected schema 1, requirements and tests")
    inputs = data.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(p, str) or not p for p in inputs):
        raise ElectricalError("inputs must list project-relative evidence files")
    for relative in inputs:
        path = Path(relative)
        if path.is_absolute() or not (project_dir / path).resolve().is_relative_to(project_dir.resolve()) or not (project_dir / path).is_file():
            raise ElectricalError(f"invalid or missing acceptance input: {relative}")
    tests = data["tests"]
    requirements = data["requirements"]
    if not isinstance(tests, list) or not tests or not isinstance(requirements, list) or not requirements:
        raise ElectricalError("declare nonempty requirements and tests")
    ids = set()
    for test in tests:
        if not isinstance(test, dict) or set(test) != {"id", "callable"}:
            raise ElectricalError("each test needs id and callable")
        key = test["id"]
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", key) or key in ids:
            raise ElectricalError(f"invalid or duplicate test ID: {key}")
        ids.add(key)
        resolve_callable(project_dir, test["callable"])
    seen, covered = set(), set()
    for requirement in requirements:
        if not isinstance(requirement, dict) or set(requirement) - {"id", "description", "tests", "assessment"}:
            raise ElectricalError("invalid requirement entry")
        key = requirement.get("id")
        if not isinstance(key, str) or not key or key in seen or not requirement.get("description"):
            raise ElectricalError("each requirement needs a unique ID and description")
        seen.add(key)
        links = requirement.get("tests", [])
        if not isinstance(links, list) or any(not isinstance(x, str) for x in links) or len(set(links)) != len(links):
            raise ElectricalError(f"{key}: tests must be unique IDs")
        if set(links) - ids:
            raise ElectricalError(f"{key}: unknown test IDs")
        covered.update(links)
        assessment = requirement.get("assessment")
        if not links and (not isinstance(assessment, dict) or not assessment.get("rationale") or not assessment.get("sources")):
            raise ElectricalError(f"{key}: needs tests or a sourced engineering assessment")
    mandatory = {"power", "mcu", "interfaces", "protection", "component-ratings"}
    if mandatory - seen:
        raise ElectricalError("missing acceptance categories: " + ", ".join(sorted(mandatory-seen)))
    if covered != ids:
        raise ElectricalError("every test must trace to a requirement")
    return data


def resolve_callable(project_dir: Path, value: str) -> tuple[Path, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ElectricalError("callable must be project-relative-file.py:function")
    relative, name = value.split(":")
    path = (project_dir / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(project_dir.resolve()) or path.suffix != ".py" or not path.is_file():
        raise ElectricalError(f"invalid test source: {relative}")
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", name):
        raise ElectricalError("test function names must start with test_")
    return path, name


def read_facts(project_dir: Path) -> dict:
    data = read_yaml(project_dir / "electrical-facts.yaml")
    if data.get("electrical_facts_schema") != 1 or set(data) - {"electrical_facts_schema", "values", "parts", "mcu"}:
        raise ElectricalError("electrical-facts.yaml: unsupported schema or fields")
    if not isinstance(data.get("values", {}), dict) or not isinstance(data.get("parts", {}), dict):
        raise ElectricalError("facts values and parts must be mappings")
    for name, fact in data.get("values", {}).items():
        if not isinstance(fact, dict) or set(fact) != {"value", "source"} or not fact["source"] or not isinstance(fact["value"], str):
            raise ElectricalError(f"{name}: fact needs a unit-bearing value and source")
    return data


def run_tests(project_dir: Path, graph: CircuitGraph, facts: dict, *, timeout: float = 30) -> tuple[dict, ...]:
    contract = read_contract(project_dir)
    results = []
    for test in contract["tests"]:
        source, name = resolve_callable(project_dir, test["callable"])
        with tempfile.TemporaryDirectory(prefix="pcbforge-test-") as tmp:
            root = Path(tmp)
            request, response = root / "input.json", root / "result.json"
            request.write_bytes(canonical({"graph": graph.payload(), "facts": facts, "source": str(source), "function": name}))
            try:
                result = subprocess.run([sys.executable, "-m", "pcbforge.electrical", str(request), str(response)],
                                        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=timeout, check=False)
                if result.returncode or not response.is_file():
                    payload = {"outcome": "fail", "error": (result.stderr or result.stdout)[-1500:] or "test process produced no result", "measurements": []}
                else:
                    payload = json.loads(response.read_text())
            except subprocess.TimeoutExpired:
                payload = {"outcome": "fail", "error": f"test exceeded {timeout:g} seconds", "measurements": []}
            payload["id"] = test["id"]
            results.append(payload)
    return tuple(results)


def _worker(request: Path, response: Path) -> None:
    context = None
    try:
        data = json.loads(request.read_text())
        context = CircuitTestContext(from_payload(data["graph"]), data["facts"])
        spec = importlib.util.spec_from_file_location("pcbforge_project_acceptance", data["source"])
        if spec is None or spec.loader is None:
            raise ElectricalError("cannot load test source")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        callback = getattr(module, data["function"])
        returned = callback(context)
        if returned is not None:
            raise ElectricalError("test must use context checks and return None")
        if not context.measurements:
            raise ElectricalError("test executed no context checks")
        payload = {"outcome": "pass", "measurements": context.measurements}
    except BaseException as exc:
        payload = {"outcome": "fail", "error": f"{type(exc).__name__}: {exc}",
                   "measurements": context.measurements if context else []}
    response.write_bytes(canonical(payload))


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), Path(sys.argv[2]))
