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
    BUILD_TEST_REPORT,
    BuildTestError,
    BuildTestInputError,
    ato_source_semantic_bytes,
    board_topology_bytes,
    build_test_inputs,
    check_build_test,
    fingerprint_inputs,
    read_board_evidence,
    saved_report_status,
)
from pcbforge.circuit_review import (
    BASELINE_PATH,
    CONTRACT_FILENAME as CIRCUIT_REVIEW_FILENAME,
    CircuitReviewError,
    CircuitReviewInputError,
    baseline_is_current,
    capture_implementation_baseline,
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
    policy_circuit_fingerprint,
    policy_exception_fingerprints,
    policy_inputs,
    policy_sourcing_fingerprint,
    policy_status_fingerprint,
    read_policy_contract,
)
from pcbforge.placement import (
    BRIEF_FILENAME,
    PLACEMENT_FILENAME,
    PlacementError,
    PlacementInputError,
    brief_inputs,
    brief_status_fingerprint,
    check_brief,
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
TRANSITION_ACTIONS = {
    "complete",
    "approved",
    "blocked",
    "reopened",
}
POLICY_EVENT_ACTIONS = {
    "exception-approved",
    "sourcing-confirmed",
    "reopened",
}
CHECK_PHASES = {
    "architect": ("build", "ioc"),
    "circuit": (
        "build",
        "parts",
        "policy",
        "ioc",
        "circuit-final",
        "build-test",
    ),
    "verify": ("build", "policy", "ioc", "drc"),
}
PHASE_EVIDENCE_CHECKS = {
    "architect": ("build", "ioc"),
    "circuit": ("build", "parts", "policy", "circuit-final", "build-test"),
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
        "architect",
        "ARCHITECT",
        "AI + user",
        True,
        "Approve the functional graph and exact MCU implementation.",
    ),
    Phase(
        "circuit",
        "CIRCUIT",
        "AI + tool",
        True,
        "Approve, implement, compile, and deterministically validate the circuit.",
    ),
    Phase("layout", "LAYOUT", "User", True, "Complete component placement in KiCad."),
    Phase("route", "ROUTE", "User", True, "Complete routing in KiCad."),
    Phase(
        "verify",
        "VERIFY",
        "Tool + AI",
        True,
        "Pass DRC, scripted audits, and the final render review.",
    ),
    Phase(
        "fab-out",
        "FAB-OUT",
        "Tool",
        True,
        "Generate and review the JLCPCB manufacturing package.",
    ),
    Phase("order", "ORDER", "User", True, "Upload the package and place the order."),
    Phase(
        "publish",
        "PUBLISH",
        "AI + user",
        False,
        "Publish or explicitly skip reusable proven modules.",
    ),
)
PHASE_BY_KEY = {
    phase.key: phase
    for phase in PHASES
}
PHASE_NUMBER = {phase.key: index for index, phase in enumerate(PHASES, start=1)}
APPROVAL_BOUND_PHASES = set(PHASE_BY_KEY)

APPROVAL_CHECKS = {
    "spec": ("policy",),
    "architect": ("build", "ioc"),
    "circuit": (
        "build",
        "parts",
        "policy",
        "ioc",
        "circuit-final",
        "build-test",
    ),
    "verify": ("build", "policy", "ioc", "drc"),
}


@dataclass(frozen=True)
class StatusEvent:
    at: str
    phase: str
    action: str
    note: str
    approval_fingerprint: str = ""
    content_fingerprint: str = ""
    renewed_from: str = ""


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
class TransitionEvent:
    at: str
    transition: str
    action: str
    note: str
    approval_fingerprint: str = ""
    content_fingerprint: str = ""
    renewed_from: str = ""


@dataclass(frozen=True)
class StatusDocument:
    updated_at: str
    events: tuple[StatusEvent, ...]
    checks: Mapping[str, CheckRecord]
    policy_events: tuple[PolicyEvent, ...] = ()
    transition_events: tuple[TransitionEvent, ...] = ()


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    state: str
    detail: str
    complete: bool


@dataclass(frozen=True)
class TransitionResult:
    key: str
    label: str
    source_phase: str
    target_phase: str
    lead: str
    state: str
    detail: str
    performed: bool
    complete: bool


@dataclass(frozen=True)
class NextAction:
    owner: str
    action: str
    command: str = ""
    command_when_ready: bool = False


@dataclass(frozen=True)
class HandoffSummary:
    last_completed: str
    performed_inactive: str
    previous_label: str
    current_label: str
    next_label: str
    current_state: str
    current_detail: str


@dataclass(frozen=True)
class StatusReport:
    project_dir: Path
    spec: ProjectSpec
    document: StatusDocument
    phases: tuple[PhaseResult, ...]
    current: PhaseResult | None
    transitions: tuple[TransitionResult, ...]
    current_transition: TransitionResult | None
    primary_action: NextAction | None
    handoff: HandoffSummary
    completed_required: int
    required_total: int
    checks_failed: bool

    @property
    def next_actions(self) -> tuple[str, ...]:
        """Compatibility view of the single primary action."""
        if self.primary_action is None:
            return ()
        values = [self.primary_action.action]
        if self.primary_action.command:
            values.append(f"Run `{self.primary_action.command}`.")
        return tuple(values)


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
class CascadeGateReview:
    key: str
    label: str
    action: str
    classification: str
    detail: str
    previous_fingerprint: str
    approval_fingerprint: str
    content_fingerprint: str
    artifacts: tuple[str, ...]
    checks: tuple[PhaseReviewCheck, ...]


@dataclass(frozen=True)
class CascadeReview:
    project_dir: Path
    ready: bool
    detail: str
    root_gate: str
    changed_slices: tuple[str, ...]
    unchanged_slices: tuple[str, ...]
    gates: tuple[CascadeGateReview, ...]
    fingerprint: str


@dataclass(frozen=True)
class _ApprovalGate:
    key: str
    label: str
    phase: str
    action: str
    stage: str
    transition: str = ""


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


def _invalid_spec_contract_digest(detail: str) -> str:
    marker = f"<invalid:{detail}>".encode()
    return hashlib.sha256(b"spec-contract-v1\0" + marker).hexdigest()


