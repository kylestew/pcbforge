#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+')
HEAD_RE = re.compile(r"^\(\s*([^\s()]+)")
REFERENCE_RE = re.compile(r'\(property\s+"Reference"\s+"((?:\\.|[^"])*)"')
AT_RE = re.compile(r"\n\s*\(at\s+([^)]+)\)")
LAYER_RE = re.compile(r'\n\s*\(layer\s+"([^"]+)"\)')
UUID_RE = re.compile(r'\n\s*\(uuid\s+"([^"]+)"\)')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_tokens(text: str) -> str:
    return " ".join(TOKEN_RE.findall(text))


def top_level_blocks(text: str) -> list[tuple[str, str]]:
    first = text.find("(")
    if first < 0:
        raise ValueError("not an s-expression")

    depth = 0
    in_string = False
    escaped = False
    block_start: int | None = None
    blocks: list[tuple[str, str]] = []

    for index, char in enumerate(text[first:], start=first):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            if depth == 1:
                block_start = index
            depth += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 1 and block_start is not None:
                block = text[block_start : index + 1]
                match = HEAD_RE.match(block)
                if match:
                    blocks.append((match.group(1), block))
                block_start = None
            if depth == 0:
                break
    return blocks


def aggregate_hash(values: list[str]) -> str:
    payload = "\n".join(sorted(values)).encode()
    return sha256_bytes(payload)


def fingerprint(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    blocks = top_level_blocks(text)

    category_hashes: dict[str, list[str]] = {}
    footprints: list[dict[str, str]] = []
    user_art_heads = {
        "segment",
        "arc",
        "via",
        "zone",
        "image",
        "target",
        "group",
        "dimension",
    }

    for head, block in blocks:
        normalized_hash = sha256_bytes(canonical_tokens(block).encode())
        category_hashes.setdefault(head, []).append(normalized_hash)

        if head.startswith("gr_"):
            user_art_heads.add(head)

        if head == "footprint":
            reference = REFERENCE_RE.search(block)
            at = AT_RE.search(block)
            layer = LAYER_RE.search(block)
            uuid = UUID_RE.search(block)
            footprints.append(
                {
                    "reference": reference.group(1) if reference else "",
                    "at": at.group(1).strip() if at else "",
                    "layer": layer.group(1) if layer else "",
                    "uuid": uuid.group(1) if uuid else "",
                }
            )

    category_summary = {
        head: {
            "count": len(hashes),
            "aggregate_sha256": aggregate_hash(hashes),
        }
        for head, hashes in sorted(category_hashes.items())
    }
    user_art_hashes = [
        item
        for head, hashes in category_hashes.items()
        if head in user_art_heads
        for item in hashes
    ]

    return {
        "path": str(path),
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
        "categories": category_summary,
        "user_art": {
            "count": len(user_art_hashes),
            "aggregate_sha256": aggregate_hash(user_art_hashes),
        },
        "footprint_placements": sorted(
            footprints, key=lambda item: item["reference"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("board", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = fingerprint(args.board)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
