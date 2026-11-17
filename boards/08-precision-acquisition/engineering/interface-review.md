# Interface checkpoint — 2026-09-10

Selected architecture: ADS131M04IPWR in TSSOP-20 with a socketed original
RP2040 Raspberry Pi Pico H for USB streaming. The module handles USB, flash
and MCU power; the carrier concentrates on the ADC, input protection/filtering,
analog power and return paths. Procurement is still pending.

`interface.json` is the machine-readable pin/package selection for the future
schematic and firmware. `check_interface.py` compares it against the installed
KiCad symbols and all numbered footprint lands. It also checks the SPI wiring
and nominal transfer budget. Run from the repository root:

```sh
uv run python boards/08-precision-acquisition/check_interface.py
```

## Package and host wiring

The [TI datasheet, revision D, table 5-1](https://www.ti.com/lit/ds/symlink/ads131m04.pdf)
is the pinout authority. Channels 1 and 3 have their N input before their P
input in pin-number order. The TSSOP and WQFN pin numbers differ; the selected
TSSOP has no exposed pad. CAP is the internal digital regulator output and
needs 220 nF to ground; it must not be tied to the 3.3 V rail. Each supply
needs its own local 1 µF bypass capacitor.

The host assignments follow the official
[Pico pinout](https://datasheets.raspberrypi.com/pico/Pico-R3-A4-Pinout.pdf):

| ADC signal | ADC pin | Pico physical pin | RP2040 GPIO |
| --- | ---: | ---: | ---: |
| DOUT → SPI0 RX | 15 | 21 | 16 |
| CS | 12 | 22 | 17 |
| SCLK | 14 | 24 | 18 |
| DIN ← SPI0 TX | 16 | 25 | 19 |
| DRDY | 13 | 26 | 20 |
| SYNC/RESET | 11 | 27 | 21 |

Use the 40-pin `RaspberryPi_Pico_Common_THT` footprint for the socketed module.
Exact socket height, USB connector clearance and module order code remain
mechanical/procurement checks. Join the host's analog and digital ground pins
to the carrier's continuous ground. VBUS pin 40 is the USB-derived analog
regulator input candidate; pin 36 supplies digital 3.3 V. Do not connect a
second supply to either output as part of this USB-powered demo.

## Timing and firmware requirements

Start at gain 1, high-resolution mode, turbo/global chop disabled, with an
external 8.192 MHz oscillator and OSR 512. The nominal conversion rate is
8 kSPS per channel. SPI mode 1 at 8 MHz transfers a full six-word, 24-bit-word
frame in 18 µs, within the 125 µs conversion interval. Four packed channels
produce 96,000 bytes/s before framing. These are calculated budgets; interrupt
latency, USB backpressure and oscillator error have not been measured.

Firmware must reset and identify the ADC, configure/read back registers,
validate CRC and signed samples, and count dropped frames. Buffer USB output
separately from acquisition and expose sequence counters to the host. The
USB application VID/PID permission must be documented before distributing
manufactured-device firmware; a Pico bootloader identity is not an application
identity allocation.

## Remaining circuit decisions

Select the oscillator and low-noise analog regulator with exact order codes.
Choose differential/common-mode limits before fixing connector protection
and matched RC values. Gain-1 differential full scale is nominally ±1.2 V;
that alone does not establish an acceptable input common-mode range. Bias,
clamp leakage, source impedance and input settling must be calculated together.
Route the ADC/filters away from Pico switching currents and clock edges, over
an uninterrupted reference plane. No schematic, routed PCB or manufacturing
readiness is claimed by this interface check.
