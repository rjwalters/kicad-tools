# Board 07 assembled SDRAM demonstration

This replacement architecture is a standalone STM32F429ZIT6 MCU with an
IS42S16400J-6TLI-TR 8 MiB, x16 SDR SDRAM. Firmware writes and verifies memory
patterns and reports over a 3.3 V UART and status LEDs. A regulated 5 V input
and an external SWD probe are the only required support equipment. This is
SDR SDRAM, without DQS; the former synthetic DDR/MIPI/HDMI fixture remains
in the parent directory as routing regression evidence.

**This directory contains the reviewed physical design and its reproducible
validation recipe.** Manufacturing export and package verification are separate
release gates. No fabricated unit has been tested; hardware bring-up remains
necessary before describing the design as hardware-proven.

## Circuit and references

The MCU uses the 144-pin LQFP and the SDRAM uses the 54-pin TSOP-II, both actual
KiCad library footprints with package-native pins. The memory connection follows
ST's Discovery board FMC bank 2 mapping. Separate 100 nF capacitors serve every
MCU VDD and memory VDD/VDDQ pin; VCAP1/2 each get their own 2.2 µF capacitor.
VBAT and PDR_ON are tied to the supply. VDDA/VREF+ receive filtered 3.3 V and
local decoupling. BOOT0 has a pull-down, NRST a pull-up and capacitor, and SWD
retains reset access. HSI is the initial clock source; SDRAM and controller
share the derived FMC clock, so no external oscillator is required for the
initial memory test. The conservative bench configuration runs the MCU at
96 MHz and the memory at 48 MHz. USB is omitted from this first circuit.

Primary design references:

