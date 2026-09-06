"""One pinned KiCad installation for commands, formats and libraries."""

from pathlib import Path
import subprocess

VERSION = "10.0.3"
BOARD_FORMAT = "20260206"
SCHEMATIC_FORMAT = "20260306"
CLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
SHARED = CLI.parents[1] / "SharedSupport"


def command(tool_root: Path | None = None) -> str:
    root = tool_root or Path(__file__).resolve().parent.parent
    return str(root / "scripts" / "kicad-cli")


def check_version(tool_root: Path | None = None, runner=subprocess.run) -> None:
    result = runner([command(tool_root), "version"], capture_output=True, text=True, check=False)
    if result.returncode or result.stdout.strip() != VERSION:
        raise RuntimeError(f"KiCad {VERSION} is required; found {result.stdout.strip() or result.stderr.strip()}")


def library_dir(kind: str, tool_root: Path | None = None) -> Path:
    # The wrapper is also the configuration seam used by isolated test fixtures.
    root = tool_root or Path(__file__).resolve().parent.parent
    import re
    wrapper = (root / "scripts" / "kicad-cli").read_text()
    match = re.search(r'^KICAD_CLI="([^"]+)"', wrapper, re.MULTILINE)
    if match is None:
        raise RuntimeError("scripts/kicad-cli must declare KICAD_CLI")
    path = Path(match.group(1)).parents[1] / "SharedSupport" / kind
    if not path.is_dir():
        raise RuntimeError(f"KiCad {kind} library not found: {path}")
    return path
