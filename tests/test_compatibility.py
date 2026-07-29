from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import yaml

from pcbforge.cli import main
from pcbforge.compatibility import CompatibilityError, validate_project_compatibility


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _pins() -> dict[str, object]:
    return {
        "schema": 15,
        "pcbforge": {"revision": "a" * 40, "dirty": False},
        "guidance": {
            "agents_schema": 16,
            "architect_schema": 5,
            "architecture_diagram_schema": 1,
            "mcu_schema": 4,
            "circuit_schema": 1,
            "build_test_schema": 1,
            "layout_handoff_schema": 1,
            "approval_schema": 6,
            "circuit_review_schema": 2,
            "policy_schema": 1,
            "status_schema": 4,
        },
    }


def _status(schema: int = 4) -> str:
    return f"""---
pcbforge_status_schema: {schema}
updated_at: ''
events: []
policy_events: []
transition_events: []
checks: {{}}
---
# Status
"""


class CompatibilityTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "board"
        project.mkdir()
        (project / ".pcbforge").write_text(
            yaml.safe_dump(_pins(), sort_keys=False),
            encoding="utf-8",
        )
        (project / "STATUS.md").write_text(_status(), encoding="utf-8")
        return project

    def test_accepts_current_project_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary))
            validate_project_compatibility(project)

    def test_rejects_incompatible_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary))
            pins = _pins()
            pins["guidance"]["circuit_review_schema"] = 1  # type: ignore[index]
            (project / ".pcbforge").write_text(
                yaml.safe_dump(pins, sort_keys=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CompatibilityError,
                "guidance.circuit_review_schema",
            ):
                validate_project_compatibility(project)

    def test_status_write_rejects_old_status_schema_without_any_write(self) -> None:
        for arguments in (("--write",), ("--check", "--write")):
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory() as temporary:
                    project = self._project(Path(temporary))
                    (project / "STATUS.md").write_text(
                        _status(2),
                        encoding="utf-8",
                    )
                    before = _tree_hash(project)

                    result = main(["status", *arguments, str(project)])

                    after = _tree_hash(project)

                self.assertEqual(result, 2)
                self.assertEqual(before, after)

    def test_approval_rejects_bad_review_schema_without_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self._project(Path(temporary))
            (project / "circuit-review.yaml").write_text(
                "circuit_review_schema: 1\n",
                encoding="utf-8",
            )
            before = _tree_hash(project)

            result = main(
                [
                    "status",
                    "approve",
                    "circuit",
                    str(project),
                    "--fingerprint",
                    "a" * 64,
                    "--note",
                    "approved",
                ]
            )

            after = _tree_hash(project)

        self.assertEqual(result, 2)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
