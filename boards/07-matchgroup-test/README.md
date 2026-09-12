# 07 — 8 MiB SDRAM exerciser

A standalone STM32F429ZIT6 MCU tests an IS42S16400J-6TLI-TR x16 SDR SDRAM.
The real circuit replaces the former synthetic DDR/MIPI/HDMI routing fixture.
It uses a six-layer board, 43 purchasable components and 216 connected pads.
Firmware runs the MCU at 96 MHz and the memory at 48 MHz, tests all 8 MiB,
and reports through status LEDs and a 3.3 V UART.

Manufacturing readiness is recorded in `output/readiness.json` and verified
against the exact source, rules, evidence and exported package. Use
`output/manufacturing.zip` only when that report is current and Ready.
Hardware testing remains pending; this design has not yet run on an assembled
board.

## Build and assembly

The reviewed physical source and build instructions are in
[`real_design/`](real_design/README.md). The electrical circuit is independently
regenerated and compared with the reviewed packages and every pad-to-net
binding. Routing is preserved as reviewed source and independently rechecked;
this is not a claim that an unconstrained autorouter produces identical copper.

The factory construction is JLC06161H-2116A, nominal 1.6 mm, with four signal
layers and dedicated ground/3.3 V reference planes. See the
[fabrication requirements](real_design/manufacturing-requirements.md).
There are 40 SMT parts, including 25 capacitors on the back, and three manually
assembled through-hole headers. Exact manufacturer part numbers and supplier
references are in [the procurement list](real_design/procurement.json).

Supply regulated 5 V ±5% at J1 pin 1 and ground at pin 2. Use an external SWD
probe at J2 and a 3.3 V UART adapter at J3. The initial bench envelope is
0–30 °C ambient and at most 230 mA continuous board current. Check the power
rails and regulator temperature during first bring-up. See the
[firmware/programming instructions](real_design/firmware/README.md).

## Electrical review

All SDRAM groups have a 5 mm length-spread budget and each signal must be
within 10 mm of SDCLK, including actual vertical travel through vias.
The validator checks 45–55 ohm geometry on every bus layer and distinct
external load budgets: 15 pF for SDCLK and 30 pF for the other drivers.
Clock track/via spacing is measured symmetrically on all signal layers.

Short front-side package escapes require an explicit engineering review of
the data/control separation recommendation. Their measured geometry and
model assumptions are documented in [the electrical review](real_design/engineering/README.md).
No IBIS corner qualification or bench validation is implied by a manufacturing
or model-based check.

## Synthetic regression witness

The previous nonfunctional fixture remains in
[`regression-fixture/`](regression-fixture/README.md) for Epic #2661 regression
coverage. Legacy `generate_design.py` defaults to `regression-output/` and
must not be used to generate this assembled demo. Its synthetic pinouts and
historical routing failures are not manufacturing artifacts.

### Staged pad-drill relocation

The synthetic recipe relocates in-pad drills through
`relocate_in_pad_vias_with_refill`. It stages the PCB and adjacent project/rule
files, retains original same-net filled-copper attachment evidence while
planning, and refills both an untouched baseline and the complete candidate
using plain native KiCad. The native CLI must support explicit zone refill and
board saving.

Publication requires no new native violation identities or multiplicities
(including unconnected items), unchanged physical pad-connectivity partitions,
and the shared via, full-stub, project drill-floor, and closed-outline gates.
The baseline is independently refilled because stale source pours can represent
connections that native refill removes even without relocation. The API returns
both raw native reports; this is a no-regression check, not a claim that the
historical synthetic fixture has zero manufacturing violations.

Any failed check leaves source files untouched. Only the validated PCB is
atomically replaced; project/rule sidecars retain their original bytes. The
legacy direct-relocation entry points remain available for their conservative
filled-copper regression controls and delegate blocked-stub extension to the
shared relocation module. Board 07 CI remains suspended until full recipe and
both dedicated job checks qualify.
