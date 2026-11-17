# 08 — Four-channel precision acquisition

Planned working demonstration of four simultaneous differential ADC inputs,
with USB streaming. The ADS131M04IPWR TSSOP converter and a socketed RP2040
Pico H host now have a checked pin/package interface and an 8 kSPS transfer
budget. Schematic, procurement and PCB implementation remain pending.
It has no manufacturing release.

## Requirements

- Four simultaneously sampled differential channels; begin at 8 kSPS/channel.
- USB telemetry from a real MCU with a published, permitted USB identity.
- Four-layer ordinary through-via fabrication, protected input connectors,
  matched analog RC input networks and a continuous ground reference.
- Explicit placement rules separating input/filter/ADC circuitry from USB and
  clocks. Do not split the ground plane under returning digital signals.
- Validate input common-mode and differential ranges before choosing protection
  or gain settings; 24-bit output format is not a 24-bit accuracy promise.

## Evidence to produce

Schematic/ERC, source-to-PCB LVS, placement and return-path measurements,
manufacturer checks and an integrity-checked assembly package. A later bench
report will measure shorted-input noise, channel crosstalk, gain/offset,
frequency response and USB sample loss against documented test conditions.

See the [interface review](engineering/interface-review.md) and run
`uv run python boards/08-precision-acquisition/check_interface.py` to check
the selected symbols, packages and nominal timing. Analog regulator, clock,
input protection/filter choices and sourced BOM remain open.
