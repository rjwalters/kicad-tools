Manufacturing package for charlieplex_3x3_routed.kicad_pcb

Fabrication tier : jlcpcb
Mode             : assembly
Generated        : 2026-11-20T11:45:40.918145+00:00

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
  The following through-hole parts are INCLUDED in the CPL and BOM for fab assembly (flag for procurement review):
  J1, J2, U1

Warning review (per rule)
-------------------------
  mask_to_copper: 1
  tht_smd_spacing: 5
  All assembly-affecting warning counts are zero.

Copper cleanup
--------------
  copper_cleanup: applied; exact-duplicate copper removed: 0 segment(s), 0 via(s)
  step dedupe: applied (segments=0, vias=0)
  step prune: applied (segments_removed=0, ends_snapped=0)
  step optimize_traces: applied (segments_before=143, segments_after=143, corners_before=114, corners_after=114, layers=2, copper_oz=1.0)

LCSC stock order
----------------
  order size: 5 board(s)
  no parts declared as held in operator inventory
  stock check WARN: Stock not verified: 8 assigned LCSC number(s) found in neither the parts cache nor the offline catalog

Assembly prep
-------------
  fiducials: applied
  board_info: applied

Accepted-risk waivers
---------------------
  #8399 [advisory] Genuine routing detours (obstacle-avoiding) that dedupe and DRC-verified prune cannot remove; removing them needs a re-route

Sign-off
--------
  Fresh kct check and native KiCad DRC (saved and refilled copper): zero errors.
  firmware: passed -- Sources and programmed image byte-identical to the AVR GCC 7.3.0 compiled audit (214 flash bytes, 22 RAM bytes); Intel HEX record checksums verified and flash size re-measured (214 bytes). avr-gcc not installed here, so no rebuild. Physical hardware bring-up remains untested.
  smt_drill_clearance: passed -- Issue #5012 actual-land overlap rule re-run (jlcpcb, 2 layers): 0 overlaps; all 4 reviewed via relocations present on their nets. Ordinary JLCPCB process.
  native_erc: passed -- Fresh native KiCad ERC: 0 errors, 0 warnings.

ATtiny85 Charlieplex LED Grid, revision B
-----------------------------------------
  Assemble the nineteen components using bom_jlcpcb.csv and cpl_jlcpcb.csv.
  Since issue #7239 the CPL also lists the through-hole U1 (ATtiny85-20PU), J1 (power) and J2 (AVR ISP) for JLCPCB assembly.
  Procurement must review those through-hole rows before ordering; see HARDWARE.md.
  Match the U1 notch/pin1 marker, J1/J2 square pin1 pads, and LED cathode marks to the drawings.
  Supply regulated 3.3-5.0V to J1 pin1, ground to pin2. Use only one power source.
  J2 pins: 1=MISO,2=VCC,3=SCK,4=MOSI,5=RESET,6=GND.
  Program firmware/charlieplex.hex with AVR ISP at a conservative clock; factory clock fuses are assumed.
  See HARDWARE.md for full power, programming, pinout and procurement instructions.
  Physical bring-up has not yet been performed.
