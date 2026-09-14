# USB joystick revision B — engineering review

Revision B replaces the previous nonphysical 32-pin MCU fixture with an actual
ATMEGA32U4-AU in the manufacturer's TQFP-44, 10×10mm, 0.8mm-pitch package.
Both generators consume `joystick_hardware.py`; schematic symbols and copper
land patterns come from installed KiCad libraries, except J3, whose local
footprint follows Samtec’s recommended PCB layout. There are no fallback parts.
`assembly-bom.csv` lists manufacturer part numbers for all 38 placements.

## Circuit and pin review

The MCU's fixed pin allocation follows Microchip's pinout and bus-powered 5V
application. USB D−/D+ are pins 3/4, with 22Ω series termination. Pin 6 UCAP
has a dedicated 1µF capacitor and is never connected to 5V. Every VCC/AVCC/UVCC
supply has local 100nF bypassing; ground pins are all connected. AREF has a
100nF capacitor. An 8MHz external crystal supplies USB timing. Unused GPIOs
are explicitly unconnected. [Microchip datasheet, pinout and sections 21.3–21.5](https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-7766-8-bit-AVR-ATmega16U4-32U4_Datasheet.pdf).

The crystal is an ECS-80-12-33-JGN-TR, 12pF nominal load. Equal 15pF load
capacitors target 12pF with approximately 4.5pF estimated stray capacitance.
The capacitors fall within Microchip's 12–22pF recommended range; crystal
ESR is 400Ω maximum.
This is an engineering starting point: oscillator margin and frequency must
be checked on assembled hardware. Pins 2/4 of the crystal case are grounded.
[ECS ECX-32 specification](https://ecsxtal.com/store/pdf/ecx-32.pdf).

The GCT USB4085 receptacle uses its actual 16-contact footprint and shield
stakes. This through-hole component requires a secondary soldering operation
after SMT reflow. It avoids the USB4105 footprint’s 0.1944mm NPTH-to-copper
gap, below the fabricator’s 0.20mm minimum.
[JLC manufacturing capabilities](https://www.jlc.com/portal/vtechnology.html).
Each orientation's duplicate USB data contacts is connected. CC1 and
CC2 terminate independently to ground through 5.1kΩ; neither goes to the MCU.
SBU pins are unused. [GCT mechanical drawing](https://gct.co/files/drawings/usb4085.pdf).

USBLC6-2SC6 protects the USB data pair and VBUS. Its I/O1 pins are 1/6,
I/O2 pins 3/4, ground pin 2, and VBUS pin 5. The internal through connections
are represented by identical net names on each channel's two pins. Place
this device close to the connector with a short ground return.
[ST USBLC6-2 datasheet](https://www.st.com/resource/en/datasheet/usblc6-2.pdf).

A 350mA-hold Littelfuse 1206L035/16YR resettable fuse connects VBUS to the
logic rail for fault protection; it does not authorize a 350mA USB load. Its
1.2Ω maximum post-reflow resistance at 20°C drops at most 0.12V at the 100mA
system budget. A 4.35V USB input therefore leaves about 4.23V before trace
loss, above the internal USB regulator's 4.0V specified input floor.
The MCU uses 8MHz because 16MHz requires 4.5V, which USB bus power cannot
guarantee. Verify this margin and fuse resistance over the intended operating
temperature on the first article; do not substitute a high-resistance 100mA fuse. The nominal exposed input capacitance is kept below 10µF using
4.7µF bulk capacitance plus local bypassing. The intended controller and
external potentiometers must remain below the pre-enumeration 100mA budget;
this connector is not a 500mA accessory supply.
[Littelfuse 1206L specification](https://www.littelfuse.com/assetdocs/littelfuse-ptc-1206l-datasheet?assetguid=2b6a1515-d4ee-4c83-8bd4-152b4901b8f5).

## Interfaces and programming

| Interface | Connection |
|---|---|
| J2 joystick | 1=5V, 2=GND, 3=X wiper, 4=Y wiper, 5=switch to ground |
| X axis | J2.3 → R10 1kΩ → PF0/ADC0, MCU pin 41; C10 10nF to ground |
| Y axis | J2.4 → R11 1kΩ → PF1/ADC1, MCU pin 40; C11 10nF to ground |
| SW1–SW4 | PD0–PD3, MCU pins 18–21; individual 10kΩ pullups |
| Joystick switch | PD4, MCU pin 25; individual 10kΩ pullup |
| J3 AVR ISP | 1=MISO, 2=5V sense, 3=SCK, 4=MOSI, 5=RESET, 6=GND |
| RESET | MCU pin 13, 10kΩ pullup; SW5 momentarily grounds it |
| HWB | MCU pin 33, 10kΩ pulldown |

J2 is the JST S5B-PH-SM4-TB surface-mount PH header. Use a matching PH housing
and contacts; verify the assembled cable's pin order before powering it.
[JST PH connector family and drawings](https://www.jst-mfg.com/product/index.php?lang=2&series=154).
J3 uses the Samtec TSM-103-01-T-DV-P-TR header with a pick-and-place cap
and tape packaging. Its local footprint uses 3.683×1.270mm lands, a 1.270mm
inner-edge gap, and 2.540mm pitch from the manufacturer’s revision-F
[recommended PCB layout](https://suddendocs.samtec.com/prints/tsm-dv-footprint.pdf).
The pin numbering is rotated consistently with the vertical PCB orientation.

Disconnect USB while programming and power the target from the ISP programmer
as specified in `firmware/README.md`; do not connect two power sources. Select ATmega32U4 and an ISP clock appropriate for the
currently programmed clock fuses. Set fuses for the external 8MHz crystal
only after checking the crystal network; keep reset and ISP enabled. The
firmware recipe uses LF=FF, HF=D9, EF=CA (nominal 3.4V brownout detection). Firmware
must configure the listed ADC/GPIO pins and provide a USB HID descriptor.
The runnable HID firmware and ISP instructions are in `firmware/`. Its build
verifies the shared joystick identity 16c0:27dc and domain-based serial string.
Electrical ERC/LVS/DRC cannot establish USB interoperability; first-article
enumeration, inputs and suspend-current tests remain required (#5000).

## Validation and release

The revision-B circuit and repaired routing pass native electrical DRC with zero
errors and zero unconnected items, and sidecar-aware kct copper/label LVS is
clean. The routing is saved design data with manual repairs, not a claim of
complete automatic routing. `routing-plan.json` binds it to every pad, pin net,
footprint position, outline, layer, pour and fabrication stack. `routing_plan.py`
rejects changed geometry. A scratch rebuild must pass fresh native refill/DRC
and kct checks before readiness is published. Exact current evidence belongs
in `output/readiness.json`, not historical board metrics.

### USB layout bounds

This ATmega32U4 is a 12Mbps full-speed controller. Full-speed signaling uses
4–20ns output transitions; the 480Mbps high-speed waveform has a different
regime. [Microchip full-speed overview](https://developerhelp.microchip.com/xwiki/bin/view/applications/usb/speeds-specs/full-speed/),
[Microchip USB3320 table 4-6, full-speed waveform timing](https://ww1.microchip.com/downloads/aemDocuments/documents/UNG/ProductDocuments/DataSheets/00001792E.pdf).
At an assumed FR4 relative permittivity of 4.6, a 16mm line has approximately
0.115ns one-way delay (length × sqrt(epsilon_r) / c), short compared with a
4ns edge. This is an engineering estimate, not measured impedance or compliance.

The main ESD-to-series-resistor run is paired on In2.Cu beside In1 ground.
The connector's reversed duplicate contacts and the ESD device's 1.9mm channel
spacing necessarily introduce short uncoupled launches. The saved layout has
15.072/15.268mm total copper per connector/ESD net, 0.197mm skew, and a connected
4.711mm common coupled run. MCU-side lengths are both 3.535mm, with 73.5%
coupling. Pair traces are 0.20mm wide with 0.15mm minimum edge gap.

The circuit-specific replay gate independently requires a connected common
run of at least 4mm, at most 16mm total copper per connector/ESD net, and at
most 0.5mm skew. MCU launches must remain at most 4mm. The branched-net
continuity threshold is 25% (4mm/16mm), while the MCU pair retains 70%.
These are design-review bounds, not USB standard limits; adding long meanders
to raise a percentage would defeat the short-link intent. No verified 90-ohm
or USB compliance claim is made. Bench signal quality and interoperability
remain first-article qualification work.

### Mandatory manufacturing process

Order **four layers, 1.6mm JLC7628 construction, ENIG, and Epoxy-filled & Capped
(POFV) vias**. Outer copper is 0.035mm; inner copper 0.0152mm; the two 7628
prepreg layers are 0.2104mm and the FR4 core is 1.065mm. The PCB records this
stack and enables via filling/capping. See the machine-readable generated
`manufacturing-requirements.json` and the routing plan's factory options.
Do not substitute tenting or solder-mask plugging for filled and copper-capped
vias: some standard through vias sit in SMT lands.

JLC explicitly offers POFV as a charged option on four-layer boards and permits
0.2–0.5mm via holes. This board uses 0.2/0.3mm standard drills, with at least
2.837mm conservative hole clearance to component PTHs (the published POFV
minimum is 0.45mm).
[JLC POFV process and design limits](https://jlcpcb.com/news/free-via-in-pad-6-20-layer-pcbs-pofv),
[JLC via covering options](https://jlcpcb.com/help/article/pcb-via-covering).
The order must explicitly select that process; Gerbers alone do not purchase it.

USB4085's stock 0.70mm lands / 0.40mm holes have a 0.15mm annular ring: this
meets JLC's multilayer absolute minimum, while their two-layer minimum is
0.18mm. Adjacent connector holes have 0.45mm spacing, matching the published
pad-hole minimum. The native project and `check_manufacturing.py` explicitly
set the reviewed 0.45mm floor, preserving every geometric check. The Python
report records this board-specific override and its primary source. Generic
`kct check` still uses 0.50mm and reports 14 connector advisories (#5006). [JLC rigid PCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities/).

J1 requires through-hole soldering after SMT reflow. `assembly-bom.csv` specifies
actual manufacturer ordering numbers for all 38 placements. Supplier codes
come from the exact-MPN review in `procurement-review.json`. The assembler must procure those exact MPNs, confirm substitutions,
inspect J1/J2/J3 orientation against the assembly drawings, and program the HID
firmware using its documented fuse and ISP procedure. Firmware build success
and manufacturable hardware do not replace first-article functional testing.
