<!-- pcbforge-mcu-schema: 2 -->
# MCU procedure

Use the approved MCU proposal from ARCHITECT. Follow the standard review and approval protocol in [the operating manual](operating-manual.md).

Create `firmware/<project>.ioc` in the pinned CubeMX version. Set the exact MCU and package.
Configure each required physical pin and peripheral. Preserve SWD, reset, boot and power requirements.
Use `DEBUG_UART_TX` and `DEBUG_UART_RX` when the specification requests a debug UART.
Run `pcbforge check-ioc` and perform a one-to-one audit against `docs/mcu.md`.
Opening CubeMX for user inspection is optional and is not an approval gate.

Run `pcbforge finish-architect` after the IOC and audit pass.
This records the architecture baseline and opens CIRCUIT without another user approval.
During CIRCUIT, map every physical IOC assignment to the native symbol pin, signal and net in `electrical-facts.yaml`.
Run the complete circuit check after any MCU or pin assignment change.
