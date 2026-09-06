"""Native project fixtures. Seeded evidence is a unit-test setup, not an audit."""
import hashlib
import io
import csv
import json
from pathlib import Path
import yaml
from pcbforge.schematic_edit import SchematicDocument
from pcbforge.schematic import canonical, digest
from pcbforge.circuit import load_graph, fingerprint_inputs, presentation_fingerprint, GRAPH_PATH, BOM_PATH, BOM_CSV_PATH, REPORT_PATH, EVIDENCE_PATH


def seed_native(project, components=None, *, evidence=True):
    from pcbforge.initialize import read_spec
    spec = read_spec(project / 'spec.md')
    path = project / f'{spec.name}.kicad_sch'
    doc = SchematicDocument.create(path)
    components = components or [{'ref': 'R1', 'value': '10k', 'mpn': 'TEST-R', 'lcsc': 'C1', 'footprint': 'Resistor_SMD:R_0603_1608Metric'}]
    for i, item in enumerate(components):
        ref=item['ref']
        doc.add_symbol(ref, 'Device:R', (50+i*25, 50), value=item.get('value',item['mpn']), footprint=item['footprint'],
                       fields={'MPN': item['mpn'], 'LCSC': item['lcsc'], 'Datasheet': 'https://example.test/datasheet', 'pcbforge_purpose': 'Unit test fixture'})
        for pin, name in [('1','+3V3'),('2','GND')]:
            at=doc.pin_position(ref,pin)
            doc.label(name,at,kind='global_label')
    path.write_text(doc.serialize())
    (project / 'circuit-review.yaml').write_text('circuit_review_schema: 4\nerc_exclusions: []\nreadability_exclusions: []\n')
    contract={'circuit_tests_schema':1, 'requirements':[{'id':n,'description':n,'tests':['supply']} for n in ['power','mcu','interfaces','protection','component-ratings']],
              'tests':[{'id':'supply','callable':'circuit_tests.py:test_supply'}]}
    (project / 'circuit-tests.yaml').write_text(yaml.safe_dump(contract))
    (project / 'circuit_tests.py').write_text('def test_supply(ctx):\n    ctx.require(ctx.graph.components[0].pins[0].net == "+3V3", "supply net")\n')
    (project / 'electrical-facts.yaml').write_text('electrical_facts_schema: 1\nvalues: {}\nparts: {}\nmcu: {}\n')
    if evidence:
        seed_evidence(project)
    return path


def seed_evidence(project):
    graph=load_graph(project)
    output=io.StringIO();w=csv.writer(output,lineterminator='\n')
    w.writerow(('Designators','Quantity','MPN','LCSC','Footprint'))
    for row in graph.bom():
        w.writerow((','.join(row['designators']),row['quantity'],row['mpn'],row['lcsc'],row['footprint']))
    report='# Unit-test seeded circuit evidence\n'
    preview=project/'review/circuit/preview';preview.mkdir(parents=True,exist_ok=True)
    image=b'<svg xmlns="http://www.w3.org/2000/svg"><text>Unit fixture</text></svg>'
    (preview/'fixture.svg').write_bytes(image)
    evidence={'previews':{'fixture.svg':hashlib.sha256(image).hexdigest()},'schema':2,'result':'pass','fingerprint':fingerprint_inputs(project),
              'presentation_fingerprint':presentation_fingerprint(project),
              'graph_hash':graph.fingerprint,'bom_hash':digest(graph.bom()),
              'report_hash':hashlib.sha256(report.encode()).hexdigest(),
              'bom_csv_hash':hashlib.sha256(output.getvalue().encode()).hexdigest()}
    for relative,data in [(GRAPH_PATH,canonical(graph.payload())),(BOM_PATH,canonical({'schema':1,'components':graph.bom()})),
                          (BOM_CSV_PATH,output.getvalue().encode()),(REPORT_PATH,report.encode()),(EVIDENCE_PATH,canonical(evidence))]:
        path=project/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    return graph
