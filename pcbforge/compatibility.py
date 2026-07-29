"""Fail-closed schema validation before current workflow code may act."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

PIN_SCHEMA = 14
STATUS_SCHEMA = 3
EXPECTED_GUIDANCE = {
    "agents_schema": 15,
    "architect_schema": 4,
    "architecture_diagram_schema": 1,
    "mcu_schema": 3,
    "circuit_schema": 1,
    "build_test_schema": 1,
    "brief_schema": 5,
    "approval_schema": 5,
    "circuit_review_schema": 2,
    "policy_schema": 1,
    "status_schema": 3,
}
STRUCTURED_ARTIFACT_SCHEMAS = {
    "policy.yaml": ("policy_schema", 1),
    "circuit-review.yaml": ("circuit_review_schema", 2),
    "build-test.yaml": ("build_test_schema", 1),
    "placement.yaml": ("placement_schema", 1),
}


class CompatibilityError(RuntimeError):
    """A project cannot be interpreted safely by this implementation."""


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
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.YAMLError("mapping keys must be scalar values") from exc
        if duplicate:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path, label: str) -> Mapping[str, Any]:
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except FileNotFoundError as exc:
        raise CompatibilityError(f"missing {label}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CompatibilityError(f"invalid {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CompatibilityError(f"invalid {label}: expected a mapping")
    return loaded


def _status_metadata(path: Path) -> Mapping[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CompatibilityError(f"cannot read STATUS.md: {exc}") from exc
    if not lines or lines[0] != "---":
        raise CompatibilityError("invalid STATUS.md: missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise CompatibilityError(
            "invalid STATUS.md: unterminated YAML frontmatter"
        ) from exc
    try:
        loaded = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise CompatibilityError(f"invalid STATUS.md frontmatter: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CompatibilityError("invalid STATUS.md frontmatter: expected a mapping")
    return loaded


def validate_project_compatibility(project_dir: Path) -> None:
    """Validate current schemas before checks, reports, or status mutations."""
    project_dir = project_dir.expanduser().resolve()
    pins_path = project_dir / ".pcbforge"
    if not pins_path.is_file():
        return
    pins = _load_yaml(pins_path, ".pcbforge")
    errors: list[str] = []
    if pins.get("schema") != PIN_SCHEMA:
        errors.append(
            f".pcbforge schema is {pins.get('schema')!r}; expected {PIN_SCHEMA} "
            "or an explicit migrate-* command"
        )
    pcbforge = pins.get("pcbforge")
    if not isinstance(pcbforge, dict):
        errors.append(".pcbforge pcbforge must be a mapping")
    elif pcbforge.get("dirty") is not False:
        errors.append(".pcbforge pcbforge.dirty must be false")
    guidance = pins.get("guidance")
    if not isinstance(guidance, dict):
        errors.append(".pcbforge guidance must be a mapping")
    else:
        for key, expected in EXPECTED_GUIDANCE.items():
            if guidance.get(key) != expected:
                errors.append(
                    f".pcbforge guidance.{key} is {guidance.get(key)!r}; "
                    f"expected {expected}"
                )

    status_path = project_dir / "STATUS.md"
    if status_path.is_file():
        status = _status_metadata(status_path)
        if status.get("pcbforge_status_schema") != STATUS_SCHEMA:
            errors.append(
                "STATUS.md pcbforge_status_schema is "
                f"{status.get('pcbforge_status_schema')!r}; expected {STATUS_SCHEMA}"
            )

    for filename, (key, expected) in STRUCTURED_ARTIFACT_SCHEMAS.items():
        path = project_dir / filename
        if not path.is_file():
            continue
        artifact = _load_yaml(path, filename)
        if artifact.get(key) != expected:
            errors.append(
                f"{filename} {key} is {artifact.get(key)!r}; expected {expected}"
            )

    if errors:
        raise CompatibilityError(
            "project compatibility preflight failed; no files were changed:\n  - "
            + "\n  - ".join(errors)
        )
