"""Native KiCad functional netclasses and presentation colors."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from pcbforge.kicad_project import read_project, project_text, write_outputs
from pcbforge.schematic import extract, project_schematic

PALETTE = {
    "power": "#D55E00",
    "ground": "#808080",
    "usb": "#AA66CC",
    "digital": "#0072B2",
    "analog": "#009E73",
}
PREFIX = "pcbforge:"
VISUAL_FIELDS = ("name", "schematic_color", "wire_width", "bus_width", "line_style", "priority")


class NetclassError(ValueError):
    pass


def rgba(color: str) -> str:
    if not isinstance(color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is None:
        raise NetclassError("color must use #RRGGBB format")
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, 1.000)"


def _unset(color: Any) -> bool:
    return color is None or color == "" or bool(
        isinstance(color, str) and re.search(r",\s*0(?:\.0*)?\s*\)$", color)
    )


def assignments(settings: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Read both legacy and current explicit assignment representations."""
    result = []
    raw = settings.get("netclass_assignments") or {}
    if not isinstance(raw, dict):
        raise NetclassError("native netclass assignments must be a mapping")
    for net, names in raw.items():
        for name in names if isinstance(names, list) else [names]:
            if not isinstance(net, str) or not isinstance(name, str):
                raise NetclassError("native netclass assignments must contain net and class names")
            result.append((net, name))
    return result


def matches_pattern(net: str, pattern: str) -> bool:
    # KiCad wildcards are * and ?. Bus brackets are literal characters.
    expression = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.fullmatch(expression, net) is not None


def set_netclass(project_dir: Path, *, name: str, nets: Sequence[str],
                 role: str | None = None, color: str | None = None,
                 tool_root: Path | None = None) -> bool:
    """Add exact nets to a native class; palette defaults preserve GUI colors."""
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) is None:
        raise NetclassError("name must be a kebab-case ID, without pcbforge:")
    if (role is None) == (color is None):
        raise NetclassError("specify either a palette role or a custom color")
    if role is not None and role not in PALETTE:
        raise NetclassError(f"unknown palette role: {role}")
    wanted = rgba(color if color is not None else PALETTE[role])
    if not nets or any(not isinstance(net, str) or not net for net in nets):
        raise NetclassError("provide at least one exact net name")
    # KiCad patterns interpret * and ?. Do not silently broaden an exact assignment.
    if any("*" in net or "?" in net for net in nets):
        raise NetclassError("exact net names cannot contain * or ?")
    project_dir = project_dir.expanduser().resolve()
    schematic = project_schematic(project_dir)
    path = schematic.with_suffix(".kicad_pro")
    before = path.read_bytes()
    project = read_project(path)
    graph = extract(schematic, tool_root=tool_root)
    unknown = sorted(set(nets) - set(graph.nets))
    if unknown:
        raise NetclassError("unknown exact nets: " + ", ".join(unknown))
    settings = project.get("net_settings", {})
    if not isinstance(settings, dict):
        raise NetclassError("native net_settings must be a mapping")
    classes = settings.get("classes")
    patterns = settings.get("netclass_patterns") or []
    if (not isinstance(classes, list) or not all(isinstance(item, dict) for item in classes)
            or not isinstance(patterns, list) or not all(isinstance(item, dict) for item in patterns)):
        raise NetclassError("invalid native netclass settings")
    if any(not isinstance(item.get("pattern"), str) or not isinstance(item.get("netclass"), str) for item in patterns):
        raise NetclassError("native netclass patterns must contain pattern and class names")
    explicit = assignments(settings)
    full_name = PREFIX + name
    conflicts = []
    for net in set(nets):
        for item in patterns:
            pattern = item.get("pattern", "")
            if item.get("netclass") not in (full_name, "Default") and (
                matches_pattern(net, pattern)
            ):
                conflicts.append(f"{net}: {item.get('netclass')}")
        conflicts.extend(f"{net}: {other}" for assigned, other in explicit
                         if assigned == net and other not in (full_name, "Default"))
    if conflicts:
        raise NetclassError("conflicting netclass assignments: " + ", ".join(sorted(set(conflicts))))
    existing = next((item for item in classes if item.get("name") == full_name), None)
    if existing is None:
        default = next((item for item in classes if item.get("name") == "Default"), None)
        if default is None:
            raise NetclassError("native Default netclass is missing")
        existing = copy.deepcopy(default)
        existing["name"] = full_name
        used = {item.get("priority") for item in classes if item.get("name") != "Default"}
        if "priority" in default:
            existing["priority"] = next(i for i in range(len(classes) + 1) if i not in used)
        for field in ("schematic_color", "pcb_color"):
            existing[field] = wanted
        classes.append(existing)
    for field in ("schematic_color", "pcb_color"):
        if color is not None or _unset(existing.get(field)):
            existing[field] = wanted
    for net in sorted(set(nets)):
        item = {"netclass": full_name, "pattern": net}
        if item not in patterns:
            patterns.append(item)
    settings["netclass_patterns"] = patterns
    if path.read_bytes() != before:
        raise NetclassError("KiCad project changed during netclass setup; retry")
    if project == read_project(path):
        return False
    return write_outputs(((path, project_text(project).encode()),), label="native netclass setup")[0]


def presentation_settings(project: Mapping[str, Any]) -> dict:
    settings = project.get("net_settings", {})
    classes = settings.get("classes", [])
    default = next((item for item in classes if item.get("name") == "Default"), {})
    fields = ("schematic_color", "wire_width", "bus_width", "line_style")
    # Routing-only classes inherit the same drawing style. Their creation during
    # handoff must not invalidate the already reviewed schematic presentation.
    visual = [item for item in classes if item.get("name") == "Default" or any(
        item.get(key, default.get(key)) != default.get(key) for key in fields)]
    names = {item.get("name") for item in visual}
    return {
        "classes": sorted(({key: item[key] for key in VISUAL_FIELDS if key in item}
                           for item in visual), key=lambda item: item.get("name", "")),
        "patterns": [p for p in settings.get("netclass_patterns", []) or [] if p.get("netclass") in names],
        "assignments": sorted((net, name) for net, name in assignments(settings) if name in names),
    }


def color_legend(project: Mapping[str, Any]) -> str:
    settings = project.get("net_settings", {})
    rows = []
    for item in sorted(settings.get("classes", []), key=lambda item: item.get("name", "")):
        name = item.get("name", "")
        sch, pcb = item.get("schematic_color"), item.get("pcb_color")
        if _unset(sch) and _unset(pcb):
            continue
        nets = {p["pattern"] for p in settings.get("netclass_patterns", []) or [] if p.get("netclass") == name}
        nets.update(net for net, assigned in assignments(settings) if assigned == name)
        values = (name, ", ".join(sorted(nets)) or "Native schematic directives", sch or "Theme default", pcb or "Theme default")
        rows.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    if not rows:
        return ""
    return "\n## Net colors\n\n| Class | Nets / patterns | Schematic | PCB |\n|---|---|---|---|\n" + "\n".join(rows) + "\n"
