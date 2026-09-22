# Editing native schematics

Load the saved file with `pcbforge.schematic_edit.SchematicDocument.load(path)`.
Use `create(path)` only for a new sheet. Keep the existing UUIDs during revisions.

```python
from pathlib import Path
from pcbforge.schematic_edit import SchematicDocument

sheet = SchematicDocument.load(Path("controller.kicad_sch"))
with sheet.transaction():
    sheet.set_field("R1", "Value", "4k7")
    sheet.text_note("Input pull-up: see the interface requirement.", (25.4, 25.4))
    sheet.save()
```

The editor preserves untouched top-level records. It stages and validates a complete local sheet hierarchy before replacing the edited file.
A failed validation leaves the source unchanged. A successful edit keeps a backup. Concurrent source changes cause the save to fail.
Reload after a human or KiCad saves the file.

The API provides symbol insertion, field edits, movement, wires, labels, junctions, no-connects and child sheets.
After connectivity exists, use `pcbforge set-netclass` to assign functional colors in the native project.
Use exact names from the extracted nets, including hierarchy prefixes.
Save and close the KiCad project before the command, then reopen it to load the new settings.
`pin_position` uses the embedded symbol definition, unit, rotation and mirror.
Use `bind_instance` to give each repeated child sheet its native project path and reference mapping.
Use `add_sheet` and native hierarchical labels for sheet interfaces.

Use millimetres on KiCad's connection grid, normally 1.27 mm. Prefer continuous wires for functional paths.
Keep labels, values and references clear of component bodies and wires.
Moving a symbol does not move its wires. Check the resulting circuit after every structural edit.

Run `pcbforge check-circuit --write-report`, then inspect the SVG previews from every changed sheet.
`pcbforge preview-schematic` exports a drawing without granting acceptance or approval.
The saved-file lint uses approximate text extents. Inspect each reported overlap and document only exact, justified exclusions.

Use `add_sheet`, `sheet_pin` and matching child `hierarchical_label` records for native sheet interfaces.
Use `bind_instance` to assign reference names to each repeated sheet instance. Preserve the existing instance UUID paths.
