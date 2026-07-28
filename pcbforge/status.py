"""Durable, evidence-backed workflow status for pcbforge projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.build_test import (
    BUILD_TEST_FILENAME,
    BuildTestError,
    BuildTestInputError,
    ato_source_semantic_bytes,
    board_topology_bytes,
    build_test_inputs,
    check_build_test,
    read_board_evidence,
    saved_report_status,
)
from pcbforge.circuit_review import (
    CONTRACT_FILENAME as CIRCUIT_REVIEW_FILENAME,
    CircuitReviewError,
    CircuitReviewInputError,
    check_circuit_review,
    circuit_review_inputs,
    circuit_review_status_fingerprint,
)
from pcbforge.initialize import InitInputError, ProjectSpec, STATUS_SCHEMA, read_spec
from pcbforge.ioc import IocProjectError, IocValidationError, check_ioc
from pcbforge.parts import PartsAuditError, check_parts
from pcbforge.policy import (
    POLICY_FILENAME,
    PolicyError,
    PolicyInputError,
    check_policy,
    load_policy_profile,
    policy_baseline_fingerprint,
    policy_exception_fingerprints,
    policy_inputs,
    policy_sourcing_fingerprint,
    policy_status_fingerprint,
    read_policy_contract,
)
from pcbforge.placement import (
    PLACEMENT_FILENAME,
    PlacementError,
    PlacementInputError,
    brief_inputs,
    brief_status_fingerprint,
    check_brief,
)
from pcbforge.schematic import (
    BASELINE_PATH,
    SchematicError,
    SchematicInputError,
    baseline_is_current,
    capture_implementation_baseline,
    check_schematic,
    schematic_inputs,
    schematic_status_fingerprint,
)

STATUS_FILENAME = "STATUS.md"
ARCHITECTURE_MARKER = "pcbforge-architecture-diagram-schema: 1"

EVENT_ACTIONS = {
    "complete",
    "blocked",
    "reopened",
    "skipped",
    "proposal-approved",
}
LEGACY_APPROVAL_BOUND_PHASES = {"spec", "architect", "brief"}
APPROVAL_ENFORCEMENT_PIN_SCHEMA = 9
UNIVERSAL_APPROVAL_PIN_SCHEMA = 11
SCHEMATIC_APPROVAL_PIN_SCHEMA = 12
CIRCUIT_REVIEW_PIN_SCHEMA = 13
POLICY_ENFORCEMENT_PIN_SCHEMA = 10
POLICY_EVENT_ACTIONS = {
    "baseline-approved",
    "exception-approved",
    "sourcing-confirmed",
    "reopened",
}
LEGACY_MANUAL_PHASES = {
    "spec",
    "architect",
    "mcu",
    "implement",
    "brief",
    "layout",
    "route",
    "verify",
    "fab-out",
    "order",
    "publish",
}
CHECK_PHASES = {
    "architect": ("build",),
    "mcu": ("build", "ioc"),
    "implement": ("build", "parts", "policy", "ioc", "schematic-final"),
    "brief": ("build-test", "brief"),
    "verify": ("build", "policy", "ioc", "drc"),
}
PHASE_EVIDENCE_CHECKS = {
    "architect": ("build",),
    "mcu": ("build", "ioc"),
    "implement": ("build", "parts", "policy", "schematic-final"),
    "build": ("build-test",),
    "brief": ("brief",),
    "verify": ("policy", "drc"),
}

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class StatusError(RuntimeError):
    """A runtime failure while deriving or writing project status."""


class StatusInputError(StatusError):
    """A user-correctable dashboard or transition error."""


class StatusCheckError(StatusError):
    """A deterministic validation failed while recording a milestone."""


@dataclass(frozen=True)
class Phase:
    key: str
    label: str
    lead: str
    required: bool
    focus: str


PHASES = (
    Phase(
        "spec",
        "SPEC",
        "AI + user",
        True,
        "Finish and approve the requirements baseline.",
    ),
    Phase(
        "init",
        "init",
        "Tool",
        True,
        "Create the validated PCBForge project scaffold.",
    ),
    Phase(
        "architect",
        "ARCHITECT",
        "AI + user",
        True,
        "Approve the functional module graph and typed interfaces.",
    ),
    Phase(
        "mcu",
        "MCU",
        "AI",
        True,
        "Select, validate, and audit the exact STM32 configuration.",
    ),
    Phase(
        "implement",
        "IMPLEMENT",
        "AI",
        True,
        "Complete physical circuit definitions and part selection.",
    ),
    Phase(
        "build",
        "build + test",
        "Tool",
        True,
        "Get a current compiler build and electrical checks passing.",
    ),
    Phase(
        "brief",
        "brief",
        "AI + tool + user",
        True,
        "Validate and approve placement guidance before layout.",
    ),
    Phase("layout", "LAYOUT", "User", True, "Complete component placement in KiCad."),
    Phase("route", "ROUTE", "User", True, "Complete routing in KiCad."),
    Phase(
        "verify",
        "verify",
        "Tool + AI",
        True,
        "Pass DRC, scripted audits, and the final render review.",
    ),
    Phase(
        "fab-out",
        "fab-out",
        "Tool",
        True,
        "Generate and review the JLCPCB manufacturing package.",
    ),
    Phase("order", "order", "User", True, "Upload the package and place the order."),
    Phase(
        "publish",
        "publish",
        "AI + user",
        False,
        "Publish or explicitly skip reusable proven modules.",
    ),
)
PHASE_BY_KEY = {phase.key: phase for phase in PHASES}
PHASE_NUMBER = {phase.key: index for index, phase in enumerate(PHASES, start=1)}
APPROVAL_BOUND_PHASES = set(PHASE_BY_KEY)

APPROVAL_CHECKS = {
    "spec": ("policy",),
    "init": ("build", "policy"),
    "architect": ("build",),
    "mcu": ("build", "ioc"),
    "implement": ("build", "parts", "policy", "ioc", "schematic-final"),
    "build": ("build-test", "policy"),
    "brief": ("build-test", "brief", "policy"),
    "verify": ("build", "policy", "ioc", "drc"),
}


@dataclass(frozen=True)
class StatusEvent:
    at: str
    phase: str
    action: str
    note: str
    approval_fingerprint: str = ""


@dataclass(frozen=True)
class CheckRecord:
    at: str
    fingerprint: str
    outcome: str
    summary: str


@dataclass(frozen=True)
class PolicyEvent:
    at: str
    action: str
    subject: str
    note: str
    approval_fingerprint: str = ""


@dataclass(frozen=True)
class StatusDocument:
    updated_at: str
    events: tuple[StatusEvent, ...]
    checks: Mapping[str, CheckRecord]
    policy_events: tuple[PolicyEvent, ...] = ()


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    state: str
    detail: str
    complete: bool


@dataclass(frozen=True)
class StatusReport:
    project_dir: Path
    spec: ProjectSpec
    document: StatusDocument
    phases: tuple[PhaseResult, ...]
    current: PhaseResult | None
    next_actions: tuple[str, ...]
    completed_required: int
    required_total: int
    checks_failed: bool


@dataclass(frozen=True)
class StatusResult:
    report: StatusReport
    wrote: bool


@dataclass(frozen=True)
class PhaseReviewCheck:
    name: str
    outcome: str
    summary: str
    fingerprint: str


@dataclass(frozen=True)
class PhaseReview:
    project_dir: Path
    phase: Phase
    ready: bool
    detail: str
    fingerprint: str
    artifacts: tuple[str, ...]
    checks: tuple[PhaseReviewCheck, ...]
    stage: str = "final"


@dataclass(frozen=True)
class ApprovalMigrationResult:
    project_dir: Path
    wrote: bool
    reopened_phases: tuple[str, ...]


class _UniqueStatusLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueStatusLoader,
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


_UniqueStatusLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _status_path(project_dir: Path) -> Path:
    return project_dir / STATUS_FILENAME


def _load_yaml_frontmatter(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise StatusInputError(f"cannot read {path}: {exc}") from exc

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise StatusInputError(f"{STATUS_FILENAME} must begin with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise StatusInputError(
            f"{STATUS_FILENAME} is missing its closing frontmatter delimiter"
        ) from exc
    try:
        loaded = yaml.load("\n".join(lines[1:end]), Loader=_UniqueStatusLoader)
    except yaml.YAMLError as exc:
        raise StatusInputError(f"invalid {STATUS_FILENAME} frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StatusInputError(f"{STATUS_FILENAME} frontmatter must be a mapping")
    return loaded


def _text(value: Any, field: str, errors: list[str], *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        errors.append(f"{field}: expected a non-empty string")
        return ""
    return value.strip()


def read_status_document(project_dir: Path) -> StatusDocument:
    """Read and validate STATUS.md frontmatter, or return an empty document."""
    data = _load_yaml_frontmatter(_status_path(project_dir))
    if not data:
        return StatusDocument(updated_at="", events=(), checks={})

    errors: list[str] = []
    allowed = {
        "pcbforge_status_schema",
        "updated_at",
        "events",
        "policy_events",
        "checks",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append("unknown keys: " + ", ".join(map(str, unknown)))
    if data.get("pcbforge_status_schema") not in {1, STATUS_SCHEMA}:
        errors.append(
            f"pcbforge_status_schema: expected integer 1 or {STATUS_SCHEMA}"
        )
    updated_at = _text(data.get("updated_at"), "updated_at", errors, required=False)

    events_raw = data.get("events", [])
    events: list[StatusEvent] = []
    if not isinstance(events_raw, list):
        errors.append("events: expected a list")
    else:
        for index, raw in enumerate(events_raw):
            prefix = f"events[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {
                "at",
                "phase",
                "action",
                "note",
                "approval_fingerprint",
            }:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            phase = _text(raw.get("phase"), f"{prefix}.phase", errors)
            action = _text(raw.get("action"), f"{prefix}.action", errors)
            note = _text(raw.get("note"), f"{prefix}.note", errors)
            approval_fingerprint = _text(
                raw.get("approval_fingerprint"),
                f"{prefix}.approval_fingerprint",
                errors,
                required=False,
            )
            if phase and phase not in PHASE_BY_KEY:
                errors.append(f"{prefix}.phase: unknown phase {phase!r}")
            if action and action not in EVENT_ACTIONS:
                errors.append(f"{prefix}.action: unknown action {action!r}")
            events.append(
                StatusEvent(
                    at=at,
                    phase=phase,
                    action=action,
                    note=note,
                    approval_fingerprint=approval_fingerprint,
                )
            )

    policy_events_raw = data.get("policy_events", [])
    policy_events: list[PolicyEvent] = []
    if not isinstance(policy_events_raw, list):
        errors.append("policy_events: expected a list")
    else:
        for index, raw in enumerate(policy_events_raw):
            prefix = f"policy_events[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {
                "at",
                "action",
                "subject",
                "note",
                "approval_fingerprint",
            }:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            action = _text(raw.get("action"), f"{prefix}.action", errors)
            subject = _text(raw.get("subject"), f"{prefix}.subject", errors)
            note = _text(raw.get("note"), f"{prefix}.note", errors)
            approval_fingerprint = _text(
                raw.get("approval_fingerprint"),
                f"{prefix}.approval_fingerprint",
                errors,
                required=False,
            )
            if action and action not in POLICY_EVENT_ACTIONS:
                errors.append(f"{prefix}.action: unknown action {action!r}")
            policy_events.append(
                PolicyEvent(
                    at,
                    action,
                    subject,
                    note,
                    approval_fingerprint,
                )
            )

    checks_raw = data.get("checks", {})
    checks: dict[str, CheckRecord] = {}
    if not isinstance(checks_raw, dict):
        errors.append("checks: expected a mapping")
    else:
        for name, raw in checks_raw.items():
            prefix = f"checks.{name}"
            if name not in {
                "build",
                "build-test",
                "parts",
                "ioc",
                "brief",
                "policy",
                "drc",
                "schematic-proposal",
                "schematic-final",
                "circuit-proposal",
                "circuit-final",
            }:
                errors.append(f"{prefix}: unknown check")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {"at", "fingerprint", "outcome", "summary"}:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            fingerprint = _text(raw.get("fingerprint"), f"{prefix}.fingerprint", errors)
            outcome = _text(raw.get("outcome"), f"{prefix}.outcome", errors)
            summary = _text(raw.get("summary"), f"{prefix}.summary", errors)
            if outcome and outcome not in {"pass", "fail"}:
                errors.append(f"{prefix}.outcome: expected 'pass' or 'fail'")
            checks[name] = CheckRecord(at, fingerprint, outcome, summary)

    if errors:
        raise StatusInputError(
            f"invalid {STATUS_FILENAME} frontmatter:\n  - " + "\n  - ".join(errors)
        )
    return StatusDocument(
        updated_at=updated_at,
        events=tuple(events),
        checks=checks,
        policy_events=tuple(policy_events),
    )


def _project_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise StatusInputError(f"project directory does not exist: {resolved}")
    try:
        read_spec(resolved / "spec.md")
    except InitInputError as exc:
        raise StatusInputError(str(exc)) from exc
    return resolved


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StatusError(f"cannot read {path}: {exc}") from exc


def _files(project_dir: Path, patterns: Sequence[str]) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in project_dir.glob(pattern) if path.is_file())
    return tuple(sorted(paths))


def _fingerprint(project_dir: Path, paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        try:
            relative = path.relative_to(project_dir)
            contents = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise StatusError(f"cannot fingerprint {path}: {exc}") from exc
        digest.update(str(relative).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(contents).digest())
    return digest.hexdigest()


def _approval_constraints_enabled(project_dir: Path) -> bool:
    path = project_dir / ".pcbforge"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueStatusLoader)
    except (FileNotFoundError, OSError, UnicodeError, yaml.YAMLError):
        return False
    return (
        isinstance(data, dict)
        and type(data.get("schema")) is int
        and data["schema"] >= APPROVAL_ENFORCEMENT_PIN_SCHEMA
    )


def _universal_approval_enabled(project_dir: Path) -> bool:
    pins = _project_pins(project_dir)
    if not pins:
        return True
    return (
        type(pins.get("schema")) is int
        and pins["schema"] >= UNIVERSAL_APPROVAL_PIN_SCHEMA
    )


def _schematic_approval_enabled(project_dir: Path) -> bool:
    pins = _project_pins(project_dir)
    return (
        type(pins.get("schema")) is int
        and pins["schema"] >= SCHEMATIC_APPROVAL_PIN_SCHEMA
    )


def _circuit_review_enabled(project_dir: Path) -> bool:
    pins = _project_pins(project_dir)
    return (
        type(pins.get("schema")) is int
        and pins["schema"] >= CIRCUIT_REVIEW_PIN_SCHEMA
    )


def _phase_check_names(
    project_dir: Path,
    checks: Mapping[str, tuple[str, ...]],
    phase: str,
) -> tuple[str, ...]:
    names = checks.get(phase, ())
    if _circuit_review_enabled(project_dir):
        return tuple(
            "circuit-final" if name == "schematic-final" else name
            for name in names
        )
    return names


def _phase_requires_approval(project_dir: Path, phase: str) -> bool:
    pins = _project_pins(project_dir)
    if not pins:
        return phase == "spec"
    schema = pins.get("schema")
    if type(schema) is not int:
        return False
    if schema >= UNIVERSAL_APPROVAL_PIN_SCHEMA:
        return True
    return (
        schema >= APPROVAL_ENFORCEMENT_PIN_SCHEMA
        and phase in LEGACY_APPROVAL_BOUND_PHASES
    )


def _project_pins(project_dir: Path) -> Mapping[str, Any]:
    path = project_dir / ".pcbforge"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueStatusLoader)
    except (FileNotFoundError, OSError, UnicodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _policy_constraints_enabled(project_dir: Path) -> bool:
    data = _project_pins(project_dir)
    return (
        type(data.get("schema")) is int
        and data["schema"] >= POLICY_ENFORCEMENT_PIN_SCHEMA
    )


def _spec_binds_policy(project_dir: Path) -> bool:
    pins = _project_pins(project_dir)
    if not pins:
        return (project_dir / POLICY_FILENAME).is_file()
    policy = pins.get("policy")
    return (
        isinstance(policy, dict)
        and policy.get("baseline_approval") == "spec"
    )


def _approval_fingerprint(
    project_dir: Path,
    phase: str,
    action: str = "complete",
    document: StatusDocument | None = None,
) -> str:
    project_dir = _project_dir(project_dir)
    if action == "proposal-approved" and phase == "implement":
        if _circuit_review_enabled(project_dir):
            try:
                paths = {
                    *circuit_review_inputs(project_dir, "proposal"),
                    (
                        project_dir
                        / "review"
                        / "implement"
                        / "proposal"
                        / "evidence.json"
                    ),
                }
            except CircuitReviewError:
                paths = {
                    path
                    for path in (
                        project_dir / ".pcbforge",
                        project_dir / CIRCUIT_REVIEW_FILENAME,
                        project_dir / "review" / "implement" / "circuit.yaml",
                        project_dir / "review" / "implement" / "circuit.svg",
                        project_dir / "docs" / "implementation-proposal.md",
                        project_dir / BASELINE_PATH,
                    )
                    if path.is_file()
                }
            check_name = "circuit-proposal"
            approval_schema = 4
        else:
            paths = {
                *schematic_inputs(project_dir, "proposal"),
                *(
                    path
                    for path in (
                        project_dir / "review" / "implement" / "proposal"
                    ).rglob("*")
                    if path.is_file()
                ),
            }
            check_name = "schematic-proposal"
            approval_schema = 3
        payload = {
            "approval_schema": approval_schema,
            "phase": phase,
            "stage": "proposal",
            "artifacts": _file_semantics(
                project_dir,
                tuple(path for path in paths if path.is_file()),
            ),
            "checks": [
                {
                    "name": check_name,
                    "required_outcome": "pass",
                }
            ],
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    if (
        action == "complete"
        and phase not in LEGACY_APPROVAL_BOUND_PHASES
    ):
        document = (
            document
            if document is not None
            else read_status_document(project_dir)
        )
        return _phase_approval_fingerprint(project_dir, phase, document)
    if phase == "brief":
        return brief_status_fingerprint(project_dir)
    paths = {
        "spec": (project_dir / "spec.md",),
        "architect": (
            project_dir / "spec.md",
            project_dir / "docs" / "architecture.md",
            *(
                (project_dir / "src" / "main.ato",)
                if action == "complete"
                else ()
            ),
        ),
    }.get(phase)
    if paths is None:
        raise AssertionError(f"{phase} has no approval fingerprint")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(project_dir).as_posix().encode())
        digest.update(b"\0")
        try:
            digest.update(
                hashlib.sha256(
                    ato_source_semantic_bytes(path)
                    if path.suffix == ".ato"
                    else path.read_bytes()
                ).digest()
            )
        except OSError:
            digest.update(b"<missing>")
    if phase == "spec" and _spec_binds_policy(project_dir):
        digest.update(b"policy-baseline\0")
        try:
            digest.update(
                policy_baseline_fingerprint(project_dir).encode()
            )
        except PolicyError as exc:
            digest.update(f"<invalid:{exc}>".encode())
    return digest.hexdigest()


def _approval_is_current(
    project_dir: Path,
    phase: str,
    event: StatusEvent | None,
    document: StatusDocument | None = None,
) -> bool:
    if not _phase_requires_approval(project_dir, phase):
        return True
    if event is not None and event.approval_fingerprint:
        return event.approval_fingerprint == _approval_fingerprint(
            project_dir,
            phase,
            event.action,
            document,
        )
    return False


def _current_proposal(
    project_dir: Path,
    document: StatusDocument,
    phase: str,
) -> StatusEvent | None:
    for event in reversed(document.events):
        if event.phase != phase:
            continue
        if (
            event.action == "reopened"
            and not event.note.startswith(
                "Approval invalidated automatically because"
            )
        ):
            return None
        if event.action == "proposal-approved":
            return (
                event
                if _approval_is_current(
                    project_dir,
                    phase,
                    event,
                    document,
                )
                else None
            )
    return None


def _current_architect_proposal(
    project_dir: Path,
    document: StatusDocument,
) -> StatusEvent | None:
    return _current_proposal(project_dir, document, "architect")


def _current_implement_proposal(
    project_dir: Path,
    document: StatusDocument,
) -> StatusEvent | None:
    return _current_proposal(project_dir, document, "implement")


def _architecture_source_started(project_dir: Path) -> bool:
    sources = _files(project_dir, ("src/**/*.ato",))
    main = project_dir / "src" / "main.ato"
    if any(path != main for path in sources):
        return True
    if not main.is_file():
        return False
    text = _read_text(main)
    text = re.sub(r'(?s)""".*?"""', "", text)
    text = re.sub(r"(?s)'''.*?'''", "", text)
    code = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return code != ["module App:", "pass"]


def _latest_policy_events(
    document: StatusDocument,
) -> Mapping[str, PolicyEvent]:
    latest: dict[str, PolicyEvent] = {}
    for event in document.policy_events:
        latest[event.subject] = event
    return latest


def _policy_approval_context(
    document: StatusDocument,
) -> tuple[str, Mapping[str, str], str]:
    latest = _latest_policy_events(document)
    baseline_event = latest.get("baseline")
    sourcing_event = latest.get("sourcing")
    baseline = (
        baseline_event.approval_fingerprint
        if baseline_event is not None
        and baseline_event.action == "baseline-approved"
        else ""
    )
    sourcing = (
        sourcing_event.approval_fingerprint
        if sourcing_event is not None
        and sourcing_event.action == "sourcing-confirmed"
        else ""
    )
    exceptions = {
        subject: event.approval_fingerprint
        for subject, event in latest.items()
        if subject not in {"baseline", "sourcing"}
        and event.action == "exception-approved"
    }
    return baseline, exceptions, sourcing


def policy_approval_context(
    document: StatusDocument,
) -> tuple[str, Mapping[str, str], str]:
    """Expose current recorded policy approvals to read-only validators."""
    return _policy_approval_context(document)


def _current_sourcing_confirmation(
    project_dir: Path,
    document: StatusDocument,
    *,
    tool_root: Path | None = None,
) -> bool:
    _, _, recorded = _policy_approval_context(document)
    if not recorded:
        return False
    try:
        expected = policy_sourcing_fingerprint(
            project_dir,
            tool_root=tool_root,
        )
    except PolicyError:
        return False
    return recorded == expected


def _current_policy_baseline(
    project_dir: Path,
    document: StatusDocument,
) -> bool:
    if not _policy_constraints_enabled(project_dir):
        return True
    pins = _project_pins(project_dir)
    policy = pins.get("policy")
    if not isinstance(policy, dict):
        return False
    mode = policy.get("baseline_approval")
    if mode == "spec":
        return True
    if mode != "policy-event":
        return False
    recorded, _, _ = _policy_approval_context(document)
    if not recorded:
        return False
    try:
        expected = policy_baseline_fingerprint(project_dir)
    except PolicyError:
        return False
    return recorded == expected


def _check_inputs(project_dir: Path, spec: ProjectSpec, name: str) -> tuple[Path, ...]:
    if name == "build":
        return _files(
            project_dir,
            (
                "spec.md",
                "ato.yaml",
                "src/**/*.ato",
                f"{spec.name}.kicad_pcb",
                f"{spec.name}.kicad_pro",
                f"{spec.name}.kicad_dru",
            ),
        )
    if name == "ioc":
        return _files(
            project_dir,
            ("spec.md", f"firmware/{spec.name}.ioc"),
        )
    if name == "parts":
        return _files(
            project_dir,
            (
                "spec.md",
                "fp-lib-table",
                "src/**/*.ato",
                "src/**/*.kicad_mod",
                "src/**/*.kicad_sym",
                "src/**/*.step",
                "src/**/*.wrl",
            ),
        )
    if name == "build-test":
        return build_test_inputs(project_dir)
    if name == "brief":
        return brief_inputs(project_dir)
    if name == "schematic-proposal":
        return schematic_inputs(project_dir, "proposal")
    if name == "schematic-final":
        return schematic_inputs(project_dir, "final")
    if name == "circuit-proposal":
        return circuit_review_inputs(project_dir, "proposal")
    if name == "circuit-final":
        return circuit_review_inputs(project_dir, "final")
    if name == "policy":
        return policy_inputs(project_dir)
    if name == "drc":
        return _files(
            project_dir,
            (
                f"{spec.name}.kicad_pcb",
                f"{spec.name}.kicad_pro",
                f"{spec.name}.kicad_dru",
            ),
        )
    raise AssertionError(f"unknown check: {name}")


def _current_check(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    name: str,
) -> tuple[bool, str]:
    record = document.checks.get(name)
    if record is None:
        return False, f"{name} has not been checked"
    try:
        inputs = _check_inputs(project_dir, spec, name)
    except (CircuitReviewError, SchematicError, OSError) as exc:
        return False, f"{name} inputs are invalid: {exc}"
    if not inputs:
        return False, f"{name} inputs are missing"
    try:
        fingerprint = _check_fingerprint(project_dir, name, inputs)
    except (
        BuildTestError,
        CircuitReviewError,
        PlacementError,
        PolicyError,
        SchematicError,
        OSError,
    ) as exc:
        return False, f"{name} inputs are invalid: {exc}"
    if record.fingerprint != fingerprint:
        return False, f"{name} result is stale"
    if record.outcome != "pass":
        return False, f"{name} failed: {record.summary}"
    return True, f"{name} passed"


def _check_fingerprint(
    project_dir: Path,
    name: str,
    inputs: Sequence[Path],
) -> str:
    if name == "build":
        digest = hashlib.sha256()
        for path in inputs:
            if path.suffix in {".kicad_pcb", ".kicad_pro", ".kicad_dru"}:
                continue
            digest.update(path.relative_to(project_dir).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        board_paths = [path for path in inputs if path.suffix == ".kicad_pcb"]
        if board_paths:
            digest.update(b"pcb-topology\0")
            try:
                digest.update(board_topology_bytes(read_board_evidence(board_paths[0])))
            except BuildTestError:
                digest.update(b"invalid\0")
                digest.update(hashlib.sha256(board_paths[0].read_bytes()).digest())
        return digest.hexdigest()
    if name == "build-test":
        from pcbforge.build_test import fingerprint_inputs

        return fingerprint_inputs(project_dir)
    if name == "brief":
        return brief_status_fingerprint(project_dir)
    if name == "schematic-proposal":
        return schematic_status_fingerprint(project_dir, "proposal")
    if name == "schematic-final":
        return schematic_status_fingerprint(project_dir, "final")
    if name == "circuit-proposal":
        return circuit_review_status_fingerprint(project_dir, "proposal")
    if name == "circuit-final":
        return circuit_review_status_fingerprint(project_dir, "final")
    if name == "policy":
        return policy_status_fingerprint(project_dir)
    return _fingerprint(project_dir, inputs)


def _phase_artifact_paths(
    project_dir: Path,
    spec: ProjectSpec,
    phase: str,
) -> tuple[Path, ...]:
    board = project_dir / f"{spec.name}.kicad_pcb"
    project = project_dir / f"{spec.name}.kicad_pro"
    rules = project_dir / f"{spec.name}.kicad_dru"
    if phase == "spec":
        candidates = (project_dir / "spec.md", project_dir / POLICY_FILENAME)
    elif phase == "init":
        candidates = (
            project_dir / ".pcbforge",
            project_dir / "ato.yaml",
            rules,
            project_dir / "AGENTS.md",
        )
    elif phase == "architect":
        candidates = (
            project_dir / "spec.md",
            project_dir / "docs" / "architecture.md",
            project_dir / "src" / "main.ato",
        )
    elif phase == "mcu":
        candidates = (
            project_dir / "firmware" / f"{spec.name}.ioc",
            project_dir / "src" / "mcu.ato",
            project_dir / "docs" / "mcu.md",
        )
    elif phase == "implement":
        candidates = (
            project_dir / "spec.md",
            project_dir / "ato.yaml",
            project_dir / "fp-lib-table",
            *_files(
                project_dir,
                (
                    "src/**/*.ato",
                    "src/**/*.kicad_mod",
                    "src/**/*.kicad_sym",
                    "src/**/*.step",
                    "src/**/*.wrl",
                    "firmware/*.ioc",
                ),
            ),
            project_dir / POLICY_FILENAME,
            project_dir / "schematic-review.yaml",
            project_dir / CIRCUIT_REVIEW_FILENAME,
            project_dir / "docs" / "implementation-proposal.md",
            project_dir / "docs" / "implementation-review.md",
            *_files(
                project_dir,
                (
                    "review/implement/proposal/**/*",
                    "review/implement/final/**/*",
                ),
            ),
            board,
        )
    elif phase == "build":
        candidates = (
            *build_test_inputs(project_dir),
            project_dir / "docs" / "build-test.md",
        )
    elif phase == "brief":
        candidates = (
            *brief_inputs(project_dir),
            project_dir / "build-test.yaml",
            project_dir / "docs" / "build-test.md",
        )
    elif phase in {"layout", "route"}:
        candidates = (board,)
    elif phase == "verify":
        candidates = (board, project, rules)
    elif phase == "fab-out":
        candidates = tuple(
            sorted(
                path
                for path in (project_dir / "fab").rglob("*")
                if path.is_file() and path.name != ".gitkeep"
            )
        )
    elif phase == "order":
        candidates = (
            project_dir / POLICY_FILENAME,
            project_dir / "build-test.yaml",
            *tuple(
                sorted(
                    path
                    for path in (project_dir / "fab").rglob("*")
                    if path.is_file() and path.name != ".gitkeep"
                )
            ),
        )
    elif phase == "publish":
        candidates = (
            *_files(
                project_dir,
                (
                    "src/**/*.ato",
                    "src/**/*.kicad_mod",
                    "src/**/*.kicad_sym",
                    "src/**/*.step",
                    "src/**/*.wrl",
                    "docs/**/*.md",
                ),
            ),
        )
    else:
        raise AssertionError(f"unknown phase: {phase}")
    return tuple(
        sorted({path for path in candidates if path.is_file()})
    )


def _file_semantics(project_dir: Path, paths: Sequence[Path]) -> list[dict[str, str]]:
    semantics = []
    for path in sorted(set(paths)):
        semantics.append(
            {
                "path": path.relative_to(project_dir).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return semantics


def _implementation_source_semantics(
    project_dir: Path,
) -> list[dict[str, str]]:
    """Bind IMPLEMENT source while allowing Step-6 marker/assert pairs."""
    semantics = []
    for path in _files(project_dir, ("src/**/*.ato",)):
        semantics.append(
            {
                "path": path.relative_to(project_dir).as_posix(),
                "sha256": hashlib.sha256(
                    ato_source_semantic_bytes(path)
                ).hexdigest(),
            }
        )
    return semantics


def _board_phase_semantics(path: Path, phase: str) -> Mapping[str, Any]:
    try:
        from pcbforge.build_test import _canonical_tokens, _top_level_blocks

        text = path.read_text(encoding="utf-8")
        board = read_board_evidence(path)
        blocks = _top_level_blocks(text)
        mechanical = sorted(
            _canonical_tokens(block)
            for head, block in blocks
            if head.startswith("gr_")
            or head in {"dimension", "image", "target"}
        )
        layout = {
            "footprint_placements": board.footprint_placements,
            "mechanical": mechanical,
        }
        if phase == "layout":
            return layout
        routing = sorted(
            _canonical_tokens(block)
            for head, block in blocks
            if head in {"segment", "arc", "via", "zone"}
        )
        if phase == "route":
            return {"layout": layout, "routing": routing}
        if phase == "verify":
            return {"canonical_board": _canonical_tokens(text)}
    except (BuildTestError, OSError, UnicodeError, ValueError):
        return {
            "invalid_board_sha256": (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else "<missing>"
            )
        }
    raise AssertionError(f"unknown board approval phase: {phase}")


def _approval_check_semantics(
    project_dir: Path,
    phase: str,
) -> list[dict[str, str]]:
    return [
        {"name": name, "required_outcome": "pass"}
        for name in _phase_check_names(project_dir, APPROVAL_CHECKS, phase)
    ]


def _phase_approval_fingerprint(
    project_dir: Path,
    phase: str,
    document: StatusDocument,
) -> str:
    spec = read_spec(project_dir / "spec.md")
    artifacts = _phase_artifact_paths(project_dir, spec, phase)
    payload: dict[str, Any] = {
        "approval_schema": (
            4
            if _circuit_review_enabled(project_dir) and phase == "implement"
            else 3
            if _schematic_approval_enabled(project_dir)
            else 2
        ),
        "phase": phase,
        "artifacts": _file_semantics(project_dir, artifacts),
        "checks": _approval_check_semantics(project_dir, phase),
    }
    if phase in {"architect", "mcu"}:
        semantic_sources = {
            item["path"]: item["sha256"]
            for item in _implementation_source_semantics(project_dir)
        }
        payload["artifacts"] = [
            {
                **item,
                "sha256": semantic_sources.get(item["path"], item["sha256"]),
            }
            for item in payload["artifacts"]
        ]
    board = project_dir / f"{spec.name}.kicad_pcb"
    if phase == "implement":
        payload["sources"] = _implementation_source_semantics(project_dir)
        try:
            payload["board_topology_sha256"] = hashlib.sha256(
                board_topology_bytes(read_board_evidence(board))
            ).hexdigest()
        except BuildTestError as exc:
            payload["board_topology_sha256"] = f"<invalid:{exc}>"
        payload["artifacts"] = [
            item
            for item in payload["artifacts"]
            if item["path"] != board.name
            and not item["path"].endswith(".ato")
        ]
    if phase in {"layout", "route", "verify"}:
        payload["board"] = _board_phase_semantics(board, phase)
        payload["artifacts"] = [
            item for item in payload["artifacts"] if item["path"] != board.name
        ]
    if phase == "order":
        try:
            payload["sourcing"] = policy_sourcing_fingerprint(project_dir)
        except PolicyError as exc:
            payload["sourcing"] = f"<invalid:{exc}>"
    return hashlib.sha256(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _static_evidence(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    phase: str,
) -> tuple[bool, str, bool]:
    """Return (satisfied, detail, partial evidence present)."""
    board = project_dir / f"{spec.name}.kicad_pcb"
    if phase == "spec":
        return True, "valid spec.md", True
    if phase == "init":
        required = (
            project_dir / ".pcbforge",
            project_dir / "ato.yaml",
            project_dir / "src" / "main.ato",
            board,
        )
        missing = [path.name for path in required if not path.is_file()]
        return (
            not missing,
            "project scaffold present"
            if not missing
            else "missing scaffold: " + ", ".join(missing),
            len(missing) < len(required),
        )
    if phase == "architect":
        diagram = project_dir / "docs" / "architecture.md"
        diagram_ok = diagram.is_file() and ARCHITECTURE_MARKER in _read_text(diagram)
        source = _files(project_dir, ("src/**/*.ato",))
        build_ok, build_detail = _current_check(project_dir, spec, document, "build")
        missing = []
        if not diagram_ok:
            missing.append("tracked architecture diagram")
        if len(source) < 2:
            missing.append("architecture source modules")
        if not build_ok:
            missing.append(build_detail)
        return (
            not missing,
            "diagram, source graph, and build evidence present"
            if not missing
            else "missing: " + ", ".join(missing),
            diagram.is_file() or len(source) > 1,
        )
    if phase == "mcu":
        ioc = project_dir / "firmware" / f"{spec.name}.ioc"
        source = project_dir / "src" / "mcu.ato"
        build_ok, build_detail = _current_check(project_dir, spec, document, "build")
        ioc_ok, ioc_detail = _current_check(project_dir, spec, document, "ioc")
        missing = []
        if not ioc.is_file():
            missing.append(ioc.name)
        if not source.is_file():
            missing.append("src/mcu.ato")
        if not build_ok:
            missing.append(build_detail)
        if not ioc_ok:
            missing.append(ioc_detail)
        return (
            not missing,
            "IOC, MCU source, audit checks present"
            if not missing
            else "missing: " + ", ".join(missing),
            ioc.is_file() or source.is_file(),
        )
    if phase == "implement":
        build_ok, build_detail = _current_check(project_dir, spec, document, "build")
        parts_ok, parts_detail = _current_check(project_dir, spec, document, "parts")
        review_ok = True
        review_detail = "circuit review parity passed"
        if _circuit_review_enabled(project_dir):
            review_ok, review_detail = _current_check(
                project_dir,
                spec,
                document,
                "circuit-final",
            )
        elif _schematic_approval_enabled(project_dir):
            review_ok, review_detail = _current_check(
                project_dir,
                spec,
                document,
                "schematic-final",
            )
        modules = _files(project_dir, ("src/modules/*.ato",))
        satisfied = build_ok and parts_ok and review_ok and bool(modules)
        missing = []
        if not modules:
            missing.append("project module sources")
        if not build_ok:
            missing.append(build_detail)
        if not parts_ok:
            missing.append(parts_detail)
        if not review_ok:
            missing.append(review_detail)
        return (
            satisfied,
            "module sources, current build, parts audit, and circuit parity present"
            if satisfied
            else "missing: " + ", ".join(missing),
            bool(modules),
        )
    if phase == "build":
        ok, detail = _current_check(project_dir, spec, document, "build-test")
        report_ok = False
        report_detail = "build-test check has not passed"
        if ok:
            fingerprint = _check_fingerprint(
                project_dir,
                "build-test",
                _check_inputs(project_dir, spec, "build-test"),
            )
            report_ok, report_detail = saved_report_status(
                project_dir,
                fingerprint,
            )
        satisfied = ok and report_ok
        return (
            satisfied,
            "build-test check and [docs/build-test.md](docs/build-test.md) are current"
            if satisfied
            else "; ".join(
                item for item in (detail if not ok else "", report_detail) if item
            ),
            "build-test" in document.checks
            or (project_dir / BUILD_TEST_FILENAME).is_file(),
        )
    if phase == "brief":
        ok, detail = _current_check(project_dir, spec, document, "brief")
        return (
            ok,
            (
                "placement contract, generated brief, and KiCad net classes are current"
                if ok
                else detail
            ),
            "brief" in document.checks or (project_dir / PLACEMENT_FILENAME).is_file(),
        )
    if phase in {"layout", "route"}:
        if not board.is_file():
            return False, f"missing {board.name}", False
        text = _read_text(board)
        footprints = text.count("(footprint ")
        routes = text.count("(segment ") + text.count("(arc ") + text.count("(via ")
        detail = f"board contains {footprints} footprints and {routes} routed objects"
        return True, detail, footprints > 0 or routes > 0
    if phase == "verify":
        drc_ok, drc_detail = _current_check(project_dir, spec, document, "drc")
        return drc_ok, drc_detail, "drc" in document.checks
    if phase == "fab-out":
        fab = project_dir / "fab"
        outputs = (
            tuple(
                path
                for path in fab.rglob("*")
                if path.is_file() and path.name != ".gitkeep"
            )
            if fab.is_dir()
            else ()
        )
        return (
            bool(outputs),
            f"{len(outputs)} fabrication output(s) present"
            if outputs
            else "fab/ has no manufacturing outputs",
            bool(outputs),
        )
    if phase == "order" and _policy_constraints_enabled(project_dir):
        sourcing_ok = _current_sourcing_confirmation(project_dir, document)
        return (
            sourcing_ok,
            (
                "post-FAB sourcing confirmation is current"
                if sourcing_ok
                else "missing current post-FAB sourcing confirmation"
            ),
            any(
                event.subject == "sourcing"
                for event in document.policy_events
            ),
        )
    if phase in {"order", "publish"}:
        return True, "explicit workflow declaration", False
    raise AssertionError(f"unknown phase: {phase}")


def _latest_events(
    events: Sequence[StatusEvent],
) -> tuple[dict[str, tuple[int, StatusEvent]], dict[str, int]]:
    latest: dict[str, tuple[int, StatusEvent]] = {}
    latest_reopen: dict[str, int] = {}
    for index, event in enumerate(events):
        latest[event.phase] = (index, event)
        if event.action == "reopened":
            latest_reopen[event.phase] = index
    return latest, latest_reopen


def _failed_checks_for_phase(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    phase: str,
) -> tuple[str, ...]:
    failures = []
    for name in _phase_check_names(project_dir, PHASE_EVIDENCE_CHECKS, phase):
        current, detail = _current_check(project_dir, spec, document, name)
        if not current and detail.startswith(f"{name} failed:"):
            failures.append(detail)
    return tuple(failures)


def _derive_phases(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
) -> tuple[PhaseResult, ...]:
    latest, reopens = _latest_events(document.events)
    results: list[PhaseResult] = []
    predecessor_invalidation = -1

    for phase in PHASES:
        event_info = latest.get(phase.key)
        event_index = event_info[0] if event_info else -1
        event = event_info[1] if event_info else None
        evidence_ok, evidence_detail, partial = _static_evidence(
            project_dir, spec, document, phase.key
        )
        failed_checks = _failed_checks_for_phase(project_dir, spec, document, phase.key)
        approval_checks_ok = all(
            _current_check(project_dir, spec, document, name)[0]
            for name in _phase_check_names(project_dir, APPROVAL_CHECKS, phase.key)
        )
        predecessors_complete = all(
            result.complete for result in results if result.phase.required
        )

        if phase.key in reopens:
            predecessor_invalidation = max(predecessor_invalidation, reopens[phase.key])

        manual_complete = (
            event is not None
            and event.action == "complete"
            and event_index > predecessor_invalidation
            and _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        )
        manual_phase = (
            _universal_approval_enabled(project_dir)
            or phase.key in LEGACY_MANUAL_PHASES
        )
        if not manual_phase:
            complete = evidence_ok and predecessors_complete
        else:
            complete = evidence_ok and manual_complete and predecessors_complete

        if event is not None and event.action == "skipped":
            complete = phase.key == "publish" and predecessors_complete
            state = "Skipped" if complete else "Blocked"
            detail = event.note if complete else "publish cannot be skipped yet"
        elif complete:
            state = "Complete"
            detail = evidence_detail
        elif (
            phase.key != "spec"
            and predecessors_complete
            and not _current_policy_baseline(project_dir, document)
        ):
            state = "Blocked"
            detail = (
                "migrated policy baseline requires explicit user approval; "
                "record `pcbforge policy approve-baseline --note \"...\"` "
                "after review"
            )
        elif event is not None and event.action == "blocked":
            state = "Blocked"
            detail = event.note
        elif not predecessors_complete:
            state = "Not started"
            detail = "waiting for the previous required phase"
        elif failed_checks:
            state = "Blocked"
            detail = "; ".join(failed_checks)
        elif event is not None and event.action == "complete" and not evidence_ok:
            state = "Blocked"
            detail = f"completion lacks current evidence: {evidence_detail}"
        elif (
            event is not None
            and event.action == "complete"
            and not _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        ):
            state = "Blocked"
            detail = (
                "user approval is stale because its approved artifacts changed; "
                "renew approval for the current artifacts"
            )
        elif event is not None and event.action == "complete":
            state = "Blocked"
            detail = "completion is stale after an earlier phase was reopened"
        elif event is not None and event.action == "proposal-approved":
            if _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            ):
                if (
                    evidence_ok
                    and approval_checks_ok
                    and _phase_requires_approval(
                    project_dir,
                    phase.key,
                    )
                ):
                    state = "Awaiting approval"
                    detail = (
                        f"checks passed; present the final {phase.label} review "
                        "packet and wait for explicit user approval"
                    )
                else:
                    state = "In progress"
                    detail = (
                        f"{phase.label} proposal approved; build and present final audit"
                    )
            else:
                state = "Blocked"
                detail = (
                    f"{phase.label} proposal approval is stale; present the changed "
                    "proposal for renewed approval before coding"
                )
        elif event is not None and event.action == "reopened":
            state = "In progress"
            detail = event.note
        elif (
            phase.key == "architect"
            and event is None
            and _approval_constraints_enabled(project_dir)
            and _architecture_source_started(project_dir)
        ):
            state = "Blocked"
            detail = (
                "architecture source exists without current proposal approval; "
                "stop source changes and present docs/architecture.md for approval"
            )
        elif (
            phase.key == "implement"
            and _schematic_approval_enabled(project_dir)
            and _current_implement_proposal(project_dir, document) is None
        ):
            baseline_ok, baseline_detail = baseline_is_current(project_dir)
            proposal_check = (
                "circuit-proposal"
                if _circuit_review_enabled(project_dir)
                else "schematic-proposal"
            )
            proposal_ok, proposal_detail = _current_check(
                project_dir,
                spec,
                document,
                proposal_check,
            )
            if not baseline_ok:
                state = "Blocked"
                detail = (
                    f"{baseline_detail}; stop physical source changes and return "
                    "to the pre-IMPLEMENT baseline"
                )
            elif proposal_ok:
                state = "Awaiting approval"
                detail = (
                    "authored circuit overview and exact proposal model are current; "
                    "present the proposal-stage review packet"
                    if _circuit_review_enabled(project_dir)
                    else (
                        "native KiCad topology proposal and ERC are current; present "
                        "the proposal-stage review packet"
                    )
                )
            else:
                state = "Ready"
                detail = (
                    "create the explanatory SVG and exact circuit proposal before "
                    f"physical source edits ({proposal_detail})"
                    if _circuit_review_enabled(project_dir)
                    else (
                        "create the review-only KiCad topology proposal before "
                        f"physical source edits ({proposal_detail})"
                    )
                )
        elif (
            evidence_ok
            and approval_checks_ok
            and predecessors_complete
            and _phase_requires_approval(project_dir, phase.key)
        ):
            state = "Awaiting approval"
            detail = (
                "technical evidence is current; present the phase review packet "
                "and wait for explicit user approval"
            )
        elif partial:
            state = "In progress"
            detail = evidence_detail
        else:
            state = "Ready"
            detail = evidence_detail

        results.append(PhaseResult(phase, state, detail, complete))

    return tuple(results)


def _actions_for(result: PhaseResult) -> tuple[str, ...]:
    phase = result.phase.key
    if result.state == "Blocked":
        return (
            f"Resolve the {result.phase.label} blocker: {result.detail}",
            "Refresh with `pcbforge status --check --write` after the fix.",
        )
    if result.state == "Awaiting approval":
        if "proposal" in result.detail.lower():
            return (
                (
                    f"Run `pcbforge status review {phase} --stage proposal` "
                    "and present the exact packet."
                ),
                "Wait for an unambiguous user approval of that fingerprint.",
                (
                    f"Then record it with `pcbforge status approve {phase} "
                    "--stage proposal --fingerprint <sha256> "
                    '--note "<approval>"`.'
                ),
            )
        return (
            f"Run `pcbforge status review {phase}` and present the exact packet.",
            "Wait for an unambiguous user approval of that fingerprint.",
            (
                f"Then record it with `pcbforge status approve {phase} "
                '--fingerprint <sha256> --note "<approval>"`.'
            ),
        )
    actions = {
        "spec": (
            "Review and finalize `spec.md`.",
            "Run `pcbforge status review spec` and present the exact packet.",
        ),
        "init": (
            "Run `pcbforge init`.",
            "Review the generated scaffold, then request explicit INIT approval.",
        ),
        "architect": (
            "Draft `docs/architecture.md` without coding the module skeleton.",
            "Present material options and record explicit proposal approval.",
            "Then build the skeleton, audit it, and request final approval.",
        ),
        "mcu": (
            "Create the canonical IOC and matching `src/mcu.ato`.",
            "Run `pcbforge check-ioc` and complete the one-to-one audit.",
            "Present `pcbforge status review mcu` and request explicit approval.",
        ),
        "implement": (
            "Create the complete explanatory SVG and circuit model before source edits.",
            "Run `pcbforge check-circuit-review --stage proposal --write`.",
            "Present and explicitly approve the IMPLEMENT proposal fingerprint.",
            "Then finish physical module bodies, parts, values, and constraints.",
            "Run `pcbforge check-parts` and replace forbidden local commodity assets.",
            "Prove compiled parity, then present the final IMPLEMENT packet.",
        ),
        "build": (
            "Define the exact acceptance contract in `build-test.yaml`.",
            "Add stable `pcbforge-test` markers to every required assertion.",
            "Run `pcbforge status --check --write` to save the passing report.",
        ),
        "brief": (
            "Define every footprint, constraint, and exact net class in `placement.yaml`.",
            "Run `pcbforge brief`, then present `brief.md` beside the approved circuit overview.",
            "Request approval of the BRIEF packet.",
        ),
        "layout": (
            "Complete placement in KiCad 9.",
            "Present the LAYOUT review packet and request explicit approval.",
        ),
        "route": (
            "Complete routing in KiCad 9.",
            "Present the ROUTE review packet and request explicit approval.",
        ),
        "verify": (
            "Run `pcbforge status --check --write` for DRC.",
            "Complete audits and render review, then request VERIFY approval.",
        ),
        "fab-out": (
            "Generate Gerbers, drills, BOM, CPL, and the JLCPCB archive in `fab/`.",
            "Present the FAB-OUT review packet and request explicit approval.",
        ),
        "order": (
            "Review and upload the fabrication package to JLCPCB.",
            "After purchase authorization, request approval of the ORDER packet.",
        ),
        "publish": ("Publish proven reusable modules, or mark PUBLISH skipped.",),
    }
    return actions[phase]


def inspect_status(
    project_dir: Path,
    *,
    document: StatusDocument | None = None,
) -> StatusReport:
    """Derive workflow status without running tools or writing files."""
    project_dir = _project_dir(project_dir)
    spec = read_spec(project_dir / "spec.md")
    document = document if document is not None else read_status_document(project_dir)
    phases = _derive_phases(project_dir, spec, document)
    current = next(
        (result for result in phases if not result.complete and result.phase.required),
        None,
    )
    if current is None:
        current = next((result for result in phases if not result.complete), None)
    next_actions = _actions_for(current)[:3] if current is not None else ()
    required = tuple(result for result in phases if result.phase.required)
    checks_failed = False
    for name, record in document.checks.items():
        if record.outcome != "fail":
            continue
        inputs = _check_inputs(project_dir, spec, name)
        if not inputs:
            continue
        try:
            check_is_current = record.fingerprint == _check_fingerprint(
                project_dir,
                name,
                inputs,
            )
        except (BuildTestError, PlacementError, PolicyError, OSError):
            check_is_current = False
        checks_failed = checks_failed or check_is_current
    return StatusReport(
        project_dir=project_dir,
        spec=spec,
        document=document,
        phases=phases,
        current=current,
        next_actions=next_actions,
        completed_required=sum(result.complete for result in required),
        required_total=len(required),
        checks_failed=checks_failed,
    )


def _summary(output: str, fallback: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:240] if lines else fallback


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: CommandRunner,
) -> tuple[bool, str]:
    try:
        completed = runner(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return False, f"could not start: {exc}"
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode:
        return False, _summary(output, f"exit {completed.returncode}")
    return True, _summary(output, "passed")


def _route_is_complete(document: StatusDocument) -> bool:
    latest, _ = _latest_events(document.events)
    event = latest.get("route")
    return event is not None and event[1].action == "complete"


def run_status_checks(
    project_dir: Path,
    document: StatusDocument,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    checked_at: str | None = None,
    write_reports: bool = False,
) -> StatusDocument:
    """Run stage-appropriate checks and return a document containing results."""
    project_dir = _project_dir(project_dir)
    spec = read_spec(project_dir / "spec.md")
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    checked_at = checked_at or _now()
    checks = dict(document.checks)

    if (
        (project_dir / POLICY_FILENAME).is_file()
        or _policy_constraints_enabled(project_dir)
        or not (project_dir / ".pcbforge").exists()
    ):
        current = next(
            (
                result
                for result in _derive_phases(project_dir, spec, document)
                if not result.complete and result.phase.required
            ),
            None,
        )
        through_phase = current.phase.key if current is not None else "verify"
        baseline_approval, exception_approvals, _ = _policy_approval_context(
            document
        )
        name = "policy"
        try:
            result = check_policy(
                project_dir,
                tool_root=tool_root,
                through_phase=through_phase,
                baseline_approval=baseline_approval,
                exception_approvals=exception_approvals,
            )
        except (PolicyInputError, PolicyError) as exc:
            ok = False
            summary = str(exc).splitlines()[0]
            try:
                fingerprint = policy_status_fingerprint(
                    project_dir,
                    tool_root=tool_root,
                )
            except PolicyError:
                fingerprint = _fingerprint(
                    project_dir,
                    policy_inputs(project_dir),
                )
        else:
            ok = result.ok
            summary = result.summary
            fingerprint = result.fingerprint
        checks[name] = CheckRecord(
            checked_at,
            fingerprint,
            "pass" if ok else "fail",
            summary,
        )

    if (project_dir / ".pcbforge").is_file():
        name = "build"
        inputs = _check_inputs(project_dir, spec, name)
        ok, summary = _run_command(
            [
                str(tool_root / "scripts" / "ato"),
                "build",
                "--frozen",
                "--verbose",
            ],
            cwd=project_dir,
            runner=runner,
        )
        checks[name] = CheckRecord(
            checked_at,
            _check_fingerprint(project_dir, name, inputs),
            "pass" if ok else "fail",
            summary,
        )

        latest, _ = _latest_events(document.events)
        implement_event = latest.get("implement")
        should_run_build_test = (project_dir / BUILD_TEST_FILENAME).is_file() or (
            implement_event is not None and implement_event[1].action == "complete"
        )
        if should_run_build_test:
            name = "build-test"
            try:
                result = check_build_test(
                    project_dir,
                    tool_root=tool_root,
                    runner=runner,
                    write_report=write_reports,
                )
            except (BuildTestInputError, BuildTestError) as exc:
                ok = False
                summary = str(exc).splitlines()[0]
                try:
                    fingerprint = _check_fingerprint(
                        project_dir,
                        name,
                        _check_inputs(project_dir, spec, name),
                    )
                except (BuildTestError, OSError):
                    fingerprint = _fingerprint(
                        project_dir,
                        _check_inputs(project_dir, spec, name),
                    )
            else:
                ok = True
                summary = result.summary
                fingerprint = result.fingerprint
            checks[name] = CheckRecord(
                checked_at,
                fingerprint,
                "pass" if ok else "fail",
                summary,
            )

        build_test_ok, _ = _current_check(
            project_dir,
            spec,
            replace(document, checks=checks),
            "build-test",
        )
        should_run_brief = (
            project_dir / PLACEMENT_FILENAME
        ).is_file() and build_test_ok
        if should_run_brief:
            name = "brief"
            try:
                result = check_brief(project_dir, tool_root=tool_root)
            except (PlacementInputError, PlacementError) as exc:
                ok = False
                summary = str(exc).splitlines()[0]
                fingerprint = brief_status_fingerprint(
                    project_dir,
                    tool_root=tool_root,
                )
            else:
                ok = True
                summary = result.summary
                fingerprint = result.fingerprint
            checks[name] = CheckRecord(
                checked_at,
                fingerprint,
                "pass" if ok else "fail",
                summary,
            )

        name = "parts"
        try:
            result = check_parts(project_dir)
        except PartsAuditError as exc:
            ok = False
            summary = str(exc).splitlines()[0]
        else:
            ok = result.ok
            summary = result.summary
        checks[name] = CheckRecord(
            checked_at,
            _fingerprint(project_dir, _check_inputs(project_dir, spec, name)),
            "pass" if ok else "fail",
            summary,
        )

        if _circuit_review_enabled(project_dir):
            if (project_dir / CIRCUIT_REVIEW_FILENAME).is_file():
                for stage in ("proposal", "final"):
                    if (
                        stage == "final"
                        and not (
                            project_dir
                            / "docs"
                            / "implementation-review.md"
                        ).is_file()
                    ):
                        continue
                    name = f"circuit-{stage}"
                    try:
                        result = check_circuit_review(
                            project_dir,
                            stage,
                            write=write_reports,
                        )
                    except (CircuitReviewInputError, CircuitReviewError) as exc:
                        ok = False
                        summary = str(exc).splitlines()[0]
                        try:
                            fingerprint = circuit_review_status_fingerprint(
                                project_dir,
                                stage,
                            )
                        except CircuitReviewError:
                            fingerprint = _fingerprint(
                                project_dir,
                                tuple(
                                    path
                                    for path in (
                                        project_dir / CIRCUIT_REVIEW_FILENAME,
                                        project_dir / ".pcbforge",
                                    )
                                    if path.is_file()
                                ),
                            )
                    else:
                        ok = True
                        summary = result.summary
                        fingerprint = result.fingerprint
                    checks[name] = CheckRecord(
                        checked_at,
                        fingerprint,
                        "pass" if ok else "fail",
                        summary,
                    )
        else:
            for stage in ("proposal", "final"):
                root = (
                    project_dir
                    / "review"
                    / "implement"
                    / stage
                    / "main.kicad_sch"
                )
                if not root.is_file():
                    continue
                name = f"schematic-{stage}"
                try:
                    result = check_schematic(
                        project_dir,
                        stage,
                        tool_root=tool_root,
                        runner=runner,
                        write=write_reports,
                    )
                except (SchematicInputError, SchematicError) as exc:
                    ok = False
                    summary = str(exc).splitlines()[0]
                    try:
                        fingerprint = schematic_status_fingerprint(
                            project_dir,
                            stage,
                        )
                    except SchematicError:
                        fingerprint = _fingerprint(
                            project_dir,
                            _check_inputs(project_dir, spec, name),
                        )
                else:
                    ok = True
                    summary = result.summary
                    fingerprint = result.fingerprint
                checks[name] = CheckRecord(
                    checked_at,
                    fingerprint,
                    "pass" if ok else "fail",
                    summary,
                )

    ioc_path = project_dir / "firmware" / f"{spec.name}.ioc"
    if ioc_path.is_file():
        name = "ioc"
        try:
            result = check_ioc(project_dir, tool_root=tool_root, runner=runner)
        except (IocProjectError, IocValidationError, InitInputError) as exc:
            ok = False
            summary = str(exc).splitlines()[0]
        else:
            ok = True
            summary = f"{result.part_number} CubeMX round-trip passed"
        checks[name] = CheckRecord(
            checked_at,
            _fingerprint(project_dir, _check_inputs(project_dir, spec, name)),
            "pass" if ok else "fail",
            summary,
        )

    if _route_is_complete(document):
        name = "drc"
        board = project_dir / f"{spec.name}.kicad_pcb"
        if board.is_file():
            with tempfile.TemporaryDirectory(
                prefix="pcbforge-status-drc-"
            ) as temporary:
                report = Path(temporary) / "drc.json"
                ok, summary = _run_command(
                    [
                        str(tool_root / "scripts" / "kicad-cli"),
                        "pcb",
                        "drc",
                        "--format",
                        "json",
                        "--output",
                        str(report),
                        "--severity-all",
                        "--exit-code-violations",
                        str(board),
                    ],
                    cwd=project_dir,
                    runner=runner,
                )
        else:
            ok, summary = False, f"missing {board.name}"
        checks[name] = CheckRecord(
            checked_at,
            _fingerprint(project_dir, _check_inputs(project_dir, spec, name)),
            "pass" if ok else "fail",
            summary,
        )

    return replace(document, checks=checks)


def _prepare_phase_review(
    project_dir: Path,
    phase: str,
    *,
    document: StatusDocument | None = None,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    checked_at: str | None = None,
) -> tuple[PhaseReview, StatusDocument]:
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    if phase not in PHASE_BY_KEY:
        raise StatusInputError(
            f"unknown phase {phase!r}; choose from {', '.join(PHASE_BY_KEY)}"
        )
    document = (
        document
        if document is not None
        else read_status_document(project_dir)
    )
    if (project_dir / ".pcbforge").is_file():
        pins = _project_pins(project_dir)
        schema = pins.get("schema")
        if type(schema) is int and schema == 10:
            raise StatusInputError(
                "project approval workflow is schema 10; run "
                "`pcbforge migrate-approvals` before reviewing phases"
            )
    checked = run_status_checks(
        project_dir,
        document,
        tool_root=tool_root,
        runner=runner,
        checked_at=checked_at,
    )
    report = inspect_status(project_dir, document=checked)
    spec = report.spec
    target_index = PHASE_NUMBER[phase] - 1
    predecessors = [
        result for result in report.phases[:target_index] if result.phase.required
    ]
    target = report.phases[target_index]
    evidence_ok, evidence_detail, _ = _static_evidence(
        project_dir,
        spec,
        checked,
        phase,
    )
    check_reviews: list[PhaseReviewCheck] = []
    check_failures = []
    for name in _phase_check_names(project_dir, APPROVAL_CHECKS, phase):
        current, detail = _current_check(project_dir, spec, checked, name)
        record = checked.checks.get(name)
        check_reviews.append(
            PhaseReviewCheck(
                name,
                record.outcome if record is not None else "missing",
                record.summary if record is not None else detail,
                record.fingerprint if record is not None else "",
            )
        )
        if not current:
            check_failures.append(detail)

    failures = []
    if target.complete:
        failures.append(f"{target.phase.label} is already complete")
    if not all(result.complete for result in predecessors):
        waiting = next(
            result.phase.label for result in predecessors if not result.complete
        )
        failures.append(f"{waiting} is not complete")
    if not evidence_ok:
        failures.append(evidence_detail)
    failures.extend(check_failures)
    if (
        phase == "architect"
        and _current_architect_proposal(project_dir, checked) is None
    ):
        failures.append("current architecture proposal approval is missing")
    if (
        phase == "implement"
        and _schematic_approval_enabled(project_dir)
        and _current_implement_proposal(project_dir, checked) is None
    ):
        failures.append("current IMPLEMENT circuit proposal approval is missing")
    if (
        phase != "spec"
        and not _current_policy_baseline(project_dir, checked)
    ):
        failures.append("migrated policy baseline approval is missing")

    artifacts = tuple(
        path.relative_to(project_dir).as_posix()
        for path in _phase_artifact_paths(project_dir, spec, phase)
    )
    fingerprint = _approval_fingerprint(
        project_dir,
        phase,
        "complete",
        checked,
    )
    ready = not failures
    detail = (
        "technical evidence passed; explicit user approval is required"
        if ready
        else "; ".join(dict.fromkeys(failures))
    )
    return (
        PhaseReview(
            project_dir,
            PHASE_BY_KEY[phase],
            ready,
            detail,
            fingerprint,
            artifacts,
            tuple(check_reviews),
        ),
        checked,
    )


def _prepare_proposal_review(
    project_dir: Path,
    phase: str,
    *,
    tool_root: Path | None,
    runner: CommandRunner,
    checked_at: str | None,
) -> tuple[PhaseReview, StatusDocument]:
    if phase not in {"architect", "implement"}:
        raise StatusInputError(
            "proposal review is only valid for architect or implement"
        )
    project_dir = _project_dir(project_dir)
    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    target_index = PHASE_NUMBER[phase] - 1
    predecessors = [
        result for result in report.phases[:target_index] if result.phase.required
    ]
    failures: list[str] = []
    checks: list[PhaseReviewCheck] = []
    if not all(result.complete for result in predecessors):
        waiting = next(
            result.phase.label for result in predecessors if not result.complete
        )
        failures.append(f"{waiting} is not complete")
    if report.phases[target_index].complete:
        failures.append(f"{PHASE_BY_KEY[phase].label} is already complete")
    if phase != "spec" and not _current_policy_baseline(project_dir, document):
        failures.append("migrated policy baseline approval is missing")

    if phase == "architect":
        diagram = project_dir / "docs" / "architecture.md"
        if (
            not diagram.is_file()
            or ARCHITECTURE_MARKER not in _read_text(diagram)
        ):
            failures.append("missing current docs/architecture.md")
        if _architecture_source_started(project_dir):
            failures.append(
                "architecture source exists before current proposal approval"
            )
        artifacts = tuple(
            path.relative_to(project_dir).as_posix()
            for path in (project_dir / "spec.md", diagram)
            if path.is_file()
        )
    else:
        if not _schematic_approval_enabled(project_dir):
            failures.append(
                "project is not migrated for staged IMPLEMENT review"
            )
        baseline_ok, baseline_detail = baseline_is_current(project_dir)
        if not baseline_ok:
            failures.append(baseline_detail)
        circuit_enabled = _circuit_review_enabled(project_dir)
        check_name = "circuit-proposal" if circuit_enabled else "schematic-proposal"
        try:
            if circuit_enabled:
                result = check_circuit_review(
                    project_dir,
                    "proposal",
                    write=False,
                )
            else:
                result = check_schematic(
                    project_dir,
                    "proposal",
                    tool_root=tool_root,
                    runner=runner,
                    write=False,
                )
        except (
            CircuitReviewInputError,
            CircuitReviewError,
            SchematicInputError,
            SchematicError,
        ) as exc:
            result = None
            failures.append(str(exc).splitlines()[0])
            try:
                fingerprint = (
                    circuit_review_status_fingerprint(project_dir, "proposal")
                    if circuit_enabled
                    else schematic_status_fingerprint(project_dir, "proposal")
                )
            except (CircuitReviewError, SchematicError):
                fingerprint = ""
            checks.append(
                PhaseReviewCheck(
                    check_name,
                    "fail",
                    str(exc).splitlines()[0],
                    fingerprint,
                )
            )
        else:
            checks.append(
                PhaseReviewCheck(
                    check_name,
                    "pass",
                    result.summary,
                    result.fingerprint,
                )
            )
            record = CheckRecord(
                checked_at or _now(),
                result.fingerprint,
                "pass",
                result.summary,
            )
            document = replace(
                document,
                checks={**document.checks, check_name: record},
            )
        try:
            proposal_inputs = (
                circuit_review_inputs(project_dir, "proposal")
                if circuit_enabled
                else schematic_inputs(project_dir, "proposal")
            )
        except (CircuitReviewError, SchematicError):
            proposal_inputs = tuple(
                path
                for path in (
                    project_dir / ".pcbforge",
                    project_dir / CIRCUIT_REVIEW_FILENAME,
                    project_dir / "review" / "implement" / "circuit.yaml",
                    project_dir / "review" / "implement" / "circuit.svg",
                    project_dir / "docs" / "implementation-proposal.md",
                    project_dir / BASELINE_PATH,
                )
                if path.is_file()
            )
        extra_artifacts = (
            (project_dir / "review" / "implement" / "proposal" / "evidence.json",)
            if circuit_enabled
            else tuple(
                path
                for path in (
                    project_dir / "review" / "implement" / "proposal"
                ).rglob("*")
                if path.is_file()
            )
        )
        artifacts = tuple(
            path.relative_to(project_dir).as_posix()
            for path in sorted(
                {
                    *proposal_inputs,
                    *(path for path in extra_artifacts if path.is_file()),
                }
            )
        )
    fingerprint = _approval_fingerprint(
        project_dir,
        phase,
        "proposal-approved",
        document,
    )
    return (
        PhaseReview(
            project_dir,
            PHASE_BY_KEY[phase],
            not failures,
            (
                "proposal evidence passed; explicit user approval is required"
                if not failures
                else "; ".join(dict.fromkeys(failures))
            ),
            fingerprint,
            artifacts,
            tuple(checks),
            "proposal",
        ),
        document,
    )


def review_phase(
    project_dir: Path,
    phase: str,
    *,
    stage: str = "final",
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    checked_at: str | None = None,
) -> PhaseReview:
    """Build a read-only, deterministic phase packet for user review."""
    phase = phase.lower()
    if stage == "proposal":
        review, _ = _prepare_proposal_review(
            project_dir,
            phase,
            tool_root=tool_root,
            runner=runner,
            checked_at=checked_at,
        )
        return review
    if stage != "final":
        raise StatusInputError("stage must be proposal or final")
    review, _ = _prepare_phase_review(
        project_dir,
        phase,
        tool_root=tool_root,
        runner=runner,
        checked_at=checked_at,
    )
    return review


def render_phase_review(review: PhaseReview) -> str:
    """Render the exact evidence packet that the user may approve."""
    lines = [
        (
            f"pcbforge phase review: {review.phase.label}"
            + (" proposal" if review.stage == "proposal" else "")
        ),
        f"readiness: {'AWAITING APPROVAL' if review.ready else 'BLOCKED'}",
        f"detail: {review.detail}",
        "artifacts:",
    ]
    lines.extend(
        f"  - {artifact}" for artifact in review.artifacts
    )
    if not review.artifacts:
        lines.append("  - (explicit workflow declaration; no tracked artifact)")
    lines.append("checks:")
    if review.checks:
        lines.extend(
            (
                f"  - {check.name}: {check.outcome} — {check.summary} "
                f"[{check.fingerprint}]"
            )
            for check in review.checks
        )
    else:
        lines.append("  - (no automated checks required)")
    lines.append(f"approval fingerprint: {review.fingerprint}")
    if review.ready:
        lines.append(
            "next: present this packet and wait for explicit user approval"
        )
    return "\n".join(lines)


def approve_phase(
    project_dir: Path,
    phase: str,
    expected_fingerprint: str,
    note: str,
    *,
    stage: str = "final",
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
) -> StatusResult:
    """Record an explicit user approval of the exact reviewed phase packet."""
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    note = note.strip()
    expected_fingerprint = expected_fingerprint.strip().lower()
    if not note:
        raise StatusInputError("--note must be a non-empty approval explanation")
    if re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None:
        raise StatusInputError("--fingerprint must be a lowercase SHA-256 value")
    if not _universal_approval_enabled(project_dir):
        raise StatusInputError(
            "project approval workflow is not migrated; run "
            "`pcbforge migrate-approvals`"
        )
    event_time = now or _now()
    if stage == "proposal":
        review, checked = _prepare_proposal_review(
            project_dir,
            phase,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage == "final":
        review, checked = _prepare_phase_review(
            project_dir,
            phase,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    else:
        raise StatusInputError("stage must be proposal or final")
    if not review.ready:
        raise StatusInputError(
            f"cannot approve {phase}: {review.detail}"
        )
    if review.fingerprint != expected_fingerprint:
        raise StatusInputError(
            "cannot approve phase: reviewed fingerprint is stale or does not "
            f"match current evidence (expected {review.fingerprint})"
        )
    if phase == "brief" and not _schematic_approval_enabled(project_dir):
        note_lower = note.lower()
        if "brief.md" not in note_lower or not re.search(
            r"schematic\s+review\s*:\s*adequate",
            note_lower,
        ):
            raise StatusInputError(
                "cannot approve brief: --note must reference brief.md and "
                "contain `schematic review: adequate`"
            )
    event = StatusEvent(
        event_time,
        phase,
        "proposal-approved" if stage == "proposal" else "complete",
        note,
        review.fingerprint,
    )
    checked = replace(checked, events=(*checked.events, event))
    if phase == "mcu" and stage == "final" and _schematic_approval_enabled(project_dir):
        capture_implementation_baseline(project_dir)
    return write_status(
        project_dir,
        tool_root=tool_root,
        runner=runner,
        now=event_time,
        document=checked,
    )


def _metadata(document: StatusDocument) -> dict[str, Any]:
    events = []
    for event in document.events:
        item = {
            "at": event.at,
            "phase": event.phase,
            "action": event.action,
            "note": event.note,
        }
        if event.approval_fingerprint:
            item["approval_fingerprint"] = event.approval_fingerprint
        events.append(item)
    policy_events = []
    for event in document.policy_events:
        item = {
            "at": event.at,
            "action": event.action,
            "subject": event.subject,
            "note": event.note,
        }
        if event.approval_fingerprint:
            item["approval_fingerprint"] = event.approval_fingerprint
        policy_events.append(item)
    return {
        "pcbforge_status_schema": STATUS_SCHEMA,
        "updated_at": document.updated_at,
        "events": events,
        "policy_events": policy_events,
        "checks": {
            name: {
                "at": record.at,
                "fingerprint": record.fingerprint,
                "outcome": record.outcome,
                "summary": record.summary,
            }
            for name, record in sorted(document.checks.items())
        },
    }


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_dashboard(report: StatusReport) -> str:
    """Render the canonical tracked Markdown dashboard."""
    metadata = yaml.safe_dump(
        _metadata(report.document),
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    current = report.current
    current_text = (
        f"{PHASE_NUMBER[current.phase.key]}. {current.phase.label} — {current.state}"
        if current is not None
        else "All phases complete"
    )
    health = (
        "🔴 Blocked"
        if report.checks_failed
        or (current is not None and current.state == "Blocked")
        else "🟢 On track"
    )
    focus = current.phase.focus if current is not None else "Workflow complete."

    completed = [
        f"- ✅ {result.phase.label}"
        for result in report.phases
        if result.complete and result.state != "Skipped"
    ]
    if not completed:
        completed = ["- Nothing completed yet."]

    blockers = [
        f"- **{result.phase.label}:** {result.detail}"
        for result in report.phases
        if result.state == "Blocked"
    ]
    for name, record in sorted(report.document.checks.items()):
        if record.outcome != "fail":
            continue
        _, detail = _current_check(
            report.project_dir,
            report.spec,
            report.document,
            name,
        )
        if detail.startswith(f"{name} failed:"):
            blockers.append(f"- **{name} check:** {record.summary}")
    if not blockers:
        blockers = ["- None."]

    recent_items = [
        (
            event.at,
            f"- **{event.at}:** {event.phase} {event.action} — {event.note}",
        )
        for event in report.document.events
    ]
    recent_items.extend(
        (
            event.at,
            f"- **{event.at}:** policy {event.action} "
            f"({event.subject}) — {event.note}",
        )
        for event in report.document.policy_events
    )
    recent = [
        rendered
        for _, rendered in sorted(recent_items, reverse=True)[:5]
    ]
    if not recent:
        recent = ["- No milestone events recorded yet."]

    actions = [
        f"{index}. {action}"
        for index, action in enumerate(report.next_actions, start=1)
    ]
    if not actions:
        actions = ["1. No required next action."]

    rows = []
    icons = {
        "Complete": "✅",
        "In progress": "🟡",
        "Ready": "🔵",
        "Awaiting approval": "🟣",
        "Not started": "⚪",
        "Blocked": "🔴",
        "Skipped": "➖",
    }
    for result in report.phases:
        number = PHASE_NUMBER[result.phase.key]
        rows.append(
            "| "
            + " | ".join(
                (
                    str(number),
                    result.phase.label,
                    result.phase.lead,
                    f"{icons[result.state]} {result.state}",
                    _escape(result.detail),
                )
            )
            + " |"
        )

    updated = report.document.updated_at or "not written yet"
    return f"""---
{metadata}
---
# {report.spec.name} project dashboard

