"""Audit project-local KiCad assets for commodity parts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pcbforge.initialize import InitInputError, read_spec


class PartsAuditError(RuntimeError):
    """A failure while auditing project part libraries."""


class PartsAuditInputError(PartsAuditError):
    """A user-correctable project input error."""


@dataclass(frozen=True)
class CommodityRule:
    category: str
    symbol: str
    footprint_library: str
    footprint_prefix: str
    packages: frozenset[str]


@dataclass(frozen=True)
class PartViolation:
    source: Path
    line: int
    component: str
    category: str
    package: str
    local_assets: tuple[str, ...]
    expected_symbol: str
    expected_footprint: str


@dataclass(frozen=True)
class PartsAuditResult:
    project_dir: Path
    scanned_parts: int
    violations: tuple[PartViolation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def summary(self) -> str:
        if self.violations:
            count = len(self.violations)
            noun = "part" if count == 1 else "parts"
            verb = "uses" if count == 1 else "use"
            return (
                f"{count} commodity {noun} {verb} project-local KiCad assets"
            )
        return (
            f"{self.scanned_parts} project-local atomic parts scanned; "
            "commodity library policy passed"
        )


_ALL_PACKAGES = frozenset(
    {
        "01005",
        "0201",
        "0402",
        "0603",
        "0805",
        "1206",
        "1210",
        "1812",
        "2010",
        "2512",
    }
)
_CAPACITOR_PACKAGES = _ALL_PACKAGES - {"2010", "2512"}

_RULES = {
    "R": CommodityRule(
        "resistor",
        "Device:R",
        "Resistor_SMD",
        "R",
        _ALL_PACKAGES,
    ),
    "C": CommodityRule(
        "capacitor",
        "Device:C",
        "Capacitor_SMD",
        "C",
        _CAPACITOR_PACKAGES,
    ),
    "LED": CommodityRule(
        "LED",
        "Device:LED",
        "LED_SMD",
        "LED",
        _ALL_PACKAGES,
    ),
}

_PACKAGE_METRIC = {
    "01005": "0402",
    "0201": "0603",
    "0402": "1005",
    "0603": "1608",
    "0805": "2012",
    "1206": "3216",
    "1210": "3225",
    "1812": "4532",
    "2010": "5025",
    "2512": "6332",
}

_COMPONENT_RE = re.compile(
    r"^component\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"(?P<body>.*?)(?=^(?:component|module|interface)\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_ATOMIC_RE = re.compile(r"\btrait\s+is_atomic_part\s*<(?P<args>[^>]*)>", re.DOTALL)
_ATTRIBUTE_RE = re.compile(
    r"\b(?P<name>footprint|symbol|model)\s*=\s*\"(?P<value>[^\"]+)\""
)
_PREFIX_RE = re.compile(
    r"\btrait\s+has_designator_prefix\s*<[^>]*\bprefix\s*=\s*\"(?P<prefix>[^\"]+)\"",
    re.DOTALL,
)
_PIN_RE = re.compile(r"^\s+pin\s+\S+", re.MULTILINE)
_LED_RE = re.compile(r"(?i)(?:^|[^A-Z])LED(?:[^A-Z]|$)")


def _package(text: str) -> str | None:
    upper = text.upper()
    for imperial, metric in _PACKAGE_METRIC.items():
        if re.search(rf"(?<!\d){imperial}(?!\d)", upper):
            return imperial
        if re.search(rf"(?<!\d){metric}\s*METRIC(?!\d)", upper):
            return imperial
    return None


def _local_assets(arguments: str) -> tuple[str, ...]:
    assets = []
    for match in _ATTRIBUTE_RE.finditer(arguments):
        value = match.group("value")
        if ":" in value or value.startswith("${"):
            continue
        if Path(value).suffix.lower() in {".kicad_mod", ".kicad_sym", ".step", ".wrl"}:
            assets.append(value)
    return tuple(assets)


def _commodity_rule(
    component: str,
    body: str,
    arguments: str,
) -> tuple[CommodityRule, str] | None:
    prefix_match = _PREFIX_RE.search(body)
    prefix = prefix_match.group("prefix").upper() if prefix_match else ""
    pin_count = len(_PIN_RE.findall(body))
    if pin_count != 2:
        return None

    combined = " ".join((component, body, arguments))
    package = _package(combined)
    if package is None:
        return None
    if prefix in {"R", "C"}:
        rule = _RULES[prefix]
        return (rule, package) if package in rule.packages else None
    if prefix == "D" and _LED_RE.search(combined):
        rule = _RULES["LED"]
        return (rule, package) if package in rule.packages else None
    return None


def _expected_footprint(rule: CommodityRule, package: str) -> str:
    metric = _PACKAGE_METRIC[package]
    return (
        f"{rule.footprint_library}:"
        f"{rule.footprint_prefix}_{package}_{metric}Metric"
    )


def check_parts(project_dir: Path) -> PartsAuditResult:
    """Find commodity parts that incorrectly carry project-local KiCad assets."""
    resolved = project_dir.expanduser().resolve()
    if not resolved.is_dir():
        raise PartsAuditInputError(f"project directory does not exist: {resolved}")
    try:
        read_spec(resolved / "spec.md")
    except InitInputError as exc:
        raise PartsAuditInputError(str(exc)) from exc

    parts_dir = resolved / "src" / "parts"
    if not parts_dir.exists():
        return PartsAuditResult(resolved, 0, ())
    if not parts_dir.is_dir():
        raise PartsAuditInputError(f"expected a directory at {parts_dir}")

    scanned = 0
    violations: list[PartViolation] = []
    for source in sorted(parts_dir.rglob("*.ato")):
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PartsAuditError(f"cannot read {source}: {exc}") from exc

        for component_match in _COMPONENT_RE.finditer(text):
            body = component_match.group("body")
            atomic_match = _ATOMIC_RE.search(body)
            if atomic_match is None:
                continue
            scanned += 1
            arguments = atomic_match.group("args")
            assets = _local_assets(arguments)
            if not assets:
                continue
            commodity = _commodity_rule(
                component_match.group("name"),
                body,
                arguments,
            )
            if commodity is None:
                continue
            rule, package = commodity
            absolute_offset = (
                component_match.start("body") + atomic_match.start()
            )
            line = text.count("\n", 0, absolute_offset) + 1
            violations.append(
                PartViolation(
                    source=source.relative_to(resolved),
                    line=line,
                    component=component_match.group("name"),
                    category=rule.category,
                    package=package,
                    local_assets=assets,
                    expected_symbol=rule.symbol,
                    expected_footprint=_expected_footprint(rule, package),
                )
            )

    return PartsAuditResult(resolved, scanned, tuple(violations))


def render_parts_audit(result: PartsAuditResult) -> str:
    """Render a concise, actionable terminal report."""
    lines = [f"pcbforge: {result.summary}"]
    for violation in result.violations:
        lines.extend(
            (
                (
                    f"{violation.source}:{violation.line}: "
                    f"{violation.component} is a standard {violation.package} "
                    f"{violation.category}"
                ),
                "  local assets: " + ", ".join(violation.local_assets),
                (
                    f"  use: {violation.expected_symbol} + "
                    f"{violation.expected_footprint}"
                ),
                "  keep the exact MPN/LCSC selection as supplier metadata",
            )
        )
    return "\n".join(lines)
