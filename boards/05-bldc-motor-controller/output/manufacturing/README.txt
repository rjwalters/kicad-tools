Sensored BLDC controller, revision B — JLCPCB 4-layer/1oz assembly

Start with an unloaded small motor, RUN open, a regulated 12V bench supply
limited to 0.5A, and RV1 at its measured low-voltage end. Initial phase-current
target is 0.5A; nominal comparator trip is 1A and firmware caps duty at 25%.
Physical motor operation and thermal performance have not been measured.
Do not increase these limits without bench validation. Read HARDWARE.md.

SMT assembly: 36 CPL placements. Hand-solder J1-J5 and RV1 afterward.
The BOM includes these six sourcing entries; they are absent from the SMT CPL.
Solder U2/U3 exposed pads; their thermal vias are part of the SMT land pattern.
Observe IC/header pin1, C1 polarity and D1/D2/D3 polarity marks.

J1:1=VIN,2=GND; J2:1/2/3=phases A/B/C; J3:1=5V,2=GND,3/4/5=Hall A/B/C,6=GND.
J4 ISP:1=MISO,2=5V target sense,3=SCK,4=MOSI,5=RESET,6=GND. Disable programmer power.
J5 RUN: close pin1 to ground pin2 after first leaving open to arm firmware.
Power from J1 only; limit Hall load to 20mA and total 5V load to 30mA.
Program firmware/bldc.hex via slow AVR ISP, using factory internal-RC clock fuses.

Fresh native DRC/refill:0 violations/0opens; native ERC:0; label/copper LVS:0.
Manufacturer checks:0 errors/0warnings. No DRC waivers.
gerbers/gerbers.zip: fabrication; bom_jlcpcb.csv: procurement; cpl_jlcpcb.csv: SMT.
kicad_project.zip includes the exact PCB/schematic and local footprint/symbol libraries.
schematic.pdf and assembly-front/back.pdf are production drawings.
HARDWARE.md, firmware/, check reports and manifest.json complete the package.
