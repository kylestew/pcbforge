"""Durable, evidence-backed workflow status for pcbforge projects."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from pcbforge.initialize import InitInputError, ProjectSpec, STATUS_SCHEMA, read_spec
from pcbforge.ioc import IocProjectError, IocValidationError, check_ioc

STATUS_FILENAME = "STATUS.md"
ARCHITECTURE_MARKER = "pcbforge-architecture-diagram-schema: 1"

EVENT_ACTIONS = {"complete", "blocked", "reopened", "skipped"}
MANUAL_PHASES = {
    "spec",
    "architect",
    "mcu",
    "implement",
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
    "implement": ("build", "ioc"),
    "verify": ("build", "ioc", "drc"),
}
PHASE_EVIDENCE_CHECKS = {
    "architect": ("build",),
    "mcu": ("build", "ioc"),
    "implement": ("build",),
    "build": ("build",),
    "verify": ("drc",),
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
        "AI + tool",
        True,
        "Prepare the placement brief and board-rule guidance.",
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


@dataclass(frozen=True)
class StatusEvent:
    at: str
    phase: str
    action: str
    note: str


@dataclass(frozen=True)
class CheckRecord:
    at: str
    fingerprint: str
    outcome: str
    summary: str


@dataclass(frozen=True)
class StatusDocument:
    updated_at: str
    events: tuple[StatusEvent, ...]
    checks: Mapping[str, CheckRecord]


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
    allowed = {"pcbforge_status_schema", "updated_at", "events", "checks"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        errors.append("unknown keys: " + ", ".join(map(str, unknown)))
    if data.get("pcbforge_status_schema") != STATUS_SCHEMA:
        errors.append(f"pcbforge_status_schema: expected integer {STATUS_SCHEMA}")
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
            if set(raw) - {"at", "phase", "action", "note"}:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            phase = _text(raw.get("phase"), f"{prefix}.phase", errors)
            action = _text(raw.get("action"), f"{prefix}.action", errors)
            note = _text(raw.get("note"), f"{prefix}.note", errors)
            if phase and phase not in PHASE_BY_KEY:
                errors.append(f"{prefix}.phase: unknown phase {phase!r}")
            if action and action not in EVENT_ACTIONS:
                errors.append(f"{prefix}.action: unknown action {action!r}")
            events.append(StatusEvent(at=at, phase=phase, action=action, note=note))

    checks_raw = data.get("checks", {})
    checks: dict[str, CheckRecord] = {}
    if not isinstance(checks_raw, dict):
        errors.append("checks: expected a mapping")
    else:
        for name, raw in checks_raw.items():
            prefix = f"checks.{name}"
            if name not in {"build", "ioc", "drc"}:
                errors.append(f"{prefix}: unknown check")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            if set(raw) - {"at", "fingerprint", "outcome", "summary"}:
                errors.append(f"{prefix}: contains unknown keys")
            at = _text(raw.get("at"), f"{prefix}.at", errors)
            fingerprint = _text(
                raw.get("fingerprint"), f"{prefix}.fingerprint", errors
            )
            outcome = _text(raw.get("outcome"), f"{prefix}.outcome", errors)
            summary = _text(raw.get("summary"), f"{prefix}.summary", errors)
            if outcome and outcome not in {"pass", "fail"}:
                errors.append(f"{prefix}.outcome: expected 'pass' or 'fail'")
            checks[name] = CheckRecord(at, fingerprint, outcome, summary)

    if errors:
        raise StatusInputError(
            f"invalid {STATUS_FILENAME} frontmatter:\n  - " + "\n  - ".join(errors)
        )
    return StatusDocument(updated_at=updated_at, events=tuple(events), checks=checks)


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
    inputs = _check_inputs(project_dir, spec, name)
    if not inputs:
        return False, f"{name} inputs are missing"
    if record.fingerprint != _fingerprint(project_dir, inputs):
        return False, f"{name} result is stale"
    if record.outcome != "pass":
        return False, f"{name} failed: {record.summary}"
    return True, f"{name} passed"


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
        diagram_ok = (
            diagram.is_file()
            and ARCHITECTURE_MARKER in _read_text(diagram)
        )
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
        build_ok, detail = _current_check(project_dir, spec, document, "build")
        modules = _files(project_dir, ("src/modules/*.ato",))
        satisfied = build_ok and bool(modules)
        return (
            satisfied,
            "module sources and current build present"
            if satisfied
            else ("missing project module sources" if not modules else detail),
            bool(modules),
        )
    if phase == "build":
        ok, detail = _current_check(project_dir, spec, document, "build")
        return ok, detail, "build" in document.checks
    if phase == "brief":
        brief = project_dir / "brief.md"
        present = brief.is_file() and bool(_read_text(brief).strip())
        return (
            present,
            "brief.md present" if present else "missing brief.md",
            brief.exists(),
        )
    if phase in {"layout", "route"}:
        if not board.is_file():
            return False, f"missing {board.name}", False
        text = _read_text(board)
        footprints = text.count("(footprint ")
        routes = (
            text.count("(segment ")
            + text.count("(arc ")
            + text.count("(via ")
        )
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
    for name in PHASE_EVIDENCE_CHECKS.get(phase, ()):
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
        failed_checks = _failed_checks_for_phase(
            project_dir, spec, document, phase.key
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
        )
        if phase.key not in MANUAL_PHASES:
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
        elif event is not None and event.action == "complete":
            state = "Blocked"
            detail = "completion is stale after an earlier phase was reopened"
        elif event is not None and event.action == "reopened":
            state = "In progress"
            detail = event.note
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
    actions = {
        "spec": (
            "Review and finalize `spec.md`.",
            "Record approval with `pcbforge status mark spec complete --note \"...\"`.",
        ),
        "init": ("Run `pcbforge init`.",),
        "architect": (
            "Create the typed module skeleton and `docs/architecture.md`.",
            "Run `pcbforge status --check --write` and present the review package.",
            "After approval, mark ARCHITECT complete.",
        ),
        "mcu": (
            "Create the canonical IOC and matching `src/mcu.ato`.",
            "Run `pcbforge check-ioc` and complete the one-to-one audit.",
            "Mark MCU complete with an audit note.",
        ),
        "implement": (
            "Finish physical module bodies, parts, values, and constraints.",
            "Run a checked dashboard refresh, then mark IMPLEMENT complete.",
        ),
        "build": ("Run `pcbforge status --check --write`.",),
        "brief": ("Create the root `brief.md` placement and rules brief.",),
        "layout": (
            "Complete placement in KiCad 9.",
            "Mark LAYOUT complete when you consider placement finished.",
        ),
        "route": (
            "Complete routing in KiCad 9.",
            "Mark ROUTE complete when you consider routing finished.",
        ),
        "verify": (
            "Run `pcbforge status --check --write` for DRC.",
            "Complete scripted audits and render review, then mark VERIFY complete.",
        ),
        "fab-out": (
            "Generate Gerbers, drills, BOM, CPL, and the JLCPCB archive in `fab/`.",
            "Review the outputs and mark FAB-OUT complete.",
        ),
        "order": (
            "Review and upload the fabrication package to JLCPCB.",
            "After authorizing the purchase, mark ORDER complete.",
        ),
        "publish": (
            "Publish proven reusable modules, or mark PUBLISH skipped.",
        ),
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
        (
            result
            for result in phases
            if not result.complete and result.phase.required
        ),
        None,
    )
    if current is None:
        current = next((result for result in phases if not result.complete), None)
    next_actions = _actions_for(current)[:3] if current is not None else ()
    required = tuple(result for result in phases if result.phase.required)
    checks_failed = any(
        record.outcome == "fail"
        and bool(inputs := _check_inputs(project_dir, spec, name))
        and record.fingerprint == _fingerprint(project_dir, inputs)
        for name, record in document.checks.items()
    )
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
            _fingerprint(project_dir, inputs),
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


def _metadata(document: StatusDocument) -> dict[str, Any]:
    return {
        "pcbforge_status_schema": STATUS_SCHEMA,
        "updated_at": document.updated_at,
        "events": [
            {
                "at": event.at,
                "phase": event.phase,
                "action": event.action,
                "note": event.note,
            }
            for event in document.events
        ],
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
        if current is not None and current.state == "Blocked"
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
    if not blockers:
        blockers = ["- None."]

    recent = [
        f"- **{event.at}:** {event.phase} {event.action} — {event.note}"
        for event in reversed(report.document.events[-5:])
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
    if action == "complete" and phase_key not in MANUAL_PHASES:
        raise StatusInputError(f"{phase_key} is completed automatically from evidence")

    target_index = PHASE_NUMBER[phase_key] - 1
    predecessors = [
        result
        for result in report.phases[:target_index]
        if result.phase.required
    ]
    if action in {"complete", "skipped"} and not all(
        result.complete for result in predecessors
    ):
        waiting = next(result.phase.label for result in predecessors if not result.complete)
        raise StatusInputError(
            f"cannot mark {phase_key} {action}: {waiting} is not complete"
        )
    target = report.phases[target_index]
    if action == "reopened" and not target.complete:
        raise StatusInputError(f"cannot reopen {phase_key}: it is not complete")
    if action == "blocked" and target.complete:
        raise StatusInputError(f"cannot block {phase_key}: reopen it first")
    if action in {"complete", "skipped"} and target.complete:
        raise StatusInputError(
            f"cannot mark {phase_key} {action}: it is already complete"
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
    initial = inspect_status(project_dir, document=document)
    _validate_transition(initial, phase, action)

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
        for check_name in CHECK_PHASES[phase]:
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
        raise StatusInputError(
            f"cannot mark {phase} complete: {evidence_detail}"
        )

    event = StatusEvent(event_time, phase, action, note)
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
