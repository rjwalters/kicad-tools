# Sensored BLDC controller, revision B

This is a small-motor demonstration board using a real ATmega328P-AU and TI
DRV8313PWPR triple half bridge. The initial operating target is 0.5 A phase
current, with a nominal 1 A current-comparator trip. It is not the original
24 V / 10 A design. Physical motor operation and thermal performance have
not been measured; these limits must not be increased without bench testing.

## Power and connections

Use a regulated, current-limited 12–24 V bench supply. Start at 12 V with a
0.5 A supply limit, turn the supply off before connecting, and keep RUN open.
J1 pin 1 is positive input, pin 2 is ground. F1 is a 1 A, 63 V slow fuse;
D1 blocks reverse polarity and D2 clamps short voltage transients. The TVS
is not a continuous regenerative brake: use a small unloaded motor and do
not drive the shaft with another motor. The 220 uF / 50 V bulk capacitor is
Panasonic EEEFT1H221AP, with an 8 mm diameter and 10.2 mm body height.

J2 pins 1/2/3 are motor phases A/B/C. J3 pins 1–6 are +5 V, GND, Hall A,
Hall B, Hall C, GND. Hall outputs must be 5 V compatible; open-drain Hall
outputs use the board's 10 kΩ pullups. Limit total Hall-sensor supply load to
20 mA. The 5 V regulator's complete MCU/Hall budget is 30 mA: at 24 V the
LDO dissipates approximately 0.57 W. Its exposed pad, plated thermal vias
and four-layer ground copper are part of the thermal design.

J5 connects the RUN switch between pin 1 and ground pin 2. An open switch
stops the motor. Closing it arms the motor only after firmware has first
seen an open switch; power-up with the switch closed cannot start the motor.
RV1 controls duty; measure SPEED relative to GND and start at its low-voltage
end. The supplied firmware caps duty at 63/256 (approximately 25%). D3 lights
for a latched driver, current, invalid-Hall, sequence or stall fault. Release
RUN, investigate the cause and restart deliberately.

## Programming and commutation

J4 is the AVR ISP header: 1=MISO, 2=+5 V target sense, 3=SCK, 4=MOSI,
5=RESET, 6=GND. Power the board from J1 while programming and disable any
programmer-supplied target power. Factory clock fuses are assumed: the
internal 8 MHz RC oscillator and CKDIV8 are enabled; firmware changes CLKPR
to divide by one after reset. Use a slow ISP clock for the factory 1 MHz
startup clock, for example `avrdude -c usbasp -p m328p -B 10 -U flash:w:bldc.hex:i`.
Do not select an external crystal clock: this board has no crystal.

Build with `make` and an AVR GCC toolchain. The included image compiles for
ATmega328P with warnings treated as errors. PWM uses PD3/PD5/PD6 at nominally
31.25 kHz; enables use PD4/PD7/PB0. Hall A/B/C are PC0/PC1/PC2. The accepted
Hall sequence is 001, 101, 100, 110, 010, 011, then 001. The corresponding
phase pairs are A+/B−, A+/C−, B+/C−, B+/A−, C+/A−, C+/B−. Different motors
can have different Hall/phase wiring; establish the mapping at low current
with the motor unloaded before normal operation. Invalid states, reverse
sequence, or approximately 200 ms without a Hall transition while driving
latch the bridge off. The watchdog also resets a stalled program.

The DRV8313's COMPN pin 13 receives the filtered shunt voltage; COMPP pin 12
receives the reference. A 56 kΩ/10 kΩ divider from its internal V3P3 output
sets approximately 0.5 V; R1 is 0.5 Ω, 1 W. The active-low nCOMPO output is
connected to the MCU's INT0 input and shuts off all enables and nSLEEP.
This is a firmware-latched trip, not a precision closed-loop current regulator.
The driver's independent internal short-circuit protection remains present.

## Assembly

The JLCPCB CPL contains SMT parts only. Hand-solder J1–J5 and RV1 afterward.
Match square header pads to pin 1. Match U1/U2/U3 pin 1, D1/D2 cathode marks,
D3 LED cathode and C1 capacitor polarity to the assembly drawings. U2 and U3
exposed pads must be soldered; their numbered plated thermal vias do not
make these packages through-hole components. The TI PWP0028C land pattern
is supplied in `board05_revB.pretty`, including its 3.1 × 5.18 mm mask opening,
segmented paste apertures and thermal vias tented on the back. Include the local symbol
and footprint libraries when opening the KiCad source project.

## Primary references

