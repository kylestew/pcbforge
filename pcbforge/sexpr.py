"""Minimal KiCad S-expression reader and writer."""

from __future__ import annotations

from typing import Iterator

Node = list  # list[str | Node]; atoms are strings, quoted strings keep a "\"" marker


class SExprError(ValueError):
    """The text is not a well-formed S-expression."""


class Quoted(str):
    """A string atom that was (and must be) written with double quotes."""

    __slots__ = ()


def _tokens(text: str) -> Iterator[str | Quoted]:
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in "()":
            yield char
            index += 1
            continue
        if char == '"':
            index += 1
            start = index
            out: list[str] = []
            while index < length:
                char = text[index]
                if char == "\\" and index + 1 < length:
                    nxt = text[index + 1]
                    out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                    index += 2
                    continue
                if char == '"':
                    break
                out.append(char)
                index += 1
            else:
                raise SExprError(f"unterminated string starting at {start}")
            index += 1
            yield Quoted("".join(out))
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in '()"':
            index += 1
        yield text[start:index]


def parse(text: str) -> Node:
    """Parse one top-level S-expression into nested lists of atoms."""
    stack: list[Node] = [[]]
    for token in _tokens(text):
        if token == "(":
            stack.append([])
        elif token == ")":
            if len(stack) == 1:
                raise SExprError("unbalanced ')'")
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(token)
    if len(stack) != 1:
        raise SExprError("unbalanced '('")
    root = stack[0]
    if len(root) != 1 or not isinstance(root[0], list):
        raise SExprError("expected exactly one top-level expression")
    return root[0]


def head(node: Node) -> str:
    return node[0] if node and isinstance(node[0], str) else ""


def children(node: Node, tag: str) -> list[Node]:
    return [item for item in node if isinstance(item, list) and head(item) == tag]


def child(node: Node, tag: str) -> Node | None:
    for item in node:
        if isinstance(item, list) and head(item) == tag:
            return item
    return None


def atoms(node: Node) -> list[str]:
    return [item for item in node[1:] if isinstance(item, str)]


def atom(node: Node | None, index: int = 1, default: str = "") -> str:
    if node is None or len(node) <= index or not isinstance(node[index], str):
        return default
    return str(node[index])


def number(node: Node | None, index: int = 1, default: float = 0.0) -> float:
    value = atom(node, index)
    try:
        return float(value)
    except ValueError:
        return default


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def dumps(node: Node, indent: int = 0) -> str:
    """Serialize a node KiCad-style (one child list per line, tab indented)."""
    pad = "\t" * indent
    parts: list[str] = []
    inline: list[str] = []
    nested: list[str] = []
    for item in node:
        if isinstance(item, list):
            nested.append(dumps(item, indent + 1))
        elif isinstance(item, Quoted):
            inline.append(quote(item))
        else:
            inline.append(str(item))
    parts.append(pad + "(" + " ".join(inline))
    if nested:
        parts.append("\n" + "\n".join(nested) + "\n" + pad + ")")
    else:
        parts.append(")")
    return "".join(parts)


def walk(node: Node) -> Iterator[Node]:
    """Yield every list node depth-first, including the root."""
    yield node
    for item in node:
        if isinstance(item, list):
            yield from walk(item)


__all__ = [
    "Node",
    "Quoted",
    "SExprError",
    "atom",
    "atoms",
    "child",
    "children",
    "dumps",
    "head",
    "number",
    "parse",
    "quote",
    "walk",
]