- [STM32F429 datasheet](https://www.st.com/resource/en/datasheet/stm32f429zi.pdf)
  for package pins, supply domains, clocks and FMC alternate functions.
- [ST MB1075-C01 reference schematic](https://www.st.com/resource/en/schematic_pack/mb1075-f429i-c01_schematic.pdf)
  for a functioning MCU/SDRAM implementation and support circuitry.
- [ISSI memory datasheet](https://www.issi.com/WW/pdf/42-45S16400J.pdf)
  for pinout, initialization, refresh and timing limits.
- [ST AN4488, section 8.4.2](https://www.st.com/resource/en/application_note/an4488-getting-started-with-stm32f4xxxx-mcu-hardware-development-stmicroelectronics.pdf)
  for the electrical routing budget: nominal 50 Ω lines, bounded clock-relative
  length differences, short traces and separation of data from address/control.
- [ST Discovery SDRAM driver](https://github.com/STMicroelectronics/32f429idiscovery-bsp/blob/main/stm32f429i_discovery_sdram.c)
  for the FMC pin assignment and initialization sequence.

The new routing groups are lower byte plus LDQM, upper byte plus UDQM,
address/bank, and command/control. Constraints come from the real SDR interface,
not the former fixture's DDR/MIPI/HDMI budgets. A common SDRAM clock relationship
must be checked across groups as well as within them. Initial layout targets
are at most 5 mm within each group and 10 mm relative to SDCLK, with traces
below 120 mm and 50 Ω ±10%. These are independent new-circuit requirements;
they do not change acceptance criteria for the archived synthetic fixture.

## Procurement check, 2026-09-09

| Device | Orderable part | Supplier evidence |
|---|---|---|
| MCU | STM32F429ZIT6 | [ST active product](https://estore.st.com/en/stm32f429zit6-cpn.html), [LCSC C84808 listing](https://www.lcsc.com/product-detail/C84808.html); exact assembly inventory still needs confirmation |
| SDRAM | IS42S16400J-6TLI-TR | [LCSC C17216754](https://www.lcsc.com/product-detail/C17216754.html), 54 units shown during research |

All 43 components now have explicit manufacturer part numbers and LCSC IDs in
[`procurement.json`](procurement.json), including the regulator, capacitors,
resistors, LEDs and three headers. Distributor listing/stock is not an assembly-stock
reservation, and the headers require through-hole assembly.

## Reproduce and check the reviewed design

```sh
uv run python boards/07-matchgroup-test/real_design/build.py /tmp/board07-release
uv run python boards/07-matchgroup-test/real_design/firmware/build.py /tmp/board07-firmware
```

The release builder checks the reviewed routing hash, independently regenerates
the circuit and physical source, and repeats native DRC/ERC, strict connectivity,
label/copper LVS, fabrication rules, layer separation, timing, impedance, load
and symmetric clock-spacing checks. It writes `sdram_demo_routed.kicad_pcb` and
the complete `validation/` evidence. The reviewed copper is an explicit artifact
in `reviewed-routing/`; this recipe does not claim that an unmeasured fresh
autorouter run produces identical copper.

An absent manufacturing bundle is recorded explicitly during this design
preflight. The only accepted `kct check` exit-2 case is an absent manifest with
every DRC/ERC/LVS check passed and zero errors or warnings. An existing failed
or stale bundle is rejected. Manufacturing readiness additionally requires a
fresh export and a passing post-export manifest check.

The electrical generator establishes the circuit independently. The physical
source is the reviewed six-layer placement, local power routing and package
fanout in `authored-source/`; its manifest binds the native PCB, project and
routing sidecar. Reproduce it with:

```sh
uv run python boards/07-matchgroup-test/real_design/build_source.py /tmp/board07-source
```

The builder checks all package identities, values and pad-to-net assignments
against freshly generated electrical source, including unconnected pins. It
is the source-only development entry point, so its output deliberately lacks
the reviewed bus trunks. Use `build.py` for the routed design.

The physical source uses JLC06161H-2116A, six copper layers, with In1 reserved
for ground and In4 for 3.3 V. Data and byte masks use F/In2; address/control use
In3/B outside authored short front-side package escapes. Bus width is 0.18 mm;
all vias are ordinary 0.5/0.2 mm full-span through vias. Native project rules
remain the acceptance floor. The project sets `KCT_PRESERVE_BOARD_RULES=1`
so constraint emission retains its 0.15 mm clearance and 0.5/0.2 mm via rules;
lower factory capability values must not change reviewed plane fills (#5023).
The source preserves 25 capacitors on the back,
including local VCAP decoupling and two 10 nF interplane capacitors. Assembly
requires both sides and manual insertion of three through-hole headers.

The independent validator must check the final routed candidate and its
matching source directory before export. Required checks include refilled
native DRC/ERC, strict connectivity, pin LVS, timing, impedance and driver load.
The clock has a separate 15 pF external-load screening condition; data/address
use 30 pF. Model assumptions and short package-escape spacing require explicit
review. Routing completion alone does not establish manufacturing readiness.

The schematic passes native ERC with zero violations. Label LVS verifies 216
bound component pins with zero mismatches. The firmware compiles with warnings
as errors and tests byte masks, all 8 MiB with four patterns, and refresh
retention; see [firmware instructions](firmware/README.md). These checks do not
prove routed connectivity or operation on hardware. The independent validator
rejects incomplete routing.

Validation refills a copy using the authored project, checks native DRC/ERC,
strict copper connectivity including all power nets, exact source pad mappings,
copper/label LVS, full `kct check`, group spread, every signal's clock-relative
length, and 45–55 Ω geometry on every routed layer. Through-via lengths are included.
It never edits the published gallery output.

The reviewed design's electrical lengths include the vertical travel between
actually connected copper layers. Unused through-barrel stubs count toward the
load estimate rather than series travel. Final group spreads are:

| Group | Spread including vertical travel |
|---|---:|
| Lower byte and LDQM | 0.0021 mm |
| Upper byte and UDQM | 2.7188 mm |
| Address and bank | 2.4208 mm |
| Command and clock | 0.9570 mm |

All remain below 5 mm, and every bus member is within 10 mm of the 72.953 mm
clock. The clock's estimated external load is 13.521 pF against its separate
15 pF limit. Geometric impedance is 51.72 Ω on the outer layers and about
48.09 Ω on the inner signal layers. These are model-based engineering checks,
not fabrication impedance certification or measured hardware timing.

Long data and address/control trunks occupy separate layer pairs. Short
package escapes share the front layer; their measured deviation from AN4488's
5 mm recommendation and the fixed TSOP pad spacing are documented in
[the engineering review](engineering/README.md). The review states the model
and edge-rate assumptions and does not claim blanket AN4488 compliance.

## Power and operating scope

This is a room-temperature bench demo, not an 85 °C industrial product. The
AP2112K regulator is rated for 600 mA, but its SOT25 thermal resistance is
184 °C/W; current rating alone is insufficient to establish the thermal budget.
At 5.25 V input, 3.25 V output and 230 mA load, the estimate is 0.46 W and an
85 °C junction rise. Use a 0–30 °C ambient target, regulated 5 V ±5%, and verify
that continuous assembled current stays below 230 mA. This estimated envelope
needs a measured current/temperature check before hardware validation is claimed.
The lower clock reduces power while retaining the complete memory test.
See the [regulator datasheet](https://www.diodes.com/datasheet/download/AP2112.pdf).

## Manufacturing and bring-up

Generate and verify the order package after the design build, confirm current
assembly inventory for the specified MPNs, and assemble the three through-hole
headers. A fabricated unit needs powered current/rail checks and the full memory
test before the design can be described as hardware-proven.
