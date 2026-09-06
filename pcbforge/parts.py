"""Audit native schematic symbols, packages and exact-part facts."""
from dataclasses import dataclass
from pathlib import Path
from pcbforge.schematic import SchematicError
from pcbforge.schematic_lint import Finding

class PartsAuditError(RuntimeError):
    pass

class PartsAuditInputError(PartsAuditError):
    pass

@dataclass(frozen=True)
class PartsAuditResult:
    project_dir: Path
    scanned_parts: int
    violations: tuple[Finding, ...]

    @property
    def ok(self):
        return not self.violations

    @property
    def summary(self):
        return f"{self.scanned_parts} schematic parts checked; {len(self.violations)} violations"


def check_parts(project_dir: Path, *, tool_root=None) -> PartsAuditResult:
    from pcbforge.circuit import load_graph, check_component_parts
    from pcbforge.electrical import read_facts
    project_dir = project_dir.resolve()
    try:
        graph = load_graph(project_dir, tool_root=tool_root)
        findings = check_component_parts(graph, read_facts(project_dir), project_dir,
                                        tool_root or Path(__file__).resolve().parent.parent)
    except (SchematicError, OSError, ValueError) as exc:
        raise PartsAuditInputError(str(exc)) from exc
    return PartsAuditResult(project_dir, len(graph.components), findings)


def render_parts_audit(result):
    return "\n".join(["pcbforge: " + result.summary, *[f"[{f.identifier}] {f.code}: {f.message}" for f in result.violations]])
