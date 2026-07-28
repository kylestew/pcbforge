"""Versioned manufacturing and technology policy checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

POLICY_SCHEMA = 1
POLICY_PROFILE_SCHEMA = 1
POLICY_PROFILE_ID = "pcbforge-standard-v1"
POLICY_FILENAME = "policy.yaml"
POLICY_PROFILE_PATH = Path("policies") / f"{POLICY_PROFILE_ID}.yaml"
PROJECT_PIN_SCHEMA = 13

ASSURANCE_RULES = (
    "reverse-polarity",
    "overcurrent",
    "connector-esd",
    "test-points",
    "polarity-marking",
    "pin1-marking",
)
ASSURANCE_STATUSES = {"required", "not-applicable", "exception"}
JLC_CLASSES = {"basic", "extended", "unknown"}
ASSEMBLY_STATUSES = {"available", "unavailable", "unknown"}
LIFECYCLE_STATUSES = {"active", "nrnd", "obsolete", "unknown"}
PHASE_ORDER = {
    "spec": 1,
    "init": 2,
    "architect": 3,
    "mcu": 4,
    "implement": 5,
    "build": 6,
    "brief": 7,
    "layout": 8,
    "route": 9,
    "verify": 10,
    "fab-out": 11,
    "order": 12,
    "publish": 13,
}
ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
LCSC_RE = re.compile(r"^C[1-9][0-9]*$")
QFN_PITCH_RE = re.compile(r"(?:^|[_-])P(0(?:\.\d+)?)mm", re.IGNORECASE)
PACKAGE_RE = re.compile(
    r"(?:^|[^0-9])(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)"
    r"(?:[^0-9]|$)",
    re.IGNORECASE,
)
PACKAGE_ORDER = {
    "01005": 0,
    "0201": 1,
    "0402": 2,
    "0603": 3,
    "0805": 4,
    "1206": 5,
    "1210": 6,
    "1812": 7,
    "2010": 8,
    "2512": 9,
}
HARD_PROFILE_KEYS = {
    "fabricator",
    "assembler",
    "allowed_layers",
    "mcu_vendor",
    "debug_interface",
    "eda",
    "circuit_language",
}
DEFAULT_PROFILE_KEYS = {
    "material",
    "thickness_mm",
    "copper_oz",
    "controlled_impedance",
    "advanced_vias",
    "commodity_min_package",
    "qfn_min_pitch_mm",
    "prefer_jlc_class",
    "prefer_active_lifecycle",
    "prefer_second_source",
}
EXCEPTION_RULE_IDS = {
    "manufacturing.material",
    "manufacturing.thickness",
    "manufacturing.copper",
    "manufacturing.controlled-impedance",
    "manufacturing.advanced-vias",
    "components.commodity-package",
    "components.advanced-package",
    "assurance.reverse-polarity",
    "assurance.overcurrent",
    "assurance.connector-esd",
    "assurance.test-points",
    "assurance.polarity-marking",
    "assurance.pin1-marking",
    "sourcing.unavailable",
    "sourcing.lifecycle",
    "sourcing.unknown",
}


class PolicyError(RuntimeError):
    """A runtime failure while evaluating project policy."""


class PolicyInputError(PolicyError):
    """A malformed or unmigrated policy input."""


@dataclass(frozen=True)
class Assurance:
    status: str
    rationale: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class SourcingPart:
    lcsc: str
    jlc_class: str
    assembly_status: str
    lifecycle: str
    checked_on: str
    second_source: str | None


@dataclass(frozen=True)
class PolicyException:
    identifier: str
    rule: str
    scope: str
    rationale: str


@dataclass(frozen=True)
class PolicyContract:
    profile: str
    manufacturing: Mapping[str, Any]
    components: Mapping[str, Any]
    assurances: Mapping[str, Assurance]
    sourcing: tuple[SourcingPart, ...]
    exceptions: tuple[PolicyException, ...]


@dataclass(frozen=True)
class PolicyViolation:
    rule: str
    scope: str
    earliest_phase: str
    message: str
    hard: bool = False


@dataclass(frozen=True)
class PolicyWarning:
    rule: str
    scope: str
    message: str


@dataclass(frozen=True)
class PolicyResult:
    project_dir: Path
    profile_id: str
    fingerprint: str
    baseline_fingerprint: str
    violations: tuple[PolicyViolation, ...]
    warnings: tuple[PolicyWarning, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def summary(self) -> str:
        if self.violations:
            count = len(self.violations)
            return f"{count} policy violation{'s' if count != 1 else ''}"
        warning_count = len(self.warnings)
        return (
            "policy passed"
            if warning_count == 0
            else f"policy passed with {warning_count} warning"
            f"{'s' if warning_count != 1 else ''}"
        )


@dataclass(frozen=True)
class PolicyMigrationResult:
    project_dir: Path
    wrote: bool
    review_items: tuple[str, ...]


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


def _load_yaml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyInputError(f"missing {label} at {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise PolicyInputError(f"cannot read {path}: {exc}") from exc
    try:
        loaded = yaml.load(text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise PolicyInputError(f"invalid {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PolicyInputError(f"{label} must be a YAML mapping")
    return loaded


def _unknown(
    data: Mapping[str, Any],
    allowed: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    unknown = sorted(set(data) - allowed, key=str)
    if unknown:
        errors.append(f"{prefix}: unknown keys: {', '.join(map(str, unknown))}")


def _missing(
    data: Mapping[str, Any],
    required: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    missing = sorted(required - set(data), key=str)
    if missing:
        errors.append(f"{prefix}: missing keys: {', '.join(map(str, missing))}")


def _text(
    data: Mapping[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
    *,
    required: bool = True,
) -> str:
    value = data.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix}.{key}: expected a non-empty string")
        return ""
    return value.strip()


def _string_list(
    value: Any,
    prefix: str,
    errors: list[str],
) -> tuple[str, ...]:
    if not isinstance(value, list):
        errors.append(f"{prefix}: expected a list of strings")
        return ()
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{prefix}[{index}]: expected a non-empty string")
            continue
        items.append(item.strip())
    if len(items) != len(set(items)):
        errors.append(f"{prefix}: values must be unique")
    return tuple(items)


def load_policy_profile(
    tool_root: Path | None = None,
) -> tuple[Mapping[str, Any], Path, str]:
    """Load and validate the tool-owned policy profile."""
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    path = tool_root / POLICY_PROFILE_PATH
    data = _load_yaml(path, "policy profile")
    errors: list[str] = []
    _unknown(
        data,
        {"policy_profile_schema", "id", "hard", "defaults", "exception_rules"},
        "policy profile",
        errors,
    )
    if data.get("policy_profile_schema") != POLICY_PROFILE_SCHEMA:
        errors.append(
            f"policy_profile_schema: expected integer {POLICY_PROFILE_SCHEMA}"
        )
    if data.get("id") != POLICY_PROFILE_ID:
        errors.append(f"id: expected {POLICY_PROFILE_ID!r}")
    hard = data.get("hard")
    defaults = data.get("defaults")
    exception_rules = data.get("exception_rules")
    if not isinstance(hard, dict):
        errors.append("hard: expected a mapping")
    else:
        _unknown(hard, HARD_PROFILE_KEYS, "hard", errors)
        _missing(hard, HARD_PROFILE_KEYS, "hard", errors)
        expected_hard = {
            "fabricator": "jlcpcb",
            "assembler": "jlcpcb",
            "mcu_vendor": "STMicroelectronics",
            "debug_interface": "swd",
            "eda": "kicad-9",
            "circuit_language": "atopile",
        }
        for key, expected in expected_hard.items():
            if hard.get(key) != expected:
                errors.append(f"hard.{key}: expected {expected!r}")
        if hard.get("allowed_layers") != [2, 4]:
            errors.append("hard.allowed_layers: expected [2, 4]")
    if not isinstance(defaults, dict):
        errors.append("defaults: expected a mapping")
    else:
        _unknown(defaults, DEFAULT_PROFILE_KEYS, "defaults", errors)
        _missing(defaults, DEFAULT_PROFILE_KEYS, "defaults", errors)
        expected_defaults = {
            "material": "FR4",
            "thickness_mm": 1.6,
            "copper_oz": 1,
            "controlled_impedance": False,
            "advanced_vias": False,
            "commodity_min_package": "0603",
            "qfn_min_pitch_mm": 0.5,
            "prefer_jlc_class": "basic",
            "prefer_active_lifecycle": True,
            "prefer_second_source": True,
        }
        for key, expected in expected_defaults.items():
            if defaults.get(key) != expected:
                errors.append(f"defaults.{key}: expected {expected!r}")
    if not isinstance(exception_rules, dict):
        errors.append("exception_rules: expected a mapping")
    else:
        _unknown(exception_rules, EXCEPTION_RULE_IDS, "exception_rules", errors)
        _missing(exception_rules, EXCEPTION_RULE_IDS, "exception_rules", errors)
        for rule, raw in exception_rules.items():
            if not isinstance(rule, str) or not rule:
                errors.append("exception_rules: rule IDs must be strings")
                continue
            if not isinstance(raw, dict) or set(raw) != {"earliest_phase"}:
                errors.append(
                    f"exception_rules.{rule}: expected only earliest_phase"
                )
                continue
            if raw.get("earliest_phase") not in PHASE_ORDER:
                errors.append(
                    f"exception_rules.{rule}.earliest_phase: unknown phase"
                )
    if errors:
        raise PolicyInputError(
            "invalid policy profile:\n  - " + "\n  - ".join(errors)
        )
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot fingerprint {path}: {exc}") from exc
    return data, path, hashlib.sha256(contents).hexdigest()


def read_policy_contract(project_dir: Path) -> PolicyContract:
    """Strictly parse the project policy contract."""
    project_dir = project_dir.expanduser().resolve()
    data = _load_yaml(project_dir / POLICY_FILENAME, POLICY_FILENAME)
    errors: list[str] = []
    _unknown(
        data,
        {
            "policy_schema",
            "profile",
            "manufacturing",
            "components",
            "assurances",
            "sourcing",
            "exceptions",
        },
        POLICY_FILENAME,
        errors,
    )
    if data.get("policy_schema") != POLICY_SCHEMA:
        errors.append(f"policy_schema: expected integer {POLICY_SCHEMA}")
    profile = _text(data, "profile", POLICY_FILENAME, errors)

    manufacturing_raw = data.get("manufacturing")
    manufacturing: dict[str, Any] = {}
    manufacturing_keys = {
        "fabricator",
        "assembler",
        "material",
        "thickness_mm",
        "copper_oz",
        "controlled_impedance",
        "advanced_vias",
    }
    if not isinstance(manufacturing_raw, dict):
        errors.append("manufacturing: expected a mapping")
    else:
        _unknown(manufacturing_raw, manufacturing_keys, "manufacturing", errors)
        manufacturing = dict(manufacturing_raw)
        for key in ("fabricator", "assembler", "material"):
            _text(manufacturing_raw, key, "manufacturing", errors)
        for key in ("thickness_mm", "copper_oz"):
            value = manufacturing_raw.get(key)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or float(value) <= 0
            ):
                errors.append(f"manufacturing.{key}: expected a positive number")
        for key in ("controlled_impedance", "advanced_vias"):
            if type(manufacturing_raw.get(key)) is not bool:
                errors.append(f"manufacturing.{key}: expected a boolean")

    components_raw = data.get("components")
    components: dict[str, Any] = {}
    if not isinstance(components_raw, dict):
        errors.append("components: expected a mapping")
    else:
        _unknown(
            components_raw,
            {"mcu_vendor", "commodity_min_package", "advanced_packages"},
            "components",
            errors,
        )
        components = dict(components_raw)
        _text(components_raw, "mcu_vendor", "components", errors)
        minimum = _text(
            components_raw,
            "commodity_min_package",
            "components",
            errors,
        )
        if minimum and minimum not in PACKAGE_ORDER:
            errors.append("components.commodity_min_package: unknown package")
        components["advanced_packages"] = _string_list(
            components_raw.get("advanced_packages"),
            "components.advanced_packages",
            errors,
        )

    assurances_raw = data.get("assurances")
    assurances: dict[str, Assurance] = {}
    if not isinstance(assurances_raw, dict):
        errors.append("assurances: expected a mapping")
    else:
        unknown_assurances = sorted(set(assurances_raw) - set(ASSURANCE_RULES))
        missing_assurances = sorted(set(ASSURANCE_RULES) - set(assurances_raw))
        if unknown_assurances:
            errors.append(
                "assurances: unknown rules: " + ", ".join(unknown_assurances)
            )
        if missing_assurances:
            errors.append(
                "assurances: missing rules: " + ", ".join(missing_assurances)
            )
        for rule in ASSURANCE_RULES:
            raw = assurances_raw.get(rule)
            prefix = f"assurances.{rule}"
            if not isinstance(raw, dict):
                if rule in assurances_raw:
                    errors.append(f"{prefix}: expected a mapping")
                continue
            _unknown(raw, {"status", "rationale", "evidence"}, prefix, errors)
            status = _text(raw, "status", prefix, errors)
            if status and status not in ASSURANCE_STATUSES:
                errors.append(
                    f"{prefix}.status: expected one of "
                    + ", ".join(sorted(ASSURANCE_STATUSES))
                )
            rationale = _text(raw, "rationale", prefix, errors)
            evidence = _string_list(raw.get("evidence"), f"{prefix}.evidence", errors)
            assurances[rule] = Assurance(status, rationale, evidence)

    sourcing_raw = data.get("sourcing")
    sourcing: list[SourcingPart] = []
    if not isinstance(sourcing_raw, list):
        errors.append("sourcing: expected a list")
    else:
        for index, raw in enumerate(sourcing_raw):
            prefix = f"sourcing[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            _unknown(
                raw,
                {
                    "lcsc",
                    "jlc_class",
                    "assembly_status",
                    "lifecycle",
                    "checked_on",
                    "second_source",
                },
                prefix,
                errors,
            )
            lcsc = _text(raw, "lcsc", prefix, errors)
            jlc_class = _text(raw, "jlc_class", prefix, errors)
            assembly_status = _text(raw, "assembly_status", prefix, errors)
            lifecycle = _text(raw, "lifecycle", prefix, errors)
            checked_on = _text(raw, "checked_on", prefix, errors)
            second_source_raw = raw.get("second_source")
            second_source = None
            if second_source_raw is not None:
                if (
                    not isinstance(second_source_raw, str)
                    or LCSC_RE.fullmatch(second_source_raw) is None
                ):
                    errors.append(
                        f"{prefix}.second_source: expected an LCSC ID or null"
                    )
                else:
                    second_source = second_source_raw
            if lcsc and LCSC_RE.fullmatch(lcsc) is None:
                errors.append(f"{prefix}.lcsc: expected an LCSC ID")
            if jlc_class and jlc_class not in JLC_CLASSES:
                errors.append(f"{prefix}.jlc_class: unknown JLC class")
            if assembly_status and assembly_status not in ASSEMBLY_STATUSES:
                errors.append(f"{prefix}.assembly_status: unknown status")
            if lifecycle and lifecycle not in LIFECYCLE_STATUSES:
                errors.append(f"{prefix}.lifecycle: unknown lifecycle")
            if checked_on:
                try:
                    parsed_date = date.fromisoformat(checked_on)
                except ValueError:
                    errors.append(f"{prefix}.checked_on: expected YYYY-MM-DD")
                else:
                    if parsed_date > date.today():
                        errors.append(f"{prefix}.checked_on: cannot be in the future")
            sourcing.append(
                SourcingPart(
                    lcsc,
                    jlc_class,
                    assembly_status,
                    lifecycle,
                    checked_on,
                    second_source,
                )
            )
        identifiers = [part.lcsc for part in sourcing if part.lcsc]
        duplicates = sorted(
            item for item in set(identifiers) if identifiers.count(item) > 1
        )
        if duplicates:
            errors.append("sourcing: duplicate LCSC IDs: " + ", ".join(duplicates))

    exceptions_raw = data.get("exceptions")
    exceptions: list[PolicyException] = []
    if not isinstance(exceptions_raw, list):
        errors.append("exceptions: expected a list")
    else:
        for index, raw in enumerate(exceptions_raw):
            prefix = f"exceptions[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            _unknown(raw, {"id", "rule", "scope", "rationale"}, prefix, errors)
            identifier = _text(raw, "id", prefix, errors)
            rule = _text(raw, "rule", prefix, errors)
            scope = _text(raw, "scope", prefix, errors)
            rationale = _text(raw, "rationale", prefix, errors)
            if identifier and ID_RE.fullmatch(identifier) is None:
                errors.append(f"{prefix}.id: expected a kebab-case ID")
            exceptions.append(PolicyException(identifier, rule, scope, rationale))
        identifiers = [
            exception.identifier for exception in exceptions if exception.identifier
        ]
        duplicates = sorted(
            item for item in set(identifiers) if identifiers.count(item) > 1
        )
        if duplicates:
            errors.append("exceptions: duplicate IDs: " + ", ".join(duplicates))

    if errors:
        raise PolicyInputError(
            f"invalid {POLICY_FILENAME}:\n  - " + "\n  - ".join(errors)
        )
    return PolicyContract(
        profile,
        manufacturing,
        components,
        assurances,
        tuple(sourcing),
        tuple(exceptions),
    )


def render_default_policy(
    *,
    sourcing_ids: Sequence[str] = (),
    advanced_packages: Sequence[str] = (),
    advanced_vias: bool = False,
) -> str:
    """Render a new policy contract whose assurance evidence is intentionally open."""
    data = {
        "policy_schema": POLICY_SCHEMA,
        "profile": POLICY_PROFILE_ID,
        "manufacturing": {
            "fabricator": "jlcpcb",
            "assembler": "jlcpcb",
            "material": "FR4",
            "thickness_mm": 1.6,
            "copper_oz": 1,
            "controlled_impedance": False,
            "advanced_vias": advanced_vias,
        },
        "components": {
            "mcu_vendor": "STMicroelectronics",
            "commodity_min_package": "0603",
            "advanced_packages": list(advanced_packages),
        },
        "assurances": {
            rule: {
                "status": "required",
                "rationale": "Applicable unless the user approves another disposition.",
                "evidence": [],
            }
            for rule in ASSURANCE_RULES
        },
        "sourcing": [
            {
                "lcsc": lcsc,
                "jlc_class": "unknown",
                "assembly_status": "unknown",
                "lifecycle": "unknown",
                "checked_on": date.today().isoformat(),
                "second_source": None,
            }
            for lcsc in sourcing_ids
        ],
        "exceptions": [],
    }
    return yaml.safe_dump(data, sort_keys=False)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def policy_baseline_fingerprint(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> str:
    """Fingerprint profile and baseline declarations, excluding evolving sourcing."""
    contract = read_policy_contract(project_dir)
    _, _, profile_hash = load_policy_profile(tool_root)
    payload = {
        "profile_sha256": profile_hash,
        "profile": contract.profile,
        "manufacturing": dict(contract.manufacturing),
        "components": dict(contract.components),
        "assurances": {
            key: asdict(value) for key, value in sorted(contract.assurances.items())
        },
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def policy_exception_fingerprints(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> Mapping[str, str]:
    """Return the current fingerprint for every declared exception."""
    contract = read_policy_contract(project_dir)
    _, _, profile_hash = load_policy_profile(tool_root)
    return {
        exception.identifier: hashlib.sha256(
            _canonical(
                {
                    "profile_sha256": profile_hash,
                    "exception": asdict(exception),
                }
            )
        ).hexdigest()
        for exception in contract.exceptions
    }


def policy_sourcing_fingerprint(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> str:
    """Bind a pre-order sourcing confirmation to BOM, policy, and fab outputs."""
    project_dir = project_dir.expanduser().resolve()
    contract = read_policy_contract(project_dir)
    _, _, profile_hash = load_policy_profile(tool_root)
    digest = hashlib.sha256()
    digest.update(profile_hash.encode())
    digest.update(
        _canonical([asdict(part) for part in sorted(contract.sourcing, key=lambda p: p.lcsc)])
    )
    for relative in (Path("build-test.yaml"),):
        path = project_dir / relative
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(
            hashlib.sha256(path.read_bytes()).digest()
            if path.is_file()
            else b"<missing>"
        )
    fab = project_dir / "fab"
    outputs = (
        sorted(path for path in fab.rglob("*") if path.is_file() and path.name != ".gitkeep")
        if fab.is_dir()
        else []
    )
    for path in outputs:
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def policy_inputs(project_dir: Path) -> tuple[Path, ...]:
    """Return visible project inputs for status staleness diagnostics."""
    project_dir = project_dir.expanduser().resolve()
    patterns = (
        "policy.yaml",
        "spec.md",
        ".pcbforge",
        "build-test.yaml",
        "*.kicad_pcb",
    )
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in project_dir.glob(pattern) if path.is_file())
    return tuple(sorted(paths))


def policy_status_fingerprint(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> str:
    """Fingerprint policy inputs and the tool-owned profile."""
    project_dir = project_dir.expanduser().resolve()
    _, profile_path, _ = load_policy_profile(tool_root)
    digest = hashlib.sha256()
    for path in (*policy_inputs(project_dir), profile_path):
        try:
            relative = path.relative_to(project_dir).as_posix()
        except ValueError:
            relative = f"tool:{path.name}"
        digest.update(relative.encode())
        digest.update(b"\0")
        if path.name == POLICY_FILENAME:
            contract = read_policy_contract(project_dir)
            digest.update(
                _canonical(
                    {
                        "baseline": policy_baseline_fingerprint(
                            project_dir,
                            tool_root=tool_root,
                        ),
                        "exceptions": [
                            asdict(exception)
                            for exception in contract.exceptions
                        ],
                        "sourcing": [
                            {
                                "lcsc": part.lcsc,
                                "jlc_class": part.jlc_class,
                                "assembly_status": part.assembly_status,
                                "lifecycle": part.lifecycle,
                                "second_source": part.second_source,
                            }
                            for part in contract.sourcing
                        ],
                    }
                )
            )
        elif path.suffix == ".kicad_pcb":
            try:
                from pcbforge.build_test import (
                    board_topology_bytes,
                    read_board_evidence,
                )

                digest.update(board_topology_bytes(read_board_evidence(path)))
                text = path.read_text(encoding="utf-8")
                semantics = {
                    "copper_layers": sorted(
                        set(
                            re.findall(
                                r'\(\d+\s+"((?:F|B|In\d+)\.Cu)"\s+',
                                text,
                            )
                        )
                    ),
                    "advanced_vias": bool(
                        re.search(
                            r"\(via\b[^)]*\(type\s+(?:blind|micro)\)",
                            text,
                            re.DOTALL,
                        )
                    ),
                }
                digest.update(_canonical(semantics))
            except Exception:
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _phase_in_scope(earliest_phase: str, through_phase: str) -> bool:
    return PHASE_ORDER[earliest_phase] <= PHASE_ORDER[through_phase]


def _pinned_policy(
    project_dir: Path,
    profile_id: str,
    profile_hash: str,
) -> tuple[str, list[PolicyViolation]]:
    path = project_dir / ".pcbforge"
    if not path.is_file():
        return "spec", []
    data = _load_yaml(path, ".pcbforge")
    schema = data.get("schema")
    if schema not in {10, 11, 12, PROJECT_PIN_SCHEMA}:
        raise PolicyInputError(
            "project policy is not migrated: run `pcbforge migrate-policy`"
        )
    policy = data.get("policy")
    violations: list[PolicyViolation] = []
    if not isinstance(policy, dict):
        violations.append(
            PolicyViolation(
                "hard.policy-pin",
                "project",
                "spec",
                ".pcbforge has no policy pin",
                True,
            )
        )
        return "", violations
    if policy.get("profile") != profile_id or policy.get("profile_sha256") != profile_hash:
        violations.append(
            PolicyViolation(
                "hard.policy-pin",
                "project",
                "spec",
                "pinned policy profile does not match the current tool profile",
                True,
            )
        )
    baseline = policy.get("baseline_approval")
    if baseline not in {"spec", "policy-event"}:
        violations.append(
            PolicyViolation(
                "hard.policy-pin",
                "project",
                "spec",
                ".pcbforge policy.baseline_approval is invalid",
                True,
            )
        )
        baseline = ""
    return str(baseline), violations


def _exception_resolution(
    violation: PolicyViolation,
    contract: PolicyContract,
    exception_approvals: Mapping[str, str],
    exception_fingerprints: Mapping[str, str],
) -> PolicyViolation | None:
    if violation.hard:
        return violation
    if violation.rule.startswith("assurance."):
        assurance = contract.assurances.get(
            violation.rule.removeprefix("assurance.")
        )
        if assurance is None or assurance.status != "exception":
            return violation
    matching = [
        exception
        for exception in contract.exceptions
        if exception.rule == violation.rule
        and exception.scope in {violation.scope, "project"}
    ]
    if not matching:
        return PolicyViolation(
            violation.rule,
            violation.scope,
            violation.earliest_phase,
            f"{violation.message}; declare and approve an exception",
        )
    if len(matching) > 1:
        return PolicyViolation(
            violation.rule,
            violation.scope,
            violation.earliest_phase,
            "multiple exceptions match this violation; keep one unambiguous exception",
        )
    exception = matching[0]
    expected = exception_fingerprints.get(exception.identifier, "")
    if exception_approvals.get(exception.identifier) != expected:
        return PolicyViolation(
            violation.rule,
            violation.scope,
            violation.earliest_phase,
            f"exception {exception.identifier!r} lacks current explicit approval",
        )
    return None


def _board_violations(
    project_dir: Path,
    minimum_package: str,
    advanced_packages: Sequence[str],
    qfn_min_pitch_mm: float,
) -> list[PolicyViolation]:
    board_paths = sorted(project_dir.glob("*.kicad_pcb"))
    if not board_paths:
        return []
    try:
        text = board_paths[0].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PolicyError(f"cannot read {board_paths[0]}: {exc}") from exc
    violations: list[PolicyViolation] = []
    copper_layers = set(
        re.findall(r'\(\d+\s+"((?:F|B|In\d+)\.Cu)"\s+', text)
    )
    if copper_layers and len(copper_layers) not in {2, 4}:
        violations.append(
            PolicyViolation(
                "hard.layers",
                board_paths[0].name,
                "spec",
                f"board has {len(copper_layers)} copper layers; only 2 or 4 are allowed",
                True,
            )
        )
    if re.search(r"\(via\b[^)]*\(type\s+(?:blind|micro)\)", text, re.DOTALL):
        violations.append(
            PolicyViolation(
                "manufacturing.advanced-vias",
                "project",
                "spec",
                "PCB contains blind, buried, or microvias",
            )
        )
    try:
        from pcbforge.build_test import read_board_evidence

        board = read_board_evidence(board_paths[0])
    except Exception:
        return violations
    minimum_rank = PACKAGE_ORDER[minimum_package]
    declared_advanced = set(advanced_packages)
    for reference, footprint in board.footprints:
        upper = footprint.upper()
        commodity = (
            reference.startswith(("R", "C"))
            or (
                reference.startswith("D")
                and "LED" in upper
            )
        ) and any(
            library in upper
            for library in ("RESISTOR_SMD", "CAPACITOR_SMD", "LED_SMD")
        )
        package_match = PACKAGE_RE.search(upper)
        if commodity and package_match:
            package = package_match.group(1)
            if PACKAGE_ORDER[package] < minimum_rank:
                violations.append(
                    PolicyViolation(
                        "components.commodity-package",
                        reference,
                        "implement",
                        f"{reference} uses {package}, below the {minimum_package} default",
                    )
                )
        advanced = False
        reason = ""
        if "WLCSP" in upper or re.search(r"(?:^|[:_-])BGA", upper):
            advanced = True
            reason = "BGA/WLCSP"
        elif "QFN" in upper:
            pitch = QFN_PITCH_RE.search(footprint)
            if pitch and float(pitch.group(1)) < qfn_min_pitch_mm:
                advanced = True
                reason = f"QFN pitch {pitch.group(1)} mm"
        declaration = f"{reference}:{footprint}"
        if advanced and declaration not in declared_advanced:
            violations.append(
                PolicyViolation(
                    "components.advanced-package",
                    reference,
                    "implement",
                    f"{reference} uses undeclared advanced package {reason}",
                )
            )
    return violations


def check_policy(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    through_phase: str = "verify",
    baseline_approval: str = "",
    exception_approvals: Mapping[str, str] | None = None,
) -> PolicyResult:
    """Evaluate current project policy without network access or writes."""
    project_dir = project_dir.expanduser().resolve()
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    if through_phase not in PHASE_ORDER:
        raise PolicyInputError(f"unknown policy phase {through_phase!r}")
    from pcbforge.initialize import (
        ATO_VERSION,
        KICAD_VERSION,
        InitInputError,
        read_spec,
    )

    try:
        spec = read_spec(project_dir / "spec.md")
    except InitInputError as exc:
        raise PolicyInputError(str(exc)) from exc
    profile, _, profile_hash = load_policy_profile(tool_root)
    contract = read_policy_contract(project_dir)
    if contract.profile != profile["id"]:
        raise PolicyInputError(
            f"policy profile {contract.profile!r} is not supported; "
            f"expected {profile['id']!r}"
        )
    hard = profile["hard"]
    defaults = profile["defaults"]
    exception_rules = profile["exception_rules"]
    valid_exception_rules = set(exception_rules)
    for exception in contract.exceptions:
        if exception.rule not in valid_exception_rules:
            raise PolicyInputError(
                f"exceptions.{exception.identifier}: unknown rule {exception.rule!r}"
            )

    baseline_mode, violations = _pinned_policy(
        project_dir,
        str(profile["id"]),
        profile_hash,
    )
    pins_path = project_dir / ".pcbforge"
    if pins_path.is_file():
        pins = _load_yaml(pins_path, ".pcbforge")
        lock_path = tool_root / "toolchain" / "uv.lock"
        rules_path = tool_root / "rules" / f"jlc-{spec.layers}layer.json"
        try:
            lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
            rules_bytes = rules_path.read_bytes()
            rules_profile = json.loads(rules_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise PolicyError(
                f"cannot validate pinned tool/rules inputs: {exc}"
            ) from exc
        toolchain = pins.get("toolchain")
        if not isinstance(toolchain, dict):
            violations.append(
                PolicyViolation(
                    "hard.toolchain",
                    ".pcbforge",
                    "init",
                    ".pcbforge toolchain pin is missing",
                    True,
                )
            )
        else:
            expected_versions = {
                "atopile": ATO_VERSION,
                "kicad": KICAD_VERSION,
                "uv_lock_sha256": lock_hash,
            }
            for key, expected in expected_versions.items():
                if toolchain.get(key) != expected:
                    violations.append(
                        PolicyViolation(
                            "hard.toolchain",
                            key,
                            "init",
                            f"{key} must be pinned to {expected}",
                            True,
                        )
                    )
        rules = pins.get("rules")
        expected_rule_name = rules_profile.get("name")
        expected_rule_hash = hashlib.sha256(rules_bytes).hexdigest()
        if (
            not isinstance(rules, dict)
            or rules.get("profile") != expected_rule_name
            or rules.get("profile_sha256") != expected_rule_hash
        ):
            violations.append(
                PolicyViolation(
                    "hard.fabricator-rules",
                    ".pcbforge",
                    "init",
                    f"missing pinned JLC {spec.layers}-layer rules profile",
                    True,
                )
            )
    baseline_fingerprint = policy_baseline_fingerprint(
        project_dir,
        tool_root=tool_root,
    )
    if (
        baseline_mode == "policy-event"
        and baseline_approval != baseline_fingerprint
    ):
        violations.append(
            PolicyViolation(
                "policy.baseline-approval",
                "project",
                "spec",
                "migrated policy baseline lacks current explicit user approval",
                True,
            )
        )

    manufacturing = contract.manufacturing
    components = contract.components
    hard_expectations = (
        ("hard.fabricator", "fabricator", hard["fabricator"]),
        ("hard.assembler", "assembler", hard["assembler"]),
    )
    for rule, key, expected in hard_expectations:
        if manufacturing.get(key) != expected:
            violations.append(
                PolicyViolation(
                    rule,
                    "project",
                    "spec",
                    f"manufacturing.{key} must be {expected!r}",
                    True,
                )
            )
    if components.get("mcu_vendor") != hard["mcu_vendor"]:
        violations.append(
            PolicyViolation(
                "hard.mcu-vendor",
                "project",
                "spec",
                f"components.mcu_vendor must be {hard['mcu_vendor']!r}",
                True,
            )
        )
    if spec.layers not in set(hard["allowed_layers"]):
        violations.append(
            PolicyViolation(
                "hard.layers",
                "spec.md",
                "spec",
                "only 2- or 4-layer boards are allowed",
                True,
            )
        )

    default_checks = (
        (
            "manufacturing.material",
            "material",
            defaults["material"],
            "board material",
        ),
        (
            "manufacturing.thickness",
            "thickness_mm",
            defaults["thickness_mm"],
            "board thickness",
        ),
        (
            "manufacturing.copper",
            "copper_oz",
            defaults["copper_oz"],
            "outer copper weight",
        ),
        (
            "manufacturing.controlled-impedance",
            "controlled_impedance",
            defaults["controlled_impedance"],
            "controlled impedance",
        ),
        (
            "manufacturing.advanced-vias",
            "advanced_vias",
            defaults["advanced_vias"],
            "advanced via technology",
        ),
    )
    for rule, key, expected, label in default_checks:
        if manufacturing.get(key) != expected:
            violations.append(
                PolicyViolation(
                    rule,
                    "project",
                    exception_rules[rule]["earliest_phase"],
                    f"{label} differs from the standard {expected!r}",
                )
            )
    minimum_package = str(components["commodity_min_package"])
    if minimum_package != defaults["commodity_min_package"]:
        violations.append(
            PolicyViolation(
                "components.commodity-package",
                "project",
                "implement",
                "project commodity-package default differs from 0603",
            )
        )
    advanced_packages = tuple(components["advanced_packages"])
    for declaration in advanced_packages:
        violations.append(
            PolicyViolation(
                "components.advanced-package",
                declaration,
                "implement",
                f"advanced package {declaration!r} is declared",
            )
        )

    violations.extend(
        _board_violations(
            project_dir,
            minimum_package,
            advanced_packages,
            float(defaults["qfn_min_pitch_mm"]),
        )
    )

    for rule, assurance in contract.assurances.items():
        policy_rule = f"assurance.{rule}"
        earliest = exception_rules[policy_rule]["earliest_phase"]
        if assurance.status == "required" and not assurance.evidence:
            violations.append(
                PolicyViolation(
                    policy_rule,
                    "project",
                    earliest,
                    f"{rule} is required but has no evidence",
                )
            )
        elif assurance.status == "exception":
            violations.append(
                PolicyViolation(
                    policy_rule,
                    "project",
                    earliest,
                    f"{rule} is explicitly excepted",
                )
            )

    warnings: list[PolicyWarning] = []
    sourcing_by_lcsc = {part.lcsc: part for part in contract.sourcing}
    build_test_path = project_dir / "build-test.yaml"
    expected_lcsc: set[str] = set()
    if build_test_path.is_file():
        try:
            from pcbforge.build_test import read_build_test_contract

            expected_lcsc = {
                component.lcsc
                for component in read_build_test_contract(project_dir).bom
            }
        except Exception as exc:
            violations.append(
                PolicyViolation(
                    "hard.exact-parts",
                    "build-test.yaml",
                    "build",
                    f"cannot validate exact BOM sourcing: {exc}",
                    True,
                )
            )
    for lcsc in sorted(expected_lcsc - set(sourcing_by_lcsc)):
        violations.append(
            PolicyViolation(
                "sourcing.unknown",
                lcsc,
                "implement",
                f"{lcsc} has no recorded sourcing evidence",
            )
        )
    for lcsc, part in sorted(sourcing_by_lcsc.items()):
        if part.jlc_class == "extended":
            warnings.append(
                PolicyWarning(
                    "sourcing.jlc-class",
                    lcsc,
                    f"{lcsc} is a JLC extended part",
                )
            )
        elif part.jlc_class == "unknown":
            violations.append(
                PolicyViolation(
                    "sourcing.unknown",
                    lcsc,
                    "implement",
                    f"{lcsc} has unknown JLC class",
                )
            )
        if part.assembly_status == "unavailable":
            violations.append(
                PolicyViolation(
                    "sourcing.unavailable",
                    lcsc,
                    "implement",
                    f"{lcsc} was unavailable for JLC assembly when checked",
                )
            )
        elif part.assembly_status == "unknown":
            violations.append(
                PolicyViolation(
                    "sourcing.unknown",
                    lcsc,
                    "implement",
                    f"{lcsc} assembly availability is unknown",
                )
            )
        if part.lifecycle in {"nrnd", "obsolete"}:
            violations.append(
                PolicyViolation(
                    "sourcing.lifecycle",
                    lcsc,
                    "implement",
                    f"{lcsc} lifecycle is {part.lifecycle}",
                )
            )
        elif part.lifecycle == "unknown":
            violations.append(
                PolicyViolation(
                    "sourcing.unknown",
                    lcsc,
                    "implement",
                    f"{lcsc} lifecycle is unknown",
                )
            )
        if part.second_source is None:
            warnings.append(
                PolicyWarning(
                    "sourcing.second-source",
                    lcsc,
                    f"{lcsc} has no recorded second source",
                )
            )

    current_exception_fingerprints = policy_exception_fingerprints(
        project_dir,
        tool_root=tool_root,
    )
    exception_approvals = exception_approvals or {}
    unresolved = []
    for violation in violations:
        if not _phase_in_scope(violation.earliest_phase, through_phase):
            continue
        resolved = _exception_resolution(
            violation,
            contract,
            exception_approvals,
            current_exception_fingerprints,
        )
        if resolved is not None:
            unresolved.append(resolved)

    used_exception_ids = {
        exception.identifier
        for exception in contract.exceptions
        if exception_approvals.get(exception.identifier)
        == current_exception_fingerprints.get(exception.identifier)
    }
    for exception in contract.exceptions:
        if exception.identifier not in used_exception_ids:
            continue
        if not any(
            violation.rule == exception.rule
            and exception.scope in {violation.scope, "project"}
            for violation in violations
        ):
            warnings.append(
                PolicyWarning(
                    "policy.unused-exception",
                    exception.identifier,
                    f"approved exception {exception.identifier!r} is not currently needed",
                )
            )

    return PolicyResult(
        project_dir,
        str(profile["id"]),
        policy_status_fingerprint(project_dir, tool_root=tool_root),
        baseline_fingerprint,
        tuple(unresolved),
        tuple(warnings),
    )


def render_policy_result(result: PolicyResult) -> str:
    """Render an actionable terminal policy report."""
    lines = [f"pcbforge: {result.summary}"]
    for violation in result.violations:
        qualifier = "hard" if violation.hard else "approval-required"
        lines.append(
            f"ERROR [{violation.rule}] ({qualifier}, {violation.scope}, "
            f"from {violation.earliest_phase}): {violation.message}"
        )
    for warning in result.warnings:
        lines.append(
            f"WARN [{warning.rule}] ({warning.scope}): {warning.message}"
        )
    return "\n".join(lines)


def _migration_discovery(
    project_dir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    sourcing_ids: tuple[str, ...] = ()
    build_test = project_dir / "build-test.yaml"
    if build_test.is_file():
        try:
            from pcbforge.build_test import read_build_test_contract

            sourcing_ids = tuple(
                sorted(
                    component.lcsc
                    for component in read_build_test_contract(project_dir).bom
                )
            )
        except Exception:
            sourcing_ids = ()

    advanced_packages: list[str] = []
    advanced_vias = False
    board_paths = sorted(project_dir.glob("*.kicad_pcb"))
    if board_paths:
        try:
            text = board_paths[0].read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            text = ""
        advanced_vias = bool(
            re.search(r"\(via\b[^)]*\(type\s+(?:blind|micro)\)", text, re.DOTALL)
        )
        try:
            from pcbforge.build_test import read_board_evidence

            board = read_board_evidence(board_paths[0])
        except Exception:
            board = None
        if board is not None:
            for reference, footprint in board.footprints:
                upper = footprint.upper()
                advanced = "WLCSP" in upper or bool(
                    re.search(r"(?:^|[:_-])BGA", upper)
                )
                if "QFN" in upper:
                    pitch = QFN_PITCH_RE.search(footprint)
                    advanced = advanced or bool(
                        pitch and float(pitch.group(1)) < 0.5
                    )
                if advanced:
                    advanced_packages.append(f"{reference}:{footprint}")
    return sourcing_ids, tuple(sorted(advanced_packages)), advanced_vias


def migrate_policy(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
) -> PolicyMigrationResult:
    """Explicitly migrate a generated schema-7-through-9 project to schema 13."""
    project_dir = project_dir.expanduser().resolve()
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    from pcbforge.initialize import (
        AGENTS_SCHEMA,
        APPROVAL_GUIDE_SCHEMA,
        ARCHITECT_GUIDE_SCHEMA,
        ARCHITECTURE_DIAGRAM_SCHEMA,
        BRIEF_GUIDE_SCHEMA,
        BUILD_TEST_GUIDE_SCHEMA,
        IMPLEMENT_GUIDE_SCHEMA,
        MCU_GUIDE_SCHEMA,
        POLICY_GUIDE_SCHEMA,
        STATUS_SCHEMA,
        CIRCUIT_REVIEW_SCHEMA,
        _render_agents,
        read_spec,
    )

    try:
        spec = read_spec(project_dir / "spec.md")
    except Exception as exc:
        raise PolicyInputError(str(exc)) from exc
    pins_path = project_dir / ".pcbforge"
    pins = dict(_load_yaml(pins_path, ".pcbforge"))
    schema = pins.get("schema")
    if schema == PROJECT_PIN_SCHEMA:
        check_policy(
            project_dir,
            tool_root=tool_root,
            through_phase="spec",
        )
        return PolicyMigrationResult(project_dir, False, ())
    if schema == 10:
        raise PolicyInputError(
            "policy is already migrated; run `pcbforge migrate-approvals` "
            "to upgrade schema 10 to current approvals"
        )
    if schema not in {7, 8, 9}:
        raise PolicyInputError(
            "migrate-policy requires generated .pcbforge schema 7, 8, or 9; "
            f"got {schema!r}"
        )
    policy_path = project_dir / POLICY_FILENAME
    if policy_path.exists():
        raise PolicyInputError(
            f"refusing to overwrite existing {POLICY_FILENAME}"
        )
    agents_path = project_dir / "AGENTS.md"
    try:
        agents = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PolicyInputError(f"cannot read {agents_path}: {exc}") from exc
    if not agents.startswith(
        f"<!-- pcbforge-agents-schema: {schema} -->"
    ):
        raise PolicyInputError(
            f"AGENTS.md is not the expected generated schema-{schema} guidance"
        )

    _, _, profile_hash = load_policy_profile(tool_root)
    sourcing_ids, advanced_packages, advanced_vias = _migration_discovery(
        project_dir
    )
    policy_contents = render_default_policy(
        sourcing_ids=sourcing_ids,
        advanced_packages=advanced_packages,
        advanced_vias=advanced_vias,
    )
    pins["schema"] = PROJECT_PIN_SCHEMA
    pins["policy"] = {
        "profile": POLICY_PROFILE_ID,
        "profile_sha256": profile_hash,
        "baseline_approval": "policy-event",
    }
    guidance = pins.get("guidance")
    if not isinstance(guidance, dict):
        raise PolicyInputError(".pcbforge guidance: expected a mapping")
    guidance = dict(guidance)
    guidance["agents_schema"] = AGENTS_SCHEMA
    guidance["architect_schema"] = ARCHITECT_GUIDE_SCHEMA
    guidance["architecture_diagram_schema"] = ARCHITECTURE_DIAGRAM_SCHEMA
    guidance["mcu_schema"] = MCU_GUIDE_SCHEMA
    guidance["implement_schema"] = IMPLEMENT_GUIDE_SCHEMA
    guidance["build_test_schema"] = BUILD_TEST_GUIDE_SCHEMA
    guidance["brief_schema"] = BRIEF_GUIDE_SCHEMA
    guidance["approval_schema"] = APPROVAL_GUIDE_SCHEMA
    guidance["policy_schema"] = POLICY_GUIDE_SCHEMA
    guidance["status_schema"] = STATUS_SCHEMA
    guidance["circuit_review_schema"] = CIRCUIT_REVIEW_SCHEMA
    pins["guidance"] = guidance
    outputs = {
        policy_path: policy_contents,
        agents_path: _render_agents(spec, tool_root),
        pins_path: yaml.safe_dump(pins, sort_keys=False),
    }
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in outputs
    }
    installed: list[Path] = []
    try:
        for path, contents in outputs.items():
            _atomic_write(path, contents)
            installed.append(path)
    except OSError as exc:
        for path in reversed(installed):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, original.decode("utf-8"))
            except OSError:
                pass
        raise PolicyError(f"could not migrate policy atomically: {exc}") from exc

    review_items = list(ASSURANCE_RULES)
    review_items.extend(sourcing_ids)
    review_items.extend(advanced_packages)
    if advanced_vias:
        review_items.append("advanced-vias")
    return PolicyMigrationResult(
        project_dir,
        True,
        tuple(review_items),
    )


def _atomic_write(path: Path, contents: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(contents)
        os.replace(temporary_name, path)
    except OSError:
        Path(temporary_name).unlink(missing_ok=True)
        raise
