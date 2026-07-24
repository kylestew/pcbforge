# Spec interview — step-one playbook

You are an AI agent starting a new pcbforge board. The user triggered this with
**"pcbforge: new board"**. Your job in this phase: run a focused interview and
produce `spec.md` — the project's spine. Nothing else is created in this
phase; no code, no scaffold, no parts.

Vendor-neutral: any agent with file read/write follows this document.

## Ground rules

1. **Conversation, not a form.** Start by inviting the user's initial idea,
   then ask only about gaps and conflicts. Never ask more than ~3 questions
   per turn.
2. **Propose, then confirm.** Offer a sensible default with one-line
   rationale; let the user veto. Don't interrogate.
3. **No exact chip.** Spec records STM32 *family* + constraints. A specific
   part number goes in only if the user names one unprompted.
4. **Layers are decided here** (2 or 4). Don't defer it.
5. **Unknowns become Open risks**, not blockers. Ship the spec with honest
   holes rather than stalling the interview.
6. **Disagree openly** when a want is infeasible for the constraint set
   (KiCad 9 flow, JLCPCB 2/4-layer hobby, LCSC parts, one-offs). Say why,
   offer the nearest feasible shape.
7. **The user gates.** They decide when spec is good. Then (and only then)
   the project moves to `init`.

## Interview flow

1. Ask the first question: **"What's your initial idea for the board? A rough
   brain-dump of what you'd like it to do is perfect."** If the trigger already
   included the idea, treat that as the answer and do not ask again.
2. Walk the dimensions below; fill gaps, flag conflicts.
3. Propose: STM32 family, rail plan, layer count, module candidates (only if
   the module library has entries — it may be empty; say so plainly),
   rough BOM feasibility vs ceiling.
4. Draft `spec.md` (format below). Show it.
5. Iterate until the user declares it good.
6. Remind: `spec.md` is a **living doc** — later changes go here first, and
   frontmatter must be updated in the same edit as the prose.

## Dimensions

| Dimension | Probe | Default if unstated |
|---|---|---|
| Purpose | one sentence, what the board does | — (must have) |
| Power in | USB-C? battery? barrel? voltage range? | usb-c |
| Rails | 3V3 only? 5V needed? analog rail? | [+3V3] |
| MCU class | flash/RAM/pin needs → family (table below) | G0 |
| Peripherals | usb-fs, i2c, spi, uart, adc, dac, pwm, can | from purpose |
| Connectors | what physically plugs in | from peripherals |
| I/O budget | GPIO count → package size | count + 20% slack |
| Size / form | dims, mounting holes, enclosure? | 50×40 mm, 4×M3 |
| **Layers** | density, analog/RF, USB routing comfort | 2 (heuristic below) |
| Special | analog precision, RF, high current, thermal, low power | none |
| Cost / qty | BOM ceiling per board; board count | qty 5 (JLC min) |
| Debug | SWD is always present (invariant); debug UART? test points? | uart yes |

### STM32 family quick guide

| Family | Shape | Pick when |
|---|---|---|
| C0 | cheapest, small flash | trivial logic, cost floor |
| G0 | modern budget workhorse, USB FS on some | default hobby choice |
| G4 | analog-rich (fast ADC, comparators, timers) | motor, power, precision analog |
| F4 | classic performance, big community | heavier compute, legacy examples |
| L4 / U5 | low power | battery life matters |
| H7 | heavy compute | rarely justified in this flow |

Prefer families with strong LCSC/JLC basic-parts availability; flag it as an
open risk if the family typically lands extended-parts-only.

### Layer heuristic

- **2-layer default.** Hobby densities, USB FS, I2C/SPI/UART all route fine.
- **4-layer when:** fine-pitch/BGA packages, precision analog wanting a quiet
  reference plane, RF, or the user simply wants routing comfort and accepts
  the cost bump. Record the reason in prose.

### Cost sanity (JLCPCB, order of magnitude)

- 2-layer boards: trivially cheap; 4-layer: moderate bump.
- Assembly: basic parts cheap; each **extended** part adds a loading fee —
  prefer basic-library parts; note likely-extended parts in Open risks.

## Output — `spec.md`

Two zones. **YAML frontmatter = machine contract** (`init` reads ONLY this,
via `yaml.safe_load`, and fails loud listing any missing/invalid key — it
never guesses). **Markdown body = human zone** (intent, for the user and for
future agent sessions recovering context). You are responsible for keeping
frontmatter and prose consistent — every prose change that affects a key
updates the key in the same edit.

### Frontmatter schema — v1

| Key | Type | Req | Allowed / format |
|---|---|---|---|
| `spec_schema` | int | ✓ | `1` |
| `name` | str | ✓ | kebab-case, becomes project dir name |
| `layers` | int | ✓ | `2` or `4` |
| `stm32_family` | str | ✓ | `C0 G0 G4 F0 F1 F4 L0 L4 U5 H7` |
| `power_in` | str | ✓ | `usb-c battery-liion battery-aa barrel header other` |
| `rails` | list[str] | ✓ | canonical power net names, e.g. `+3V3`, `+5V`; ≥1 |
| `peripherals` | list[str] | ✓ | of `usb-fs i2c spi uart adc dac pwm can other`; may be `[]` |
| `board_mm` | [num,num] | ✓ | `[width, height]` |
| `connectors` | list[str] |  | free text, short |
| `mounting` | str |  | e.g. `4x M3` |
| `qty` | int |  | default 5 (JLC minimum) |
| `bom_ceiling_usd` | num |  | per-board target |
| `modules_planned` | list[str] |  | names from the module library index |
| `debug_uart` | bool |  | default true; SWD is an invariant, not a key |
| `special` | list[str] |  | of `analog-precision rf high-current thermal low-power` |

Schema changes bump `spec_schema` and this file — this table IS the
spec↔`init` coupling.

### Body template

```markdown
# <name>

## Purpose
One paragraph. What it does, for whom, success criterion.

## Function
Signal chain / behavior prose. Peripheral-by-peripheral intent.

## Open risks
- honest unknowns (family stock, crystal vs HSI, extended parts, …)

## Decisions log
- YYYY-MM-DD: <decision + one-line why>
```

### Example frontmatter

```yaml
---
spec_schema: 1
name: garden-logger
layers: 2
stm32_family: G0
power_in: usb-c
rails: [+3V3]
peripherals: [usb-fs, i2c, adc]
board_mm: [50, 40]
connectors: [usb-c, qwiic]
qty: 5
bom_ceiling_usd: 8
debug_uart: true
special: []
---
```

## After the gate

User declares spec good → next phase is `init` (project scaffold), then
ARCHITECT (module graph proposal in code). Not this playbook's job — stop at
a good `spec.md`.

Session-resume note: any later session re-reads `spec.md` first. Keep the
prose good enough that a cold agent recovers full intent from it.
