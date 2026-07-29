"""Validate project STM32CubeMX configurations without modifying them."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from pcbforge.initialize import ProjectSpec, read_spec

CUBEMX_VERSION = "6.18"
IOC_FILE_VERSION = "6"

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class IocError(RuntimeError):
    """Base class for MCU configuration failures."""


class IocProjectError(IocError):
    """A missing or incompatible project/tool environment."""


class IocValidationError(IocError):
    """An invalid or semantically unstable CubeMX configuration."""


@dataclass(frozen=True)
class PinAssignment:
    pin: str
    label: str | None
    signal: str
    mode: str | None


@dataclass(frozen=True)
class IocCheckResult:
    project_dir: Path
    ioc_path: Path
    part_number: str
    family: str
    package: str
    pins: tuple[PinAssignment, ...]


def _unescape_property_key(key: str) -> str:
    """Normalize the escaped spaces CubeMX writes in Java-properties keys."""

    return key.replace("\\ ", " ")


def _parse_ioc(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IocValidationError(f"{path}: expected UTF-8 text") from exc
    except OSError as exc:
        raise IocProjectError(f"cannot read {path}: {exc}") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise IocValidationError(
                f"{path}:{line_number}: expected a key=value assignment"
            )
        raw_key, value = line.split("=", 1)
        if not raw_key or raw_key != raw_key.strip():
            raise IocValidationError(
                f"{path}:{line_number}: invalid key {raw_key!r}"
            )
        key = _unescape_property_key(raw_key)
        if key in values:
            raise IocValidationError(
                f"{path}:{line_number}: duplicate key {key!r}"
            )
        values[key] = value
    return values


def _indexed_values(
    values: Mapping[str, str],
    prefix: str,
    count_key: str,
    errors: list[str],
) -> list[str]:
    raw_count = values.get(count_key)
    try:
        count = int(raw_count) if raw_count is not None else None
    except ValueError:
        count = None
    if count is None or count < 0:
        errors.append(f"{count_key}: expected a non-negative integer")
        return []

    pattern = re.compile(rf"^{re.escape(prefix)}([0-9]+)$")
    indexed = {
        int(match.group(1)): value
        for key, value in values.items()
        if (match := pattern.fullmatch(key)) is not None
    }
    expected = set(range(count))
    actual = set(indexed)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(map(str, missing)))
        if unexpected:
            detail.append("unexpected " + ", ".join(map(str, unexpected)))
        errors.append(f"{prefix} entries do not match {count_key}: {'; '.join(detail)}")
    return [indexed[index] for index in sorted(indexed)]


def _pin_assignments(
    values: Mapping[str, str],
    pins: list[str],
) -> tuple[PinAssignment, ...]:
    assignments = []
    for pin in pins:
        signal = values.get(f"{pin}.Signal")
        if signal is None or pin.startswith("VP_"):
            continue
        assignments.append(
            PinAssignment(
                pin=pin,
                label=values.get(f"{pin}.GPIO_Label"),
                signal=_resolved_signal(signal, values),
                mode=values.get(f"{pin}.Mode"),
            )
        )
    return tuple(sorted(assignments, key=lambda item: _natural_key(item.pin)))


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(token) if token.isdigit() else token
        for token in re.split(r"([0-9]+)", value)
    )


def _peripheral_present(peripheral: str, signals: set[str]) -> bool:
    prefixes = {
        "usb-fs": ("USB_", "USB_DRD_FS"),
        "i2c": ("I2C",),
        "spi": ("SPI", "I2S"),
        "uart": ("USART", "UART", "LPUART"),
        "adc": ("ADC",),
        "dac": ("DAC",),
        "pwm": ("TIM", "LPTIM"),
        "can": ("CAN", "FDCAN"),
    }
    expected = prefixes.get(peripheral)
    if expected is None:
        return True
    return any(signal.startswith(expected) for signal in signals)


def _validate_ioc(
    values: Mapping[str, str],
    spec: ProjectSpec,
) -> tuple[list[str], tuple[PinAssignment, ...]]:
    errors: list[str] = []
    required = (
        "File.Version",
        "Mcu.CPN",
        "Mcu.Family",
        "Mcu.Name",
        "Mcu.Package",
        "MxCube.Version",
    )
    for key in required:
        if not values.get(key):
            errors.append(f"{key}: required")

    if values.get("File.Version") != IOC_FILE_VERSION:
        errors.append(f"File.Version: expected {IOC_FILE_VERSION}")
    cube_version = values.get("MxCube.Version", "")
    if not (
        cube_version == CUBEMX_VERSION
        or cube_version.startswith(f"{CUBEMX_VERSION}.")
    ):
        errors.append(f"MxCube.Version: expected {CUBEMX_VERSION}.x")

    expected_family = f"STM32{spec.stm32_family}"
    if values.get("Mcu.Family") != expected_family:
        errors.append(
            f"Mcu.Family: expected {expected_family} from spec.md, "
            f"got {values.get('Mcu.Family')!r}"
        )

    pins = _indexed_values(values, "Mcu.Pin", "Mcu.PinsNb", errors)
    ips = _indexed_values(values, "Mcu.IP", "Mcu.IPNb", errors)
    if len(pins) != len(set(pins)):
        errors.append("Mcu.Pin entries must be unique")
    if len(ips) != len(set(ips)):
        errors.append("Mcu.IP entries must be unique")

    for pin in pins:
        if not values.get(f"{pin}.Signal"):
            errors.append(f"{pin}.Signal: required for configured pin")

    assignments = _pin_assignments(values, pins)
    signals = {
        _resolved_signal(assignment.signal, values)
        for assignment in assignments
    }
    signals.update(
        _resolved_signal(value, values)
        for key, value in values.items()
        if key.endswith(".Signal")
    )
    if "SYS_JTMS-SWDIO" not in signals or "SYS_JTCK-SWCLK" not in signals:
        errors.append("SWD requires SYS_JTMS-SWDIO and SYS_JTCK-SWCLK")

    labels = [
        assignment.label
        for assignment in assignments
        if assignment.label is not None
    ]
    if any(not label for label in labels):
        errors.append("GPIO labels must not be empty")
    normalized_labels = [label.casefold() for label in labels if label]
    if len(normalized_labels) != len(set(normalized_labels)):
        errors.append("GPIO labels must be unique (case-insensitive)")

    if spec.debug_uart:
        required_debug_labels = {"debug_uart_tx", "debug_uart_rx"}
        missing = required_debug_labels - set(normalized_labels)
        if missing:
            errors.append(
                "debug UART requires GPIO labels "
                + ", ".join(sorted(label.upper() for label in missing))
            )

    for peripheral in spec.peripherals:
        if not _peripheral_present(peripheral, signals):
            errors.append(
                f"spec peripheral {peripheral!r} has no corresponding pin signal"
            )

    return errors, assignments


def _semantic_keys(values: Mapping[str, str]) -> set[str]:
    pins = [
        value
        for key, value in values.items()
        if re.fullmatch(r"Mcu\.Pin[0-9]+", key)
    ]
    ips = [
        value
        for key, value in values.items()
        if re.fullmatch(r"Mcu\.IP[0-9]+", key)
    ]
    exact = {
        "File.Version",
        "Mcu.CPN",
        "Mcu.Family",
        "Mcu.Name",
        "Mcu.Package",
        "Mcu.PinsNb",
        "Mcu.IPNb",
    }
    prefixes = tuple(
        [f"{pin}." for pin in pins]
        + [f"{ip}." for ip in ips]
        + ["RCC.", "NVIC.", "SH."]
    )
    return {
        key
        for key in values
        if key in exact or key.startswith(prefixes)
    }


def _indexed_set(values: Mapping[str, str], prefix: str) -> set[str]:
    return {
        value
        for key, value in values.items()
        if re.fullmatch(rf"{re.escape(prefix)}[0-9]+", key)
    }


def _resolved_signal(value: str, values: Mapping[str, str]) -> str:
    selection = values.get(f"SH.{value}.0")
    if selection is None:
        return value
    return selection.split(",", 1)[0]


def _equivalent_value(
    key: str,
    source_value: str,
    round_trip_value: str,
    source: Mapping[str, str],
    round_trip: Mapping[str, str],
) -> bool:
    if source_value == round_trip_value:
        return True
    if key.endswith((".GPIOParameters", ".IPParameters")):
        return set(source_value.split(",")) == set(round_trip_value.split(","))
    if key.endswith(".Signal"):
        return _resolved_signal(source_value, source) == _resolved_signal(
            round_trip_value,
            round_trip,
        )
    return False


def _compare_round_trip(
    source: Mapping[str, str],
    round_trip: Mapping[str, str],
) -> list[str]:
    differences: list[str] = []
    for prefix in ("Mcu.Pin", "Mcu.IP"):
        source_values = _indexed_set(source, prefix)
        round_trip_values = _indexed_set(round_trip, prefix)
        if source_values != round_trip_values:
            missing = sorted(source_values - round_trip_values)
            added = sorted(round_trip_values - source_values)
            if missing:
                differences.append(
                    f"CubeMX dropped {prefix} values: {', '.join(missing)}"
                )
            if added:
                differences.append(
                    f"CubeMX added {prefix} values: {', '.join(added)}"
                )

    for key in sorted(_semantic_keys(source)):
        if key not in round_trip:
            differences.append(f"CubeMX dropped {key}")
        elif not _equivalent_value(
            key,
            source[key],
            round_trip[key],
            source,
            round_trip,
        ):
            differences.append(
                f"CubeMX changed {key}: {source[key]!r} -> {round_trip[key]!r}"
            )
    return differences


def _run_cubemx_round_trip(
    ioc_path: Path,
    project_dir: Path,
    tool_root: Path,
    runner: CommandRunner,
) -> dict[str, str]:
    wrapper = tool_root / "scripts" / "cubemx"
    version = runner(
        [str(wrapper), "version"],
        cwd=project_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        detail = (version.stderr or version.stdout).strip()
        raise IocProjectError(detail or "STM32CubeMX 6.18 is unavailable")
    if version.stdout.strip() != CUBEMX_VERSION:
        raise IocProjectError(
            f"expected STM32CubeMX {CUBEMX_VERSION}, got {version.stdout.strip()!r}"
        )

    if '"' in str(ioc_path) or "\n" in str(ioc_path):
        raise IocProjectError("project path cannot contain quotes or newlines")

    with tempfile.TemporaryDirectory(prefix="pcbforge-cubemx-") as temporary:
        temporary_path = Path(temporary)
        round_trip = temporary_path / ioc_path.name
        script = temporary_path / "check.txt"
        script.write_text(
            "\n".join(
                (
                    f'config load "{ioc_path}"',
                    f'config saveext "{round_trip}"',
                    "exit",
                    "",
                )
            ),
            encoding="utf-8",
        )
        completed = runner(
            [str(wrapper), "-q", str(script)],
            cwd=project_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise IocValidationError(
                "CubeMX rejected the configuration"
                + (f": {detail}" if detail else "")
            )
        if not round_trip.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise IocValidationError(
                "CubeMX did not produce a round-trip configuration"
                + (f": {detail}" if detail else "")
            )
        return _parse_ioc(round_trip)


def check_ioc(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> IocCheckResult:
    """Validate a project's canonical .ioc and a CubeMX semantic round trip."""
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise IocProjectError(f"project directory does not exist: {project_dir}")
    if not (project_dir / ".pcbforge").is_file():
        raise IocProjectError(f"project is not initialized: {project_dir}")

    spec = read_spec(project_dir / "spec.md")
    ioc_path = project_dir / "firmware" / f"{spec.name}.ioc"
    if not ioc_path.is_file():
        raise IocProjectError(f"expected MCU configuration: {ioc_path}")

    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    source = _parse_ioc(ioc_path)
    errors, assignments = _validate_ioc(source, spec)
    if errors:
        raise IocValidationError(
            "invalid MCU configuration:\n- " + "\n- ".join(errors)
        )

    round_trip = _run_cubemx_round_trip(
        ioc_path,
        project_dir,
        tool_root,
        runner,
    )
    round_trip_errors, _ = _validate_ioc(round_trip, spec)
    errors = round_trip_errors + _compare_round_trip(source, round_trip)
    if errors:
        raise IocValidationError(
            "CubeMX round-trip validation failed:\n- " + "\n- ".join(errors)
        )

    return IocCheckResult(
        project_dir=project_dir,
        ioc_path=ioc_path,
        part_number=source["Mcu.CPN"],
        family=source["Mcu.Family"],
        package=source["Mcu.Package"],
        pins=assignments,
    )
