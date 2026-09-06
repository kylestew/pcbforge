"""Durable, evidence-backed workflow status for pcbforge projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.artifact_hash import evidence_bytes
from pcbforge.circuit_evidence import (
    ELECTRICAL_TEST_FILENAME,
    CIRCUIT_REPORT_PATH,
    CircuitEvidenceError,
    CircuitEvidenceInputError,
    _spatial_errors,
    board_topology_bytes,
    circuit_evidence_inputs,
    fingerprint_inputs,
    read_board_evidence,
    saved_report_status,
)
from pcbforge.architecture_baseline import (
    BASELINE_PATH, ArchitectureBaselineError, baseline_is_current,
    capture_implementation_baseline,
)

from pcbforge.initialize import InitInputError, ProjectSpec, STATUS_SCHEMA, read_spec
from pcbforge.ioc import IocProjectError, IocValidationError, check_ioc
from pcbforge.markdown_metadata import metadata_trailer, metadata_yaml
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
from pcbforge.placement_check import (
    PlacementCheckError,
    check_placement,
    placement_check_inputs,
)
STATUS_FILENAME = "STATUS.md"
ARCHITECTURE_MARKER = "pcbforge-architecture-diagram-schema: 2"

EVENT_ACTIONS = {
    "complete",
    "blocked",
    "reopened",
    "skipped",
    "proposal-approved",
    "ai-assisted",
}
ANNOTATION_ACTIONS = {"ai-assisted"}
TRANSITION_ACTIONS = {
    "complete",
    "approved",
    "blocked",
    "reopened",
}
TRANSITIONS = {
    "initialize",
    "architecture-baseline",
    "circuit-sync",
    "layout-handoff",
    "fab-out",
}
REVIEW_KEYS = {
    "spec",
    "architect:proposal",
    "circuit",
    "layout:handoff",
    "layout",
    "verify",
    "order",
    "publish",
    "cascade",
}
POLICY_EVENT_ACTIONS = {
    "exception-approved",
    "sourcing-confirmed",
    "reopened",
}
# Advisory checks are run, recorded, and displayed, but never gate anything.
# They are deliberately absent from PHASE_EVIDENCE_CHECKS (which blocks a phase)
# and APPROVAL_CHECKS (which gates approval and is hashed into the approval
# fingerprint). That absence is not sufficient on its own: two loops below walk
# every recorded check with no registry filter, so both consult this set.
ADVISORY_CHECKS = frozenset({"placement"})
#: Which phase row each advisory check is displayed under.
ADVISORY_CHECK_PHASE = {"placement": "layout"}
PHASE_EVIDENCE_CHECKS = {
    "architect": ("ioc",),
    "circuit": ("circuit", "policy"),
    "verify": ("policy", "drc"),
    "order": ("fab",),
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
        "Approve the checked schematic, update the PCB, and verify synchronization.",
    ),
    Phase(
        "layout",
        "LAYOUT",
        "User",
        True,
        "Complete component placement and routing in KiCad.",
    ),
    Phase(
        "verify",
        "VERIFY",
        "Tool + AI",
        True,
        "Pass DRC, scripted audits, and the final render review.",
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
APPROVAL_BOUND_PHASES = set(PHASE_BY_KEY) - {"architect"}

APPROVAL_CHECKS = {
    "spec": ("policy",), "architect": ("ioc",), "circuit": ("circuit", "policy", "ioc"),
    "verify": ("circuit", "policy", "ioc", "drc"), "order": ("fab",),
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
    scope: str = ""


@dataclass(frozen=True)
class ReviewRecord:
    at: str
    fingerprint: str


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
    reviews: Mapping[str, ReviewRecord] = field(default_factory=dict)


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
    notes: tuple[str, ...] = ()


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


def _review_key(phase: str, stage: str) -> str:
    if stage == "final":
        key = phase
    elif stage == "proposal":
        key = f"{phase}:proposal"
    elif stage == "handoff" and phase == "layout":
        key = "layout:handoff"
    else:
        raise StatusInputError(f"no saved-review key for {phase}:{stage}")
    if key not in REVIEW_KEYS:
        raise StatusInputError(f"unsupported review key {key!r}")
    return key


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


def _load_yaml_metadata(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise StatusInputError(f"cannot read {path}: {exc}") from exc

    try:
        yaml_text = metadata_yaml(text)
    except ValueError as exc:
        raise StatusInputError(f"invalid {STATUS_FILENAME} metadata: {exc}") from exc
    try:
        loaded = yaml.load(yaml_text, Loader=_UniqueStatusLoader)
    except yaml.YAMLError as exc:
        raise StatusInputError(f"invalid {STATUS_FILENAME} metadata: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StatusInputError(f"{STATUS_FILENAME} metadata must be a mapping")
    return loaded


def _text(value: Any, field: str, errors: list[str], *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        errors.append(f"{field}: expected a non-empty string")
        return ""
    return value.strip()


def read_status_document(project_dir: Path) -> StatusDocument:
    """Read and validate STATUS.md metadata, or return an empty document."""
    data = _load_yaml_metadata(_status_path(project_dir))
    if not data:
        return StatusDocument(updated_at="", events=(), checks={})

    errors: list[str] = []
    allowed = {
        "pcbforge_status_schema",
        "updated_at",
        "events",
        "policy_events",
        "transition_events",
        "reviews",
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
            if transition and transition not in TRANSITIONS:
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

    reviews_raw = data.get("reviews", {})
    reviews: dict[str, ReviewRecord] = {}
    if not isinstance(reviews_raw, dict):
        errors.append("reviews: expected a mapping")
    else:
        for key, raw in reviews_raw.items():
            prefix = f"reviews.{key}"
            if not isinstance(key, str) or key not in REVIEW_KEYS:
                errors.append(f"{prefix}: unknown review key")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {"at", "fingerprint"}:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            fingerprint = _text(
                raw.get("fingerprint"),
                f"{prefix}.fingerprint",
                errors,
            )
            if (
                fingerprint
                and re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                errors.append(
                    f"{prefix}.fingerprint: expected a lowercase SHA-256"
                )
            reviews[key] = ReviewRecord(at, fingerprint)

    checks_raw = data.get("checks", {})
    checks: dict[str, CheckRecord] = {}
    if not isinstance(checks_raw, dict):
        errors.append("checks: expected a mapping")
    else:
        for name, raw in checks_raw.items():
            prefix = f"checks.{name}"
            if name not in {"circuit", "parts", "ioc", "layout-handoff", "placement", "policy", "drc", "fab"}:
                errors.append(f"{prefix}: unknown check")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {"at", "fingerprint", "outcome", "summary", "scope"}:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            fingerprint = _text(raw.get("fingerprint"), f"{prefix}.fingerprint", errors)
            outcome = _text(raw.get("outcome"), f"{prefix}.outcome", errors)
            summary = _text(raw.get("summary"), f"{prefix}.summary", errors)
            if outcome and outcome not in {"pass", "fail"}:
                errors.append(f"{prefix}.outcome: expected 'pass' or 'fail'")
            scope = raw.get("scope", "")
            if not isinstance(scope, str) or scope and scope not in PHASE_BY_KEY:
                errors.append(f"{prefix}.scope: unknown policy phase")
            checks[name] = CheckRecord(at, fingerprint, outcome, summary, scope)

    if errors:
        raise StatusInputError(
            f"invalid {STATUS_FILENAME} metadata:\n  - " + "\n  - ".join(errors)
        )
    return StatusDocument(
        updated_at=updated_at,
        events=tuple(events),
        checks=checks,
        policy_events=tuple(policy_events),
        transition_events=tuple(transition_events),
        reviews=reviews,
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
            contents = evidence_bytes(path)
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


def _check_is_spent(project_dir, document, name):
    return name not in {"circuit", "ioc", "policy", "layout-handoff", "placement", "drc", "fab"}



def _advisory_phase(name: str) -> str:
    """The phase row an advisory check is displayed under, or ``""``."""
    return ADVISORY_CHECK_PHASE.get(name, "")


def _phase_check_names(
    project_dir: Path,
    checks: Mapping[str, tuple[str, ...]],
    phase: str,
) -> tuple[str, ...]:
    return checks.get(phase, ())


def _phase_requires_approval(project_dir: Path, phase: str) -> bool:
    if not (project_dir / ".pcbforge").is_file():
        return phase == "spec"
    return phase in APPROVAL_BOUND_PHASES


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


def _approval_payload(project_dir, phase, action="complete", document=None):
    if action == "proposal-approved":
        if phase != "architect":
            raise StatusInputError("only ARCHITECT has a proposal approval")
        paths = tuple(p for p in (project_dir / "docs/architecture.md", project_dir / "docs/mcu.md") if p.is_file())
        return {"approval_schema": 2, "phase": phase, "stage": "proposal",
                "spec_contract": spec_contract_digest(project_dir),
                "artifacts": _file_semantics(project_dir, paths), "checks": []}
    if action == "complete":
        return _phase_approval_payload(project_dir, phase, document or read_status_document(project_dir))
    raise StatusInputError(f"{phase} has no approval fingerprint for {action}")



def _approval_is_current(
    project_dir: Path,
    phase: str,
    event: StatusEvent | None,
    document: StatusDocument | None = None,
) -> bool:
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






def _architect_proposal_was_approved(document: StatusDocument) -> bool:
    """Return whether architecture source was ever authorized by a proposal."""
    return any(
        event.phase == "architect"
        and event.action == "proposal-approved"
        and bool(event.approval_fingerprint)
        for event in document.events
    )


def _architecture_source_started(project_dir):
    return any((project_dir / "firmware").glob("*.ioc"))



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


def _check_inputs(project_dir, spec, name):
    if name == "circuit":
        try:
            return circuit_evidence_inputs(project_dir)
        except (ValueError, OSError):
            return _files(project_dir, ("*.kicad_sch", "circuit-tests.yaml", "electrical-facts.yaml", "circuit-review.yaml", ".pcbforge"))
    if name == "ioc":
        return _files(project_dir, ("spec.md", f"firmware/{spec.name}.ioc"))
    if name == "policy":
        return policy_inputs(project_dir)
    if name == "layout-handoff":
        return brief_inputs(project_dir)
    if name == "placement":
        return placement_check_inputs(project_dir)
    if name == "drc":
        return _files(project_dir, ("*.kicad_pcb", "*.kicad_pro", "*.kicad_dru", "**/*.kicad_sch"))
    if name == "fab":
        return _fab_artifact_paths(project_dir)
    return ()



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
    except (ArchitectureBaselineError, OSError) as exc:
        return False, f"{name} inputs are invalid: {exc}"
    if not inputs:
        return False, f"{name} inputs are missing"
    try:
        fingerprint = _check_fingerprint(project_dir, name, inputs)
    except (
        CircuitEvidenceError,
        ArchitectureBaselineError,
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


def _check_fingerprint(project_dir, name, inputs, *, tool_root=None):
    if name == "circuit":
        from pcbforge.circuit import fingerprint_inputs, presentation_fingerprint
        return _payload_fingerprint({"electrical": fingerprint_inputs(project_dir), "presentation": presentation_fingerprint(project_dir)})
    if name == "layout-handoff":
        return brief_status_fingerprint(project_dir, tool_root=tool_root)
    if name == "policy":
        return policy_status_fingerprint(project_dir, tool_root=tool_root)
    if name == "ioc":
        return _payload_fingerprint({"spec": spec_contract_digest(project_dir), "ioc": _file_semantics(project_dir, tuple(p for p in inputs if p.name != "spec.md"))})
    return _fingerprint(project_dir, inputs)







def _phase_artifact_paths(project_dir, spec, phase):
    patterns = {
      "spec": ("spec.md", "policy.yaml"),
      "architect": ("docs/architecture.md", "docs/mcu.md", "firmware/*.ioc"),
      "circuit": ("*.kicad_sch", "**/*.kicad_sch", "circuit-tests.yaml", "electrical-facts.yaml", "circuit-review.yaml", "circuit_tests*.py", "tests/**/*.py", "docs/circuit-check.md", "review/circuit/evidence.json", "review/circuit/preview/*.svg"),
      "layout": ("*.kicad_pcb",),
      "verify": ("*.kicad_pcb", "*.kicad_pro", "*.kicad_dru", "review/circuit/preview/*.svg"),
      "order": ("policy.yaml", "build/circuit/bom.json", "fab/**/*"),
      "publish": ("docs/publish.md",),
    }
    return tuple(p for p in _files(project_dir, patterns[phase]) if not any(x.endswith("backups") for x in p.parts))



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
                "sha256": hashlib.sha256(evidence_bytes(path)).hexdigest(),
            }
        )
    return semantics


def _implementation_source_semantics(project_dir):
    return _file_semantics(project_dir, _files(project_dir, ("firmware/*.ioc",)))



def _board_phase_semantics(path: Path, phase: str) -> Mapping[str, Any]:
    try:
        from pcbforge.circuit_evidence import _canonical_tokens, _top_level_blocks

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
        routing = sorted(
            _canonical_tokens(block)
            for head, block in blocks
            if head in {"segment", "arc", "via", "zone"}
        )
        if phase == "layout":
            return {"layout": layout, "routing": routing}
        if phase == "verify":
            return {"canonical_board": _canonical_tokens(text)}
    except (CircuitEvidenceError, OSError, UnicodeError, ValueError):
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
        try:
            from pcbforge.circuit import fingerprint_inputs as native_fingerprint
            payload["circuit"] = native_fingerprint(project_dir, include_spec=False)
        except (ValueError, OSError) as exc:
            payload["circuit"] = f"<invalid:{exc}>"
        payload["artifacts"] = []
    if phase in {"layout", "verify"}:
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


def _static_evidence(project_dir, spec, document, phase):
    if phase == "spec":
        return True, "valid requirements", True
    if phase == "architect":
        paths = [project_dir / "docs/architecture.md", project_dir / "docs/mcu.md", project_dir / f"firmware/{spec.name}.ioc"]
        missing = [p.relative_to(project_dir).as_posix() for p in paths if not p.is_file()]
        ok, detail = _current_check(project_dir, spec, document, "ioc")
        if not ok:
            missing.append(detail)
        return not missing, "; ".join(missing) or "architecture and IOC are current", any(p.is_file() for p in paths)
    if phase == "circuit":
        from pcbforge.circuit import read_evidence
        try:
            read_evidence(project_dir)
            return True, "checked native schematic is current", True
        except (ValueError, OSError) as exc:
            return False, str(exc), (project_dir / ELECTRICAL_TEST_FILENAME).is_file()
    if phase == "layout":
        exists = (project_dir / f"{spec.name}.kicad_pcb").is_file()
        return exists, "declare placement and routing complete", exists
    if phase == "verify":
        ok, detail = _current_check(project_dir, spec, document, "drc")
        return ok, detail, "drc" in document.checks
    if phase == "order":
        ok = _current_sourcing_confirmation(project_dir, document)
        return ok, "current sourcing confirmation" if ok else "confirm the post-FAB sourcing snapshot", False
    return True, "publish or explicitly skip reusable native circuits", False



def _latest_events(
    events: Sequence[StatusEvent],
) -> tuple[dict[str, tuple[int, StatusEvent]], dict[str, int]]:
    latest: dict[str, tuple[int, StatusEvent]] = {}
    latest_reopen: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.action in ANNOTATION_ACTIONS:
            continue
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
    if _project_pins(project_dir).get("schema") != 2:
        return False
    spec = read_spec(project_dir / "spec.md")
    return all((project_dir / name).is_file() for name in (
        ".pcbforge", f"{spec.name}.kicad_sch", f"{spec.name}.kicad_pcb",
        f"{spec.name}.kicad_pro", "circuit-tests.yaml", "electrical-facts.yaml",
        "circuit-review.yaml"))



def _layout_handoff_fingerprint(
    project_dir: Path,
    document: StatusDocument,
) -> str:
    return _payload_fingerprint(
        _layout_handoff_payload(project_dir, document)
    )


def _layout_handoff_payload(project_dir, document):
    latest, _ = _latest_events(document.events)
    circuit = latest.get("circuit")
    return {"approval_schema": 2, "transition": "layout-handoff",
            "source_phase": "circuit", "target_phase": "layout",
            "circuit_approval": circuit[1].approval_fingerprint if circuit and circuit[1].action == "complete" else "",
            "handoff": brief_status_fingerprint(project_dir, include_circuit=False),
            "checks": [{"name": name, "required_outcome": "pass"} for name in ("circuit", "layout-handoff", "policy")]}



def layout_assist_is_authorized(project_dir: Path) -> bool:
    """Whether an explicit spatial-edit request may proceed right now.

    True only inside an open LAYOUT whose handoff approval is still current --
    the same condition that gates `status mark layout ai-assisted`. The spatial
    edit commands call this rather than re-deriving it, so a change to what
    "current" means can never leave them out of step.
    """
    try:
        project_dir = _project_dir(Path(project_dir))
        document = read_status_document(project_dir)
        report = inspect_status(project_dir, document=document)
    except (StatusError, InitInputError, OSError):
        return False
    if report.current is None or report.current.phase.key != "layout":
        return False
    return _current_layout_handoff(project_dir, document) is not None


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
                for name in ("circuit", "layout-handoff", "policy")
            )
        ):
            return None
        current = _layout_handoff_fingerprint(project_dir, document)
    except (PlacementError, StatusError, OSError):
        return None
    return event if event.approval_fingerprint == current else None


def _architecture_baseline_payload(
    project_dir: Path,
    document: StatusDocument,
) -> Mapping[str, Any]:
    proposal = next(
        (
            event
            for event in reversed(document.events)
            if event.phase == "architect" and event.action == "proposal-approved"
        ),
        None,
    )
    baseline = project_dir / BASELINE_PATH
    return {
        "transition_schema": 1,
        "transition": "architecture-baseline",
        "source_phase": "architect",
        "target_phase": "circuit",
        "proposal_content_fingerprint": (
            proposal.content_fingerprint if proposal is not None else ""
        ),
        "baseline": _file_semantics(
            project_dir,
            (baseline,) if baseline.is_file() else (),
        ),
        "checks": [
            {"name": name, "required_outcome": "pass"}
            for name in ("ioc",)
        ],
    }


def _current_architecture_baseline(project_dir, document):
    event = _latest_transition_events(document.transition_events).get("architecture-baseline")
    if event is None or event.action != "complete" or not event.content_fingerprint:
        return None
    try:
        spec = read_spec(project_dir / "spec.md")
        if _current_architect_proposal(project_dir, document) is None or not _current_check(project_dir, spec, document, "ioc")[0]:
            return None
        if not baseline_is_current(project_dir)[0]:
            return None
        return event if event.content_fingerprint == _payload_fingerprint(_architecture_baseline_payload(project_dir, document)) else None
    except (ValueError, OSError, StatusError):
        return None



def _fab_artifact_paths(project_dir: Path) -> tuple[Path, ...]:
    fab = project_dir / "fab"
    if not fab.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in fab.rglob("*")
            if path.is_file() and path.name != ".gitkeep"
        )
    )


def _fab_out_payload(
    project_dir: Path,
    document: StatusDocument,
) -> Mapping[str, Any]:
    verify = next(
        (
            event
            for event in reversed(document.events)
            if event.phase == "verify" and event.action == "complete"
        ),
        None,
    )
    return {
        "transition_schema": 1,
        "transition": "fab-out",
        "source_phase": "verify",
        "target_phase": "order",
        "verify_content_fingerprint": (
            verify.content_fingerprint if verify is not None else ""
        ),
        "artifacts": _file_semantics(project_dir, _fab_artifact_paths(project_dir)),
    }


def _current_fab_out(
    project_dir: Path,
    document: StatusDocument,
) -> TransitionEvent | None:
    event = _latest_transition_events(document.transition_events).get("fab-out")
    if event is None or event.action != "complete" or not event.content_fingerprint:
        return None
    latest, _ = _latest_events(document.events)
    verify_info = latest.get("verify")
    if (
        verify_info is None
        or verify_info[1].action != "complete"
        or not _approval_is_current(
            project_dir,
            "verify",
            verify_info[1],
            document,
        )
        or not _fab_artifact_paths(project_dir)
    ):
        return None
    try:
        current = _payload_fingerprint(_fab_out_payload(project_dir, document))
    except (StatusError, OSError):
        return None
    return event if event.content_fingerprint == current else None


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


def _derive_phases(project_dir, spec, document):
    latest, reopens = _latest_events(document.events)
    results = []
    invalidated = -1
    for phase in PHASES:
        info = latest.get(phase.key)
        index, event = info if info else (-1, None)
        invalidated = max(invalidated, reopens.get(phase.key, -1))
        predecessors = all(p.complete for p in results if p.phase.required)
        evidence, detail, partial = _static_evidence(project_dir, spec, document, phase.key)
        current = event is not None and event.action == "complete" and index > invalidated and _approval_is_current(project_dir, phase.key, event, document)
        if phase.key == "architect":
            current = _current_architecture_baseline(project_dir, document) is not None
            predecessors = predecessors and _initialization_transition_complete(project_dir)
        elif phase.key == "circuit":
            from pcbforge.pcb_update import sync_is_current
            synced = bool(current and sync_is_current(project_dir, event.approval_fingerprint, document))
            if current and not synced:
                detail = "schematic approved; prepare the native PCB update, update in KiCad, then run finish-circuit"
            current = synced
        elif phase.key == "layout":
            predecessors = predecessors and _current_layout_handoff(project_dir, document) is not None
        elif phase.key == "order":
            predecessors = predecessors and _current_fab_out(project_dir, document) is not None
        complete = bool(current and predecessors and evidence)
        if phase.key == "publish" and event and event.action == "skipped":
            complete = predecessors
            state = "Skipped" if complete else "Not started"
        elif complete:
            state = "Complete"
        elif not predecessors:
            state, detail = "Not started", "waiting for the preceding phase or transition"
        elif event and event.action in {"blocked", "reopened"}:
            state, detail = "Blocked", event.note
        elif event and event.action == "complete" and index <= invalidated:
            state, detail = "Blocked", "approval is stale after an upstream reopen"
        elif phase.key == "circuit" and event and event.action == "complete" and _approval_is_current(project_dir, phase.key, event, document):
            state = "In progress"
        elif event and event.action == "complete":
            state, detail = "Stale", "approval is stale: approved content or upstream evidence changed"
        elif phase.key == "architect":
            proposal = _current_architect_proposal(project_dir, document)
            state = "Ready" if proposal and evidence else "In progress" if proposal else "Awaiting approval"
            if not proposal:
                detail = "review the architecture and exact MCU proposal"
                if _architecture_source_started(project_dir) and not _architect_proposal_was_approved(document):
                    state, detail = "Blocked", "architecture source exists before current proposal approval"
                elif _architect_proposal_was_approved(document):
                    state, detail = "Blocked", "architecture approval is stale; review the changed proposal"

        elif phase.key == "spec" and evidence and "policy" not in document.checks:
            state, detail = "Awaiting approval", "review the requirements and policy baseline"
        elif evidence and all(_current_check(project_dir, spec, document, n)[0] for n in APPROVAL_CHECKS.get(phase.key, ())):
            state = "Awaiting approval"
        else:
            state = "In progress" if partial else "Ready"
        results.append(PhaseResult(phase, state, detail, complete))
    return tuple(results)



def _action_for(result, project_dir, document):
    if result is None:
        return None
    phase = result.phase.key
    if phase == "circuit":
        latest, _ = _latest_events(document.events)
        info = latest.get("circuit")
        if info and info[1].action == "complete" and _approval_is_current(project_dir, "circuit", info[1], document):
            pending = (project_dir / "review/circuit/pcb-update.json").is_file()
            return NextAction("User + tool", "Update PCB from Schematic in KiCad, save the board, then verify." if pending else "Prepare the approved native PCB update.",
                              "pcbforge finish-circuit" if pending else "pcbforge prepare-pcb-update")
    if phase == "architect":
        if _current_architect_proposal(project_dir, document):
            return NextAction("AI + tool", "Validate the IOC and record the architecture baseline.", "pcbforge finish-architect")
        return NextAction("AI + user", "Present the architecture and exact MCU proposal.", "pcbforge status review architect --stage proposal")
    if result.state == "Awaiting approval":
        return NextAction("User", "Review and approve the current phase packet.", f"pcbforge status review {phase}")
    return NextAction(result.phase.lead, result.detail, "pcbforge status --check --write")



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

    baseline_event = latest.get("architecture-baseline")
    baseline_performed = any(
        event.transition == "architecture-baseline"
        and event.action in {"complete", "reopened"}
        for event in document.transition_events
    )
    baseline_current = _current_architecture_baseline(project_dir, document)
    if baseline_current is not None:
        baseline_state = "Complete"
        baseline_detail = "checked architecture source baseline is current"
    elif not by_phase["spec"].complete or not initialized:
        baseline_state = "Not started"
        baseline_detail = "waiting for the initialized ARCHITECT phase"
    elif baseline_event is not None and baseline_event.action == "blocked":
        baseline_state = "Blocked"
        baseline_detail = baseline_event.note
    elif baseline_performed:
        baseline_state = "Stale"
        baseline_detail = (
            "architecture baseline is stale because proposal, source, or checks changed"
        )
    elif by_phase["architect"].state == "Ready":
        baseline_state = "Ready"
        baseline_detail = "run `pcbforge finish-architect`"
    else:
        baseline_state = "Not started"
        baseline_detail = "waiting for current ARCHITECT proposal and checks"

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
        required_checks = ("circuit", "layout-handoff", "policy")
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

    fab_event = latest.get("fab-out")
    fab_performed = any(
        event.transition == "fab-out"
        and event.action in {"complete", "reopened"}
        for event in document.transition_events
    )
    fab_current = _current_fab_out(project_dir, document)
    if fab_current is not None:
        fab_state = "Complete"
        fab_detail = "validated fabrication package is current"
    elif not by_phase["verify"].complete:
        fab_state = "Inactive" if fab_performed else "Not started"
        fab_detail = "waiting for current VERIFY approval"
    elif fab_event is not None and fab_event.action == "blocked":
        fab_state = "Blocked"
        fab_detail = fab_event.note
    elif fab_performed:
        fab_state = "Stale"
        fab_detail = "fabrication outputs changed after the packet was recorded"
    elif _fab_artifact_paths(project_dir):
        fab_state = "Ready"
        fab_detail = "fabrication outputs are present and await generator validation"
    else:
        fab_state = "Ready"
        fab_detail = "generate and validate the fabrication package"

    from pcbforge.pcb_update import sync_is_current
    circuit_info = _latest_events(document.events)[0].get("circuit")
    circuit_event = circuit_info[1] if circuit_info else None
    sync_performed = "circuit-sync" in latest
    schematic_approved = circuit_event is not None and circuit_event.action == "complete" and _approval_is_current(project_dir, "circuit", circuit_event, document)
    sync_current = schematic_approved and sync_is_current(project_dir, circuit_event.approval_fingerprint, document)
    sync_state = "Complete" if sync_current else "Ready" if schematic_approved else "Inactive" if sync_performed else "Not started"
    sync_detail = "PCB matches the approved schematic" if sync_current else "Prepare, perform and verify the native PCB update" if schematic_approved else "Waiting for current schematic approval"
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
            "architecture-baseline",
            "ARCHITECT → CIRCUIT: architecture baseline",
            "architect",
            "circuit",
            "AI + tool",
            baseline_state,
            baseline_detail,
            baseline_performed,
            baseline_state == "Complete",
        ),
        TransitionResult("circuit-sync", "CIRCUIT: native PCB synchronization", "circuit", "layout",
                         "User + tool", sync_state, sync_detail, sync_performed, bool(sync_current)),
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
        TransitionResult(
            "fab-out",
            "VERIFY → ORDER: FAB-OUT",
            "verify",
            "order",
            "Tool",
            fab_state,
            fab_detail,
            fab_performed,
            fab_state == "Complete",
        ),
    )


def _transition_action(result: TransitionResult) -> NextAction:
    if result.key == "circuit-sync":
        return NextAction("User + tool", result.detail, "pcbforge prepare-pcb-update", True)
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
    if result.key == "architecture-baseline":
        return NextAction(
            "AI + tool",
            (
                f"Resolve the ARCHITECT baseline blocker: {result.detail}"
                if result.state in {"Blocked", "Stale"}
                else "Capture the checked ARCHITECT source baseline and open CIRCUIT."
            ),
            "pcbforge finish-architect",
            result.state in {"Blocked", "Stale"},
        )
    if result.key == "layout-handoff":
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
    return NextAction(
        "Tool",
        (
            f"Regenerate and validate the FAB-OUT packet: {result.detail}"
            if result.state in {"Blocked", "Stale"}
            else "Generate and validate Gerbers, drills, BOM, CPL, and archive."
        ),
        "pcbforge fab-out",
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
    compact_transition_labels = {
        "initialize": "INITIALIZE transition",
        "architecture-baseline": "ARCHITECTURE BASELINE transition",
        "layout-handoff": "LAYOUT HANDOFF transition",
        "circuit-sync": "PCB SYNC transition",
        "fab-out": "FAB-OUT transition",
    }
    workflow: list[tuple[str, str, bool, str, str, bool]] = []
    for result in phases:
        for transition in (t for t in transitions if t.target_phase == result.phase.key):
            compact_label = compact_transition_labels[transition.key]
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
        current_label = compact_transition_labels[current_transition.key]
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
        if record.outcome != "fail" or name in ADVISORY_CHECKS:
            continue
        if _check_is_spent(project_dir, document, name):
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
        except (CircuitEvidenceError, PlacementError, PolicyError, OSError):
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


def _drc_report_status(report: Path) -> tuple[bool, str]:
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except OSError as exc:
        return False, f"could not read DRC report: {exc}"
    except json.JSONDecodeError as exc:
        return False, f"invalid DRC report JSON: {exc.msg}"

    if not isinstance(payload, dict):
        return False, "invalid DRC report JSON: expected an object"

    categories = ("violations", "unconnected_items", "schematic_parity")
    active: dict[str, int] = {}
    exclusions = 0

    for category in categories:
        findings = payload.get(category)
        if not isinstance(findings, list):
            return False, f"invalid DRC report JSON: {category} must be a list"

        active[category] = 0
        for finding in findings:
            if not isinstance(finding, dict):
                return False, (
                    f"invalid DRC report JSON: {category} entries must be objects"
                )
            if finding.get("excluded") is True:
                exclusions += 1
            else:
                active[category] += 1

    active_total = sum(active.values())
    summary = (
        f"{active_total} active DRC findings "
        f"({active['violations']} violations, "
        f"{active['unconnected_items']} unconnected, "
        f"{active['schematic_parity']} parity), "
        f"{exclusions} exclusions"
    )
    return active_total == 0, summary


def _layout_is_complete(project_dir: Path, document: StatusDocument) -> bool:
    latest, _ = _latest_events(document.events)
    event = latest.get("layout")
    return (
        event is not None
        and event[1].action == "complete"
        and _approval_is_current(project_dir, "layout", event[1], document)
    )


def run_status_checks(project_dir, document, *, tool_root=None, runner=subprocess.run, checked_at=None, write_reports=False, force_checks=False):
    from pcbforge.circuit import check_circuit, read_evidence
    project_dir = _project_dir(project_dir)
    spec = read_spec(project_dir / "spec.md")
    checked_at = checked_at or _now()
    checks = {k: v for k,v in document.checks.items() if k in {"policy","ioc","circuit","layout-handoff","placement","drc","fab"}}
    current = next((p.phase.key for p in _derive_phases(project_dir,spec,document) if not p.complete and p.phase.required), "verify")
    base, exceptions, _ = _policy_approval_context(document)
    jobs = {"policy": lambda: check_policy(project_dir, tool_root=tool_root, through_phase=current, baseline_approval=base, exception_approvals=exceptions)}
    if (project_dir / f"firmware/{spec.name}.ioc").is_file():
        jobs["ioc"] = lambda: check_ioc(project_dir, tool_root=tool_root, runner=runner)
    if _current_architecture_baseline(project_dir, document) is not None:
        jobs["circuit"] = lambda: check_circuit(project_dir, tool_root=tool_root, runner=runner, write_report=write_reports)
    if (project_dir / PLACEMENT_FILENAME).is_file():
        jobs["layout-handoff"] = lambda: check_brief(project_dir, tool_root=tool_root)
        jobs["placement"] = lambda: check_placement(project_dir, tool_root=tool_root)
    if _layout_is_complete(project_dir, document):
        def drc():
            with tempfile.TemporaryDirectory(prefix="pcbforge-drc-") as tmp:
                output = Path(tmp) / "drc.json"
                result = runner([str((tool_root or Path(__file__).resolve().parent.parent) / "scripts/kicad-cli"), "pcb", "drc", "--schematic-parity", "--format", "json", "--severity-all", "--output", str(output), str(project_dir / f"{spec.name}.kicad_pcb")], cwd=project_dir, capture_output=True, text=True, check=False)
                if result.returncode or not output.is_file():
                    raise StatusCheckError("KiCad DRC did not produce a successful report")
                ok, summary = _drc_report_status(output)
                if not ok:
                    raise StatusCheckError(summary)
                from types import SimpleNamespace
                return SimpleNamespace(summary=summary)
        jobs["drc"] = drc
    if (project_dir / "fab/manifest.json").is_file():
        from pcbforge.fab import check_fab
        jobs["fab"] = lambda: check_fab(project_dir, tool_root=tool_root)
    for name, callback in jobs.items():
        inputs = _check_inputs(project_dir, spec, name)
        try:
            fingerprint = _check_fingerprint(project_dir, name, inputs, tool_root=tool_root)
        except (ValueError, OSError):
            fingerprint = _fingerprint(project_dir, inputs)
        prior = checks.get(name)
        reusable = prior and prior.outcome == "pass" and prior.fingerprint == fingerprint and not force_checks
        if reusable and name == "policy" and PHASE_NUMBER.get(prior.scope, 0) < PHASE_NUMBER[current]:
            reusable = False
        if reusable and name == "circuit":
            try:
                read_evidence(project_dir, require_presentation=True)
            except (ValueError, OSError):
                reusable = False
        if reusable:
            continue
        try:
            result = callback()
            ok = getattr(result, "ok", True)
            summary = getattr(result, "summary", "check passed")
        except (ValueError, RuntimeError, OSError) as exc:
            ok, summary = False, str(exc)
        checks[name] = CheckRecord(checked_at, fingerprint, "pass" if ok else "fail", summary, current if name == "policy" else "")
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
    if phase == "architect":
        raise StatusInputError(
            "ARCHITECT finalization is automatic; run `pcbforge finish-architect`"
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
    checked = _invalidate_stale_approvals(
        project_dir,
        checked,
        at=checked_at,
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
    if phase == "layout" and _current_layout_handoff(project_dir, checked) is None:
        failures.append("current CIRCUIT → LAYOUT handoff approval is missing")
    if phase == "order" and _current_fab_out(project_dir, checked) is None:
        failures.append("current VERIFY → ORDER FAB-OUT transition is missing")
    if (
        phase != "spec"
        and not _current_policy_baseline(project_dir, checked)
    ):
        failures.append("project policy is not bound to the approved SPEC")

    artifacts = tuple(
        path.relative_to(project_dir).as_posix()
        for path in _phase_review_artifact_paths(project_dir, spec, phase)
    )
    notes = tuple(
        f"{event.at}: {event.note}"
        for event in checked.events
        if event.phase == phase and event.action == "ai-assisted"
    )
    fingerprint = _approval_fingerprint(
        project_dir,
        phase,
        "complete",
        checked,
    )
    if phase == "circuit":
        from pcbforge.circuit import read_evidence, presentation_fingerprint
        try:
            read_evidence(project_dir, require_presentation=True)
        except (ValueError, OSError) as exc:
            failures.append(str(exc))
        fingerprint = _payload_fingerprint({"electrical_approval": fingerprint, "presentation": presentation_fingerprint(project_dir)})
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
            notes=notes,
        ),
        checked,
    )


def _prepare_proposal_review(
    project_dir: Path,
    phase: str,
    *,
    document: StatusDocument | None = None,
    tool_root: Path | None,
    runner: CommandRunner,
    checked_at: str | None,
) -> tuple[PhaseReview, StatusDocument]:
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    proposal_phases = {"architect"}
    if phase not in proposal_phases:
        raise StatusInputError(
            "proposal review is only valid for architect or circuit"
        )
    document = (
        document
        if document is not None
        else read_status_document(project_dir)
    )
    failures: list[str] = []
    checks: list[PhaseReviewCheck] = []

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
    document = _invalidate_stale_approvals(
        project_dir,
        document,
        at=checked_at,
    )
    report = inspect_status(project_dir, document=document)
    target_index = _phase_number(project_dir, phase) - 1
    predecessors = [
        result for result in report.phases[:target_index] if result.phase.required
    ]
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
    document: StatusDocument | None = None,
    tool_root: Path | None,
    runner: CommandRunner,
    checked_at: str | None,
) -> tuple[PhaseReview, StatusDocument]:
    project_dir = _project_dir(project_dir)
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
    checked = _invalidate_stale_approvals(
        project_dir,
        checked,
        at=checked_at,
    )
    report = inspect_status(project_dir, document=checked)
    circuit = next(
        result for result in report.phases if result.phase.key == "circuit"
    )
    failures: list[str] = []
    if not circuit.complete:
        failures.append("CIRCUIT is not complete")
    if _current_layout_handoff(project_dir, checked) is not None:
        failures.append("LAYOUT handoff is already approved")
    spec = report.spec
    check_reviews: list[PhaseReviewCheck] = []
    for name in ("circuit", "layout-handoff", "policy"):
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
            project_dir / "circuit-tests.yaml",
            project_dir / "docs" / "circuit-check.md",
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
        if phase.key == "architect":
            gates.append(
                _ApprovalGate(
                    f"{phase.key}:proposal",
                    f"{phase.label} proposal",
                    phase.key,
                    "proposal-approved",
                    "proposal",
                )
            )
        if phase.key != "architect":
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
        return ()
    if gate.stage == "handoff":
        return ("circuit", "layout-handoff", "policy")
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
        if name == "policy" and record and PHASE_NUMBER.get(record.scope, 0) < PHASE_NUMBER[gate.phase]:
            current, detail = False, f"policy check does not cover {gate.phase}; refresh checks"

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


def _record_review(
    project_dir: Path,
    document: StatusDocument,
    key: str,
    fingerprint: str,
    *,
    at: str,
) -> StatusResult:
    if key not in REVIEW_KEYS:
        raise StatusInputError(f"unsupported review key {key!r}")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise StatusInputError("cannot save a review without a valid fingerprint")
    return write_status(
        project_dir,
        now=at,
        document=replace(
            document,
            reviews={
                **document.reviews,
                key: ReviewRecord(at, fingerprint),
            },
        ),
    )


def prepare_cascade_review(
    project_dir: Path,
    document: StatusDocument | None = None,
    *,
    record: bool = False,
    reviewed_at: str | None = None,
) -> CascadeReview:
    """Prove which stale approvals can be renewed without full ceremony."""
    project_dir = _project_dir(project_dir)
    document = document if document is not None else read_status_document(project_dir)
    event_time = reviewed_at or _now()
    document = _invalidate_stale_approvals(
        project_dir,
        document,
        at=event_time,
    )
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
            CircuitEvidenceError,
            ArchitectureBaselineError,
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
            CircuitEvidenceError,
            ArchitectureBaselineError,
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
        automatic_failure = ""
        if (
            _phase_number(project_dir, gate.phase)
            >= _phase_number(project_dir, "circuit")
            and _current_architecture_baseline(project_dir, provisional) is None
        ):
            automatic_failure = (
                "ARCHITECT → CIRCUIT automatic baseline is not current; "
                "rerun finish-architect before downstream renewal"
            )
        if (
            _phase_number(project_dir, gate.phase)
            >= _phase_number(project_dir, "order")
            and _current_fab_out(project_dir, provisional) is None
        ):
            automatic_failure = (
                "VERIFY → ORDER FAB-OUT transition is not current; regenerate "
                "and validate fabrication outputs before downstream renewal"
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
        elif automatic_failure:
            classification = "blocked"
            detail = automatic_failure
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
    review = CascadeReview(
        project_dir,
        ready,
        detail,
        gates[root_index].key,
        changed_slices,
        unchanged_slices,
        tuple(reviews),
        fingerprint,
    )
    if record and review.ready:
        _record_review(
            project_dir,
            document,
            "cascade",
            review.fingerprint,
            at=event_time,
        )
    return review


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
            "next: present this packet, wait for explicit user approval, then run "
            f"`pcbforge status renew --last-reviewed --note \"<approval>\"`"
        )
    return "\n".join(lines)


def _validate_review_selector(
    expected_fingerprint: str | None,
    *,
    last_reviewed: bool,
) -> None:
    if expected_fingerprint is not None and last_reviewed:
        raise StatusInputError(
            "--fingerprint and --last-reviewed are mutually exclusive"
        )
    if expected_fingerprint is None and not last_reviewed:
        raise StatusInputError("one of --fingerprint or --last-reviewed is required")


def _resolve_review_fingerprint(
    document: StatusDocument,
    key: str,
    expected_fingerprint: str | None,
    *,
    last_reviewed: bool,
    current_fingerprint: str,
) -> str:
    _validate_review_selector(
        expected_fingerprint,
        last_reviewed=last_reviewed,
    )
    if last_reviewed:
        record = document.reviews.get(key)
        if record is None:
            raise StatusInputError(
                f"no saved review for {key}; rerun status review"
            )
        if record.fingerprint != current_fingerprint:
            raise StatusInputError(
                "artifacts changed since review; rerun status review"
            )
        return record.fingerprint
    fingerprint = (expected_fingerprint or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise StatusInputError("--fingerprint must be a lowercase SHA-256 value")
    return fingerprint


def renew_cascade(
    project_dir: Path,
    expected_fingerprint: str | None,
    note: str,
    *,
    last_reviewed: bool = False,
    now: str | None = None,
) -> StatusResult:
    """Record one explicit decision across an eligible approval cascade."""
    project_dir = _project_dir(project_dir)
    note = note.strip()
    if not note:
        raise StatusInputError("--note must be a non-empty approval explanation")
    _validate_review_selector(
        expected_fingerprint,
        last_reviewed=last_reviewed,
    )
    event_time = now or _now()
    document = read_status_document(project_dir)
    document = _invalidate_stale_approvals(
        project_dir,
        document,
        at=event_time,
    )
    review = prepare_cascade_review(
        project_dir,
        document,
        reviewed_at=event_time,
    )
    expected = _resolve_review_fingerprint(
        document,
        "cascade",
        expected_fingerprint,
        last_reviewed=last_reviewed,
        current_fingerprint=review.fingerprint,
    )
    if review.fingerprint != expected:
        raise StatusInputError(
            "cannot renew cascade: reviewed fingerprint is stale or does not "
            "match current evidence"
        )
    if not review.ready:
        raise StatusInputError(f"cannot renew cascade: {review.detail}")
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
    """Build and save a deterministic phase packet for user review."""
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    event_time = checked_at or _now()
    document = read_status_document(project_dir)
    if stage == "handoff":
        if phase != "layout":
            raise StatusInputError(
                "handoff review is only valid for layout"
            )
        review, review_document = _prepare_layout_handoff_review(
            project_dir,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage == "proposal":
        review, review_document = _prepare_proposal_review(
            project_dir,
            phase,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage != "final":
        raise StatusInputError("stage must be proposal, handoff, or final")
    else:
        review, review_document = _prepare_phase_review(
            project_dir,
            phase,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    if review.ready:
        _record_review(
            project_dir,
            review_document,
            _review_key(phase, stage),
            review.fingerprint,
            at=event_time,
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
    if review.notes:
        lines.append("user-requested AI spatial work in this phase:")
        lines.extend(f"  - {note}" for note in review.notes)
    lines.append(f"approval fingerprint: {review.fingerprint}")
    if review.ready:
        stage = (
            f" --stage {review.stage}"
            if review.stage in {"proposal", "handoff"}
            else ""
        )
        lines.append(
            "next: present this packet, wait for explicit user approval, then run "
            f"`pcbforge status approve {review.phase.key}{stage} "
            "--last-reviewed --note \"<approval>\"`"
        )
    return "\n".join(lines)


def approve_phase(
    project_dir: Path,
    phase: str,
    expected_fingerprint: str | None,
    note: str,
    *,
    stage: str = "final",
    last_reviewed: bool = False,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
) -> StatusResult:
    """Record an explicit user approval of the exact reviewed phase packet."""
    project_dir = _project_dir(project_dir)
    phase = phase.lower()
    note = note.strip()
    if not note:
        raise StatusInputError("--note must be a non-empty approval explanation")
    _validate_review_selector(
        expected_fingerprint,
        last_reviewed=last_reviewed,
    )
    event_time = now or _now()
    document = read_status_document(project_dir)
    if stage == "handoff":
        if phase != "layout":
            raise StatusInputError(
                "handoff approval is only valid for layout"
            )
        review, checked = _prepare_layout_handoff_review(
            project_dir,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage == "proposal":
        review, checked = _prepare_proposal_review(
            project_dir,
            phase,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    elif stage == "final":
        review, checked = _prepare_phase_review(
            project_dir,
            phase,
            document=document,
            tool_root=tool_root,
            runner=runner,
            checked_at=event_time,
        )
    else:
        raise StatusInputError("stage must be proposal, handoff, or final")
    expected = _resolve_review_fingerprint(
        checked,
        _review_key(phase, stage),
        expected_fingerprint,
        last_reviewed=last_reviewed,
        current_fingerprint=review.fingerprint,
    )
    if not review.ready:
        raise StatusInputError(
            f"cannot approve {phase}: {review.detail}"
        )
    if review.fingerprint != expected:
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
            approval_fingerprint=_payload_fingerprint(payload),
            content_fingerprint=_content_fingerprint(payload),
        )
        checked = replace(checked, events=(*checked.events, event))
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
        "reviews": {
            key: {
                "at": record.at,
                "fingerprint": record.fingerprint,
            }
            for key, record in sorted(document.reviews.items())
        },
        "checks": {
            name: {
                "at": record.at,
                "fingerprint": record.fingerprint,
                "outcome": record.outcome,
                "summary": record.summary,
                "scope": record.scope,
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
        if record.outcome != "fail" or name in ADVISORY_CHECKS:
            continue
        if _check_is_spent(report.project_dir, report.document, name):
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
    for result in report.phases:
        for transition in (t for t in report.transitions if t.target_phase == result.phase.key):
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
        for name in sorted(ADVISORY_CHECKS):
            record = report.document.checks.get(name)
            if record is None or _advisory_phase(name) != result.phase.key:
                continue
            rows.append(
                "| "
                + " | ".join(
                    (
                        "·",
                        f"{name} check",
                        "Tool",
                        "⚠️ Advisory",
                        _escape(record.summary),
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
    body = f"""# {report.spec.name} project dashboard

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
    return body.rstrip() + metadata_trailer(metadata)


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
        event_info = latest.get(phase.key)
        if event_info is None:
            continue
        event = event_info[1]
        if (
            phase.key not in APPROVAL_BOUND_PHASES
            and event.action != "proposal-approved"
        ):
            continue
        if event.action not in {"complete", "proposal-approved"}:
            continue
        try:
            approval_current = _approval_is_current(
                project_dir,
                phase.key,
                event,
                document,
            )
        except (ArchitectureBaselineError, OSError):
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
    architecture = latest_transitions.get("architecture-baseline")
    if architecture is not None and architecture.action == "complete":
        architecture_stale = not (project_dir / BASELINE_PATH).is_file()
        try:
            proposal = next(
                (
                    event
                    for event in reversed(document.events)
                    if event.phase == "architect"
                    and event.action == "proposal-approved"
                ),
                None,
            )
            proposal_content = _content_fingerprint(
                _approval_payload(
                    project_dir,
                    "architect",
                    "proposal-approved",
                    document,
                )
            )
            architecture_stale = architecture_stale or (
                proposal is None
                or proposal.content_fingerprint != proposal_content
                or architecture.content_fingerprint
                != _payload_fingerprint(
                    _architecture_baseline_payload(project_dir, document)
                )
            )
            baseline_ok, _ = baseline_is_current(project_dir)
            architecture_stale = architecture_stale or not baseline_ok
        except (CircuitEvidenceError, ArchitectureBaselineError, StatusError, OSError):
            architecture_stale = True
        if architecture_stale:
            transition_invalidations.append(
                TransitionEvent(
                    at or _now(),
                    "architecture-baseline",
                    "reopened",
                    (
                        "Automatic transition invalidated because ARCHITECT "
                        "proposal or baseline content changed"
                    ),
                )
            )
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
    fab_out = latest_transitions.get("fab-out")
    if fab_out is not None and fab_out.action == "complete":
        try:
            fab_stale = (
                not _fab_artifact_paths(project_dir)
                or fab_out.content_fingerprint
                != _payload_fingerprint(_fab_out_payload(project_dir, document))
            )
        except (StatusError, OSError):
            fab_stale = True
        if fab_stale:
            transition_invalidations.append(
                TransitionEvent(
                    at or _now(),
                    "fab-out",
                    "reopened",
                    (
                        "Automatic transition invalidated because fabrication "
                        "outputs changed"
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


def finish_architect(
    project_dir: Path,
    *,
    tool_root: Path | None = None,
    runner: CommandRunner = subprocess.run,
    now: str | None = None,
) -> StatusResult:
    """Capture the checked ARCHITECT baseline and open CIRCUIT."""
    project_dir = _project_dir(project_dir)
    document = read_status_document(project_dir)
    if _current_architecture_baseline(project_dir, document) is not None:
        raise StatusInputError("architecture baseline is already current")
    event_time = now or _now()
    spec = read_spec(project_dir / "spec.md")
    board = project_dir / f"{spec.name}.kicad_pcb"
    failures: list[str] = []
    before = None
    try:
        before = read_board_evidence(board)
    except CircuitEvidenceError as exc:
        failures.append(str(exc).splitlines()[0])
    checked = run_status_checks(
        project_dir,
        document,
        tool_root=tool_root,
        runner=runner,
        checked_at=event_time,
    )
    after = None
    try:
        after = read_board_evidence(board)
    except CircuitEvidenceError as exc:
        failures.append(str(exc).splitlines()[0])
    report = inspect_status(project_dir, document=checked)
    spec_phase = next(item for item in report.phases if item.phase.key == "spec")
    if not spec_phase.complete or not _initialization_transition_complete(project_dir):
        failures.append("SPEC and initialization must be complete")
    if _current_architect_proposal(project_dir, checked) is None:
        failures.append("current architecture proposal approval is missing")
    evidence_ok, evidence_detail, _ = _static_evidence(
        project_dir,
        spec,
        checked,
        "architect",
    )
    if not evidence_ok:
        failures.append(evidence_detail)
    for name in ("ioc",):
        current, detail = _current_check(project_dir, spec, checked, name)
        if not current:
            failures.append(detail)
    if before is not None and after is not None:
        failures.extend(_spatial_errors(before, after))
    if not failures:
        try:
            capture_implementation_baseline(project_dir)
        except (ArchitectureBaselineError, OSError) as exc:
            failures.append(str(exc).splitlines()[0])
    if failures:
        note = "; ".join(dict.fromkeys(failures))
        blocked = TransitionEvent(
            event_time,
            "architecture-baseline",
            "blocked",
            note,
        )
        write_status(
            project_dir,
            tool_root=tool_root,
            runner=runner,
            now=event_time,
            document=replace(
                checked,
                transition_events=(*checked.transition_events, blocked),
            ),
        )
        raise StatusCheckError(f"cannot finish ARCHITECT: {note}")
    payload = _architecture_baseline_payload(project_dir, checked)
    event = TransitionEvent(
        event_time,
        "architecture-baseline",
        "complete",
        "Architecture checks passed and source baseline was captured",
        content_fingerprint=_payload_fingerprint(payload),
    )
    return write_status(
        project_dir,
        tool_root=tool_root,
        runner=runner,
        now=event_time,
        document=replace(
            checked,
            transition_events=(*checked.transition_events, event),
        ),
    )


def record_fab_out_transition(
    project_dir: Path,
    *,
    note: str = "Fabrication package generated and validated",
    now: str | None = None,
) -> StatusResult:
    """Record the future fab generator's validated output transition."""
    project_dir = _project_dir(project_dir)
    note = note.strip()
    if not note:
        raise StatusInputError("FAB-OUT transition note must be non-empty")
    document = read_status_document(project_dir)
    if _current_fab_out(project_dir, document) is not None:
        raise StatusInputError("FAB-OUT transition is already current")
    report = inspect_status(project_dir, document=document)
    verify = next(item for item in report.phases if item.phase.key == "verify")
    if not verify.complete:
        raise StatusInputError("cannot record FAB-OUT before VERIFY is complete")
    if not _fab_artifact_paths(project_dir):
        raise StatusInputError("cannot record FAB-OUT without fabrication outputs")
    event_time = now or _now()
    event = TransitionEvent(
        event_time,
        "fab-out",
        "complete",
        note,
        content_fingerprint=_payload_fingerprint(
            _fab_out_payload(project_dir, document)
        ),
    )
    return write_status(
        project_dir,
        now=event_time,
        document=replace(
            document,
            transition_events=(*document.transition_events, event),
        ),
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
    if action == "ai-assisted":
        if phase_key != "layout":
            raise StatusInputError(
                "only layout records user-requested AI spatial assistance"
            )
        if _current_layout_handoff(report.project_dir, report.document) is None:
            raise StatusInputError(
                "cannot record layout ai-assisted work: the CIRCUIT → LAYOUT "
                "handoff is not currently approved"
            )
        return
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
        policy_baseline_current = _current_policy_baseline(project_dir, document)
        preproject_spec_exception = (
            phase == "spec" and not (project_dir / ".pcbforge").exists()
        )
        if not policy_baseline_current and not preproject_spec_exception:
            raise StatusInputError(
                "project policy is not bound to the approved SPEC"
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
        if _current_fab_out(project_dir, document) is None:
            raise StatusInputError(
                "cannot confirm sourcing before the FAB-OUT transition is current"
            )
        if not (project_dir / ELECTRICAL_TEST_FILENAME).is_file():
            raise StatusInputError(
                "cannot confirm sourcing without current circuit-tests.yaml"
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


def mark_status(project_dir, phase, action, note, *, tool_root=None, runner=subprocess.run, now=None):
    project_dir = _project_dir(project_dir)
    if action == "complete" and phase in PHASE_BY_KEY:
        raise StatusInputError(f"use `pcbforge status approve {phase}` after explicit user review")
    if phase not in PHASE_BY_KEY or action not in {"reopened", "blocked", "skipped", "ai-assisted"} or not note.strip():
        raise StatusInputError("use a valid phase, non-approval action and explanatory note")
    document = read_status_document(project_dir)
    if action == "skipped" and phase != "publish":
        raise StatusInputError("only PUBLISH can be skipped")
    if action == "ai-assisted" and (phase != "layout" or not layout_assist_is_authorized(project_dir)):
        raise StatusInputError("spatial assists require an open LAYOUT and current handoff")
    if action == "skipped" and not all(p.complete for p in inspect_status(project_dir,document=document).phases if p.phase.required):
        raise StatusInputError("finish required phases before skipping PUBLISH")
    event = StatusEvent(now or _now(), phase, action, note.strip())
    return write_status(project_dir, document=replace(document, events=(*document.events,event)), now=now, tool_root=tool_root, runner=runner)