- [TI DRV8313 datasheet](https://www.ti.com/lit/ds/symlink/drv8313.pdf), pin table,
  charge-pump capacitors, V3P3 bypass, section 8.2.2.2 current monitor and
  PWP0028C package drawing 4223582/A.
- [TI TPS7A16 datasheet](https://www.ti.com/lit/ds/symlink/tps7a16.pdf), fixed-5 V
  DGNR pinout, bypass requirements, power-good reset and thermal guidance.
- [Microchip ATmega328P](https://www.microchip.com/en-us/product/ATmega328P),
  TQFP32 pinout, internal RC clock, ISP and timers.
- [TI sensored BLDC reference](https://www.ti.com/tool/TIDA-00827).
- [Panasonic EEEFT1H221AP dimensions and ratings](https://na.industrial.panasonic.com/products/capacitors/aluminum-electrolytic-capacitors/series/88931/model/89261).
- [Bourns SF-1206S fuse data](https://www.bourns.com/docs/product-datasheets/sf-1206s.pdf).

The procurement table below specifies actual manufacturer identities and
packages. Stock and assembly-library availability can change; match the
specified identity/package before accepting any substitution.

| References | Value / manufacturer part | Footprint | LCSC |
|---|---|---|---|
| U1 | ATmega328P-AU — ATMEGA328P-AU | Package_QFP:TQFP-32_7x7mm_P0.8mm | [C14877](https://www.lcsc.com/product-detail/C14877.html) |
| U2 | DRV8313PWPR — DRV8313PWPR | board05_revB:TI_PWP0028C_EP3.4x9.7_Mask3.1x5.18_ThermalVias | [C92482](https://www.lcsc.com/product-detail/C92482.html) |
| U3 | TPS7A1650DGNR — TPS7A1650DGNR | Package_SO:HVSSOP-8-1EP_3x3mm_P0.65mm_EP1.57x1.89mm_ThermalVias | [C468238](https://www.lcsc.com/product-detail/C468238.html) |
| J1 | 12-24V INPUT — ZX-PZ2.54-1-2PZZ | Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical | [C7501260](https://www.lcsc.com/product-detail/C7501260.html) |
| J2 | MOTOR ABC — 2.54-1*3P | Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical | [C49257](https://www.lcsc.com/product-detail/C49257.html) |
| J3 | HALL — 2.54-1*6P | Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical | [C37208](https://www.lcsc.com/product-detail/C37208.html) |
| J4 | AVR ISP — 2.54-2*3P | Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical | [C65114](https://www.lcsc.com/product-detail/C65114.html) |
| J5 | RUN SWITCH — ZX-PZ2.54-1-2PZZ | Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical | [C7501260](https://www.lcsc.com/product-detail/C7501260.html) |
| RV1 | 10k — 3296W-1-103LF | Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical | [C34846](https://www.lcsc.com/product-detail/C34846.html) |
| F1 | 1A 63V — SF-1206S100-2 | Fuse:Fuse_1206_3216Metric | [C3167176](https://www.lcsc.com/product-detail/C3167176.html) |
| D1 | SS36 — SS36 | Diode_SMD:D_SMA | [C16015](https://www.lcsc.com/product-detail/C16015.html) |
| D2 | SMBJ24A — SMBJ24A | Diode_SMD:D_SMB | [C224017](https://www.lcsc.com/product-detail/C224017.html) |
| D3 | RED — 17-21SURC/S530-A2/TR8 | LED_SMD:LED_0805_2012Metric | [C131244](https://www.lcsc.com/product-detail/C131244.html) |
| C1 | 220uF 50V — EEEFT1H221AP | Capacitor_SMD:CP_Elec_8x10.5 | [C178594](https://www.lcsc.com/product-detail/C178594.html) |
| C2, C3, C5, C9, C10, C11, C12, C14, C15, C16 | 100nF — CC0805KRX7R9BB104 | Capacitor_SMD:C_0805_2012Metric | [C49678](https://www.lcsc.com/product-detail/C49678.html) |
| C4, C13 | 10nF — CL21B103KBANNNC | Capacitor_SMD:C_0805_2012Metric | [C1710](https://www.lcsc.com/product-detail/C1710.html) |
| C6 | 470nF — CL21B474KBFNNNE | Capacitor_SMD:C_0805_2012Metric | [C13967](https://www.lcsc.com/product-detail/C13967.html) |
| C7 | 1uF — CL21B105KBFNNNE | Capacitor_SMD:C_0805_2012Metric | [C28323](https://www.lcsc.com/product-detail/C28323.html) |
| C8 | 10uF — CL21A106KAYNNNE | Capacitor_SMD:C_0805_2012Metric | [C15850](https://www.lcsc.com/product-detail/C15850.html) |
| R1 | 0.5R — WSL2512R5000FEA | Resistor_SMD:R_2512_6332Metric | [C511023](https://www.lcsc.com/product-detail/C511023.html) |
| R2 | 56k — 0805W8F5602T5E | Resistor_SMD:R_0805_2012Metric | [C17756](https://www.lcsc.com/product-detail/C17756.html) |
| R3, R5, R6, R7, R8, R9, R10, R11, R12 | 10k — 0805W8F1002T5E | Resistor_SMD:R_0805_2012Metric | [C17414](https://www.lcsc.com/product-detail/C17414.html) |
| R4, R13 | 1k — 0805W8F1001T5E | Resistor_SMD:R_0805_2012Metric | [C17513](https://www.lcsc.com/product-detail/C17513.html) |

The 3D preview uses installed KiCad models with matching visible body/lead geometry
for U2/U3; their hidden exposed-pad dimensions are approximate. The actual
footprint copper, mask and paste follow the specified native/TI land patterns.
