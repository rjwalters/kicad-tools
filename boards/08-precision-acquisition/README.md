# 08 — Four-channel precision acquisition

Planned working demonstration of four simultaneous differential ADC inputs,
with USB streaming and an ADS131M04-family converter. This example is scoped;
schematic, procurement and PCB implementation are intentionally pending while
board09 is developed. It has no manufacturing release.

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

The initial ADC candidate is [TI ADS131M04](https://www.ti.com/product/ADS131M04).
Exact package, MCU, reference/filter choices and sourced BOM remain open.
