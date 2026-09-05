"""Connected-path reference drawing: the tidy style `agent/circuit-kicad.md` asks for.

A USB-powered STM32 encoder knob: supply chain from the USB header through
the fuse and LDO to the MCU, encoder contacts with pull-up and filter
branches, the LED series path, and the debug header with the reset network,
all drawn as continuous wires. Only the USB data pair uses net labels.
"""

from __future__ import annotations

from pcbforge.kicad_sch import ReviewSchematic

CONNECTED_MODEL = """circuit_model_schema: 1
components:
  - {reference: J1, kind: connector, value: USB 4-pin header, footprint: "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", mpn: PH4, lcsc: C1, purpose: USB power and data in.}
  - {reference: F1, kind: fuse, value: 500mA PPTC, footprint: "Fuse:Fuse_1206_3216Metric", mpn: 1206L050YR, lcsc: C2, purpose: VBUS overcurrent protection.}
  - {reference: U3, kind: ic, value: AP2112K-3.3, footprint: "Package_TO_SOT_SMD:SOT-23-5", mpn: AP2112K-3.3TRG1, lcsc: C3, purpose: 3.3 V regulator.}
  - {reference: C1, kind: capacitor, value: 1uF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP1, lcsc: C4, purpose: LDO input.}
  - {reference: C2, kind: capacitor, value: 1uF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP1, lcsc: C4, purpose: LDO output.}
  - {reference: U1, kind: ic, value: STM32G0B1KBT6, footprint: "Package_QFP:LQFP-32_7x7mm_P0.8mm", mpn: STM32G0B1KBT6, lcsc: C5, purpose: MCU.}
  - {reference: C3, kind: capacitor, value: 100nF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP2, lcsc: C6, purpose: MCU decoupling.}
  - {reference: SW1, kind: switch, value: EC11 encoder, footprint: "Rotary_Encoder:RotaryEncoder_Alps_EC11E-Switch_Vertical_H20mm_MountingHoles", mpn: EC11E15244B2, lcsc: C7, purpose: Rotary control with push switch.}
  - {reference: R3, kind: resistor, value: 10k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES10K, lcsc: C8, purpose: Encoder A pull-up.}
  - {reference: R4, kind: resistor, value: 10k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES10K, lcsc: C8, purpose: Encoder B pull-up.}
  - {reference: C5, kind: capacitor, value: 1nF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP3, lcsc: C9, purpose: Encoder A filter.}
  - {reference: C6, kind: capacitor, value: 1nF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP3, lcsc: C9, purpose: Encoder B filter.}
  - {reference: R5, kind: resistor, value: 10k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES10K, lcsc: C8, purpose: Push switch pull-up.}
  - {reference: D1, kind: led, value: RED, footprint: "LED_SMD:LED_0603_1608Metric", mpn: LED1, lcsc: C10, purpose: Status LED.}
  - {reference: R6, kind: resistor, value: 1k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES1K, lcsc: C11, purpose: LED current limit.}
  - {reference: J2, kind: connector, value: SWD 2x5 keyed, footprint: "knob:SWD_Header_2x05_Keyed", mpn: FTSH-105-01-L-DV-K, lcsc: C12, purpose: SWD debug header; position 7 is the key.}
  - {reference: R7, kind: resistor, value: 10k, footprint: "Resistor_SMD:R_0603_1608Metric", mpn: RES10K, lcsc: C8, purpose: Reset pull-up.}
  - {reference: C8, kind: capacitor, value: 100nF, footprint: "Capacitor_SMD:C_0603_1608Metric", mpn: CAP2, lcsc: C6, purpose: Reset filter.}
nets:
  - {id: vbus, display_name: VBUS, compiler_name: VBUS, nodes: [J1.1, F1.1]}
  - {id: protected-vbus, display_name: VBUS_PROT, compiler_name: VBUS_PROT, nodes: [F1.2, U3.1, U3.3, C1.1]}
  - {id: rail-3v3, display_name: +3V3, compiler_name: +3V3, nodes: [U3.5, C2.1, U1.4, C3.1, R3.1, R4.1, R5.1, R6.1, J2.1, R7.1]}
  - {id: gnd, display_name: GND, compiler_name: GND, nodes: [J1.4, U3.2, C1.2, C2.2, U1.5, C3.2, C5.2, C6.2, SW1.C, SW1.S2, J2.3, J2.5, J2.9, C8.2]}
  - {id: usb-dm, display_name: USB_DM, compiler_name: USB_DM, nodes: [J1.2, U1.22]}
  - {id: usb-dp, display_name: USB_DP, compiler_name: USB_DP, nodes: [J1.3, U1.23]}
  - {id: encoder-a, display_name: ENC_A, compiler_name: ENC_A, nodes: [U1.7, SW1.A, R3.2, C5.1]}
  - {id: encoder-b, display_name: ENC_B, compiler_name: ENC_B, nodes: [U1.8, SW1.B, R4.2, C6.1]}
  - {id: encoder-sw, display_name: ENC_SW, compiler_name: ENC_SW, nodes: [U1.9, SW1.S1, R5.2]}
  - {id: status-led, display_name: LED_K, compiler_name: LED_K, nodes: [U1.10, D1.1]}
  - {id: led-anode, display_name: LED_A, compiler_name: LED_A, nodes: [D1.2, R6.2]}
  - {id: swdio, display_name: SWDIO, compiler_name: SWDIO, nodes: [U1.24, J2.2]}
  - {id: swclk, display_name: SWCLK, compiler_name: SWCLK, nodes: [U1.25, J2.4]}
  - {id: reset, display_name: NRST, compiler_name: NRST, nodes: [U1.6, J2.10, R7.2, C8.1]}
__NC_NETS__
groups:
  - {id: usb-entry, title: USB entry and protection, purpose: USB header and VBUS fuse., references: [J1, F1]}
  - {id: regulation, title: 3.3 V regulation, purpose: LDO with its capacitors., references: [U3, C1, C2]}
  - {id: mcu, title: STM32 controller, purpose: MCU and decoupling., references: [U1, C3]}
  - {id: encoder, title: Encoder input, purpose: Contacts with pull-ups and filters., references: [SW1, R3, R4, C5, C6, R5]}
  - {id: indicator, title: Status indicator, purpose: MCU-sunk LED., references: [D1, R6]}
  - {id: debug, title: SWD and reset, purpose: Debug header and reset network., references: [J2, R7, C8]}
paths:
  - {id: supply, title: Supply chain, purpose: USB through fuse and LDO to the MCU., nodes: [J1.1, F1.1, F1.2, U3.1, U3.5, U1.4]}
  - {id: encoder-a-path, title: Encoder A, purpose: MCU to contact with branches., nodes: [U1.7, SW1.A]}
  - {id: led-path, title: LED drive, purpose: MCU sinks the LED through R6., nodes: [U1.10, D1.1, D1.2, R6.2, R6.1]}
  - {id: reset-path, title: Reset and SWD, purpose: Header to MCU., nodes: [J2.10, U1.6]}
"""