> Generated by PCBForge from project evidence and explicit workflow events.
> Use `pcbforge status` commands instead of editing this body.

_Last updated: {updated}_

## Current status

**Phase:** {current_text}<br>
**Progress:** {report.completed_required} of {report.required_total} required phases complete<br>
**Health:** {health}<br>
**Current focus:** {focus}

## What's next

{chr(10).join(actions)}

## Completed

{chr(10).join(completed)}

## Blockers

{chr(10).join(blockers)}

## Workflow

| # | Phase | Lead | Status | Evidence or blocker |
|---:|---|---|---|---|
{chr(10).join(rows)}

## Recent progress

{chr(10).join(recent)}
"""


def render_terminal(report: StatusReport) -> str:
    """Render a concise terminal view from the same status model."""
    current = report.current
    lines = [
        (
            f"{report.spec.name}: {report.completed_required}/"
            f"{report.required_total} required phases complete"
        )
    ]
    if current is None:
        lines.append("current: workflow complete")
    else:
        lines.append(
            f"current: {PHASE_NUMBER[current.phase.key]}. "
            f"{current.phase.label} — {current.state}"
        )
        lines.append(f"status: {current.detail}")
    if report.next_actions:
        lines.append("next:")
        lines.extend(
            f"  {index}. {action}"
            for index, action in enumerate(report.next_actions, 1)
        )
    return "\n".join(lines)


def _atomic_write(path: Path, contents: str) -> None:
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
            raise StatusError(f"cannot write {path}: {exc}") from exc


def _invalidate_stale_approvals(
    project_dir: Path,
    document: StatusDocument,
    *,
    at: str | None,
) -> StatusDocument:
    latest, _ = _latest_events(document.events)
    invalidations: list[StatusEvent] = []
    for phase in PHASES:
        if phase.key not in APPROVAL_BOUND_PHASES:
            continue
        event_info = latest.get(phase.key)
        if event_info is None:
            continue
        event = event_info[1]
        if event.action not in {"complete", "proposal-approved"}:
            continue
        if (
            not event.approval_fingerprint
            and not _approval_constraints_enabled(project_dir)
        ):
            continue
        try:
            approval_current = _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        except (CircuitReviewError, SchematicError, OSError):
            approval_current = False
        if approval_current:
            continue
        invalidations.append(
            StatusEvent(
                at or _now(),
                phase.key,
                "reopened",
                (
                    "Approval invalidated automatically because the approved "
                    "artifact fingerprint changed"
                ),
            )
        )
    policy_invalidations: list[PolicyEvent] = []
    if _policy_constraints_enabled(project_dir):
        latest_policy = _latest_policy_events(document)
        try:
            baseline_expected = policy_baseline_fingerprint(project_dir)
            exception_expected = policy_exception_fingerprints(project_dir)
            sourcing_expected = policy_sourcing_fingerprint(project_dir)
        except PolicyError:
            baseline_expected = ""
            exception_expected = {}
            sourcing_expected = ""
        for subject, event in latest_policy.items():
            if event.action == "baseline-approved":
                expected = baseline_expected
            elif event.action == "exception-approved":
                expected = exception_expected.get(subject, "")
            elif event.action == "sourcing-confirmed":
                expected = sourcing_expected
            else:
                continue
            if expected and event.approval_fingerprint == expected:
                continue
            policy_invalidations.append(
                PolicyEvent(
                    at or _now(),
                    "reopened",
                    subject,
                    (
                        "Policy approval invalidated automatically because its "
                        "approved fingerprint changed"
                    ),
                )
            )
            if subject == "sourcing":
                order_info = latest.get("order")
                if order_info is not None and order_info[1].action == "complete":
                    invalidations.append(
                        StatusEvent(
                            at or _now(),
                            "order",
                            "reopened",
                            "Order reopened because sourcing confirmation is stale",
                        )
                    )
    if not invalidations and not policy_invalidations:
        return document
    return replace(
        document,
        events=(*document.events, *invalidations),
        policy_events=(*document.policy_events, *policy_invalidations),
    )


def write_status(
    project_dir: Path,
    *,
    check: bool = False,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
    document: StatusDocument | None = None,
) -> StatusResult:
    """Create or refresh STATUS.md, avoiding timestamp-only rewrites."""
    project_dir = _project_dir(project_dir)
    document = document if document is not None else read_status_document(project_dir)
    document = import_legacy_architect_approval(project_dir, document)
    if check:
        document = run_status_checks(
            project_dir,
            document,
            tool_root=tool_root,
            runner=runner,
            checked_at=now,
            write_reports=True,
        )
    document = _invalidate_stale_approvals(
        project_dir,
        document,
        at=now,
    )

    old_report = inspect_status(project_dir, document=document)
    old_rendered = render_dashboard(old_report)
    path = _status_path(project_dir)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except (OSError, UnicodeError) as exc:
        raise StatusError(f"cannot read {path}: {exc}") from exc

    if existing == old_rendered:
        return StatusResult(report=old_report, wrote=False)

    document = replace(document, updated_at=now or _now())
    report = inspect_status(project_dir, document=document)
    rendered = render_dashboard(report)
    if existing == rendered:
        return StatusResult(report=report, wrote=False)
    _atomic_write(path, rendered)
    return StatusResult(report=report, wrote=True)


def migrate_approvals(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    now: str | None = None,
) -> ApprovalMigrationResult:
    """Atomically migrate a generated schema-10 project to universal approvals."""
    project_dir = _project_dir(project_dir)
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    pins_path = project_dir / ".pcbforge"
    pins = dict(_project_pins(project_dir))
    schema = pins.get("schema")
    if schema == CIRCUIT_REVIEW_PIN_SCHEMA:
        return ApprovalMigrationResult(project_dir, False, ())
    if schema != 10:
        raise StatusInputError(
            "migrate-approvals requires generated .pcbforge schema 10; "
            f"got {schema!r}"
        )

    agents_path = project_dir / "AGENTS.md"
    try:
        agents = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StatusInputError(f"cannot read {agents_path}: {exc}") from exc
    if not agents.startswith("<!-- pcbforge-agents-schema: 10 -->"):
        raise StatusInputError(
            "AGENTS.md is not the expected generated schema-10 guidance"
        )

    from pcbforge.initialize import (
        AGENTS_SCHEMA,
        APPROVAL_GUIDE_SCHEMA,
        BRIEF_GUIDE_SCHEMA,
        IMPLEMENT_GUIDE_SCHEMA,
        MCU_GUIDE_SCHEMA,
        CIRCUIT_REVIEW_SCHEMA,
        _render_agents,
    )

    document = read_status_document(project_dir)
    latest, _ = _latest_events(document.events)
    event_time = now or _now()
    reopenings: list[StatusEvent] = []
    reopened_phases: list[str] = []
    predecessor_approvals_current = True
    for phase in PHASES:
        event_info = latest.get(phase.key)
        if event_info is None:
            if phase.required:
                predecessor_approvals_current = False
            continue
        event = event_info[1]
        if event.action != "complete":
            if phase.required:
                predecessor_approvals_current = False
            continue
        preserve = (
            predecessor_approvals_current
            and phase.key in LEGACY_APPROVAL_BOUND_PHASES
            and bool(event.approval_fingerprint)
            and _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        )
        if preserve:
            continue
        if phase.required:
            predecessor_approvals_current = False
        reopenings.append(
            StatusEvent(
                event_time,
                phase.key,
                "reopened",
                (
                    "Schema-13 migration requires explicit approval of the "
                    "current phase review fingerprint"
                ),
            )
        )
        reopened_phases.append(phase.key)
    migrated_document = replace(
        document,
        events=(*document.events, *reopenings),
    )

    guidance = pins.get("guidance")
    if not isinstance(guidance, dict):
        raise StatusInputError(".pcbforge guidance: expected a mapping")
    guidance = dict(guidance)
    guidance.pop("schematic_review_schema", None)
    pins["schema"] = CIRCUIT_REVIEW_PIN_SCHEMA
    pins["guidance"] = {
        **guidance,
        "agents_schema": AGENTS_SCHEMA,
        "approval_schema": APPROVAL_GUIDE_SCHEMA,
        "mcu_schema": MCU_GUIDE_SCHEMA,
        "implement_schema": IMPLEMENT_GUIDE_SCHEMA,
        "brief_schema": BRIEF_GUIDE_SCHEMA,
        "circuit_review_schema": CIRCUIT_REVIEW_SCHEMA,
        "status_schema": STATUS_SCHEMA,
    }
    spec = read_spec(project_dir / "spec.md")
    outputs = {
        pins_path: yaml.safe_dump(pins, sort_keys=False),
        agents_path: _render_agents(spec, tool_root),
    }
    status_path = _status_path(project_dir)
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (*outputs, status_path)
    }
    installed: list[Path] = []
    try:
        for path, contents in outputs.items():
            _atomic_write(path, contents)
            installed.append(path)
        write_status(
            project_dir,
            tool_root=tool_root,
            now=event_time,
            document=migrated_document,
        )
        installed.append(status_path)
    except (OSError, StatusError) as exc:
        for path in reversed(installed):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{path.name}.rollback.",
                        suffix=".tmp",
                        dir=path.parent,
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(original)
                    os.replace(temporary_name, path)
            except OSError:
                pass
        raise StatusError(f"could not migrate approvals atomically: {exc}") from exc
    return ApprovalMigrationResult(
        project_dir,
        True,
        tuple(reopened_phases),
    )


def migrate_schematic_review(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    now: str | None = None,
    adopt_existing: bool = False,
) -> ApprovalMigrationResult:
    """Legacy alias: atomically migrate schema 11 to current circuit review."""
    project_dir = _project_dir(project_dir)
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    pins_path = project_dir / ".pcbforge"
    pins = dict(_project_pins(project_dir))
    schema = pins.get("schema")
    if schema == CIRCUIT_REVIEW_PIN_SCHEMA:
        return ApprovalMigrationResult(project_dir, False, ())
    if schema != UNIVERSAL_APPROVAL_PIN_SCHEMA:
        raise StatusInputError(
            "migrate-schematic-review requires generated .pcbforge schema 11; "
            f"got {schema!r}"
        )
    agents_path = project_dir / "AGENTS.md"
    try:
        agents = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StatusInputError(f"cannot read {agents_path}: {exc}") from exc
    if not agents.startswith("<!-- pcbforge-agents-schema: 11 -->"):
        raise StatusInputError(
            "AGENTS.md is not the expected generated schema-11 guidance"
        )
    from pcbforge.initialize import (
        AGENTS_SCHEMA,
        APPROVAL_GUIDE_SCHEMA,
        BRIEF_GUIDE_SCHEMA,
        IMPLEMENT_GUIDE_SCHEMA,
        MCU_GUIDE_SCHEMA,
        CIRCUIT_REVIEW_SCHEMA,
        _render_agents,
    )

    guidance = pins.get("guidance")
    if not isinstance(guidance, dict):
        raise StatusInputError(".pcbforge guidance: expected a mapping")
    guidance = dict(guidance)
    guidance.pop("schematic_review_schema", None)
    pins["schema"] = CIRCUIT_REVIEW_PIN_SCHEMA
    pins["guidance"] = {
        **guidance,
        "agents_schema": AGENTS_SCHEMA,
        "mcu_schema": MCU_GUIDE_SCHEMA,
        "implement_schema": IMPLEMENT_GUIDE_SCHEMA,
        "brief_schema": BRIEF_GUIDE_SCHEMA,
        "approval_schema": APPROVAL_GUIDE_SCHEMA,
        "circuit_review_schema": CIRCUIT_REVIEW_SCHEMA,
        "status_schema": STATUS_SCHEMA,
    }
    try:
        revision_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tool_root,
            text=True,
            capture_output=True,
            check=False,
        )
        dirty_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=tool_root,
            text=True,
            capture_output=True,
            check=False,
        )
        revision = revision_result.stdout.strip()
        dirty = bool(dirty_result.stdout.strip())
    except OSError:
        revision = ""
        dirty = True
    pcbforge_pin = pins.get("pcbforge")
    if isinstance(pcbforge_pin, dict) and revision:
        pins["pcbforge"] = {
            **pcbforge_pin,
            "revision": revision,
            "dirty": dirty,
        }

    document = read_status_document(project_dir)
    latest, _ = _latest_events(document.events)
    existing_implement = latest.get("implement")
    implementation_indicators = any(
        path.is_file()
        for path in (
            project_dir / "docs" / "implementation-proposal.md",
            project_dir / "docs" / "implementation-review.md",
            project_dir / "schematic-review.yaml",
        )
    )
    implemented = implementation_indicators or (
        existing_implement is not None
        and existing_implement[1].action == "complete"
    )
    if implemented and not adopt_existing:
        raise StatusInputError(
            "IMPLEMENT is already complete; rewind to the pre-IMPLEMENT boundary "
            "or rerun with --adopt-existing to label this as legacy adoption"
        )
    event_time = now or _now()
    reopenings: list[StatusEvent] = []
    reopened: list[str] = []
    mcu_event = latest.get("mcu")
    if (
        mcu_event is not None
        and mcu_event[1].action == "complete"
        and not adopt_existing
    ):
        reopenings.append(
            StatusEvent(
                event_time,
                "mcu",
                "reopened",
                (
                    "Schema-13 migration requires renewed MCU approval to "
                    "capture a trustworthy pre-IMPLEMENT source baseline"
                ),
            )
        )
        reopened.append("mcu")
    implement_index = PHASE_NUMBER["implement"] - 1
    for phase in PHASES[implement_index:]:
        event_info = latest.get(phase.key)
        if event_info is None or event_info[1].action != "complete":
            continue
        reopenings.append(
            StatusEvent(
                event_time,
                phase.key,
                "reopened",
                (
                    (
                        "Schema-13 legacy adoption: this circuit existed before "
                        "the authored pre-source circuit gate; create and approve "
                        "an adoption proposal without claiming pre-source review"
                    )
                    if phase.key == "implement" and adopt_existing
                    else (
                        "Schema-13 migration requires the authored Step 5 circuit "
                        "proposal and final compiled parity evidence"
                    )
                ),
            )
        )
        reopened.append(phase.key)
    if implemented and adopt_existing and "implement" not in reopened:
        reopenings.append(
            StatusEvent(
                event_time,
                "implement",
                "reopened",
                (
                    "Schema-13 legacy adoption: this circuit existed before "
                    "the authored pre-source circuit gate; create and approve "
                    "an adoption proposal without claiming pre-source review"
                ),
            )
        )
        reopened.append("implement")
    migrated_document = replace(
        document,
        events=(
            *document.events,
            *reopenings,
        ),
    )
    spec = read_spec(project_dir / "spec.md")
    outputs = {
        pins_path: yaml.safe_dump(pins, sort_keys=False),
        agents_path: _render_agents(spec, tool_root),
    }
    status_path = _status_path(project_dir)
    baseline_path = project_dir / BASELINE_PATH
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (*outputs, status_path, baseline_path)
    }
    installed: list[Path] = []
    try:
        for path, contents in outputs.items():
            _atomic_write(path, contents)
            installed.append(path)
        if (
            mcu_event is not None
            and mcu_event[1].action == "complete"
            and adopt_existing
        ):
            capture_implementation_baseline(project_dir)
            installed.append(project_dir / BASELINE_PATH)
        write_status(
            project_dir,
            tool_root=tool_root,
            now=event_time,
            document=migrated_document,
        )
        installed.append(status_path)
    except (OSError, StatusError) as exc:
        for path in reversed(installed):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError:
                pass
        raise StatusError(
            f"could not migrate schematic review atomically: {exc}"
        ) from exc
    return ApprovalMigrationResult(project_dir, True, tuple(reopened))


def migrate_circuit_review(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    now: str | None = None,
    adopt_existing: bool = False,
) -> ApprovalMigrationResult:
    """Atomically migrate schema 12 to authored SVG circuit review."""
    project_dir = _project_dir(project_dir)
    tool_root = (
        tool_root.resolve()
        if tool_root is not None
        else Path(__file__).resolve().parent.parent
    )
    pins_path = project_dir / ".pcbforge"
    pins = dict(_project_pins(project_dir))
    schema = pins.get("schema")
    if schema == CIRCUIT_REVIEW_PIN_SCHEMA:
        return ApprovalMigrationResult(project_dir, False, ())
    if schema != SCHEMATIC_APPROVAL_PIN_SCHEMA:
        raise StatusInputError(
            "migrate-circuit-review requires generated .pcbforge schema 12; "
            f"got {schema!r}"
        )
    agents_path = project_dir / "AGENTS.md"
    try:
        agents = agents_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StatusInputError(f"cannot read {agents_path}: {exc}") from exc
    if not agents.startswith("<!-- pcbforge-agents-schema: 12 -->"):
        raise StatusInputError(
            "AGENTS.md is not the expected generated schema-12 guidance"
        )
    from pcbforge.initialize import (
        AGENTS_SCHEMA,
        APPROVAL_GUIDE_SCHEMA,
        BRIEF_GUIDE_SCHEMA,
        CIRCUIT_REVIEW_SCHEMA,
        IMPLEMENT_GUIDE_SCHEMA,
        MCU_GUIDE_SCHEMA,
        STATUS_SCHEMA,
        _render_agents,
    )

    guidance = pins.get("guidance")
    if not isinstance(guidance, dict):
        raise StatusInputError(".pcbforge guidance: expected a mapping")
    guidance = dict(guidance)
    guidance.pop("schematic_review_schema", None)
    pins["schema"] = CIRCUIT_REVIEW_PIN_SCHEMA
    pins["guidance"] = {
        **guidance,
        "agents_schema": AGENTS_SCHEMA,
        "mcu_schema": MCU_GUIDE_SCHEMA,
        "implement_schema": IMPLEMENT_GUIDE_SCHEMA,
        "brief_schema": BRIEF_GUIDE_SCHEMA,
        "approval_schema": APPROVAL_GUIDE_SCHEMA,
        "circuit_review_schema": CIRCUIT_REVIEW_SCHEMA,
        "status_schema": STATUS_SCHEMA,
    }
    try:
        revision_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tool_root,
            text=True,
            capture_output=True,
            check=False,
        )
        dirty_result = subprocess.run(
            ["git", "status", "--short"],
            cwd=tool_root,
            text=True,
            capture_output=True,
            check=False,
        )
        revision = revision_result.stdout.strip()
        dirty = bool(dirty_result.stdout.strip())
    except OSError:
        revision = ""
        dirty = True
    pcbforge_pin = pins.get("pcbforge")
    if isinstance(pcbforge_pin, dict) and revision:
        pins["pcbforge"] = {
            **pcbforge_pin,
            "revision": revision,
            "dirty": dirty,
        }

    document = read_status_document(project_dir)
    latest, _ = _latest_events(document.events)
    mcu_event = latest.get("mcu")
    mcu_complete = (
        mcu_event is not None and mcu_event[1].action == "complete"
    )
    baseline_ok, baseline_detail = baseline_is_current(project_dir)
    if mcu_complete and not baseline_ok and not adopt_existing:
        raise StatusInputError(
            f"{baseline_detail}; rewind physical source to the MCU handoff or "
            "rerun with --adopt-existing"
        )

    event_time = now or _now()
    reopenings: list[StatusEvent] = []
    reopened: list[str] = []
    implement_history = any(
        event.phase == "implement"
        and event.action in {"proposal-approved", "complete"}
        for event in document.events
    )
    if implement_history:
        reopenings.append(
            StatusEvent(
                event_time,
                "implement",
                "reopened",
                (
                    "Schema-13 legacy adoption requires an authored circuit SVG, "
                    "exact proposal model, and compiled parity"
                    if adopt_existing and not baseline_ok
                    else (
                        "Schema-13 replaces the native KiCad proposal with an "
                        "authored circuit SVG and exact proposal model"
                    )
                ),
            )
        )
        reopened.append("implement")
    implement_index = PHASE_NUMBER["implement"]
    for phase in PHASES[implement_index:]:
        event_info = latest.get(phase.key)
        if event_info is None or event_info[1].action != "complete":
            continue
        reopenings.append(
            StatusEvent(
                event_time,
                phase.key,
                "reopened",
                "Schema-13 circuit review migration invalidates downstream approval",
            )
        )
        reopened.append(phase.key)
    migrated_document = replace(
        document,
        events=(*document.events, *reopenings),
        checks={
            name: record
            for name, record in document.checks.items()
            if name not in {"schematic-proposal", "schematic-final"}
        },
    )
    spec = read_spec(project_dir / "spec.md")
    outputs = {
        pins_path: yaml.safe_dump(pins, sort_keys=False),
        agents_path: _render_agents(spec, tool_root),
    }
    status_path = _status_path(project_dir)
    baseline_path = project_dir / BASELINE_PATH
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (*outputs, status_path, baseline_path)
    }
    installed: list[Path] = []
    try:
        for path, contents in outputs.items():
            _atomic_write(path, contents)
            installed.append(path)
        if mcu_complete and adopt_existing and not baseline_ok:
            capture_implementation_baseline(project_dir)
            installed.append(baseline_path)
        write_status(
            project_dir,
            tool_root=tool_root,
            now=event_time,
            document=migrated_document,
        )
        installed.append(status_path)
    except (OSError, StatusError, SchematicError) as exc:
        for path in reversed(installed):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError:
                pass
        raise StatusError(
            f"could not migrate circuit review atomically: {exc}"
        ) from exc
    return ApprovalMigrationResult(project_dir, True, tuple(reopened))


def _validate_transition(
    report: StatusReport,
    phase_key: str,
    action: str,
) -> None:
    if phase_key not in PHASE_BY_KEY:
        raise StatusInputError(
            f"unknown phase {phase_key!r}; choose from {', '.join(PHASE_BY_KEY)}"
        )
    if action not in EVENT_ACTIONS:
        raise StatusInputError(
            f"unknown action {action!r}; choose from {', '.join(sorted(EVENT_ACTIONS))}"
        )
    if action == "skipped" and phase_key != "publish":
        raise StatusInputError("only the optional publish phase may be skipped")
    if action == "proposal-approved" and phase_key != "architect":
        raise StatusInputError(
            "proposal-approved is only valid for the architect phase"
        )
    if (
        action == "proposal-approved"
        and _schematic_approval_enabled(report.project_dir)
    ):
        raise StatusInputError(
            "schema-12-or-newer proposal approval requires `pcbforge status approve "
            f"{phase_key} --stage proposal --fingerprint <sha256> --note \"...\"`"
        )
    if action == "complete" and _universal_approval_enabled(
        report.project_dir
    ):
        raise StatusInputError(
            "schema-11-or-newer completion requires `pcbforge status approve "
            f"{phase_key} --fingerprint <sha256> --note \"...\"`"
        )
    if action == "complete" and phase_key not in LEGACY_MANUAL_PHASES:
        raise StatusInputError(f"{phase_key} is completed automatically from evidence")

    target_index = PHASE_NUMBER[phase_key] - 1
    predecessors = [
        result for result in report.phases[:target_index] if result.phase.required
    ]
    if action in {"complete", "skipped", "proposal-approved"} and not all(
        result.complete for result in predecessors
    ):
        waiting = next(
            result.phase.label for result in predecessors if not result.complete
        )
        raise StatusInputError(
            f"cannot mark {phase_key} {action}: {waiting} is not complete"
        )
    target = report.phases[target_index]
    if action == "proposal-approved" and target.complete:
        raise StatusInputError(
            "cannot approve an architect proposal after ARCHITECT is complete"
        )
    if action == "reopened" and not target.complete:
        prior_approval = any(
            event.phase == phase_key
            and event.action in {"complete", "proposal-approved"}
            for event in report.document.events
        )
        if not prior_approval:
            raise StatusInputError(
                f"cannot reopen {phase_key}: it has no prior approval"
            )
    if action == "blocked" and target.complete:
        raise StatusInputError(f"cannot block {phase_key}: reopen it first")
    if action in {"complete", "skipped"} and target.complete:
        raise StatusInputError(
            f"cannot mark {phase_key} {action}: it is already complete"
        )


def mark_policy(
    project_dir: Path,
    action: str,
    note: str,
    *,
    subject: str = "",
    tool_root: Path | None = None,
    now: str | None = None,
) -> StatusResult:
    """Persist an explicit policy approval already supplied by the user."""
    project_dir = _project_dir(project_dir)
    note = note.strip()
    if not note:
        raise StatusInputError("--note must be a non-empty explanation")
    if action not in {
        "baseline-approved",
        "exception-approved",
        "sourcing-confirmed",
    }:
        raise StatusInputError(f"unknown policy action {action!r}")
    if not _policy_constraints_enabled(project_dir):
        raise StatusInputError(
            "project policy is not migrated: run `pcbforge migrate-policy`"
        )

    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    baseline_approval, exception_approvals, _ = _policy_approval_context(document)
    event_time = now or _now()
    phase_reopen: StatusEvent | None = None

    if action == "baseline-approved":
        pins = _project_pins(project_dir)
        policy_pin = pins.get("policy")
        if (
            not isinstance(policy_pin, dict)
            or policy_pin.get("baseline_approval") != "policy-event"
        ):
            raise StatusInputError(
                "this project's policy baseline is approved with SPEC, not a "
                "separate migration gate"
            )
        subject = "baseline"
        fingerprint = policy_baseline_fingerprint(
            project_dir,
            tool_root=tool_root,
        )
        checked = check_policy(
            project_dir,
            tool_root=tool_root,
            through_phase="spec",
            baseline_approval=fingerprint,
            exception_approvals=exception_approvals,
        )
        if not checked.ok:
            raise StatusInputError(
                "cannot approve policy baseline:\n  - "
                + "\n  - ".join(
                    f"[{violation.rule}] {violation.message}"
                    for violation in checked.violations
                )
            )
    elif action == "exception-approved":
        if not _current_policy_baseline(project_dir, document):
            raise StatusInputError(
                "approve the migrated policy baseline before any exception"
            )
        if not subject:
            raise StatusInputError("exception approval requires an exception ID")
        fingerprints = policy_exception_fingerprints(
            project_dir,
            tool_root=tool_root,
        )
        if subject not in fingerprints:
            raise StatusInputError(
                f"policy.yaml has no exception with ID {subject!r}"
            )
        contract = read_policy_contract(project_dir)
        exception = next(
            item for item in contract.exceptions if item.identifier == subject
        )
        profile, _, _ = load_policy_profile(tool_root)
        phase = str(
            profile["exception_rules"][exception.rule]["earliest_phase"]
        )
        fingerprint = fingerprints[subject]
        provisional = dict(exception_approvals)
        provisional[subject] = fingerprint
        checked = check_policy(
            project_dir,
            tool_root=tool_root,
            through_phase=phase,
            baseline_approval=baseline_approval,
            exception_approvals=provisional,
        )
        unused = any(
            warning.rule == "policy.unused-exception"
            and warning.scope == subject
            for warning in checked.warnings
        )
        if unused:
            raise StatusInputError(
                f"cannot approve exception {subject!r}: it is not currently needed"
            )
        target = report.phases[PHASE_NUMBER[phase] - 1]
        if target.complete:
            phase_reopen = StatusEvent(
                event_time,
                phase,
                "reopened",
                (
                    f"Policy exception {subject!r} changed the approved "
                    f"{target.phase.label} contract"
                ),
            )
    else:
        if not _current_policy_baseline(project_dir, document):
            raise StatusInputError(
                "approve the migrated policy baseline before sourcing confirmation"
            )
        subject = "sourcing"
        fab_result = report.phases[PHASE_NUMBER["fab-out"] - 1]
        if not fab_result.complete:
            raise StatusInputError(
                "cannot confirm sourcing before FAB-OUT is complete"
            )
        if not (project_dir / BUILD_TEST_FILENAME).is_file():
            raise StatusInputError(
                "cannot confirm sourcing without current build-test.yaml"
            )
        fab_outputs = tuple(
            path
            for path in (project_dir / "fab").rglob("*")
            if path.is_file() and path.name != ".gitkeep"
        )
        if not fab_outputs:
            raise StatusInputError(
                "cannot confirm sourcing without current fabrication outputs"
            )
        checked = check_policy(
            project_dir,
            tool_root=tool_root,
            through_phase="verify",
            baseline_approval=baseline_approval,
            exception_approvals=exception_approvals,
        )
        if not checked.ok:
            raise StatusInputError(
                "cannot confirm sourcing while policy violations remain:\n  - "
                + "\n  - ".join(
                    f"[{violation.rule}] {violation.message}"
                    for violation in checked.violations
                )
            )
        fingerprint = policy_sourcing_fingerprint(
            project_dir,
            tool_root=tool_root,
        )

    event = PolicyEvent(
        event_time,
        action,
        subject,
        note,
        fingerprint,
    )
    events = (
        (*document.events, phase_reopen)
        if phase_reopen is not None
        else document.events
    )
    document = replace(
        document,
        events=events,
        policy_events=(*document.policy_events, event),
    )
    return write_status(
        project_dir,
        tool_root=tool_root,
        now=event_time,
        document=document,
    )


def mark_status(
    project_dir: Path,
    phase: str,
    action: str,
    note: str,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
) -> StatusResult:
    """Append a workflow event after validating order and current evidence."""
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    action = action.lower()
    note = note.strip()
    if not note:
        raise StatusInputError("--note must be a non-empty explanation")

    document = read_status_document(project_dir)
    pins = _project_pins(project_dir)
    if pins.get("schema") == 10:
        raise StatusInputError(
            "project approval workflow is schema 10; run "
            "`pcbforge migrate-approvals` before recording status events"
        )
    initial = inspect_status(project_dir, document=document)
    _validate_transition(initial, phase, action)
    if action == "complete" and (
        _policy_constraints_enabled(project_dir)
        or (
            phase == "spec"
            and not (project_dir / ".pcbforge").exists()
        )
    ):
        baseline_approval, exception_approvals, _ = _policy_approval_context(
            document
        )
        try:
            policy_result = check_policy(
                project_dir,
                tool_root=tool_root,
                through_phase=phase,
                baseline_approval=baseline_approval,
                exception_approvals=exception_approvals,
            )
        except (PolicyInputError, PolicyError) as exc:
            raise StatusInputError(
                f"cannot mark {phase} complete: {exc}"
            ) from exc
        if not policy_result.ok:
            raise StatusInputError(
                f"cannot mark {phase} complete: policy failed:\n  - "
                + "\n  - ".join(
                    f"[{violation.rule}] {violation.message}"
                    for violation in policy_result.violations
                )
            )
    if (
        action == "complete"
        and phase == "architect"
        and _approval_constraints_enabled(project_dir)
        and _current_architect_proposal(project_dir, document) is None
    ):
        raise StatusInputError(
            "cannot mark architect complete: the current docs/architecture.md "
            "proposal has not received explicit user approval; record "
            "`status mark architect proposal-approved` before coding"
        )
    if action == "proposal-approved":
        diagram = project_dir / "docs" / "architecture.md"
        if (
            not diagram.is_file()
            or ARCHITECTURE_MARKER not in _read_text(diagram)
        ):
            raise StatusInputError(
                "cannot approve architect proposal: missing current "
                "docs/architecture.md"
            )
    if (
        action == "complete"
        and phase == "brief"
        and not _schematic_approval_enabled(project_dir)
    ):
        note_lower = note.lower()
        if "brief.md" not in note_lower or not re.search(
            r"schematic\s+review\s*:\s*adequate",
            note_lower,
        ):
            raise StatusInputError(
                "cannot mark brief complete: --note must reference brief.md and "
                "contain `schematic review: adequate`"
            )

    event_time = now or _now()
    if action == "complete" and phase in CHECK_PHASES:
        document = run_status_checks(
            project_dir,
            document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
        checked_report = inspect_status(project_dir, document=document)
        failures = []
        for check_name in _phase_check_names(project_dir, CHECK_PHASES, phase):
            ok, detail = _current_check(
                project_dir, checked_report.spec, document, check_name
            )
            if not ok:
                failures.append(detail)
        if failures:
            raise StatusCheckError(
                f"cannot mark {phase} complete:\n  - " + "\n  - ".join(failures)
            )

    evidence_ok, evidence_detail, _ = _static_evidence(
        project_dir,
        initial.spec,
        document,
        phase,
    )
    if action == "complete" and not evidence_ok:
        raise StatusInputError(f"cannot mark {phase} complete: {evidence_detail}")

    approval_fingerprint = (
        _approval_fingerprint(project_dir, phase, action, document)
        if _phase_requires_approval(project_dir, phase)
        and action in {"complete", "proposal-approved"}
        else ""
    )
    event = StatusEvent(
        event_time,
        phase,
        action,
        note,
        approval_fingerprint,
    )
    document = replace(document, events=(*document.events, event))
    return write_status(
        project_dir,
        tool_root=tool_root,
        runner=runner,
        now=event_time,
        document=document,
    )


def import_legacy_architect_approval(
    project_dir: Path,
    document: StatusDocument,
) -> StatusDocument:
    """Import the prior spec.md architecture gate on first dashboard creation."""
    if any(event.phase == "architect" for event in document.events):
        return document
    spec_path = project_dir / "spec.md"
    try:
        text = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return document
    match = re.search(
        (
            r"(?ms)^-\s*(\d{4}-\d{2}-\d{2}):\s*ARCHITECT approved"
            r"\s*[—-]\s*(.*?)(?=^-\s*\d{4}-\d{2}-\d{2}:|\Z)"
        ),
        text,
    )
    if match is None:
        return document
    at = f"{match.group(1)}T00:00:00+00:00"
    summary = re.sub(r"\s+", " ", match.group(2)).strip()
    note = f"Imported from legacy spec.md decision: {summary}"
    return replace(
        document,
        events=(*document.events, StatusEvent(at, "architect", "complete", note)),
    )
