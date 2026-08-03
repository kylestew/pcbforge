"""Canonical hashing for compiler artifacts used in tracked evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

PROJECT_PIN_FILENAME = ".pcbforge"
# Which PCBForge checkout may execute is not part of any design decision, so
# it must not invalidate a human approval. Every other pin — toolchain, rules,
# policy, guidance schemas — still binds.
PIN_EXECUTION_KEY = "pcbforge"


class ArtifactHashError(RuntimeError):
    """A compiler artifact cannot be represented safely in approval evidence."""


def semantic_bom_bytes(path: Path) -> bytes:
    """Return canonical BOM JSON without the volatile top-level build identifier."""
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactHashError(f"missing compiler BOM: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactHashError(f"invalid compiler BOM {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactHashError(f"invalid compiler BOM {path}: expected a JSON object")
    payload = dict(payload)
    payload.pop("build_id", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def semantic_bom_sha256(path: Path) -> str:
    """Hash the electrically meaningful compiler BOM representation."""
    return hashlib.sha256(semantic_bom_bytes(path)).hexdigest()


def semantic_pin_bytes(path: Path) -> bytes:
    """Return canonical project-pin bytes without the executable-checkout block.

    A malformed pin hashes as its raw bytes so corruption still fails closed.
    """
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return path.read_bytes()
    if not isinstance(payload, dict):
        return path.read_bytes()
    payload = {key: value for key, value in payload.items() if key != PIN_EXECUTION_KEY}
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def evidence_bytes(path: Path) -> bytes:
    """Return the bytes that tracked evidence should bind for this file."""
    if path.name == PROJECT_PIN_FILENAME:
        return semantic_pin_bytes(path)
    return path.read_bytes()
