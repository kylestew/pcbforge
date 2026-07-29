from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pcbforge_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("pcbforge_bootstrap", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def _run(command: list[str], cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


class BootstrapArgumentTests(unittest.TestCase):
    def test_finds_project_for_every_command_shape(self) -> None:
        cwd = Path("/tmp/work")
        cases = {
            ("init", "board"): cwd / "board",
            ("check-circuit-review", "--stage", "final", "board"): cwd / "board",
            ("policy", "approve-baseline", "board", "--note", "yes"): cwd / "board",
            (
                "policy",
                "approve-exception",
                "ex-1",
                "board",
                "--note",
                "yes",
            ): cwd / "board",
            ("status", "--check", "--write", "board"): cwd / "board",
            (
                "status",
                "mark",
                "layout",
                "complete",
                "board",
                "--note",
                "done",
            ): cwd / "board",
            (
                "status",
                "review",
                "circuit",
                "--stage",
                "final",
                "board",
            ): cwd / "board",
            (
                "status",
                "approve",
                "circuit",
                "board",
                "--fingerprint",
                "a" * 64,
                "--note",
                "yes",
            ): cwd / "board",
            ("migrate-circuit-phase", "board"): cwd / "board",
            ("migrate-placement-brief", "board"): cwd / "board",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(
                    bootstrap.project_argument(argv, cwd),
                    expected.resolve(),
                )


class BootstrapResolutionTests(unittest.TestCase):
    def _repository(self, root: Path) -> tuple[Path, Path, str, str]:
        repository = root / "pcbforge"
        repository.mkdir()
        _run(["git", "init"], repository)
        _run(["git", "config", "user.name", "PCBForge Test"], repository)
        _run(["git", "config", "user.email", "pcbforge@example.invalid"], repository)
        (repository / ".gitignore").write_text(
            "toolchain/.venv/\n",
            encoding="utf-8",
        )
        toolchain = repository / "toolchain"
        toolchain.mkdir()
        (toolchain / "uv.lock").write_text("revision one\n", encoding="utf-8")
        (repository / "version.txt").write_text("one\n", encoding="utf-8")
        _run(["git", "add", "."], repository)
        _run(["git", "commit", "-m", "revision one"], repository)
        first = _run(["git", "rev-parse", "HEAD"], repository)

        (repository / "version.txt").write_text("two\n", encoding="utf-8")
        (toolchain / "uv.lock").write_text("revision two\n", encoding="utf-8")
        _run(["git", "add", "."], repository)
        _run(["git", "commit", "-m", "revision two"], repository)
        second = _run(["git", "rev-parse", "HEAD"], repository)

        old_checkout = root / "pcbforge-old"
        _run(
            ["git", "worktree", "add", "--detach", str(old_checkout), first],
            repository,
        )
        for checkout in (repository, old_checkout):
            python = checkout / "toolchain" / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable))
        return repository, old_checkout, first, second

    def _project(
        self,
        root: Path,
        revision: str,
        lockfile: Path,
        *,
        dirty: bool = False,
    ) -> Path:
        project = root / "board"
        project.mkdir()
        lock_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
        (project / ".pcbforge").write_text(
            yaml.safe_dump(
                {
                    "schema": 13,
                    "pcbforge": {"revision": revision, "dirty": dirty},
                    "toolchain": {"uv_lock_sha256": lock_hash},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (project / "STATUS.md").write_text("unchanged\n", encoding="utf-8")
        return project

    def test_routes_to_clean_registered_pinned_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, old_checkout, first, _ = self._repository(root)
            project = self._project(
                root,
                first,
                old_checkout / "toolchain" / "uv.lock",
            )

            execution = bootstrap.resolve_execution(
                repository,
                ["status", str(project)],
                cwd=root,
            )

        self.assertEqual(execution.checkout, old_checkout.resolve())
        self.assertEqual(execution.revision, first)

    def test_prefers_clean_current_checkout_when_it_matches_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, _, _, second = self._repository(root)
            project = self._project(
                root,
                second,
                repository / "toolchain" / "uv.lock",
            )

            execution = bootstrap.resolve_execution(
                repository,
                ["check-policy", str(project)],
                cwd=root,
            )

        self.assertEqual(execution.checkout, repository.resolve())
        self.assertEqual(execution.revision, second)

    def test_missing_or_dirty_pinned_copy_fails_without_project_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, old_checkout, first, _ = self._repository(root)
            project = self._project(
                root,
                first,
                old_checkout / "toolchain" / "uv.lock",
            )
            (old_checkout / "version.txt").write_text("dirty\n", encoding="utf-8")
            before = _tree_hash(project)

            with self.assertRaisesRegex(bootstrap.BootstrapError, "no clean"):
                bootstrap.resolve_execution(
                    repository,
                    ["status", "--write", str(project)],
                    cwd=root,
                )

            after = _tree_hash(project)

        self.assertEqual(before, after)

    def test_dirty_pin_fails_but_explicit_migration_uses_clean_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, old_checkout, first, second = self._repository(root)
            project = self._project(
                root,
                first,
                old_checkout / "toolchain" / "uv.lock",
                dirty=True,
            )
            before = _tree_hash(project)

            with self.assertRaisesRegex(bootstrap.BootstrapError, "not reproducible"):
                bootstrap.resolve_execution(
                    repository,
                    ["status", str(project)],
                    cwd=root,
                )
            migration = bootstrap.resolve_execution(
                repository,
                ["migrate-circuit-phase", str(project)],
                cwd=root,
            )

            after = _tree_hash(project)

        self.assertEqual(migration.checkout, repository.resolve())
        self.assertEqual(migration.revision, second)
        self.assertEqual(before, after)

    def test_rejects_mismatched_lock_or_missing_pinned_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, old_checkout, first, _ = self._repository(root)
            project = self._project(
                root,
                first,
                old_checkout / "toolchain" / "uv.lock",
            )
            pins_path = project / ".pcbforge"
            pins = yaml.safe_load(pins_path.read_text(encoding="utf-8"))
            pins["toolchain"]["uv_lock_sha256"] = "0" * 64
            pins_path.write_text(
                yaml.safe_dump(pins, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "mismatched toolchain lock",
            ):
                bootstrap.resolve_execution(
                    repository,
                    ["status", str(project)],
                    cwd=root,
                )

            pins["toolchain"]["uv_lock_sha256"] = hashlib.sha256(
                (old_checkout / "toolchain" / "uv.lock").read_bytes()
            ).hexdigest()
            pins_path.write_text(
                yaml.safe_dump(pins, sort_keys=False),
                encoding="utf-8",
            )
            (
                old_checkout / "toolchain" / ".venv" / "bin" / "python"
            ).unlink()
            with self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "environment is missing",
            ):
                bootstrap.resolve_execution(
                    repository,
                    ["status", str(project)],
                    cwd=root,
                )

    def test_unpinned_work_refuses_dirty_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, _, _, _ = self._repository(root)
            project = root / "new-board"
            project.mkdir()
            (repository / "version.txt").write_text("dirty\n", encoding="utf-8")
            before = _tree_hash(project)

            with self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "launcher checkout is dirty",
            ):
                bootstrap.resolve_execution(
                    repository,
                    ["init", str(project)],
                    cwd=root,
                )

            after = _tree_hash(project)

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
