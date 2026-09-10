# ATtiny85 Charlieplex LED Grid

A working 3 × 3 red LED sequencer built around a real ATtiny85-20PU. Four GPIO pins drive nine LEDs through four 330 Ω resistors. The 50 × 55 mm, two-layer board includes regulated 3.3–5 V power input, supply decoupling, reset pullup, and a standard six-pin AVR ISP header.

See [HARDWARE.md](HARDWARE.md) for the complete pinout, verified component identities, power instructions and firmware programming. U1, J1 and J2 are hand-soldered after the sixteen SMT components are assembled. The supplied firmware advances through D1–D9; physical hardware testing has not yet been performed.

## Rebuild

From the repository root, with KiCad and the project Python environment installed:

```sh
uv run python boards/02-charlieplex-led/generate_design.py /tmp/charlieplex-rebuild
```

The schematic and PCB generators share `hardware_design.py` and `design_spec.py`. Native library footprints preserve the actual DIP and header geometry. The deterministic routing recipe traces all twelve nets, then `finalize_routing.py` applies two reviewed escape-via corrections, raises silkscreen widths to the fabrication minimum, and requires native KiCad DRC to have zero findings. These explicit corrections address router defects; they do not suppress checks.

`output/manufacturing.zip` contains the fabrication and assembly package. Its manifest records the package files and SHA256 hashes. `output/readiness.json` records current validation evidence and input hashes.

## Charlieplexing

Each LED connects between two GPIO nodes. To light it, one GPIO drives high, another drives low, and the other two remain inputs without pullups. Four GPIOs can address twelve directed pairs; this board populates nine. The current path contains two 330 Ω resistors, yielding about 4.5 mA with a 5 V supply and a 2 V LED drop. The firmware blanks the matrix before changing direction to avoid ghosting.
