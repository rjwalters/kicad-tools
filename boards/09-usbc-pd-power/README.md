# 09 — USB-C PD 5 V power supply

Active development: a standalone USB-C PD sink powers a fixed 5 V buck
regulator, targeting 3 A output, with I2C current/voltage telemetry. This is a
real assembled-demo design, not a synthetic routing fixture. No manufacturing
or measured-performance release has been made.

## Initial architecture

USB-C input → negotiated and switched VBUS → synchronous buck → shunt → 5 V output.
A separate low-current 3.3 V supply powers the telemetry interface. An external
3.3 V I2C host reads telemetry and inspects the PD configuration; ordinary
standalone power conversion does not require application firmware.

The generated circuit uses STUSB4500QTR, TPS54302DDCR and INA226AIDGSR.
Use the PD controller's documented 15 V / 20 V default profiles, subject to
readback verification. The buck remains disabled on ordinary 5 V USB input.
The output rating is a design target until electrical and thermal bench tests.

## Layout objectives

Compact input switching loop; small switch-node copper; feedback kept away
from switch node and inductor; independent Kelvin traces at the output shunt;
short IC bypass connections; broad power/ground copper and via arrays sized
for the declared current. Four-layer ordinary through-via fabrication.

## Release gates

Fresh native ERC and DRC, complete label/copper LVS, exact sourced package and
pinout review, voltage/current/tolerance calculations, physical layout checks,
and independent refill/export identity. Bench measurements later cover ripple,
efficiency, load steps, temperature, attach/detach and unsupported-charger
behavior. Test equipment, conditions and limits must accompany every result.

## Current checkpoint and reproduction

47 components and all 32 nets are routed on a four-layer PCB, with filled
GND planes on both inner layers. Native ERC and refilled native DRC pass:
zero violations and zero unconnected items. Label and physical copper LVS
pass, with 153 bound pads. The development check guards every net and rejects
signal traces on the inner ground layers.

Manufacturing remains blocked. The manufacturer check reports 51 ampacity
errors requiring branch-current and thermal review, and 22 component MPNs
remain unselected. Strict KiCad-tools net status still reports four USB ground
pads disconnected despite native KiCad's clean result; this disagreement is
tracked in [#5061](https://github.com/rjwalters/kicad-tools/issues/5061).
Physical copper LVS does not independently prove plane connectivity.

From the repository root:

```sh
uv run python boards/09-usbc-pd-power/generate_design.py
uv run python boards/09-usbc-pd-power/check_design.py
uv run pytest tests/test_board09_telemetry.py tests/test_board09_critical_routing.py --no-cov -q
uv run kct render boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb --no-3d
```

The check command validates a development checkpoint; its successful exit
does **not** indicate manufacturing readiness. `output/development-check.json`
records source hashes, checks and outstanding gates. It reruns native DRC,
rejects geometry violations and opens. No manufacturing ZIP exists.
Critical-net opens are correlated using saved PCB item UUIDs; removing a
previously completed critical connection fails the development check.

See the [engineering review](engineering/review.md),
[telemetry instructions](host/README.md), and
[routing investigation](engineering/routing-investigation/README.md).
Open the [schematic PDF](output/schematic.pdf) or
[routed PCB preview](output/renders/pcb-front.svg) for review.