def spec_contract_digest(project_dir: Path) -> str:
    """Fingerprint normative SPEC content while excluding its decisions log."""
    path = project_dir.expanduser().resolve() / "spec.md"
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return _invalid_spec_contract_digest("missing spec.md")
    except OSError as exc:
        return _invalid_spec_contract_digest(
            f"spec.md read {type(exc).__name__} errno={exc.errno}"
        )

    try:
        contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _invalid_spec_contract_digest(
            f"spec.md utf-8 byte={exc.start} reason={exc.reason}"
        )

    lines = contents.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        return _invalid_spec_contract_digest("missing opening frontmatter delimiter")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip(b"\r\n") == b"---"
        )
    except StopIteration:
        return _invalid_spec_contract_digest("missing closing frontmatter delimiter")

    try:
        frontmatter = yaml.load(
            b"".join(lines[1:end]).decode("utf-8"),
            Loader=_UniqueStatusLoader,
        )
    except yaml.YAMLError as exc:
        return _invalid_spec_contract_digest(f"frontmatter {exc}")
    if not isinstance(frontmatter, dict):
        return _invalid_spec_contract_digest("frontmatter is not a mapping")
    try:
        canonical_frontmatter = json.dumps(
            frontmatter,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as exc:
        return _invalid_spec_contract_digest(f"frontmatter {exc}")

    body: list[bytes] = []
    skipping_decisions = False
    heading = re.compile(rb"^#{1,2}(?!#)[ \t]+")
    for line in lines[end + 1 :]:
        content = line.rstrip(b"\r\n").rstrip(b" \t")
        if content == b"## Decisions log":
            skipping_decisions = True
            continue
        if skipping_decisions:
            if heading.match(content):
                skipping_decisions = False
            else:
                continue
        body.append(line)

    digest = hashlib.sha256()
    digest.update(b"spec-contract-v1\0")
    digest.update(canonical_frontmatter)
    digest.update(b"\0")
    digest.update(b"".join(body))
    return digest.hexdigest()


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
        "transition_events",
        "checks",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append("unknown keys: " + ", ".join(map(str, unknown)))
    if type(data.get("pcbforge_status_schema")) is not int or data.get(
        "pcbforge_status_schema"
    ) != STATUS_SCHEMA:
        errors.append("pcbforge_status_schema: unsupported version — restart the project")
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
                "content_fingerprint",
                "renewed_from",
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
            content_fingerprint = _text(
                raw.get("content_fingerprint"),
                f"{prefix}.content_fingerprint",
                errors,
                required=False,
            )
            renewed_from = _text(
                raw.get("renewed_from"),
                f"{prefix}.renewed_from",
                errors,
                required=False,
            )
            for field, value in (
                ("content_fingerprint", content_fingerprint),
                ("renewed_from", renewed_from),
            ):
                if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    errors.append(f"{prefix}.{field}: expected a lowercase SHA-256")
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
                    content_fingerprint=content_fingerprint,
                    renewed_from=renewed_from,
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

    transition_events_raw = data.get("transition_events", [])
    transition_events: list[TransitionEvent] = []
    if not isinstance(transition_events_raw, list):
        errors.append("transition_events: expected a list")
    else:
        for index, raw in enumerate(transition_events_raw):
            prefix = f"transition_events[{index}]"
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {
                "at",
                "transition",
                "action",
                "note",
                "approval_fingerprint",
                "content_fingerprint",
                "renewed_from",
            }:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            transition = _text(
                raw.get("transition"),
                f"{prefix}.transition",
                errors,
            )
            action = _text(raw.get("action"), f"{prefix}.action", errors)
            note = _text(raw.get("note"), f"{prefix}.note", errors)
            approval_fingerprint = _text(
                raw.get("approval_fingerprint"),
                f"{prefix}.approval_fingerprint",
                errors,
                required=False,
            )
            content_fingerprint = _text(
                raw.get("content_fingerprint"),
                f"{prefix}.content_fingerprint",
                errors,
                required=False,
            )
            renewed_from = _text(
                raw.get("renewed_from"),
                f"{prefix}.renewed_from",
                errors,
                required=False,
            )
            for field, value in (
                ("content_fingerprint", content_fingerprint),
                ("renewed_from", renewed_from),
            ):
                if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    errors.append(f"{prefix}.{field}: expected a lowercase SHA-256")
            if transition and transition not in {
                "initialize",
                "layout-handoff",
            }:
                errors.append(
                    f"{prefix}.transition: unknown transition {transition!r}"
                )
            if action and action not in TRANSITION_ACTIONS:
                errors.append(f"{prefix}.action: unknown action {action!r}")
            transition_events.append(
                TransitionEvent(
                    at,
                    transition,
                    action,
                    note,
                    approval_fingerprint,
                    content_fingerprint,
                    renewed_from,
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
                "layout-handoff",
                "policy",
                "drc",
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
        transition_events=tuple(transition_events),
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


def _workflow_phase_map(project_dir: Path) -> Mapping[str, Phase]:
    return PHASE_BY_KEY


def _phase_number(project_dir: Path, phase: str) -> int:
    return PHASE_NUMBER[phase]


def _phase_check_names(
    project_dir: Path,
    checks: Mapping[str, tuple[str, ...]],
    phase: str,
) -> tuple[str, ...]:
    return checks.get(phase, ())


def _phase_requires_approval(project_dir: Path, phase: str) -> bool:
    return phase == "spec" if not (project_dir / ".pcbforge").is_file() else phase in APPROVAL_BOUND_PHASES


def _project_pins(project_dir: Path) -> Mapping[str, Any]:
    path = project_dir / ".pcbforge"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueStatusLoader)
    except (FileNotFoundError, OSError, UnicodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _approval_fingerprint(
    project_dir: Path,
    phase: str,
    action: str = "complete",
    document: StatusDocument | None = None,
) -> str:
    project_dir = _project_dir(project_dir)
    return _payload_fingerprint(
        _approval_payload(project_dir, phase, action, document)
    )


UPSTREAM_APPROVAL_FIELDS = frozenset(
    {
        "spec_contract",
        "policy_baseline",
        "policy_circuit",
        "circuit_approval",
        "predecessor_approval",
        "predecessor_approvals",
    }
)


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _content_fingerprint(payload: Mapping[str, Any]) -> str:
    return _payload_fingerprint(
        {
            key: value
            for key, value in payload.items()
            if key not in UPSTREAM_APPROVAL_FIELDS
        }
    )


def _approval_payload(
    project_dir: Path,
    phase: str,
    action: str = "complete",
    document: StatusDocument | None = None,
) -> Mapping[str, Any]:
    if action == "proposal-approved":
        if phase == "architect":
            paths = {
                path
                for path in (
                    project_dir / "docs" / "architecture.md",
                    project_dir / "docs" / "mcu.md",
                )
                if path.is_file()
            }
            checks: list[dict[str, str]] = []
        elif phase == "circuit":
            try:
                paths = {
                    *circuit_review_inputs(project_dir, "proposal"),
                    project_dir / "review" / "circuit" / "proposal" / "evidence.json",
                }
            except CircuitReviewError:
                paths = {
                    path
                    for path in (
                        project_dir / ".pcbforge",
                        project_dir / CIRCUIT_REVIEW_FILENAME,
                        project_dir / "review" / "circuit" / "circuit.yaml",
                        project_dir / "review" / "circuit" / "circuit.svg",
                        project_dir / "docs" / "circuit-proposal.md",
                        project_dir / BASELINE_PATH,
                    )
                    if path.is_file()
                }
            checks = [{"name": "circuit-proposal", "required_outcome": "pass"}]
        else:
            raise AssertionError(f"{phase} has no proposal fingerprint")
        paths.discard(project_dir / "spec.md")
        payload = {
            "approval_schema": 1,
            "phase": phase,
            "stage": "proposal",
            "spec_contract": spec_contract_digest(project_dir),
            "artifacts": _file_semantics(
                project_dir,
                tuple(path for path in paths if path.is_file()),
            ),
            "checks": checks,
        }
        return payload
    if action == "complete":
        document = (
            document
            if document is not None
            else read_status_document(project_dir)
        )
        return _phase_approval_payload(project_dir, phase, document)
    raise AssertionError(f"{phase} has no approval fingerprint")


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


def _current_circuit_proposal(
    project_dir: Path,
    document: StatusDocument,
) -> StatusEvent | None:
    return _current_proposal(project_dir, document, "circuit")


def _architect_proposal_was_approved(document: StatusDocument) -> bool:
    """Return whether architecture source was ever authorized by a proposal."""
    return any(
        event.phase == "architect"
        and event.action == "proposal-approved"
        and bool(event.approval_fingerprint)
        for event in document.events
    )


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
    sourcing_event = latest.get("sourcing")
    sourcing = (
        sourcing_event.approval_fingerprint
        if sourcing_event is not None
        and sourcing_event.action == "sourcing-confirmed"
        else ""
    )
    exceptions = {
        subject: event.approval_fingerprint
        for subject, event in latest.items()
        if subject != "sourcing"
        and event.action == "exception-approved"
    }
    return "", exceptions, sourcing


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
    pins = _project_pins(project_dir)
    policy = pins.get("policy")
    return isinstance(policy, dict) and policy.get("baseline_approval") == "spec"


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
    if name == "layout-handoff":
        return brief_inputs(project_dir)
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
    except (CircuitReviewError, OSError) as exc:
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
    *,
    tool_root: Path | None = None,
) -> str:
    if name == "build":
        digest = hashlib.sha256()
        for path in inputs:
            if path.suffix in {".kicad_pcb", ".kicad_pro", ".kicad_dru"}:
                continue
            digest.update(path.relative_to(project_dir).as_posix().encode())
            digest.update(b"\0")
            if path == project_dir / "spec.md":
                digest.update(spec_contract_digest(project_dir).encode())
            else:
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
    if name == "layout-handoff":
        return brief_status_fingerprint(project_dir, tool_root=tool_root)
    if name == "circuit-proposal":
        return circuit_review_status_fingerprint(project_dir, "proposal")
    if name == "circuit-final":
        return circuit_review_status_fingerprint(project_dir, "final")
    if name == "policy":
        return policy_status_fingerprint(project_dir, tool_root=tool_root)
    if name == "ioc":
        digest = hashlib.sha256()
        for path in sorted(set(inputs)):
            digest.update(path.relative_to(project_dir).as_posix().encode())
            digest.update(b"\0")
            if path == project_dir / "spec.md":
                digest.update(spec_contract_digest(project_dir).encode())
            else:
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()
    return _fingerprint(project_dir, inputs)


def _reusable_check_record(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    name: str,
    *,
    tool_root: Path,
    force_checks: bool,
) -> CheckRecord | None:
    """Return an unchanged passing record when its current inputs still match."""
    if force_checks:
        return None
    record = document.checks.get(name)
    if record is None or record.outcome != "pass":
        return None
    try:
        inputs = _check_inputs(project_dir, spec, name)
        if not inputs:
            return None
        fingerprint = _check_fingerprint(
            project_dir,
            name,
            inputs,
            tool_root=tool_root,
        )
    except (
        BuildTestError,
        CircuitReviewError,
        InitInputError,
        PlacementError,
        PolicyError,
        OSError,
    ):
        return None
    if record.fingerprint != fingerprint:
        return None
    if name == "build-test":
        report_ok, _ = saved_report_status(project_dir, fingerprint)
        if not report_ok:
            return None
    return record


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
    elif phase == "architect":
        candidates = (
            project_dir / "docs" / "architecture.md",
            project_dir / "src" / "main.ato",
            *_files(project_dir, ("src/modules/*.ato",)),
            project_dir / "docs" / "mcu.md",
            project_dir / "firmware" / f"{spec.name}.ioc",
            project_dir / "src" / "mcu.ato",
        )
    elif phase == "circuit":
        candidates = (
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
            project_dir / CIRCUIT_REVIEW_FILENAME,
            project_dir / "docs" / "circuit-proposal.md",
            project_dir / "docs" / "circuit-review.md",
            *_files(
                project_dir,
                (
                    "review/circuit/proposal/**/*",
                    "review/circuit/final/**/*",
                ),
            ),
            board,
            *build_test_inputs(project_dir),
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
    excluded = {
        project_dir / "spec.md"
    } if phase in {"architect", "circuit"} else set()
    if phase == "circuit":
        excluded.add(project_dir / POLICY_FILENAME)
    return tuple(
        sorted(
            {
                path
                for path in candidates
                if path.is_file() and path not in excluded
            }
        )
    )


def _phase_review_artifact_paths(
    project_dir: Path,
    spec: ProjectSpec,
    phase: str,
) -> tuple[Path, ...]:
    """Return full human-facing files, including scoped shared contracts."""
    paths = set(_phase_artifact_paths(project_dir, spec, phase))
    if phase in {"architect", "circuit"}:
        paths.add(project_dir / "spec.md")
    if phase == "circuit":
        paths.add(project_dir / POLICY_FILENAME)
    return tuple(sorted(path for path in paths if path.is_file()))


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
    """Bind circuit source while allowing acceptance marker/assert pairs."""
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


def _phase_approval_payload(
    project_dir: Path,
    phase: str,
    document: StatusDocument,
) -> Mapping[str, Any]:
    spec = read_spec(project_dir / "spec.md")
    artifacts = _phase_artifact_paths(project_dir, spec, phase)
    payload: dict[str, Any] = {
        "approval_schema": 1,
        "phase": phase,
        "artifacts": _file_semantics(project_dir, artifacts),
        "checks": _approval_check_semantics(project_dir, phase),
    }
    if phase == "spec":
        payload.pop("artifacts")
        payload["spec_contract"] = spec_contract_digest(project_dir)
        try:
            payload["policy_baseline"] = policy_baseline_fingerprint(project_dir)
        except PolicyError as exc:
            payload["policy_baseline"] = f"<invalid:{exc}>"
    if phase == "architect":
        payload["spec_contract"] = spec_contract_digest(project_dir)
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
    if phase == "circuit":
        payload["spec_contract"] = spec_contract_digest(project_dir)
        try:
            payload["policy_circuit"] = policy_circuit_fingerprint(project_dir)
        except PolicyError as exc:
            payload["policy_circuit"] = f"<invalid:{exc}>"
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
    return payload


def _phase_approval_fingerprint(
    project_dir: Path,
    phase: str,
    document: StatusDocument,
) -> str:
    return _payload_fingerprint(
        _phase_approval_payload(project_dir, phase, document)
    )


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
    if phase == "architect":
        diagram = project_dir / "docs" / "architecture.md"
        diagram_ok = diagram.is_file() and ARCHITECTURE_MARKER in _read_text(diagram)
        source = _files(project_dir, ("src/**/*.ato",))
        build_ok, build_detail = _current_check(project_dir, spec, document, "build")
        mcu_doc = project_dir / "docs" / "mcu.md"
        ioc = project_dir / "firmware" / f"{spec.name}.ioc"
        mcu_source = project_dir / "src" / "mcu.ato"
        ioc_ok, ioc_detail = _current_check(project_dir, spec, document, "ioc")
        missing = []
        if not diagram_ok:
            missing.append("tracked architecture diagram")
        if not mcu_doc.is_file():
            missing.append("docs/mcu.md")
        if not ioc.is_file():
            missing.append(ioc.name)
        if not mcu_source.is_file():
            missing.append("src/mcu.ato")
        if len(source) < 2:
            missing.append("architecture source modules")
        if not build_ok:
            missing.append(build_detail)
        if not ioc_ok:
            missing.append(ioc_detail)
        return (
            not missing,
            "architecture, exact MCU plan, IOC, source graph, and audit evidence present"
            if not missing
            else "missing: " + ", ".join(missing),
            diagram.is_file() or mcu_doc.is_file() or len(source) > 1,
        )
    if phase == "circuit":
        build_ok, build_detail = _current_check(project_dir, spec, document, "build")
        parts_ok, parts_detail = _current_check(project_dir, spec, document, "parts")
        review_ok, review_detail = _current_check(
            project_dir, spec, document, "circuit-final"
        )
        acceptance_ok, acceptance_detail = _current_check(
            project_dir, spec, document, "build-test"
        )
        if acceptance_ok:
            fingerprint = _check_fingerprint(
                project_dir,
                "build-test",
                _check_inputs(project_dir, spec, "build-test"),
            )
            acceptance_ok, acceptance_detail = saved_report_status(
                project_dir,
                fingerprint,
            )
        modules = _files(project_dir, ("src/modules/*.ato",))
        satisfied = (
            build_ok
            and parts_ok
            and review_ok
            and acceptance_ok
            and bool(modules)
        )
        missing = []
        if not modules:
            missing.append("project module sources")
        if not build_ok:
            missing.append(build_detail)
        if not parts_ok:
            missing.append(parts_detail)
        if not review_ok:
            missing.append(review_detail)
        if not acceptance_ok:
            missing.append(acceptance_detail)
        return (
            satisfied,
            "module sources, compiled circuit parity, parts audit, and deterministic acceptance report are current"
            if satisfied
            else "missing: " + ", ".join(missing),
            bool(modules) or (project_dir / BUILD_TEST_FILENAME).is_file(),
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
    if phase == "order":
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
    if phase == "publish":
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


def _latest_transition_events(
    events: Sequence[TransitionEvent],
) -> Mapping[str, TransitionEvent]:
    latest: dict[str, TransitionEvent] = {}
    for event in events:
        latest[event.transition] = event
    return latest


def _initialization_transition_complete(project_dir: Path) -> bool:
    pins = _project_pins(project_dir)
    if type(pins.get("schema")) is not int or pins.get("schema") != 1:
        return False
    spec = read_spec(project_dir / "spec.md")
    return all(
        path.is_file()
        for path in (
            project_dir / ".pcbforge",
            project_dir / "ato.yaml",
            project_dir / "src" / "main.ato",
            project_dir / f"{spec.name}.kicad_pcb",
        )
    )


def _layout_handoff_fingerprint(
    project_dir: Path,
    document: StatusDocument,
) -> str:
    return _payload_fingerprint(
        _layout_handoff_payload(project_dir, document)
    )


def _layout_handoff_payload(
    project_dir: Path,
    document: StatusDocument,
) -> Mapping[str, Any]:
    latest, _ = _latest_events(document.events)
    circuit = latest.get("circuit")
    circuit_approval = (
        circuit[1].approval_fingerprint
        if circuit is not None and circuit[1].action == "complete"
        else ""
    )
    paths = tuple(
        sorted(
            path
            for path in (
                *brief_inputs(project_dir),
                project_dir / "build-test.yaml",
                project_dir / "docs" / "build-test.md",
            )
            if path.is_file()
        )
    )
    return {
        "approval_schema": 1,
        "transition": "layout-handoff",
        "source_phase": "circuit",
        "target_phase": "layout",
        "circuit_approval": circuit_approval,
        "artifacts": _file_semantics(project_dir, paths),
        "checks": [
            {"name": name, "required_outcome": "pass"}
            for name in ("build-test", "layout-handoff", "policy")
        ],
    }


def _current_layout_handoff(
    project_dir: Path,
    document: StatusDocument,
) -> TransitionEvent | None:
    event = _latest_transition_events(document.transition_events).get(
        "layout-handoff"
    )
    if (
        event is None
        or event.action != "approved"
        or not event.approval_fingerprint
    ):
        return None
    try:
        spec = read_spec(project_dir / "spec.md")
        latest, _ = _latest_events(document.events)
        circuit_info = latest.get("circuit")
        if (
            circuit_info is None
            or circuit_info[1].action != "complete"
            or not _approval_is_current(
                project_dir,
                "circuit",
                circuit_info[1],
                document,
            )
            or not all(
                _current_check(project_dir, spec, document, name)[0]
                for name in ("build-test", "layout-handoff", "policy")
            )
        ):
            return None
        current = _layout_handoff_fingerprint(project_dir, document)
    except (PlacementError, StatusError, OSError):
        return None
    return event if event.approval_fingerprint == current else None


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
        transition_wait = ""
        if (
            phase.key == "architect"
            and not _initialization_transition_complete(project_dir)
        ):
            predecessors_complete = False
            transition_wait = "waiting for the SPEC → ARCHITECT initialization transition"
        if (
            phase.key == "layout"
            and _current_layout_handoff(project_dir, document) is None
        ):
            predecessors_complete = False
            transition_wait = "waiting for the CIRCUIT → LAYOUT handoff"

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
            detail = "project policy is not bound to the approved SPEC"
        elif event is not None and event.action == "blocked":
            state = "Blocked"
            detail = event.note
        elif not predecessors_complete:
            state = "Not started"
            detail = transition_wait or "waiting for the previous required phase"
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
            and _architecture_source_started(project_dir)
        ):
            state = "Blocked"
            detail = (
                "architecture source exists without current proposal approval; "
                "stop source changes and present docs/architecture.md for approval"
            )
        elif (
            phase.key == "circuit"
            and _current_circuit_proposal(project_dir, document) is None
        ):
            baseline_ok, baseline_detail = baseline_is_current(project_dir)
            proposal_ok, proposal_detail = _current_check(
                project_dir,
                spec,
                document,
                "circuit-proposal",
            )
            if not baseline_ok:
                state = "Blocked"
                detail = (
                    f"{baseline_detail}; stop physical source changes and return "
                    "to the pre-circuit baseline"
                )
            elif proposal_ok:
                state = "Awaiting approval"
                detail = (
                    "authored circuit overview and exact proposal model are current; "
                    "present the proposal-stage review packet"
                )
            else:
                state = "Ready"
                detail = (
                    "create the explanatory SVG and exact circuit proposal before "
                    f"physical source edits ({proposal_detail})"
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


def _action_for(
    result: PhaseResult,
    project_dir: Path,
    document: StatusDocument,
) -> NextAction:
    phase = result.phase.key
    if result.state == "Blocked":
        return NextAction(
            result.phase.lead,
            f"Resolve the {result.phase.label} blocker: {result.detail}",
            "pcbforge status --check --write",
            True,
        )
    if result.state == "Awaiting approval":
        if "proposal" in result.detail.lower():
            return NextAction(
                "AI → user",
                (
                    "Present the exact proposal packet and wait for explicit "
                    "user approval."
                ),
                f"pcbforge status review {phase} --stage proposal",
            )
        return NextAction(
            "AI → user",
            (
                f"Present the exact {result.phase.label} packet and wait for "
                "explicit user approval."
            ),
            f"pcbforge status review {phase}",
        )
    actions = {
        "spec": NextAction(
            "AI + user",
            "Review and finalize `spec.md`, then prepare its approval packet.",
            "pcbforge status review spec",
            True,
        ),
        "architect": NextAction(
            "AI",
            "Draft `docs/architecture.md` and the exact MCU plan in `docs/mcu.md`.",
            "pcbforge status review architect --stage proposal",
            True,
        ),
        "circuit": NextAction(
            "AI",
            (
                "Create the explanatory SVG and exact circuit proposal before "
                "source edits."
            ),
            "pcbforge status review circuit --stage proposal",
            True,
        ),
        "layout": NextAction(
            "User",
            "Complete placement in KiCad 9, then prepare the LAYOUT review packet.",
            "pcbforge status review layout",
            True,
        ),
        "route": NextAction(
            "User",
            "Complete routing in KiCad 9, then prepare the ROUTE review packet.",
            "pcbforge status review route",
            True,
        ),
        "verify": NextAction(
            "Tool + AI",
            "Run DRC and complete the final audits and render review.",
            "pcbforge status --check --write",
        ),
        "fab-out": NextAction(
            "Tool",
            "Generate and review the Gerbers, drills, BOM, CPL, and JLCPCB archive.",
            "pcbforge status review fab-out",
            True,
        ),
        "order": NextAction(
            "User",
            "Review the fabrication package and place the authorized JLCPCB order.",
            "pcbforge status review order",
        ),
        "publish": NextAction(
            "AI + user",
            "Publish proven reusable modules, or explicitly skip PUBLISH.",
            "pcbforge status review publish",
            True,
        ),
    }
    if (
        phase == "architect"
        and _current_architect_proposal(project_dir, document) is not None
    ):
        return NextAction(
            "AI + tool",
            (
                "Implement and audit the approved architecture skeleton, IOC, "
                "and MCU boundary."
            ),
            "pcbforge status --check --write",
            True,
        )
    if (
        phase == "circuit"
        and _current_circuit_proposal(project_dir, document) is not None
    ):
        return NextAction(
            "AI + tool",
            "Implement and deterministically validate the approved circuit proposal.",
            "pcbforge status --check --write",
            True,
        )
    return actions[phase]


def _derive_transitions(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    phases: Sequence[PhaseResult],
) -> tuple[TransitionResult, ...]:
    by_phase = {result.phase.key: result for result in phases}
    latest = _latest_transition_events(document.transition_events)

    initialized = _initialization_transition_complete(project_dir)
    initialize_event = latest.get("initialize")
    initialize_performed = initialized or any(
        event.transition == "initialize"
        and event.action in {"complete", "reopened"}
        for event in document.transition_events
    )
    if not by_phase["spec"].complete and initialize_performed:
        initialize_state = "Inactive"
        initialize_detail = (
            "initialization was performed, but is inactive while SPEC is reopened"
        )
    elif not by_phase["spec"].complete:
        initialize_state = "Not started"
        initialize_detail = "waiting for SPEC approval"
    elif initialized:
        initialize_state = "Complete"
        initialize_detail = "validated project scaffold present"
    elif initialize_event is not None and initialize_event.action == "blocked":
        initialize_state = "Blocked"
        initialize_detail = initialize_event.note
    elif initialize_performed:
        initialize_state = "Stale"
        initialize_detail = (
            "initialization was recorded, but the validated scaffold is incomplete"
        )
    else:
        initialize_state = "Ready"
        initialize_detail = "run `pcbforge init` to create the validated scaffold"

    handoff_event = latest.get("layout-handoff")
    handoff_performed = any(
        event.transition == "layout-handoff"
        and event.action in {"approved", "reopened"}
        for event in document.transition_events
    )
    handoff_current = _current_layout_handoff(project_dir, document)
    if handoff_current is not None:
        handoff_state = "Complete"
        handoff_detail = "placement contract and LAYOUT handoff approval are current"
    elif not by_phase["circuit"].complete:
        if handoff_performed:
            handoff_state = "Inactive"
            handoff_detail = (
                "the LAYOUT handoff was performed, but is inactive while "
                "CIRCUIT is reopened"
            )
        else:
            handoff_state = "Not started"
            handoff_detail = "waiting for CIRCUIT approval"
    elif handoff_event is not None and handoff_event.action == "blocked":
        handoff_state = "Blocked"
        handoff_detail = handoff_event.note
    elif handoff_performed:
        handoff_state = "Stale"
        handoff_detail = (
            "handoff approval is stale because CIRCUIT or placement artifacts changed"
        )
    else:
        required_checks = ("build-test", "layout-handoff", "policy")
        check_results = [
            _current_check(project_dir, spec, document, name)
            for name in required_checks
        ]
        failed = [
            detail
            for current, detail in check_results
            if not current and detail.startswith(
                tuple(f"{name} failed:" for name in required_checks)
            )
        ]
        if failed:
            handoff_state = "Blocked"
            handoff_detail = "; ".join(failed)
        elif all(current for current, _ in check_results):
            handoff_state = "Awaiting approval"
            handoff_detail = (
                "placement evidence is current; present the LAYOUT handoff packet"
            )
        elif (project_dir / PLACEMENT_FILENAME).is_file():
            handoff_state = "In progress"
            handoff_detail = next(
                detail for current, detail in check_results if not current
            )
        else:
            handoff_state = "Ready"
            handoff_detail = (
                "author placement.yaml and run `pcbforge prepare-layout`"
            )

    return (
        TransitionResult(
            "initialize",
            "SPEC → ARCHITECT: initialize",
            "spec",
            "architect",
            "Tool",
            initialize_state,
            initialize_detail,
            initialize_performed,
            initialize_state == "Complete",
        ),
        TransitionResult(
            "layout-handoff",
            "CIRCUIT → LAYOUT: layout handoff",
            "circuit",
            "layout",
            "AI + tool + user",
            handoff_state,
            handoff_detail,
            handoff_performed,
            handoff_state == "Complete",
        ),
    )


def _transition_action(result: TransitionResult) -> NextAction:
    if result.key == "initialize":
        if result.state == "Blocked":
            return NextAction(
                "AI + tool",
                f"Resolve the initialization blocker: {result.detail}",
                "pcbforge init",
                True,
            )
        if result.state == "Stale":
            return NextAction(
                "AI + tool",
                "Restore the missing validated project scaffold.",
                "pcbforge init",
                True,
            )
        return NextAction(
            "Tool",
            "Create and validate the project scaffold, then continue to ARCHITECT.",
            "pcbforge init",
        )
    if result.state == "Blocked":
        return NextAction(
            "AI + tool",
            f"Resolve the LAYOUT handoff blocker: {result.detail}",
            "pcbforge status --check --write",
            True,
        )
    if result.state == "Stale":
        return NextAction(
            "AI + tool",
            "Refresh the placement evidence and prepare a new LAYOUT handoff.",
            "pcbforge status --check --write",
        )
    if result.state == "Awaiting approval":
        return NextAction(
            "AI → user",
            (
                "Present the exact LAYOUT handoff packet and wait for explicit "
                "user approval."
            ),
            "pcbforge status review layout --stage handoff",
        )
    return NextAction(
        "AI + tool",
        "Author the exact placement contract in `placement.yaml`.",
        "pcbforge prepare-layout",
        True,
    )


def by_phase_complete(
    phases: Sequence[PhaseResult],
    phase: str,
) -> bool:
    return next(
        (result.complete for result in phases if result.phase.key == phase),
        False,
    )


def _derive_handoff_summary(
    project_dir: Path,
    phases: Sequence[PhaseResult],
    transitions: Sequence[TransitionResult],
    current: PhaseResult | None,
    current_transition: TransitionResult | None,
) -> HandoffSummary:
    transitions_by_target = {
        transition.target_phase: transition
        for transition in transitions
    }
    workflow: list[tuple[str, str, bool, str, str, bool]] = []
    for result in phases:
        transition = transitions_by_target.get(result.phase.key)
        if transition is not None:
            compact_label = (
                "INITIALIZE transition"
                if transition.key == "initialize"
                else "LAYOUT HANDOFF transition"
            )
            workflow.append(
                (
                    "transition",
                    transition.key,
                    transition.complete,
                    transition.label,
                    compact_label,
                    transition.state != "Skipped",
                )
            )
        workflow.append(
            (
                "phase",
                result.phase.key,
                result.complete,
                result.phase.label,
                result.phase.label,
                result.state != "Skipped",
            )
        )

    completed = [
        label
        for _, _, complete, label, _, counts_as_completed in workflow
        if complete and counts_as_completed
    ]
    last_completed = (
        completed[-1]
        if completed
        else "Nothing yet — valid progress begins at SPEC"
    )

    workflow_positions = {
        (kind, key): index
        for index, (kind, key, _, _, _, _) in enumerate(workflow)
    }
    performed_invalid = [
        (
            workflow_positions[("transition", transition.key)],
            transition,
        )
        for transition in transitions
        if transition.performed and not transition.complete
    ]
    performed_inactive = ""
    if performed_invalid:
        _, transition = max(performed_invalid, key=lambda item: item[0])
        performed_inactive = (
            f"{transition.label} — {transition.state}: {transition.detail}"
        )

    if current_transition is not None:
        current_kind = "transition"
        current_key = current_transition.key
        current_label = (
            "INITIALIZE transition"
            if current_transition.key == "initialize"
            else "LAYOUT HANDOFF transition"
        )
        current_state = current_transition.state
        current_detail = current_transition.detail
    elif current is not None:
        current_kind = "phase"
        current_key = current.phase.key
        current_label = (
            f"{_phase_number(project_dir, current.phase.key)}. "
            f"{current.phase.label}"
        )
        current_state = current.state
        current_detail = current.detail
    else:
        return HandoffSummary(
            last_completed,
            performed_inactive,
            completed[-1] if completed else "Start",
            "Workflow complete",
            "—",
            "Complete",
            "All required and optional workflow phases are resolved.",
        )

    current_index = workflow_positions[(current_kind, current_key)]
    previous_label = (
        workflow[current_index - 1][4]
        if current_index > 0
        else "Start"
    )
    next_label = (
        workflow[current_index + 1][4]
        if current_index + 1 < len(workflow)
        else "Finish"
    )
    return HandoffSummary(
        last_completed,
        performed_inactive,
        previous_label,
        current_label,
        next_label,
        current_state,
        current_detail,
    )


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
    transitions = _derive_transitions(project_dir, spec, document, phases)
    current_transition = next(
        (
            transition
            for transition in transitions
            if not transition.complete
            and by_phase_complete(phases, transition.source_phase)
        ),
        None,
    )
    if (
        current_transition is not None
        and current is not None
        and current.phase.key != current_transition.target_phase
    ):
        current_transition = None
    primary_action = (
        _transition_action(current_transition)
        if current_transition is not None
        else _action_for(current, project_dir, document)
        if current is not None
        else None
    )
    handoff = _derive_handoff_summary(
        project_dir,
        phases,
        transitions,
        current,
        current_transition,
    )
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
        transitions=transitions,
        current_transition=current_transition,
        primary_action=primary_action,
        handoff=handoff,
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
    force_checks: bool = False,
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
        reusable = _reusable_check_record(
            project_dir,
            spec,
            document,
            name,
            tool_root=tool_root,
            force_checks=force_checks,
        )
        if reusable is None:
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

    build_available = False
    if (project_dir / ".pcbforge").is_file():
        name = "build"
        reusable = _reusable_check_record(
            project_dir,
            spec,
            document,
            name,
            tool_root=tool_root,
            force_checks=force_checks,
        )
        if reusable is not None:
            build_available = True
        else:
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
            build_available = ok

        should_run_build_test = (project_dir / BUILD_TEST_FILENAME).is_file()
        if should_run_build_test:
            name = "build-test"
            reusable = _reusable_check_record(
                project_dir,
                spec,
                document,
                name,
                tool_root=tool_root,
                force_checks=force_checks,
            )
            if reusable is None:
                try:
                    result = check_build_test(
                        project_dir,
                        tool_root=tool_root,
                        runner=runner,
                        write_report=write_reports,
                        skip_build=build_available,
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
            name = "layout-handoff"
            reusable = _reusable_check_record(
                project_dir,
                spec,
                document,
                name,
                tool_root=tool_root,
                force_checks=force_checks,
            )
            if reusable is None:
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
        reusable = _reusable_check_record(
            project_dir,
            spec,
            document,
            name,
            tool_root=tool_root,
            force_checks=force_checks,
        )
        if reusable is None:
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

        if (project_dir / CIRCUIT_REVIEW_FILENAME).is_file():
            for stage in ("proposal", "final"):
                if stage == "final" and not (
                    project_dir / "docs" / "circuit-review.md"
                ).is_file():
                    continue
                name = f"circuit-{stage}"
                reusable = _reusable_check_record(
                    project_dir,
                    spec,
                    document,
                    name,
                    tool_root=tool_root,
                    force_checks=force_checks,
                )
                if reusable is None:
                    try:
                        result = check_circuit_review(
                            project_dir, stage, write=write_reports
                        )
                    except (CircuitReviewInputError, CircuitReviewError) as exc:
                        ok = False
                        summary = str(exc).splitlines()[0]
                        try:
                            fingerprint = circuit_review_status_fingerprint(
                                project_dir, stage
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

    ioc_path = project_dir / "firmware" / f"{spec.name}.ioc"
    if ioc_path.is_file():
        name = "ioc"
        reusable = _reusable_check_record(
            project_dir,
            spec,
            document,
            name,
            tool_root=tool_root,
            force_checks=force_checks,
        )
        if reusable is None:
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
                _check_fingerprint(
                    project_dir,
                    name,
                    _check_inputs(project_dir, spec, name),
                ),
                "pass" if ok else "fail",
                summary,
            )

    if _route_is_complete(document):
        name = "drc"
        board = project_dir / f"{spec.name}.kicad_pcb"
        reusable = _reusable_check_record(
            project_dir,
            spec,
            document,
            name,
            tool_root=tool_root,
            force_checks=force_checks,
        )
        if reusable is None:
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
    phase_map = _workflow_phase_map(project_dir)
    if phase not in phase_map:
        raise StatusInputError(
            f"unknown phase {phase!r}; choose from {', '.join(phase_map)}"
        )
    document = (
        document
        if document is not None
        else read_status_document(project_dir)
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
    target_index = _phase_number(project_dir, phase) - 1
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
    if phase == "circuit" and _current_circuit_proposal(project_dir, checked) is None:
        failures.append(
            f"current {phase.upper()} circuit proposal approval is missing"
        )
    if phase == "layout" and _current_layout_handoff(project_dir, checked) is None:
        failures.append("current CIRCUIT → LAYOUT handoff approval is missing")
    if (
        phase != "spec"
        and not _current_policy_baseline(project_dir, checked)
    ):
        failures.append("project policy is not bound to the approved SPEC")

    artifacts = tuple(
        path.relative_to(project_dir).as_posix()
        for path in _phase_review_artifact_paths(project_dir, spec, phase)
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
            phase_map[phase],
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
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    proposal_phases = {"architect", "circuit"}
    if phase not in proposal_phases:
        raise StatusInputError(
            "proposal review is only valid for architect or circuit"
        )
    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    target_index = _phase_number(project_dir, phase) - 1
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
        failures.append(
            f"{_workflow_phase_map(project_dir)[phase].label} is already complete"
        )
    if phase != "spec" and not _current_policy_baseline(project_dir, document):
        failures.append("project policy is not bound to the approved SPEC")

    if phase == "architect":
        diagram = project_dir / "docs" / "architecture.md"
        mcu_plan = project_dir / "docs" / "mcu.md"
        if (
            not diagram.is_file()
            or ARCHITECTURE_MARKER not in _read_text(diagram)
        ):
            failures.append("missing current docs/architecture.md")
        if not mcu_plan.is_file():
            failures.append("missing current docs/mcu.md")
        if (
            _architecture_source_started(project_dir)
            and not _architect_proposal_was_approved(document)
        ):
            failures.append(
                "architecture source exists before current proposal approval"
            )
        artifacts = tuple(
            path.relative_to(project_dir).as_posix()
            for path in (
                project_dir / "spec.md",
                diagram,
                mcu_plan,
            )
            if path.is_file()
        )
    else:
        baseline_ok, baseline_detail = baseline_is_current(project_dir)
        if not baseline_ok:
            failures.append(baseline_detail)
        check_name = "circuit-proposal"
        try:
            result = check_circuit_review(project_dir, "proposal", write=False)
        except (CircuitReviewInputError, CircuitReviewError) as exc:
            result = None
            failures.append(str(exc).splitlines()[0])
            try:
                fingerprint = circuit_review_status_fingerprint(
                    project_dir, "proposal"
                )
            except CircuitReviewError:
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
            proposal_inputs = circuit_review_inputs(project_dir, "proposal")
        except CircuitReviewError:
            proposal_inputs = tuple(
                path
                for path in (
                    project_dir / ".pcbforge",
                    project_dir / CIRCUIT_REVIEW_FILENAME,
                    project_dir / "review" / "circuit" / "circuit.yaml",
                    project_dir / "review" / "circuit" / "circuit.svg",
                    project_dir / "docs" / "circuit-proposal.md",
                    project_dir / BASELINE_PATH,
                )
                if path.is_file()
            )
        extra_artifacts = (
            project_dir / "review" / "circuit" / "proposal" / "evidence.json",
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
            _workflow_phase_map(project_dir)[phase],
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


def _prepare_layout_handoff_review(
    project_dir: Path,
    *,
    tool_root: Path | None,
    runner: CommandRunner,
    checked_at: str | None,
) -> tuple[PhaseReview, StatusDocument]:
    project_dir = _project_dir(project_dir)
    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    circuit = next(
        result for result in report.phases if result.phase.key == "circuit"
    )
    failures: list[str] = []
    if not circuit.complete:
        failures.append("CIRCUIT is not complete")
    if _current_layout_handoff(project_dir, document) is not None:
        failures.append("LAYOUT handoff is already approved")

    checked = run_status_checks(
        project_dir,
        document,
        tool_root=tool_root,
        runner=runner,
        checked_at=checked_at,
    )
    spec = report.spec
    check_reviews: list[PhaseReviewCheck] = []
    for name in ("build-test", "layout-handoff", "policy"):
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
            failures.append(detail)

    artifacts = tuple(
        path.relative_to(project_dir).as_posix()
        for path in (
            *brief_inputs(project_dir),
            project_dir / "build-test.yaml",
            project_dir / "docs" / "build-test.md",
        )
        if path.is_file()
    )
    fingerprint = _layout_handoff_fingerprint(project_dir, checked)
    return (
        PhaseReview(
            project_dir,
            _workflow_phase_map(project_dir)["layout"],
            not failures,
            (
                "layout handoff evidence passed; explicit user approval is required"
                if not failures
                else "; ".join(dict.fromkeys(failures))
            ),
            fingerprint,
            artifacts,
            tuple(check_reviews),
            "handoff",
        ),
        checked,
    )


def _approval_gate_sequence() -> tuple[_ApprovalGate, ...]:
    gates: list[_ApprovalGate] = []
    for phase in PHASES:
        if phase.key in {"architect", "circuit"}:
            gates.append(
                _ApprovalGate(
                    f"{phase.key}:proposal",
                    f"{phase.label} proposal",
                    phase.key,
                    "proposal-approved",
                    "proposal",
                )
            )
        gates.append(
            _ApprovalGate(
                phase.key,
                f"{phase.label} final",
                phase.key,
                "complete",
                "final",
            )
        )
        if phase.key == "circuit":
            gates.append(
                _ApprovalGate(
                    "layout:handoff",
                    "CIRCUIT → LAYOUT handoff",
                    "layout",
                    "approved",
                    "handoff",
                    "layout-handoff",
                )
            )
    return tuple(gates)


def _gate_prior_approval(
    document: StatusDocument,
    gate: _ApprovalGate,
) -> tuple[int, StatusEvent | TransitionEvent] | None:
    if gate.transition:
        for index in range(len(document.transition_events) - 1, -1, -1):
            event = document.transition_events[index]
            if event.transition == gate.transition and event.action == gate.action:
                return index, event
        return None
    if gate.phase == "publish":
        latest = next(
            (
                event
                for event in reversed(document.events)
                if event.phase == "publish"
            ),
            None,
        )
        if latest is not None and latest.action == "skipped":
            return None
    for index in range(len(document.events) - 1, -1, -1):
        event = document.events[index]
        if event.phase == gate.phase and event.action == gate.action:
            return index, event
    return None


def _automatic_approval_reopen(event: StatusEvent | TransitionEvent) -> bool:
    return event.action == "reopened" and event.note.startswith(
        "Approval invalidated automatically because"
    )


def _gate_control_after(
    document: StatusDocument,
    gate: _ApprovalGate,
    approval_index: int,
) -> tuple[str, bool]:
    events: Sequence[StatusEvent | TransitionEvent]
    if gate.transition:
        events = document.transition_events[approval_index + 1 :]
        relevant = (
            event
            for event in events
            if isinstance(event, TransitionEvent)
            and event.transition == gate.transition
        )
    else:
        events = document.events[approval_index + 1 :]
        relevant = (
            event
            for event in events
            if isinstance(event, StatusEvent) and event.phase == gate.phase
        )
    automatically_reopened = False
    for event in relevant:
        if _automatic_approval_reopen(event):
            if gate.stage != "proposal":
                automatically_reopened = True
            continue
        if event.action == "reopened":
            return "gate was explicitly reopened", automatically_reopened
        if event.action == "blocked":
            return "gate was explicitly blocked", automatically_reopened
    return "", automatically_reopened


def _gate_payload(
    project_dir: Path,
    gate: _ApprovalGate,
    document: StatusDocument,
) -> Mapping[str, Any]:
    if gate.transition:
        return _layout_handoff_payload(project_dir, document)
    return _approval_payload(
        project_dir,
        gate.phase,
        gate.action,
        document,
    )


def _gate_artifacts(
    project_dir: Path,
    spec: ProjectSpec,
    gate: _ApprovalGate,
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    if gate.stage == "final":
        return tuple(
            path.relative_to(project_dir).as_posix()
            for path in _phase_review_artifact_paths(
                project_dir,
                spec,
                gate.phase,
            )
        )
    artifacts = {
        str(item["path"])
        for item in payload.get("artifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if gate.stage == "proposal":
        artifacts.add("spec.md")
    return tuple(sorted(artifacts))


def _gate_check_names(gate: _ApprovalGate) -> tuple[str, ...]:
    if gate.stage == "proposal":
        return ("circuit-proposal",) if gate.phase == "circuit" else ()
    if gate.stage == "handoff":
        return ("build-test", "layout-handoff", "policy")
    return APPROVAL_CHECKS.get(gate.phase, ())


def _gate_check_reviews(
    project_dir: Path,
    spec: ProjectSpec,
    document: StatusDocument,
    gate: _ApprovalGate,
) -> tuple[tuple[PhaseReviewCheck, ...], tuple[str, ...]]:
    reviews: list[PhaseReviewCheck] = []
    failures: list[str] = []
    for name in _gate_check_names(gate):
        current, detail = _current_check(project_dir, spec, document, name)
        record = document.checks.get(name)
        reviews.append(
            PhaseReviewCheck(
                name,
                (
                    record.outcome
                    if current and record is not None
                    else "stale"
                    if record is not None
                    else "missing"
                ),
                record.summary if current and record is not None else detail,
                record.fingerprint if record is not None else "",
            )
        )
        if not current:
            failures.append(detail)
    return tuple(reviews), tuple(failures)


def _append_gate_approval(
    document: StatusDocument,
    gate: _ApprovalGate,
    *,
    at: str,
    note: str,
    approval_fingerprint: str,
    content_fingerprint: str,
    renewed_from: str,
) -> StatusDocument:
    if gate.transition:
        event = TransitionEvent(
            at=at,
            transition=gate.transition,
            action=gate.action,
            note=note,
            approval_fingerprint=approval_fingerprint,
            content_fingerprint=content_fingerprint,
            renewed_from=renewed_from,
        )
        return replace(
            document,
            transition_events=(*document.transition_events, event),
        )
    event = StatusEvent(
        at=at,
        phase=gate.phase,
        action=gate.action,
        note=note,
        approval_fingerprint=approval_fingerprint,
        content_fingerprint=content_fingerprint,
        renewed_from=renewed_from,
    )
    return replace(document, events=(*document.events, event))


def prepare_cascade_review(
    project_dir: Path,
    document: StatusDocument | None = None,
) -> CascadeReview:
    """Prove which stale approvals can be renewed without full ceremony."""
    project_dir = _project_dir(project_dir)
    document = document if document is not None else read_status_document(project_dir)
    spec = read_spec(project_dir / "spec.md")
    gates = _approval_gate_sequence()
    prior = {
        gate.key: _gate_prior_approval(document, gate)
        for gate in gates
    }
    root_index: int | None = None
    root_error = ""
    for index, gate in enumerate(gates):
        approval = prior[gate.key]
        if approval is None:
            continue
        approval_index, event = approval
        explicit, automatic = _gate_control_after(
            document,
            gate,
            approval_index,
        )
        try:
            current_fingerprint = _payload_fingerprint(
                _gate_payload(project_dir, gate, document)
            )
        except (
            BuildTestError,
            CircuitReviewError,
            PlacementError,
            PolicyError,
            StatusError,
            OSError,
        ) as exc:
            root_index = index
            root_error = str(exc).splitlines()[0]
            break
        if explicit or automatic or event.approval_fingerprint != current_fingerprint:
            root_index = index
            root_error = explicit
            break
    if root_index is None:
        return CascadeReview(
            project_dir,
            False,
            "no stale approval chain is available for cascade renewal",
            "",
            (),
            (),
            (),
            "",
        )

    provisional = document
    reviews: list[CascadeGateReview] = []
    eligible_fingerprints: list[dict[str, str]] = []
    stopped = False
    changed_slices: tuple[str, ...] = ()
    unchanged_slices: tuple[str, ...] = ()
    for index, gate in enumerate(gates[root_index:], start=root_index):
        approval = prior[gate.key]
        if approval is None:
            continue
        approval_index, event = approval
        explicit, _ = _gate_control_after(document, gate, approval_index)
        payload: Mapping[str, Any] = {}
        payload_error = ""
        try:
            payload = _gate_payload(project_dir, gate, provisional)
            approval_fingerprint = _payload_fingerprint(payload)
            content_fingerprint = _content_fingerprint(payload)
            artifacts = _gate_artifacts(project_dir, spec, gate, payload)
        except (
            BuildTestError,
            CircuitReviewError,
            PlacementError,
            PolicyError,
            StatusError,
            OSError,
        ) as exc:
            approval_fingerprint = ""
            content_fingerprint = ""
            artifacts = ()
            payload_error = str(exc).splitlines()[0]
        checks, check_failures = _gate_check_reviews(
            project_dir,
            spec,
            provisional,
            gate,
        )

        if stopped:
            classification = "deferred"
            detail = "requires full ceremony after the earlier cascade stop"
        elif explicit:
            classification = "blocked"
            detail = explicit
        elif payload_error:
            classification = "blocked"
            detail = f"cannot compute current approval payload: {payload_error}"
        elif check_failures:
            classification = "blocked"
            detail = (
                "required saved checks are not current and passing: "
                + "; ".join(check_failures)
            )
        elif not event.content_fingerprint:
            classification = "delta"
            detail = "prior approval has no content fingerprint; full review required"
        elif event.content_fingerprint != content_fingerprint:
            classification = "delta"
            detail = "gate-owned semantic content changed; full review required"
        else:
            classification = "eligible"
            detail = "gate-owned semantic content is unchanged"
            eligible_fingerprints.append(
                {
                    "gate": gate.key,
                    "action": gate.action,
                    "approval_fingerprint": approval_fingerprint,
                }
            )
            provisional = _append_gate_approval(
                provisional,
                gate,
                at="cascade-review",
                note="cascade renewal simulation",
                approval_fingerprint=approval_fingerprint,
                content_fingerprint=content_fingerprint,
                renewed_from=event.approval_fingerprint,
            )

        reviews.append(
            CascadeGateReview(
                gate.key,
                gate.label,
                gate.action,
                classification,
                detail,
                event.approval_fingerprint,
                approval_fingerprint,
                content_fingerprint,
                artifacts,
                checks,
            )
        )
        if index == root_index:
            upstream = sorted(set(payload) & UPSTREAM_APPROVAL_FIELDS)
            if classification == "eligible":
                changed_slices = (
                    (
                        "upstream approval scope changed (one or more): "
                        + ", ".join(upstream)
                    )
                    if upstream
                    and event.approval_fingerprint != approval_fingerprint
                    else "predecessor approval ordering changed",
                )
                unchanged_slices = (
                    "gate-owned semantic content: " + content_fingerprint,
                    *(f"artifact: {artifact}" for artifact in artifacts),
                )
            elif classification == "delta":
                changed_slices = (
                    "gate-owned semantic content changed or cannot be proven unchanged",
                )
            else:
                changed_slices = (root_error or detail,)
        if classification in {"delta", "blocked"}:
            stopped = True

    fingerprint = (
        _payload_fingerprint(
            {
                "cascade_schema": 1,
                "gates": eligible_fingerprints,
            }
        )
        if eligible_fingerprints
        else ""
    )
    ready = bool(eligible_fingerprints)
    stop = next(
        (
            review
            for review in reviews
            if review.classification in {"delta", "blocked"}
        ),
        None,
    )
    detail = (
        f"{len(eligible_fingerprints)} unchanged gate(s) can be renewed"
        + (
            f"; cascade stops at {stop.label}: {stop.detail}"
            if stop is not None
            else ""
        )
        if ready
        else (
            f"cascade cannot renew {reviews[0].label}: {reviews[0].detail}"
            if reviews
            else "no prior approvals are available for renewal"
        )
    )
    return CascadeReview(
        project_dir,
        ready,
        detail,
        gates[root_index].key,
        changed_slices,
        unchanged_slices,
        tuple(reviews),
        fingerprint,
    )


def render_cascade_review(review: CascadeReview) -> str:
    """Render a consolidated, human-reviewable cascade renewal packet."""
    lines = [
        "pcbforge cascade review",
        f"readiness: {'AWAITING APPROVAL' if review.ready else 'BLOCKED'}",
        f"detail: {review.detail}",
        f"root gate: {review.root_gate or '(none)'}",
        "changed slices:",
    ]
    lines.extend(f"  - {item}" for item in review.changed_slices)
    if not review.changed_slices:
        lines.append("  - (none)")
    lines.append("unchanged slices:")
    lines.extend(f"  - {item}" for item in review.unchanged_slices)
    if not review.unchanged_slices:
        lines.append("  - (none proven)")
    lines.append("gates:")
    for gate in review.gates:
        lines.append(
            f"  - {gate.label}: {gate.classification.upper()} — {gate.detail}"
        )
        if gate.classification == "eligible":
            lines.append(
                "    approval: "
                f"{gate.previous_fingerprint} -> {gate.approval_fingerprint}"
            )
        for check in gate.checks:
            lines.append(
                f"    check {check.name}: {check.outcome} — {check.summary}"
            )
    if review.fingerprint:
        lines.append(f"cascade fingerprint: {review.fingerprint}")
    if review.ready:
        lines.append(
            "next: present this packet and wait for explicit user approval"
        )
    return "\n".join(lines)


def renew_cascade(
    project_dir: Path,
    expected_fingerprint: str,
    note: str,
    *,
    now: str | None = None,
) -> StatusResult:
    """Record one explicit decision across an eligible approval cascade."""
    project_dir = _project_dir(project_dir)
    expected_fingerprint = expected_fingerprint.strip().lower()
    note = note.strip()
    if not note:
        raise StatusInputError("--note must be a non-empty approval explanation")
    if re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint) is None:
        raise StatusInputError("--fingerprint must be a lowercase SHA-256 value")
    document = read_status_document(project_dir)
    review = prepare_cascade_review(project_dir, document)
    if review.fingerprint != expected_fingerprint:
        raise StatusInputError(
            "cannot renew cascade: reviewed fingerprint is stale or does not "
            "match current evidence"
        )
    if not review.ready:
        raise StatusInputError(f"cannot renew cascade: {review.detail}")
    event_time = now or _now()
    gate_by_key = {gate.key: gate for gate in _approval_gate_sequence()}
    renewed = document
    for item in review.gates:
        if item.classification != "eligible":
            break
        renewed = _append_gate_approval(
            renewed,
            gate_by_key[item.key],
            at=event_time,
            note=note,
            approval_fingerprint=item.approval_fingerprint,
            content_fingerprint=item.content_fingerprint,
            renewed_from=item.previous_fingerprint,
        )
    return write_status(
        project_dir,
        now=event_time,
        document=renewed,
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
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    if stage == "handoff":
        if phase != "layout":
            raise StatusInputError(
                "handoff review is only valid for layout"
            )
        review, _ = _prepare_layout_handoff_review(
            project_dir,
            tool_root=tool_root,
            runner=runner,
            checked_at=checked_at,
        )
        return review
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
        raise StatusInputError("stage must be proposal, handoff, or final")
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
            + (
                " proposal"
                if review.stage == "proposal"
                else " handoff"
                if review.stage == "handoff"
                else ""
            )
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
    event_time = now or _now()
    if stage == "handoff":
        if phase != "layout":
            raise StatusInputError(
                "handoff approval is only valid for layout"
            )
        review, checked = _prepare_layout_handoff_review(
            project_dir,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage == "proposal":
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
        raise StatusInputError("stage must be proposal, handoff, or final")
    if not review.ready:
        raise StatusInputError(
            f"cannot approve {phase}: {review.detail}"
        )
    if review.fingerprint != expected_fingerprint:
        raise StatusInputError(
            "cannot approve phase: reviewed fingerprint is stale or does not "
            f"match current evidence (expected {review.fingerprint})"
        )
    if stage == "handoff":
        payload = _layout_handoff_payload(project_dir, checked)
        transition_event = TransitionEvent(
            at=event_time,
            transition="layout-handoff",
            action="approved",
            note=note,
            approval_fingerprint=review.fingerprint,
            content_fingerprint=_content_fingerprint(payload),
        )
        checked = replace(
            checked,
            transition_events=(
                *checked.transition_events,
                transition_event,
            ),
        )
    else:
        action = "proposal-approved" if stage == "proposal" else "complete"
        payload = _approval_payload(
            project_dir,
            phase,
            action,
            checked,
        )
        event = StatusEvent(
            at=event_time,
            phase=phase,
            action=action,
            note=note,
            approval_fingerprint=review.fingerprint,
            content_fingerprint=_content_fingerprint(payload),
        )
        checked = replace(checked, events=(*checked.events, event))
    if stage == "final" and phase == "architect":
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
        if event.content_fingerprint:
            item["content_fingerprint"] = event.content_fingerprint
        if event.renewed_from:
            item["renewed_from"] = event.renewed_from
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
    transition_events = []
    for event in document.transition_events:
        item = {
            "at": event.at,
            "transition": event.transition,
            "action": event.action,
            "note": event.note,
        }
        if event.approval_fingerprint:
            item["approval_fingerprint"] = event.approval_fingerprint
        if event.content_fingerprint:
            item["content_fingerprint"] = event.content_fingerprint
        if event.renewed_from:
            item["renewed_from"] = event.renewed_from
        transition_events.append(item)
    return {
        "pcbforge_status_schema": STATUS_SCHEMA,
        "updated_at": document.updated_at,
        "events": events,
        "policy_events": policy_events,
        "transition_events": transition_events,
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
    handoff = report.handoff
    health = (
        "🔴 Blocked"
        if report.checks_failed
        or handoff.current_state == "Blocked"
        else "🟠 Needs attention"
        if handoff.current_state == "Stale"
        else "🟢 On track"
    )

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
    blockers.extend(
        f"- **{transition.label}:** {transition.detail}"
        for transition in report.transitions
        if transition.state in {"Blocked", "Stale"}
    )
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
    recent_items.extend(
        (
            event.at,
            f"- **{event.at}:** transition {event.transition} "
            f"{event.action} — {event.note}",
        )
        for event in report.document.transition_events
    )
    recent = [
        rendered
        for _, rendered in sorted(recent_items, reverse=True)[:5]
    ]
    if not recent:
        recent = ["- No milestone events recorded yet."]

    rows = []
    icons = {
        "Complete": "✅",
        "In progress": "🟡",
        "Ready": "🔵",
        "Awaiting approval": "🟣",
        "Not started": "⚪",
        "Blocked": "🔴",
        "Inactive": "⏸️",
        "Stale": "🟠",
        "Skipped": "➖",
    }
    transitions_by_target = {
        transition.target_phase: transition
        for transition in report.transitions
    }
    for result in report.phases:
        transition = transitions_by_target.get(result.phase.key)
        if transition is not None:
            rows.append(
                "| "
                + " | ".join(
                    (
                        "↳",
                        transition.label,
                        transition.lead,
                        f"{icons[transition.state]} {transition.state}",
                        _escape(transition.detail),
                    )
                )
                + " |"
            )
        number = _phase_number(report.project_dir, result.phase.key)
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
    performed_line = (
        f"\n**Previously performed:** {handoff.performed_inactive}<br>"
        if handoff.performed_inactive
        else ""
    )
    validity_line = ""
    if handoff.performed_inactive:
        validity_line = (
            "\n**Validity:** "
            + (
                "Forward progress currently begins at SPEC.<br>"
                if handoff.last_completed.startswith("Nothing")
                else (
                    "Forward progress currently ends at "
                    f"{handoff.last_completed}.<br>"
                )
            )
        )
    primary_action = report.primary_action
    next_owner = primary_action.owner if primary_action is not None else "None"
    next_action = (
        primary_action.action
        if primary_action is not None
        else "No required next action."
    )
    command_line = (
        "\n**"
        + (
            "Command when ready"
            if primary_action.command_when_ready
            else "Command"
        )
        + f":** `{primary_action.command}`"
        if primary_action is not None and primary_action.command
        else ""
    )
    return f"""---
{metadata}
---
# {report.spec.name} project dashboard

> Generated by PCBForge from project evidence and explicit workflow events.
> Use `pcbforge status` commands instead of editing this body.

_Last updated: {updated}_

## Handoff

> {handoff.previous_label} → **{handoff.current_label}** → {handoff.next_label}

**Just completed:** {handoff.last_completed}<br>{performed_line}{validity_line}
**Current:** {handoff.current_label} — {handoff.current_state}<br>
**Why:** {handoff.current_detail}<br>
**Progress:** {report.completed_required} of {report.required_total} required phases complete<br>
**Health:** {health}

**Next owner:** {next_owner}<br>
**Next action:** {next_action}{command_line}

## Completed

{chr(10).join(completed)}

## Blockers

{chr(10).join(blockers)}

## Workflow

| # | Phase | Lead | Status | Evidence or blocker |
|---:|---|---|---|---|
{chr(10).join(rows)}

## Recent history

{chr(10).join(recent)}
"""


def render_next(report: StatusReport) -> str:
    """Render only the current workflow handoff."""
    handoff = report.handoff
    lines = [
        f"last valid: {handoff.last_completed}",
    ]
    if handoff.performed_inactive:
        lines.append(f"performed: {handoff.performed_inactive}")
    lines.extend(
        (
            f"current: {handoff.current_label} — {handoff.current_state}",
            f"status: {handoff.current_detail}",
            (
                f"workflow: {handoff.previous_label} -> "
                f"[{handoff.current_label}] -> {handoff.next_label}"
            ),
        )
    )
    if report.primary_action is not None:
        lines.append(f"next owner: {report.primary_action.owner}")
        lines.append(f"next action: {report.primary_action.action}")
        if report.primary_action.command:
            command_label = (
                "command when ready"
                if report.primary_action.command_when_ready
                else "command"
            )
            lines.append(f"{command_label}: {report.primary_action.command}")
    else:
        lines.append("next action: none")
    return "\n".join(lines)


def render_terminal(report: StatusReport) -> str:
    """Render a concise terminal view from the same status model."""
    progress = (
        f"{report.spec.name}: {report.completed_required}/"
        f"{report.required_total} required phases complete"
    )
    return f"{progress}\n{render_next(report)}"


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
        try:
            approval_current = _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        except (CircuitReviewError, OSError):
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
    latest_policy = _latest_policy_events(document)
    try:
        exception_expected = policy_exception_fingerprints(project_dir)
        sourcing_expected = policy_sourcing_fingerprint(project_dir)
    except PolicyError:
        exception_expected = {}
        sourcing_expected = ""
    for subject, event in latest_policy.items():
        if event.action == "exception-approved":
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
    transition_invalidations: list[TransitionEvent] = []
    latest_transitions = _latest_transition_events(document.transition_events)
    handoff = latest_transitions.get("layout-handoff")
    if (
        handoff is not None
        and handoff.action == "approved"
        and _current_layout_handoff(project_dir, document) is None
    ):
        transition_invalidations.append(
            TransitionEvent(
                at or _now(),
                "layout-handoff",
                "reopened",
                (
                    "Approval invalidated automatically because CIRCUIT "
                    "or placement-handoff artifacts changed"
                ),
            )
        )
    if (
        not invalidations
        and not policy_invalidations
        and not transition_invalidations
    ):
        return document
    return replace(
        document,
        events=(*document.events, *invalidations),
        policy_events=(*document.policy_events, *policy_invalidations),
        transition_events=(
            *document.transition_events,
            *transition_invalidations,
        ),
    )


def write_status(
    project_dir: Path,
    *,
    check: bool = False,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
    document: StatusDocument | None = None,
    force_checks: bool = False,
) -> StatusResult:
    """Create or refresh STATUS.md, avoiding timestamp-only rewrites."""
    project_dir = _project_dir(project_dir)
    document = document if document is not None else read_status_document(project_dir)
    if check:
        document = run_status_checks(
            project_dir,
            document,
            tool_root=tool_root,
            runner=runner,
            checked_at=now,
            write_reports=True,
            force_checks=force_checks,
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


def record_initialization_blocker(
    project_dir: Path,
    note: str,
    *,
    now: str | None = None,
) -> StatusResult | None:
    """Record an eligible initialization failure without scaffolding."""
    project_dir = _project_dir(project_dir)
    if (project_dir / ".pcbforge").exists():
        return None
    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    spec_phase = next(
        result for result in report.phases if result.phase.key == "spec"
    )
    if not spec_phase.complete:
        return None
    event_time = now or _now()
    document = replace(
        document,
        transition_events=(
            *document.transition_events,
            TransitionEvent(
                event_time,
                "initialize",
                "blocked",
                note.strip() or "Initialization failed",
            ),
        ),
    )
    return write_status(
        project_dir,
        now=event_time,
        document=document,
    )


def _validate_transition(
    report: StatusReport,
    phase_key: str,
    action: str,
) -> None:
    phase_map = _workflow_phase_map(report.project_dir)
    if phase_key not in phase_map:
        raise StatusInputError(
            f"unknown phase {phase_key!r}; choose from {', '.join(phase_map)}"
        )
    if action not in EVENT_ACTIONS:
        raise StatusInputError(
            f"unknown action {action!r}; choose from {', '.join(sorted(EVENT_ACTIONS))}"
        )
    if action == "skipped" and phase_key != "publish":
        raise StatusInputError("only the optional publish phase may be skipped")
    if action == "proposal-approved":
        raise StatusInputError(
            "proposal approval requires `pcbforge status approve "
            f"{phase_key} --stage proposal --fingerprint <sha256> --note \"...\"`"
        )
    if action == "complete":
        raise StatusInputError(
            "completion requires `pcbforge status approve "
            f"{phase_key} --fingerprint <sha256> --note \"...\"`"
        )

    target_index = _phase_number(report.project_dir, phase_key) - 1
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
        "exception-approved",
        "sourcing-confirmed",
    }:
        raise StatusInputError(f"unknown policy action {action!r}")

    document = read_status_document(project_dir)
    report = inspect_status(project_dir, document=document)
    baseline_approval, exception_approvals, _ = _policy_approval_context(document)
    event_time = now or _now()
    phase_reopen: StatusEvent | None = None

    if action == "exception-approved":
        if not _current_policy_baseline(project_dir, document):
            raise StatusInputError(
                "project policy is not bound to the approved SPEC"
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
        target = report.phases[_phase_number(project_dir, phase) - 1]
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
                "project policy is not bound to the approved SPEC"
            )
        subject = "sourcing"
        fab_result = report.phases[_phase_number(project_dir, "fab-out") - 1]
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
    if (
        phase == "layout"
        and _current_layout_handoff(project_dir, document) is None
        and action in {"blocked", "reopened"}
    ):
        event = TransitionEvent(
            now or _now(),
            "layout-handoff",
            action,
            note,
        )
        return write_status(
            project_dir,
            tool_root=tool_root,
            runner=runner,
            now=event.at,
            document=replace(
                document,
                transition_events=(
                    *document.transition_events,
                    event,
                ),
            ),
        )
    initial = inspect_status(project_dir, document=document)
    _validate_transition(initial, phase, action)
    event_time = now or _now()
    event = StatusEvent(
        event_time,
        phase,
        action,
        note,
    )
    document = replace(document, events=(*document.events, event))
    return write_status(
        project_dir,
        tool_root=tool_root,
        runner=runner,
        now=event_time,
        document=document,
    )
