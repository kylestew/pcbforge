"""Prepare and verify the user-operated native KiCad PCB update."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import uuid

from pcbforge import sexpr as sx
from pcbforge.circuit import load_graph, read_evidence, fingerprint_inputs
from pcbforge.fsutil import commit_outputs
from pcbforge.kicad_tools import BOARD_FORMAT
from pcbforge.schematic import SchematicError, canonical, digest, project_schematic, properties, from_payload, semantic_diff

UPDATE_PATH = Path("review/circuit/pcb-update.json")
SYNC_PATH = Path("review/circuit/pcb-sync.json")


def _normal(node, renames=None):
    if isinstance(node, list):
        tag = sx.head(node)
        if tag in {"filled_polygon", "fill_segments", "fill_version", "net_code"}:
            return None
        if tag == "net" and renames:
            name = sx.atom(node, 2) or sx.atom(node)
            return ["net", renames.get(name, name)]
        atoms, children = [], []
        for item in node:
            value = _normal(item, renames)
            if value is not None:
                (children if isinstance(item, list) else atoms).append(value)
        return [*atoms, *sorted(children, key=lambda x: canonical(x))]
    if isinstance(node, sx.Quoted):
        return str(node)
    try:
        number = Decimal(node)
        if number.is_finite():
            return str(number.normalize())
    except (InvalidOperation, ValueError):
        pass
    return str(node)


# Defaults emitted by an unchanged KiCad 10.0.3 load/save of the scaffold.
# Expand only known defaults; explicit non-default values remain significant.
_SETUP_DEFAULTS = sx.parse(r"""(setup
	(covering
		(front no)
		(back no)
	)
	(plugging
		(front no)
		(back no)
	)
	(capping no)
	(filling no)
	(pcbplotparams
		(layerselection 0x00000000_00000000_55555555_5755f5ff)
		(plot_on_all_layers_selection 0x00000000_00000000_00000000_00000000)
		(disableapertmacros no)
		(usegerberextensions no)
		(usegerberattributes yes)
		(usegerberadvancedattributes yes)
		(creategerberjobfile yes)
		(dashed_line_dash_ratio 12)
		(dashed_line_gap_ratio 3)
		(svgprecision 4)
		(plotframeref no)
		(mode 1)
		(useauxorigin no)
		(pdf_front_fp_property_popups yes)
		(pdf_back_fp_property_popups yes)
		(pdf_metadata yes)
		(pdf_single_document no)
		(dxfpolygonmode yes)
		(dxfimperialunits yes)
		(dxfusepcbnewfont yes)
		(psnegative no)
		(psa4output no)
		(plot_black_and_white yes)
		(sketchpadsonfab no)
		(plotpadnumbers no)
		(hidednponfab no)
		(sketchdnponfab yes)
		(crossoutdnponfab yes)
		(subtractmaskfromsilk no)
		(outputformat 1)
		(mirror no)
		(drillshape 1)
		(scaleselection 1)
		(outputdirectory "")
	)
)""")


def _settings_node(node):
    if sx.head(node) != "setup":
        return node
    node = list(node)
    tenting = sx.child(node, "tenting")
    if tenting is not None and not any(isinstance(v, list) for v in tenting):
        node[node.index(tenting)] = ["tenting", *[
            [side, "yes" if side in sx.atoms(tenting) else "no"]
            for side in ("front", "back")]]
    for default in _SETUP_DEFAULTS[1:]:
        if sx.child(node, sx.head(default)) is None:
            node.append(default)
    return node


def board_state(path: Path, renames=None) -> dict:
    try:
        root = sx.parse(path.read_text())
    except (OSError, sx.SExprError) as exc:
        raise SchematicError(f"cannot inspect PCB: {exc}") from exc
    if sx.head(root) != "kicad_pcb":
        raise SchematicError("expected a KiCad PCB")
    if sx.atom(sx.child(root, "version")) != BOARD_FORMAT:
        raise SchematicError("save the PCB with the pinned KiCad 10.0.3 before synchronization")
    footprints = {}
    for fp in sx.children(root, "footprint"):
        props = properties(fp)
        identity = sx.atom(sx.child(fp, "path"))
        if not identity or identity in footprints:
            raise SchematicError(f"{props.get('Reference', '?')}: missing or duplicate native schematic identity")
        pad_nets = {}
        for pad in sx.children(fp, "pad"):
            number = sx.atom(pad)
            if not number:
                continue
            net = sx.child(pad, "net")
            name = sx.atom(net, 2) or sx.atom(net)
            if number in pad_nets and pad_nets[number] != name:
                raise SchematicError(f"{props.get('Reference')}.{number}: duplicate pads disagree on net")
            pad_nets[number] = name
        spatial = [n for n in fp if isinstance(n, list) and
                   (sx.head(n) in {"at", "layer", "uuid", "locked"} or sx.head(n).startswith("fp_"))]
        # Preserve field geometry/visibility, but allow approved field-value updates.
        for pad in sx.children(fp, "pad"):
            spatial.append([v for v in pad if not isinstance(v, list) or sx.head(v) != "net"])
        spatial.extend(sx.children(fp, "model"))
        if "locked" in sx.atoms(fp):
            spatial.append(["locked", "yes"])
        field_geometry = {sx.atom(n): _normal(["property", *[p for p in n if isinstance(p, list)]]) for n in sx.children(fp, "property")}
        footprints[identity] = {"reference": props.get("Reference", ""), "footprint": sx.atom(fp),
                                "dnp": "dnp" in sx.atoms(sx.child(fp, "attr") or []), "exclude_bom": "exclude_from_bom" in sx.atoms(sx.child(fp, "attr") or []),
                                "fields": props, "field_geometry": field_geometry, "pads": pad_nets, "spatial": _normal(["spatial", *spatial], renames)}
    artwork = [n for n in root if isinstance(n, list) and
               (sx.head(n).startswith("gr_") or sx.head(n) in {"segment", "arc", "via", "zone", "dimension", "image", "target", "group"})]
    settings = [_settings_node(n) for n in root if isinstance(n, list) and sx.head(n) in {"layers", "setup", "general"}]
    return {"footprints": footprints, "artwork": _normal(["artwork", *artwork], renames),
            "settings": _normal(["settings", *settings], renames)}


def _approved(project_dir):
    from pcbforge.status import read_status_document, _latest_events, _approval_is_current, inspect_status, PHASE_NUMBER
    document = read_status_document(project_dir)
    latest, reopens = _latest_events(document.events)
    item = latest.get("circuit")
    event = item[1] if item else None
    if event is None or event.action != "complete" or not _approval_is_current(project_dir, "circuit", event, document):
        raise SchematicError("approve the current checked schematic before updating the PCB")
    if item[0] <= max((index for phase,index in reopens.items() if PHASE_NUMBER[phase] <= PHASE_NUMBER["circuit"]), default=-1):
        raise SchematicError("circuit approval predates an upstream reopen")
    if not all(p.complete for p in inspect_status(project_dir, document=document).phases[:2]):
        raise SchematicError("complete SPEC and ARCHITECT before updating the PCB")
    return document, event


def prepare_pcb_update(project_dir: Path) -> Path:
    project_dir = project_dir.resolve()
    read_evidence(project_dir, require_presentation=True)
    document, approval = _approved(project_dir)
    graph = load_graph(project_dir)
    board = project_schematic(project_dir).with_suffix(".kicad_pcb")
    raw = board.read_bytes()
    before = board_state(board)
    previous = None
    if (project_dir / SYNC_PATH).is_file():
        previous = json.loads((project_dir / SYNC_PATH).read_text())
    old_graph = from_payload(previous["graph"]) if previous else None
    existing = project_dir / UPDATE_PATH
    superseded = None
    pending = None
    if existing.is_file():
        pending = json.loads(existing.read_text())
        consumed = previous and previous.get("update_sha256") == hashlib.sha256(existing.read_bytes()).hexdigest()
        if not consumed and pending.get("approval") == approval.approval_fingerprint and pending.get("before_sha256") != hashlib.sha256(raw).hexdigest():
            raise SchematicError("PCB changed after update preparation; check the pending update before replacing its baseline")
    if before["footprints"] and old_graph is None:
        raise SchematicError("existing PCB has no native synchronization baseline; fresh projects only")
    already_removed = []
    if old_graph is not None:
        # A prior user edit can already have removed footprints that the current
        # approved schematic also removes. Preserve the actual PCB as baseline.
        current_ids = {c.identity for c in graph.components if not c.exclude_board}
        prefix = "/" + old_graph.root_uuid + "/"
        remaining = []
        for component in old_graph.components:
            native_id = "/" + component.identity[len(prefix):]
            if (not component.exclude_board
                    and component.identity.startswith(prefix)
                    and component.identity not in current_ids
                    and native_id not in before["footprints"]):
                already_removed.append(component.reference)
            else:
                remaining.append(component)
        try:
            _parity(replace(old_graph, components=tuple(remaining)), before)
        except SchematicError:
            # A separately approved revision can already be on the board when a
            # newer circuit revision is approved. This starts a NEW preservation
            # interval; it must not manufacture a completed synchronization receipt.
            if (not pending or pending.get("schema") != 1
                    or pending.get("approval") == approval.approval_fingerprint
                    or document is None
                    or not any(e.phase == "circuit" and e.action == "complete"
                               and e.approval_fingerprint == pending.get("approval")
                               for e in document.events)):
                raise
            candidate = from_payload(pending["graph"])
            predecessor = from_payload(pending["previous_graph"])
            if candidate.fingerprint != pending.get("circuit") or predecessor.fingerprint != old_graph.fingerprint:
                raise SchematicError("pending PCB revision has an invalid graph or predecessor")
            prior_backup = (project_dir / pending["backup"]).resolve()
            if (not prior_backup.is_relative_to(project_dir / "pcb-update-backups")
                    or not prior_backup.is_file()
                    or hashlib.sha256(prior_backup.read_bytes()).hexdigest() != pending.get("before_sha256")):
                raise SchematicError("pending PCB revision backup is missing or changed")
            _parity(candidate, before)
            prior_raw = existing.read_bytes()
            prior_sha = hashlib.sha256(prior_raw).hexdigest()
            archive = project_dir / "review/circuit" / f"pcb-update-superseded-{prior_sha}.json"
            superseded = (archive, prior_raw)
            old_graph = candidate
            already_removed = []
    backup = project_dir / "pcb-update-backups" / f"{board.stem}-{uuid.uuid4().hex}.kicad_pcb"
    backup.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": 1, "approval": approval.approval_fingerprint, "circuit": graph.fingerprint,
            "before_sha256": hashlib.sha256(raw).hexdigest(), "backup": backup.relative_to(project_dir).as_posix(),
            "before": before, "graph": graph.payload(), "previous_graph": old_graph.payload() if old_graph else None,
            "already_removed": sorted(already_removed),
            "changes": semantic_diff(old_graph, graph) if old_graph else [f"Add {c.reference}" for c in graph.components if not c.exclude_board]}
    existing.parent.mkdir(parents=True, exist_ok=True)
    if board.read_bytes() != raw:
        raise SchematicError("PCB changed during preparation; retry")
    if superseded:
        data["superseded_update"] = {
            "archive": superseded[0].relative_to(project_dir).as_posix(),
            "sha256": hashlib.sha256(superseded[1]).hexdigest(),
            "summary": "Current PCB matches the prior approved circuit. Prior spatial preservation was not certified; this backup starts a new preservation interval.",
        }
    commit_outputs(([superseded] if superseded else []) + [(backup, raw), (existing, canonical(data))], label="PCB update baseline")
    return existing


def _parity(graph, state):
    # KiCad PCB paths are relative to the root sheet. The graph keeps the
    # root UUID for project-scoped identity; retain every child-sheet segment.
    prefix = "/" + graph.root_uuid + "/"
    expected = {}
    for component in graph.components:
        if component.exclude_board:
            continue
        if not component.identity.startswith(prefix):
            raise SchematicError(f"{component.reference}: identity is outside the schematic root")
        identity = "/" + component.identity[len(prefix):]
        if identity in expected:
            raise SchematicError("duplicate native schematic identity")
        expected[identity] = component
    actual = state["footprints"]
    if expected.keys() != actual.keys():
        missing = [expected[k].reference for k in expected.keys()-actual.keys()]
        extra = [actual[k]["reference"] for k in actual.keys()-expected.keys()]
        raise SchematicError(f"schematic/PCB footprint identities differ; missing {missing}, unexpected {extra}")
    for identity, component in expected.items():
        fp = actual[identity]
        if component.reference != fp["reference"] or component.footprint != fp["footprint"]:
            raise SchematicError(f"{component.reference}: reference or footprint differs from schematic")
        if fp["fields"].get("Value") != component.value:
            raise SchematicError(f"{component.reference}: PCB value differs from schematic")
        if fp["dnp"] != component.dnp or fp["exclude_bom"] != component.exclude_bom:
            raise SchematicError(f"{component.reference}: PCB DNP or BOM flag differs from schematic")
        for field in set(component.fields) - {"Footprint", "dnp", "exclude_from_bom", "exclude_from_board"}:
            if component.fields.get(field, "") != fp["fields"].get(field, ""):
                raise SchematicError(f"{component.reference}: PCB field {field} differs; enable footprint field updates in KiCad")
        expected_pins = {p.number: p for p in component.pins}
        for number, net in fp["pads"].items():
            pin = expected_pins.get(number)
            if pin is None:
                raise SchematicError(f"{component.reference}.{number}: pad is absent from schematic")
            # KiCad can omit a net for an intentional NC; it must never connect elsewhere.
            if pin.no_connect:
                if net and net != pin.net:
                    raise SchematicError(f"{component.reference}.{number}: intentional no-connect is connected")
            elif net != pin.net:
                raise SchematicError(f"{component.reference}.{number}: PCB net {net!r} differs from {pin.net!r}")
        if {p.number for p in component.pins if p.electrical != "no_connect"} - fp["pads"].keys():
            raise SchematicError(f"{component.reference}: required pads are missing")


@dataclass(frozen=True)
class PCBUpdateResult:
    fingerprint: str
    graph: dict
    board: dict
    summary: str


def check_pcb_update(project_dir: Path) -> PCBUpdateResult:
    project_dir = project_dir.resolve()
    read_evidence(project_dir)
    _, approval = _approved(project_dir)
    try:
        data = json.loads((project_dir / UPDATE_PATH).read_text())
    except (OSError, ValueError) as exc:
        raise SchematicError("run prepare-pcb-update before the native KiCad update") from exc
    graph = load_graph(project_dir)
    if data.get("schema") != 1 or data.get("approval") != approval.approval_fingerprint or data.get("circuit") != graph.fingerprint:
        raise SchematicError("PCB update baseline is stale")
    backup = (project_dir / data["backup"]).resolve()
    if not backup.is_relative_to(project_dir / "pcb-update-backups") or hashlib.sha256(backup.read_bytes()).hexdigest() != data["before_sha256"]:
        raise SchematicError("PCB update backup is missing or changed")
    renames = {}
    if data["previous_graph"]:
        old = from_payload(data["previous_graph"])
        def nets_by_identity(g):
            return {frozenset((g.component(e.rsplit('.',1)[0]).identity, e.rsplit('.',1)[1]) for e in nodes): name
                    for name,nodes in g.nets.items()}
        a, b = nets_by_identity(old), nets_by_identity(graph)
        renames = {a[key]: b[key] for key in a.keys() & b.keys() if a[key] != b[key]}
    before = board_state(backup, renames)
    board = project_schematic(project_dir).with_suffix(".kicad_pcb")
    raw = board.read_bytes()
    after = board_state(board)
    _parity(graph, after)
    if before["artwork"] != after["artwork"] or before["settings"] != after["settings"]:
        raise SchematicError("PCB update changed existing copper, zones, board settings or artwork; inspect the backup")
    for identity in before["footprints"].keys() & after["footprints"].keys():
        a, b = before["footprints"][identity], after["footprints"][identity]
        if a["footprint"] == b["footprint"]:
            if a["spatial"] != b["spatial"] or any(b["field_geometry"].get(k) != v for k, v in a["field_geometry"].items()):
                raise SchematicError(f"{b['reference']}: PCB update changed placement or existing drawing/field geometry")
        else:
            # A package replacement changes footprint-local artwork, but not its placement.
            for tag in ("at", "layer"):
                get = lambda state: next((n for n in state["spatial"] if isinstance(n,list) and n[0] == tag), None)
                if get(a) != get(b):
                    raise SchematicError(f"{b['reference']}: footprint replacement changed {tag}")
    if board.read_bytes() != raw:
        raise SchematicError("PCB changed during verification; retry")
    return PCBUpdateResult(digest({"approval": approval.approval_fingerprint, "graph": graph.fingerprint}),
                           graph.payload(), after, "native PCB parity and spatial preservation passed")


def finish_circuit(project_dir: Path, *, now=None):
    from pcbforge.status import TransitionEvent, read_status_document, write_status, _now
    result = check_pcb_update(project_dir)
    path = project_dir / SYNC_PATH
    payload = {"schema": 1, "fingerprint": result.fingerprint, "graph": result.graph,
               "update_sha256": hashlib.sha256((project_dir / UPDATE_PATH).read_bytes()).hexdigest()}
    original = path.read_bytes() if path.exists() else None
    path.write_bytes(canonical(payload))
    try:
        document = read_status_document(project_dir)
        event = TransitionEvent(now or _now(), "circuit-sync", "complete", result.summary, content_fingerprint=result.fingerprint)
        return write_status(project_dir, document=replace(document, transition_events=(*document.transition_events, event)), now=now)
    except BaseException:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
        raise


def sync_is_current(project_dir: Path, approval: str, document=None) -> bool:
    try:
        data = json.loads((project_dir / SYNC_PATH).read_text())
        graph = load_graph(project_dir)
        if data.get("schema") != 1 or data["fingerprint"] != digest({"approval": approval, "graph": graph.fingerprint}):
            return False
        if from_payload(data["graph"]).fingerprint != graph.fingerprint:
            return False
        from pcbforge.status import read_status_document, _latest_transition_events
        event = _latest_transition_events((document or read_status_document(project_dir)).transition_events).get("circuit-sync")
        if event is None or event.action != "complete" or event.content_fingerprint != data["fingerprint"]:
            return False
        _parity(graph, board_state(project_schematic(project_dir).with_suffix(".kicad_pcb")))
        return True
    except (OSError, KeyError, ValueError):
        return False
