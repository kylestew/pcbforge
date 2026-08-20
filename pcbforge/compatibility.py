"""Fail-closed schema validation before current workflow code may act."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from pcbforge.markdown_metadata import metadata_yaml

PIN_SCHEMA = 1
STATUS_SCHEMA = 1
EXPECTED_GUIDANCE = {
    "agents_schema": 1,
    "architect_schema": 1,
    "architecture_diagram_schema": 1,
    "mcu_schema": 1,
    "circuit_schema": 1,
    "build_test_schema": 1,
    "layout_handoff_schema": 1,
    "approval_schema": 1,
    "circuit_review_schema": 3,
    "policy_schema": 1,
    "status_schema": 1,
}
STRUCTURED_ARTIFACT_SCHEMAS = {
    "policy.yaml": ("policy_schema", 1),
    "circuit-review.yaml": ("circuit_review_schema", 3),
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
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CompatibilityError(f"cannot read STATUS.md: {exc}") from exc
    try:
        yaml_text = metadata_yaml(text)
    except ValueError as exc:
        raise CompatibilityError(f"invalid STATUS.md metadata: {exc}") from exc
    try:
        loaded = yaml.load(yaml_text, Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise CompatibilityError(f"invalid STATUS.md metadata: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CompatibilityError("invalid STATUS.md metadata: expected a mapping")
    return loaded


def validate_project_compatibility(project_dir: Path) -> None:
    """Validate current schemas before checks, reports, or status mutations."""
    project_dir = project_dir.expanduser().resolve()
    pins_path = project_dir / ".pcbforge"
    if not pins_path.is_file():
        return
    pins = _load_yaml(pins_path, ".pcbforge")
    errors: list[str] = []
    if type(pins.get("schema")) is not int or pins.get("schema") != PIN_SCHEMA:
        errors.append(".pcbforge schema: unsupported version — restart the project")
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
            if type(guidance.get(key)) is not int or guidance.get(key) != expected:
                errors.append(
                    f".pcbforge guidance.{key}: unsupported version — restart the project"
                )

    status_path = project_dir / "STATUS.md"
    if status_path.is_file():
        status = _status_metadata(status_path)
        if type(status.get("pcbforge_status_schema")) is not int or status.get(
            "pcbforge_status_schema"
        ) != STATUS_SCHEMA:
            errors.append(
                "STATUS.md pcbforge_status_schema: unsupported version — restart the project"
            )

    for filename, (key, expected) in STRUCTURED_ARTIFACT_SCHEMAS.items():
        path = project_dir / filename
        if not path.is_file():
            continue
        artifact = _load_yaml(path, filename)
        if type(artifact.get(key)) is not int or artifact.get(key) != expected:
            errors.append(
                f"{filename} {key}: unsupported version — restart the project"
            )

    if errors:
        raise CompatibilityError(
            "project compatibility preflight failed; no files were changed:\n  - "
            + "\n  - ".join(errors)
        )
