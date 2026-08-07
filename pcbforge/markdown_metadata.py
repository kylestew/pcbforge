"""Machine-readable metadata blocks for generated Markdown artifacts."""

from __future__ import annotations


TRAILER_MARKER = "<!-- pcbforge-metadata -->"
TRAILER_FENCE = "```yaml"
TRAILER_CLOSE = "```"


def metadata_yaml(text: str) -> str:
    """Return YAML from a trailing metadata block or legacy front matter."""
    lines = text.splitlines()
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError("unterminated legacy YAML front matter") from exc
        return "\n".join(lines[1:end])

    try:
        marker = len(lines) - 1 - lines[::-1].index(TRAILER_MARKER)
    except ValueError as exc:
        raise ValueError("missing trailing YAML metadata block") from exc
    if marker + 2 >= len(lines) or lines[marker + 1] != TRAILER_FENCE:
        raise ValueError("invalid trailing YAML metadata block")
    if lines[-1] != TRAILER_CLOSE:
        raise ValueError("unterminated trailing YAML metadata block")
    return "\n".join(lines[marker + 2 : -1])


def metadata_trailer(yaml_text: str) -> str:
    """Render YAML after the human-readable Markdown body."""
    return (
        f"\n\n{TRAILER_MARKER}\n{TRAILER_FENCE}\n"
        f"{yaml_text.rstrip()}\n{TRAILER_CLOSE}\n"
    )
