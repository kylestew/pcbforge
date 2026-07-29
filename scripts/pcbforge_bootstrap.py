#!/usr/bin/env python3
"""Resolve and execute the clean PCBForge checkout pinned by a project."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

MIGRATION_COMMANDS = {
    "migrate-policy",
    "migrate-approvals",
    "migrate-schematic-review",
    "migrate-circuit-review",
    "migrate-circuit-phase",
    "migrate-placement-brief",
    "migrate-phase-transitions",
}
OPTIONS_WITH_VALUES = {"--stage", "--note", "--fingerprint"}
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class BootstrapError(RuntimeError):
    """The requested implementation cannot be selected safely."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class ProjectPin:
    revision: str
    dirty: bool
    lock_sha256: str


@dataclass(frozen=True)
class Execution:
    checkout: Path
    python: Path
    project_dir: Path | None
    revision: str


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _positional(argv: Sequence[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in OPTIONS_WITH_VALUES:
            index += 2
            continue
        if value.startswith("--"):
            index += 1
            continue
        values.append(value)
        index += 1
    return values


def project_argument(argv: Sequence[str], cwd: Path) -> Path | None:
    """Locate PROJECT_DIR without importing the versioned CLI implementation."""
    if not argv or argv[0] in {"-h", "--help"}:
        return None
    command = argv[0]
    tail = list(argv[1:])
    values = _positional(tail)
    project = "."
    if command == "policy":
        if not values:
            return None
        policy_command = values[0]
        if policy_command == "approve-exception":
            if len(values) >= 3:
                project = values[2]
        elif len(values) >= 2:
            project = values[1]
    elif command == "status":
        if values and values[0] in {"mark", "review", "approve"}:
            mode = values[0]
            required = 3 if mode == "mark" else 2
            if len(values) > required:
                project = values[required]
        elif values:
            project = values[0]
    elif command in {
        "init",
        "check-ioc",
        "check-parts",
        "check-schematic",
        "check-circuit-review",
        "check-build-test",
        "brief",
        "check-brief",
        "prepare-layout",
        "check-layout-handoff",
        "check-policy",
        *MIGRATION_COMMANDS,
    }:
        if values:
            project = values[0]
    else:
        return None
    return (cwd / project).expanduser().resolve() if not Path(project).is_absolute() else Path(project).expanduser().resolve()


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    cwd: Path,
    purpose: str,
) -> str:
    try:
        completed = runner(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(f"{purpose} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise BootstrapError(f"{purpose} failed: {detail or completed.returncode}")
    return completed.stdout


def _load_pin(path: Path) -> ProjectPin:
    try:
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except FileNotFoundError as exc:
        raise BootstrapError(f"missing project pin: {path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise BootstrapError(f"invalid project pin {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"invalid project pin {path}: expected a mapping")
    pcbforge = payload.get("pcbforge")
    toolchain = payload.get("toolchain")
    if not isinstance(pcbforge, dict) or not isinstance(toolchain, dict):
        raise BootstrapError(
            f"invalid project pin {path}: pcbforge and toolchain must be mappings"
        )
    revision = pcbforge.get("revision")
    dirty = pcbforge.get("dirty")
    lock_sha256 = toolchain.get("uv_lock_sha256")
    if not isinstance(revision, str) or SHA1_RE.fullmatch(revision) is None:
        raise BootstrapError(
            f"invalid project pin {path}: pcbforge.revision must be a full Git SHA"
        )
    if type(dirty) is not bool:
        raise BootstrapError(
            f"invalid project pin {path}: pcbforge.dirty must be true or false"
        )
    if (
        not isinstance(lock_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", lock_sha256) is None
    ):
        raise BootstrapError(
            f"invalid project pin {path}: toolchain.uv_lock_sha256 must be SHA-256"
        )
    return ProjectPin(revision, dirty, lock_sha256)


def _worktrees(
    launcher_root: Path,
    runner: CommandRunner,
) -> tuple[tuple[Path, str], ...]:
    output = _run(
        runner,
        ["git", "worktree", "list", "--porcelain"],
        cwd=launcher_root,
        purpose="Git worktree discovery",
    )
    records: list[tuple[Path, str]] = []
    path: Path | None = None
    revision = ""
    for line in (*output.splitlines(), ""):
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("HEAD "):
            revision = line.removeprefix("HEAD ").strip()
        elif not line and path is not None:
            records.append((path, revision))
            path = None
            revision = ""
    return tuple(records)


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BootstrapError(f"cannot hash {path}: {exc}") from exc


def _is_clean(checkout: Path, runner: CommandRunner) -> bool:
    return not _run(
        runner,
        ["git", "status", "--porcelain"],
        cwd=checkout,
        purpose=f"worktree check for {checkout}",
    ).strip()


def _execution_for_checkout(
    checkout: Path,
    revision: str,
    project_dir: Path | None,
) -> Execution:
    python = checkout / "toolchain" / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise BootstrapError(
            f"toolchain environment is missing for {checkout}; run: "
            f"uv sync --project {shlex.quote(str(checkout / 'toolchain'))}"
        )
    return Execution(checkout, python, project_dir, revision)


def resolve_execution(
    launcher_root: Path,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> Execution:
    """Select a safe checkout using only project pins and Git metadata."""
    launcher_root = launcher_root.resolve()
    cwd = (cwd or Path.cwd()).resolve()
    project_dir = project_argument(argv, cwd)
    command = argv[0] if argv else ""
    migration = command in MIGRATION_COMMANDS
    pin_path = project_dir / ".pcbforge" if project_dir is not None else None

    if migration or pin_path is None or not pin_path.is_file():
        if not _is_clean(launcher_root, runner):
            raise BootstrapError(
                "the launcher checkout is dirty; commit or stash PCBForge changes "
                "before initialization, migration, or unpinned project work"
            )
        revision = _run(
            runner,
            ["git", "rev-parse", "HEAD"],
            cwd=launcher_root,
            purpose="launcher revision check",
        ).strip()
        return _execution_for_checkout(
            launcher_root,
            revision,
            project_dir,
        )

    pin = _load_pin(pin_path)
    if pin.dirty:
        raise BootstrapError(
            f"{pin_path} records pcbforge.dirty: true and is not reproducible; "
            "use an explicit migrate-* command from a clean checkout"
        )

    candidates = [
        path
        for path, revision in _worktrees(launcher_root, runner)
        if revision == pin.revision
    ]
    candidates.sort(key=lambda path: (path != launcher_root, str(path)))
    rejections: list[str] = []
    for checkout in candidates:
        if not _is_clean(checkout, runner):
            rejections.append(f"{checkout} is dirty")
            continue
        lockfile = checkout / "toolchain" / "uv.lock"
        if not lockfile.is_file() or _sha256(lockfile) != pin.lock_sha256:
            rejections.append(f"{checkout} has a mismatched toolchain lock")
            continue
        try:
            return _execution_for_checkout(
                checkout,
                pin.revision,
                project_dir,
            )
        except BootstrapError as exc:
            rejections.append(str(exc))

    suggested = launcher_root.parent / f"pcbforge-{pin.revision[:12]}"
    details = f" ({'; '.join(rejections)})" if rejections else ""
    raise BootstrapError(
        f"no clean executable PCBForge worktree matches project revision "
        f"{pin.revision}{details}\n"
        f"create one with: git -C {shlex.quote(str(launcher_root))} worktree add "
        f"--detach {shlex.quote(str(suggested))} {pin.revision}\n"
        f"then run: uv sync --project "
        f"{shlex.quote(str(suggested / 'toolchain'))}"
    )


def _environment(execution: Execution, launcher_root: Path) -> Mapping[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "").split(os.pathsep)
    filtered = [
        item
        for item in existing
        if item and Path(item).expanduser().resolve() != launcher_root.resolve()
    ]
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(execution.checkout), *filtered]
    )
    return environment


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        print("pcbforge bootstrap: missing launcher root", file=sys.stderr)
        return 2
    launcher_root = Path(values.pop(0))
    try:
        execution = resolve_execution(launcher_root, values)
    except BootstrapError as exc:
        print(f"pcbforge: {exc}", file=sys.stderr)
        return 2
    os.execve(
        execution.python,
        [
            str(execution.python),
            "-m",
            "pcbforge.cli",
            *values,
        ],
        _environment(execution, launcher_root),
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
