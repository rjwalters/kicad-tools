# USB joystick controller — revision B

Real ATmega32U4-AU USB HID controller hardware: USB-C bus power and ESD,
8MHz crystal, reset/HWB support, AVR ISP, two filtered joystick axes and
four buttons plus a joystick switch. The MCU uses the actual TQFP-44 pinout.

Revision A was a routing fixture with a fictitious 32-pin MCU mapping. Its
saved routing and manufacturing outputs are not valid evidence for revision B.
See [DESIGN_REVIEW.md](DESIGN_REVIEW.md) for primary sources, exact interface
pinouts, bring-up requirements, and remaining release checks.

```sh
uv run python boards/03-usb-joystick/generate_schematic.py
uv run python boards/03-usb-joystick/generate_pcb.py
uv run python boards/03-usb-joystick/route_demo.py
```

`joystick_hardware.py` is the shared circuit and placement source;
`assembly-bom.csv` supplies actual manufacturer ordering numbers. The generators
require the real KiCad symbol and footprint libraries and fail on missing parts.

The board is 80×60mm, four layers. The inner layers are ground and VCC planes; both outer layers also have ground
pours; the router must explicitly route VBUS. J2 accepts a 5V
potentiometer joystick with pin order 5V/GND/X/Y/SW. J3 is standard six-pin
AVR ISP; disconnect USB and power the target from the programmer during programming. The USB4085 connector
requires through-hole soldering after the other parts are reflowed.

`route_demo.py` replays reviewed fixed-placement copper with a strict physical
fingerprint; it does not claim the generic autorouter completed this revision.
The required fabrication options are four-layer JLC7628, ENIG, and **Epoxy-filled
& Capped (POFV)** vias. Do not order standard tented vias.

Runnable USB HID firmware, binary build artifacts and ISP/fuse instructions
are in [firmware/README.md](firmware/README.md). Hardware ERC or DRC passing
does not claim successful first-article USB enumeration or suspend-current tests.
Readiness must come from a fresh `output/readiness.json` tied to the generated
revision-B design and its manufacturing files.