_UNUSED_PINS = (
    [f"U1.{n}" for n in range(1, 33) if n not in (4, 5, 6, 7, 8, 9, 10, 22, 23, 24, 25)]
    + ["U3.4", "SW1.MP", "J2.6", "J2.8"]
)
# every unused pad is a named single-node net, as `agent/circuit.md` requires
CONNECTED_MODEL = CONNECTED_MODEL.replace(
    "__NC_NETS__\n",
    "".join(
        f"  - {{id: nc-{node.replace('.', '-').lower()}, display_name: unused {node}, "
        f"compiler_name: NC_{node.replace('.', '_').upper()}, nodes: [{node}]}}\n"
        for node in _UNUSED_PINS
    ),
)

# the keyed header is the only part with no official footprint: pad 7 is the key
CONNECTED_PADS = {"J2": {"1", "2", "3", "4", "5", "6", "8", "9", "10"}}


def connected_nets() -> dict[str, list[str]]:
    """KiCad net name -> endpoints, as the fake netlist export reports them."""
    import yaml

    model = yaml.safe_load(CONNECTED_MODEL)
    return {net["compiler_name"]: list(net["nodes"]) for net in model["nets"]}


def draw_connected(sch: ReviewSchematic) -> None:
    p = sch.pin
    # --- supply chain, left to right along one rail -----------------------
    sch.place("J1", (45.72, 96.52), mirror="y", ref_pos=(38.1, 88.9), value_pos=(38.1, 91.44))
    sch.place("F1", (73.66, 71.12), rotation=90, ref_pos=(69.85, 66.04), value_pos=(69.85, 63.5))
    sch.place("U3", (101.6, 73.66), ref_pos=(96.52, 63.5), value_pos=(96.52, 66.04))
    sch.place("C1", (86.36, 81.28))
    sch.place("C2", (119.38, 81.28))
    rail_y = p("U3", 1)[1]
    sch.wire(p("J1", 1), (60.96, p("J1", 1)[1]), (60.96, rail_y), p("F1", 1), path="supply")
    sch.flag((60.96, rail_y))
    sch.label_at((60.96, 81.28), "vbus", direction="right")
    sch.label_at((83.82, rail_y), "protected-vbus", direction="up")
    sch.wire(p("F1", 2), p("U3", 1), path="supply")
    sch.flag((81.28, rail_y))
    sch.drop("C1", 1, rail_y)
    en = p("U3", 3)
    sch.wire(en, (91.44, en[1]), (91.44, rail_y))
    sch.power("U3", 2, "gnd", direction="down")
    sch.power("C1", 2, "gnd", direction="down")
    sch.power("J1", 4, "gnd", direction="down", flag=True)
    sch.label("J1", 2, "usb-dm", direction="right", length=5.08)
    sch.label("J1", 3, "usb-dp", direction="right", length=5.08)

    # --- MCU in the middle; the 3.3 V rail continues to VDD ---------------
    sch.place("U1", (165.1, 127.0), ref_pos=(139.7, 96.52), value_pos=(139.7, 99.06))
    sch.place("C3", (147.32, 81.28))
    vdd = p("U1", 4)
    sch.wire(p("U3", 5), (vdd[0], rail_y), vdd, path="supply")
    sch.drop("C2", 1, rail_y)
    sch.drop("C3", 1, rail_y)
    sch.power_at((132.08, rail_y), "rail-3v3", direction="up")
    sch.power("C2", 2, "gnd", direction="down")
    sch.power("C3", 2, "gnd", direction="down")
    sch.power("U1", 5, "gnd", direction="down")
    sch.label("U1", 22, "usb-dm", length=2.54)
    sch.label("U1", 23, "usb-dp", length=2.54)

    # --- encoder: continuous runs with pull-up and filter branches --------
    sch.place("SW1", (231.14, 106.68), ref_pos=(226.06, 96.52), value_pos=(226.06, 99.06))
    a_y, b_y = 63.5, 91.44
    a = p("SW1", "A")
    b = p("SW1", "B")
    sch.wire(p("U1", 7), (193.04, p("U1", 7)[1]), (193.04, a_y), (215.9, a_y), (215.9, a[1]), a, path="encoder-a-path")
    sch.wire(p("U1", 8), (195.58, p("U1", 8)[1]), (195.58, b_y), (208.28, b_y), (208.28, b[1]), b)
    sch.place("R3", (203.2, 55.88))
    sch.place("C5", (195.58, 68.58))
    sch.place("R4", (205.74, 83.82))
    sch.place("C6", (198.12, 96.52))
    sch.wire(p("R3", 2), (p("R3", 2)[0], a_y))
    sch.wire((p("C5", 1)[0], a_y), p("C5", 1))
    sch.wire(p("R4", 2), (p("R4", 2)[0], b_y))
    sch.wire((p("C6", 1)[0], b_y), p("C6", 1))
    sch.power("R3", 1, "rail-3v3", direction="up")
    sch.power("R4", 1, "rail-3v3", direction="up")
    sch.power("C5", 2, "gnd", direction="down")
    sch.power("C6", 2, "gnd", direction="down")
    sch.label_at((198.12, a_y), "encoder-a", direction="up")
    sch.label_at((201.93, b_y), "encoder-b", direction="up")
    sch.power("SW1", "C", "gnd", direction="left")
    sch.power("SW1", "S2", "gnd", direction="right")
    # push switch: around the encoder to S1, pull-up on the corner
    s1 = p("SW1", "S1")
    sw_y = 119.38
    sch.wire(p("U1", 9), (203.2, p("U1", 9)[1]), (203.2, sw_y), (251.46, sw_y), (251.46, s1[1]), s1)
    sch.place("R5", (259.08, sw_y), rotation=270)
    sch.wire((251.46, sw_y), p("R5", 2))
    sch.power("R5", 1, "rail-3v3", direction="up")
    sch.label_at((208.28, sw_y), "encoder-sw", direction="down")

    # --- LED: MCU sinks through the LED and the series resistor ----------
    sch.place("D1", (218.44, 132.08), ref_pos=(214.63, 127.0), value_pos=(220.98, 127.0))
    sch.place("R6", (233.68, 132.08), rotation=270, ref_pos=(229.87, 137.16), value_pos=(234.95, 137.16))
    k = p("D1", 1)
    sch.wire(p("U1", 10), (200.66, p("U1", 10)[1]), (200.66, k[1]), k, path="led-path")
    sch.wire(p("D1", 2), p("R6", 2), path="led-path")
    sch.power("R6", 1, "rail-3v3", direction="up", path="led-path")
    sch.label_at((200.66, 124.46), "status-led", direction="left")
    sch.label_at((226.06, k[1]), "led-anode", direction="up")

    # --- debug header and reset network, wired straight to the MCU -------
    sch.pin_names(
        "J2",
        {"1": "VTREF", "2": "SWDIO", "3": "GND", "4": "SWCLK", "5": "GND", "6": "SWO", "8": "NC", "9": "GND", "10": "NRST"},
        sides={"4": "right", "2": "right", "10": "right", "1": "top", "3": "bottom", "5": "bottom", "9": "bottom", "6": "left", "8": "left"},
        pitch=5.08,
    )
    sch.place("J2", (139.7, 190.5), ref_pos=(153.67, 203.2), value_pos=(153.67, 205.74))
    sch.place("R7", (114.3, 149.86))
    sch.place("C8", (114.3, 175.26))
    reset = p("U1", 6)
    nrst = p("J2", 10)
    lane = 127.0
    sch.wire(reset, (lane, reset[1]), (lane, 215.9), (172.72, 215.9), (172.72, nrst[1]), nrst, path="reset-path")
    sch.wire(p("R7", 2), (p("R7", 2)[0], 162.56), (lane, 162.56))
    sch.wire(p("C8", 1), (p("C8", 1)[0], 162.56))
    sch.power("R7", 1, "rail-3v3", direction="up")
    sch.power("C8", 2, "gnd", direction="down")
    sch.label_at((lane, 135.89), "reset", direction="right")
    for pin, header_pin, x in ((24, 2, 215.9), (25, 4, 213.36)):
        a_, b_ = p("U1", pin), p("J2", header_pin)
        sch.wire(a_, (x, a_[1]), (x, b_[1]), b_)
    sch.label_at((203.2, p("U1", 24)[1]), "swdio", direction="up")
    sch.label_at((208.28, p("U1", 25)[1]), "swclk", direction="down")
    sch.power("J2", 1, "rail-3v3", direction="up")
    for pin in (3, 5, 9):
        sch.power("J2", pin, "gnd", direction="down")

    # titles sit where the eye starts reading each region
    sch.group_title("usb-entry", (38.1, 60.96))
    sch.group_title("regulation", (86.36, 58.42))
    sch.group_title("mcu", (170.18, 96.52))
    sch.group_title("encoder", (218.44, 49.53))
    sch.group_title("indicator", (223.52, 147.32))
    sch.group_title("debug", (88.9, 148.59))
