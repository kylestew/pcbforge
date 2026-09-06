# Native layout regression fixture

`native-layout.kicad_pcb` derives from KiCad's multichannel demo, licensed CC-BY-SA 4.0.
The source and full attribution are in [the pilot notice](../../pilots/kicad9-multichannel/NOTICE.md).
The source fixture used KiCad 9.0.9. This copy changes the version and net serialization
for KiCad 10 parser regression tests. Component and copper geometry is preserved.
This conversion is test-data preparation, not a project migration or GUI validation.
