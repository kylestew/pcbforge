#!/usr/bin/env python3
"""Native workflow regression pilot. Writes only to a new output directory."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pcbforge.electrical import CircuitTestContext
from pcbforge.initialize import initialize_project
from pcbforge.policy import render_default_policy
from pcbforge.schematic import extract, semantic_diff, source_files
from pcbforge.schematic_edit import SchematicDocument
from pcbforge.schematic_lint import lint_saved
from pcbforge.status import review_phase, approve_phase, inspect_status
from pcbforge.kicad_tools import check_version


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def motor_requirements(graph):
    """Independent expectations from the frozen Roamer hardware README."""
    ctx = CircuitTestContext(graph, {})
    for ref in ('U3', 'U4'):
        ctx.rail(ref+'.1', 'VBAT_SW')
        ctx.rail(ref+'.8', '+3V3')
        ctx.rail(ref+'.4', 'GND')
        ctx.decoupling(ref+'.8', ref+'.4', minimum=90e-9, maximum=110e-9)
    ctx.within('left VM bypass', ctx.value('C11','F'), 90e-9,110e-9,'F')
    ctx.within('right VM bypass', ctx.value('C14','F'), 90e-9,110e-9,'F')
    ctx.isolated('U3.1','U3.8')
    ctx.isolated('U4.1','U4.8')
    return ctx.measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('output must be a new directory')
    if output.is_relative_to(ROOT):
        parser.error('keep output outside the tool checkout so initialization has a clean pin')
    # Initialization must bind a real clean revision; no metadata mocks.
    dirty = subprocess.check_output(['git','status','--short'], cwd=ROOT, text=True)
    if dirty:
        parser.error('commit the implementation before running the clean-pin pilot')
    check_version(ROOT)
    output.mkdir(parents=True)
    results = {'kicad':'10.0.3', 'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
               'gui_validation':'pending user-operated check', 'hardware_approval':False}
    initialized = []
    for layers in (2,4):
        name = f'native-{layers}layer'
        project = output/name; project.mkdir()
        (project/'spec.md').write_text(f'''---
spec_schema: 1
name: {name}
layers: {layers}
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: []
board_mm: [50, 40]
---
# Synthetic native initialization fixture
This disposable project tests initialization. It is not a hardware design.
''')
        (project/'policy.yaml').write_text(render_default_policy())
        review = review_phase(project, 'spec')
        if not review.ready:
            raise RuntimeError(review.detail)
        approve_phase(project, 'spec', review.fingerprint, 'Synthetic regression fixture approval; not a user hardware decision')
        initialize_project(project, tool_root=ROOT)
        graph = extract(project/f'{name}.kicad_sch')
        assert not graph.components
        assert inspect_status(project).current.phase.key == 'architect'
        board = project/f'{name}.kicad_pcb'
        assert ('"In1.Cu"' in board.read_text()) == (layers == 4)
        initialized.append({'layers':layers,'schematic_sha256':sha(project/f'{name}.kicad_sch'),'board_sha256':sha(board)})
    results['fresh_initialization'] = initialized

    original = ROOT/'pilots/roamer-rev-a/baseline/source/hardware/driver-board'
    files = [*original.glob('*.kicad_sch'), *original.glob('*.kicad_pro'), *original.glob('*.kicad_pcb')]
    source_hashes = {str(p.relative_to(ROOT)):sha(p) for p in files}
    copied = output/'roamer-native'; copied.mkdir()
    for file in files:
        shutil.copy2(file, copied/file.name)
    path = copied/'driver-board.kicad_sch'
    initial = extract(path)
    assert len(initial.components) == 69 and len(initial.nets) == 67
    checks = motor_requirements(initial)
    bytewise = {p:sha(p) for p in source_files(path)}
    doc = SchematicDocument.load(path)
    doc.save()
    assert bytewise == {p:sha(p) for p in bytewise}, 'no-op save changed native source'
    doc.text_note('Native workflow round-trip regression', (20.32,20.32))
    backup = doc.save()
    cosmetic = extract(path)
    assert initial.fingerprint == cosmetic.fingerprint
    assert backup and backup.is_file()

    child = copied/'motor_control.kicad_sch'
    before_fault = child.read_bytes()
    doc = SchematicDocument.load(child)
    doc.set_field('C11','Value','1pF')
    fault_backup = doc.save()
    faulty = extract(path)
    delta = semantic_diff(initial, faulty)
    try:
        motor_requirements(faulty)
    except AssertionError as exc:
        fault = str(exc)
    else:
        raise AssertionError('deliberate bypass fault was not detected')
    assert fault_backup and fault_backup.read_bytes() == before_fault
    child.write_bytes(before_fault)
    assert extract(path).fingerprint == initial.fingerprint
    assert source_hashes == {str(p.relative_to(ROOT)):sha(p) for p in files}
    assert sha(copied/'driver-board.kicad_pcb') == sha(original/'driver-board.kicad_pcb')
    previews = output/'previews'; previews.mkdir()
    subprocess.run([str(ROOT/'scripts/kicad-cli'),'sch','export','svg','--output',str(previews),str(path)],check=True,capture_output=True,text=True)
    findings = lint_saved(path)
    (output/"readability-findings.json").write_text(json.dumps([dict(asdict(f), id=f.identifier) for f in findings],indent=2)+"\n")
    results['representative_round_trip'] = {'source_revision':'3bc3361573dd21070efe9f76bba473947e2a0c21',
        'components':len(initial.components),'nets':len(initial.nets),'sheets':len(source_files(path)),
        'graph_sha256':initial.fingerprint,'source_sha256':source_hashes,'measurements':checks,'no_op_bytes_preserved':True,
        'cosmetic_graph_preserved':True,'source_and_routed_pcb_preserved':True,
        'deliberate_fault_rejected':fault,'fault_delta':delta,'lint_findings':len(findings),
        'lint_summary':dict(Counter(f.code for f in findings)),
        'preview_sha256':{p.name:sha(p) for p in previews.glob('*.svg')},
        'full_circuit_acceptance':'not claimed: historical design lacks the new sourced-facts contract'}
    (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))


if __name__ == '__main__':
    main()
