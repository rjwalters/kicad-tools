# ATtiny85 charlieplex board, revision B

This revision replaces the synthetic MCU with an ATtiny85-20PU, adds regulated power input, reset pullup, decoupling, and the standard six-pin AVR ISP programming interface. Nineteen components fit on the existing 50 × 55 mm two-layer outline. The LEDs and passives are SMT; U1, J1 and J2 are hand-soldered through-hole parts.

Supply **regulated 3.3–5.0 V** to J1 pin 1, with ground on pin 2. There is no regulator or reverse-polarity protection. Use either J1 or a programmer that supplies target power; do not connect independent power sources simultaneously. ISP pin 2 is directly connected to the board supply, so a separately powered programmer must use it only as target-voltage sense.

| MCU pin | Function | Connection |
|---|---|---|
| 1 | PB5 / RESET | 10 kΩ pullup R5, ISP pin 5 |
| 2 | PB3 | LINE_A through R1 |
| 3 | PB4 | LINE_B through R2 |
| 4 | GND | Ground |
| 5 | PB0 / MOSI | LINE_C through R3, ISP pin 4 |
| 6 | PB1 / MISO | LINE_D through R4, ISP pin 1 |
| 7 | PB2 / SCK | ISP pin 3 |
| 8 | VCC | Supply, 100 nF C1 and 4.7 µF C2 |

J2 uses the standard AVR ISP numbering: 1 MISO, 2 VCC, 3 SCK, 4 MOSI, 5 RESET, 6 GND. The square pad marks pin 1. RESET remains enabled; the firmware does not repurpose it. The internal RC oscillator avoids crystal components. Factory clock fuses select 8 MHz divided by 8, giving the 1 MHz used by the supplied firmware.

Each active LED has two 330 Ω resistors in series. With approximately 2 V LED forward voltage, peak current is about (5−2)/660 = **4.5 mA** at 5 V, or about 2 mA at 3.3 V. The demo lights one LED for 150 ms, blanks all GPIOs before changing polarity, then advances through D1–D9. Inactive matrix pins have no pullups. Sharing MOSI/MISO with the matrix is isolated by the series resistors; program at a conservative ISP clock.

## Firmware

Install an AVR GCC toolchain and avrdude, then run:

```sh
make -C firmware
avrdude -c usbasp -p t85 -B 10 -U flash:w:firmware/charlieplex.hex:i
```

The prebuilt `firmware/charlieplex.hex` is built from `main.c` with AVR GCC 7.3.0, `-Os`, and `F_CPU=1000000UL`. It uses 214 bytes of flash (including initialized data) and 22 bytes of RAM. Programming and physical bring-up still require hardware. No fuse-writing command is needed on a factory-default chip. A reused chip must have its internal clock and RESET/SPI programming fuses restored using its programmer; do not disable RESET.

## Parts and evidence

| References | Manufacturer part | LCSC | Package |
|---|---|---|---|
| U1 | Microchip ATTINY85-20PU | [C965497](https://www.lcsc.com/product-detail/C965497.html) | PDIP-8 |
| D1–D9 | Everlight 17-21SURC/S530-A2/TR8 | [C131244](https://www.lcsc.com/product-detail/C131244.html) | Red LED 0805 |
| R1–R4 | UNI-ROYAL 0805W8F3300T5E | [C17630](https://www.lcsc.com/product-detail/C17630.html) | 330 Ω 0805 |
| R5 | UNI-ROYAL 0805W8F1002T5E | [C17414](https://www.lcsc.com/product-detail/C17414.html) | 10 kΩ 0805 |
| C1 | Yageo CC0805KRX7R9BB104 | [C49678](https://www.lcsc.com/product-detail/C49678.html) | 100 nF 50 V 0805 |
| C2 | Samsung CL21A475KAQNNNE | [C1779](https://www.lcsc.com/product-detail/C1779.html) | 4.7 µF 25 V 0805 |
| J1 | Megastar ZX-PZ2.54-1-2PZZ | [C7501260](https://www.lcsc.com/product-detail/C7501260.html) | 1×2 male, 2.54 mm |
| J2 | BOOMELE 2.54-2*3P | [C65114](https://www.lcsc.com/product-detail/C65114.html) | 2×3 male, 2.54 mm |

Electrical basis: [Microchip ATtiny25/45/85 datasheet](https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-2586-AVR-8-bit-Microcontroller-ATtiny25-ATtiny45-ATtiny85_Datasheet.pdf), especially pin configurations, clock system, electrical characteristics, and section 20.5 serial programming. Supplier identities and packages checked 2026-09-09. Stock and assembler eligibility can change.

All schematic and PCB generators call `hardware_design.py`; pin assignments and LED topology come from `design_spec.py`. Physical pad geometry comes from the installed KiCad standard libraries. The routed output increases silkscreen strokes to 0.15 mm and applies the reviewed escape-via corrections in `finalize_routing.py`. Generate into a separate directory and validate ERC, label/copper LVS and native DRC before replacing published outputs.
