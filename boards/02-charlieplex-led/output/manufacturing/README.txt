ATtiny85 Charlieplex LED Grid, revision B
Target: JLCPCB two-layer fabrication, SMT assembly plus manual through-hole assembly.

Assemble the sixteen SMT components using bom_jlcpcb.csv and cpl_jlcpcb.csv.
Hand-solder U1 (ATtiny85-20PU), J1 (power) and J2 (AVR ISP) afterward.
The BOM includes these three sourcing entries; they are intentionally absent from the SMT CPL.
Match the U1 notch/pin1 marker, J1/J2 square pin1 pads, and LED cathode marks to the drawings.

Supply regulated 3.3-5.0V to J1 pin1, ground to pin2. Use only one power source.
J2 pins: 1=MISO,2=VCC,3=SCK,4=MOSI,5=RESET,6=GND.
Program firmware/charlieplex.hex with AVR ISP at a conservative clock; factory clock fuses are assumed.
See HARDWARE.md for full power, programming, pinout and procurement instructions.
Firmware compiled successfully; physical bring-up has not yet been performed.

Validation: native KiCad DRC0 violations/0opens; ERC0; label/copper LVS0 mismatches.
Fresh kct manufacturing checks:0 errors/0 warnings. No accepted-risk waivers.

Contents: gerbers/gerbers.zip for fabrication; BOM/CPL for procurement/SMT placement;
kicad_project.zip for KiCad source; schematic.pdf and assembly-front/back.pdf drawings;
firmware/ sources, Makefile and programmed image; HARDWARE.md and project.kct component identities;
check-report.json, native-drc.json, native-erc.json, lvs.json validation evidence;
report.md/pdf and images/ previews; manifest.json full-package SHA256 checksums.
