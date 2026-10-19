Four-channel LVDS demonstrator — revision C
Assembly mode: SMT assembly plus J1/J2/J3 through-hole headers assembled by the fab.
Fabrication: JLC04161H-7628, 4 layers, 1.6mm nominal, 1oz outer copper.
F.Cu signals / In1.Cu GND / In2.Cu 3.3V / B.Cu auxiliary signals.
Controlled differential impedance: 100 ohm target; 0.26mm trace,0.15mm gap.
Explicit stackup in PCB; calculated differential impedance97.51ohm.
Supply: externally regulated3.3V only; J1pin1positive,pin2ground.
No firmware. Bench test instructions and supplier links: procurement-review.md.

bom_jlcpcb.csv and cpl_jlcpcb.csv list the SMT parts and the three 2.54 mm
through-hole headers J1/J2/J3 (LCSC C492401, C492407; issue #7422).
Procurement must review those rows before ordering. All parts have explicit
supplier IDs. No auto-matching.

Native DRC after zone refill: zero violations and zero unconnected items.
Tool DRC,ERC,label-LVS,copper-LVS: zero errors/warnings/mismatches.
No electrical or assembly-rule waivers. Hardware has not been bench-tested.
Full sources,Gerbers/drills,schematic/assembly PDFs and evidence are included.
