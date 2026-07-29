"""Canonical hashing for compiler artifacts used in tracked evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


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
