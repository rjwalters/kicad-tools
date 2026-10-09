Manufacturing package for charlieplex_3x3_routed.kicad_pcb

Fabrication tier : jlcpcb
Mode             : assembly
Generated        : 2026-10-09T00:58:07.604374+00:00

Assembly package: BOM and CPL are included.

Contents
--------
  HARDWARE.md
  assembly-back.pdf
  assembly-front.pdf
  bom_jlcpcb.csv
  check-report.json
  cpl_jlcpcb.csv
  design-source.zip
  fill-consistency.json
  firmware/Makefile
  firmware/charlieplex.hex
  firmware/main.c
  gerbers/gerbers.zip
  images/assembly.png
  images/layer_B_Cu.png
  images/layer_F_Cu.png
  images/pcb_back.png
  images/pcb_copper.png
  images/pcb_front.png
  images/schematic_charlieplex_3x3.png
  kct-check.json
  kicad_project.zip
  lvs.json
  manifest.json
  native-drc.json
  native-erc.json
  project.kct
  renders/3d-back.png
  renders/3d-front.png
  renders/pcb-back.svg
  renders/pcb-front.svg
  report.md
  report.pdf
  schematic.pdf

Hand-solder / through-hole items
--------------------------------
  The following parts are EXCLUDED from the SMT CPL and must be hand-soldered:
  J1, J2, U1

Warning review (per rule)
-------------------------
  no warnings reported
  All assembly-affecting warning counts are zero.

Accepted-risk waivers
---------------------
  no accepted-risk waivers

Sign-off
--------
  Fresh kct check and native KiCad DRC (saved and refilled copper): zero errors.
  firmware: passed -- Sources and programmed image byte-identical to the AVR GCC 7.3.0 compiled audit (214 flash bytes, 22 RAM bytes); Intel HEX record checksums verified and flash size re-measured (214 bytes). avr-gcc not installed here, so no rebuild. Physical hardware bring-up remains untested.
  smt_drill_clearance: passed -- Issue #5012 actual-land overlap rule re-run (jlcpcb, 2 layers): 0 overlaps; all 11 reviewed via relocations present on their nets. Ordinary JLCPCB process.
  native_erc: passed -- Fresh native KiCad ERC: 0 errors, 0 warnings.

ATtiny85 Charlieplex LED Grid, revision B
-----------------------------------------
  Assemble the sixteen SMT components using bom_jlcpcb.csv and cpl_jlcpcb.csv.
  Hand-solder U1 (ATtiny85-20PU), J1 (power) and J2 (AVR ISP) afterward.
  The BOM includes these three sourcing entries; they are intentionally absent from the SMT CPL.
  Match the U1 notch/pin1 marker, J1/J2 square pin1 pads, and LED cathode marks to the drawings.
  Supply regulated 3.3-5.0V to J1 pin1, ground to pin2. Use only one power source.
  J2 pins: 1=MISO,2=VCC,3=SCK,4=MOSI,5=RESET,6=GND.
  Program firmware/charlieplex.hex with AVR ISP at a conservative clock; factory clock fuses are assumed.
  See HARDWARE.md for full power, programming, pinout and procurement instructions.
  Physical bring-up has not yet been performed.
