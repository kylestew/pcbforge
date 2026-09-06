# Native KiCad regression pilot

Run from a clean implementation checkout with the pinned KiCad installation:

```sh
toolchain/.venv/bin/python pilots/native-kicad/run_pilot.py --output /private/tmp/native-kicad-pilot
```

The output directory must not exist. The pilot creates disposable two- and four-layer projects with real revision pins.
Their SPEC approval records are synthetic regression fixtures. They do not represent a user's hardware approval.

The representative schematic comes from the frozen Roamer release already retained in this repository.
It has an STM32 MCU, power conversion, USB and two motor channels across three connected native sheets.
The pilot checks extraction, unchanged saves, cosmetic edits, backups, electrical changes and independent motor-supply requirements.
A deliberately incorrect bypass capacitor must fail the requirement test. Source files and the routed PCB must retain their original hashes.

The pilot writes SVG previews and `results.json` outside the checkout.
The historical design has no new electrical-facts contract, so this pilot does not claim complete circuit acceptance or migrate that project.

The unit suite covers single CIRCUIT approval, simulated PCB updates, parity, routed geometry and durable invalidation.
A user-operated KiCad save, PCB update and final visual acceptance remain a separate validation step.
Command-line extraction and simulated updates do not establish GUI behavior.
