# STM32 development board fabrication and regulator correction

U1 is **Microchip MCP1825S-3302E/DB**, exact supplier part **C148031**.
Its SOT-223 pinout is 1=VIN, 2/tab=GND, 3=VOUT, matching this board's
+5V/GND/+3.3V copper. The previous AMS1117 part had a different pinout and
was electrically wrong despite clean schematic-to-board LVS (#5011).
The new stock KiCad symbol follows the manufacturer pinout, and +3.3V is
driven by its real output rather than an artificial power flag.
[Microchip MCP1825S datasheet](https://ww1.microchip.com/downloads/aemDocuments/documents/APID/ProductDocuments/DataSheets/MCP1825-Family-Data-Sheet-DS20002056.pdf),
[exact supplier listing](https://www.lcsc.com/product-detail/C148031.html).

Use a regulated **5V input**; do not apply more than the regulator's 6V operating
maximum. The part supports 500mA and stabilizes with at least 1µF ceramic output
capacitance; C2 is 10µF. The board's stated 300mA rail budget remains subject to
first-article thermal/load verification. At 5V input and 300mA load, nominal
regulator dissipation is 0.51W. Do not infer an all-temperature load rating from
DRC. Verify the 3.3V rail before fitting peripherals, then check SWD programming,
crystal startup, reset/BOOT0, and the PB12 user LED on assembled hardware.

Order **two layers, 1.6mm FR4, 1oz outer copper, tented vias, and the paid
0.15mm minimum mechanical via-hole option**. The seven fine-pitch vias are
ordinary F.Cu–B.Cu through vias, diameter 0.30mm / drill 0.15mm, annular ring
0.075mm. The remaining vias are 0.60/0.30mm. JLC's published two-layer minimum
is 0.15mm drill / 0.25mm diameter; 0.15mm holes incur extra cost and require the
corresponding order selection. These are not laser microvias and no
via-in-pad process is required.
[JLC drilling and via capabilities](https://jlcpcb.com/capabilities/pcb-capabilities/).

Eight vias were moved clear of SMT soldering lands: seven STM32 escapes and
one partial drill overlap at the regulator tab. Every affected pad receives an
explicit F.Cu tail; signal escapes also receive tails on their connected track
layers. The process gate checks drill-circle separation from every SMT land,
including unassigned pads. The old containment-only detector missed the
regulator overlap (#5012); generic relocation also created no-net pad shorts
(#5010) and disconnected ground pads (#4981).

`manufacturing_process.py` binds the repair to the reviewed physical circuit,
checks actual via geometry/type, emits the reviewed native rules, and requires
matching `manufacturing-requirements.json` factory options. `check_manufacturing.py`
runs the complete Python/ERC/LVS/manifest checks with those explicit rules and
records override provenance without suppressing findings. Native DRC and ERC
must also pass. The generic manufacturer profile does not encode this paid
process (#5009); re-emitting generic rules requires restoring the reviewed
native rules and collecting fresh evidence before any manufacturing package.

The local `kicad_tools_pwr.kicad_sym` and `sym-lib-table` keep the +3.3V power
symbol portable with the project. Exact current release evidence is
`output/readiness.json`; old archives cannot qualify a corrected board.
