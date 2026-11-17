#!/usr/bin/env python3
"""Generate the synthetic mains-style fixture for #8422 (deterministic, no inputs).

Fully synthetic: nothing here is derived from any real design.  A 2-layer 70x52 mm
board with mains-style nets (/LINE 150 V-class, /FUSED_LINE, /NEUT, 0 V reference) and
low-voltage signal nets whose short completion paths run through gaps between pads of
*different* footprints that are too narrow for the 1.6 mm IEC 60664-1 creepage class
(150 V, PD2, IIIa).  Usage: ``python generate_fixture.py > mains_fixture.kicad_pcb``.
"""

from __future__ import annotations

import sys

OX, OY, W, H = 100.0, 100.0, 70.0, 52.0

NETS = ["", "/LINE", "/FUSED_LINE", "/NEUT", "GND", "/SENSE_A", "/SENSE_B", "/SENSE_C", "/SENSE_D"]

# (reference, x, y, axis, [(pad, net)]) -- 2-pin THT parts, pads 2.0 mm round, 5.08 mm apart
# along `axis`.  Footprint origin is pad 1.
PARTS = [
    # Mains "wall": three different footprints stacked in a column.  Pads of adjacent
    # footprints are 2.42 mm apart edge-to-edge, so a trace squeezed between them gets
    # ~1.1 mm of creepage to each side against a 1.6 mm requirement.
    ("J1", 35, 12.0, "y", [("1", "/LINE"), ("2", "/NEUT")]),
    ("F1", 35, 21.5, "y", [("1", "/FUSED_LINE"), ("2", "/NEUT")]),
    ("K1", 35, 31.0, "y", [("1", "/LINE"), ("2", "/FUSED_LINE")]),
    # Low-voltage parts either side of the wall.  The straight links between them run
    # through the 2.42 mm gaps; the creepage-clean routes detour round the wall's ends.
    ("U1", 8, 19.3, "x", [("1", "/SENSE_A"), ("2", "GND")]),
    ("U2", 56, 19.3, "x", [("1", "/SENSE_A"), ("2", "GND")]),
    ("U3", 8, 28.8, "x", [("1", "/SENSE_B"), ("2", "/SENSE_C")]),
    ("U4", 56, 28.8, "x", [("1", "/SENSE_B"), ("2", "/SENSE_C")]),
    ("U5", 8, 40.0, "x", [("1", "/SENSE_D"), ("2", "GND")]),
    ("U6", 56, 40.0, "x", [("1", "/SENSE_D"), ("2", "GND")]),
]


def pad(num: str, net: str, dx: float, dy: float) -> str:
    n = NETS.index(net)
    return (
        f'    (pad "{num}" thru_hole circle (at {dx} {dy}) (size 2 2) (drill 0.9) '
        f'(layers "*.Cu" "*.Mask") (net {n} "{net}"))'
    )


def footprint(i: int, ref: str, x: float, y: float, axis: str, pads: list[tuple[str, str]]) -> str:
    body = "\n".join(
        pad(num, net, 5.08 * k if axis == "x" else 0.0, 5.08 * k if axis == "y" else 0.0)
        for k, (num, net) in enumerate(pads)
    )
    return (
        f'  (footprint "Synthetic:TH2_{ref}" (layer "F.Cu") '
        f'(uuid "00000000-0000-4000-8000-{i:012d}") (at {OX + x} {OY + y})\n'
        f'    (property "Reference" "{ref}" (at 2.5 -2.5 0) (layer "F.SilkS"))\n'
        f'    (property "Value" "syn" (at 2.5 2.5 0) (layer "F.Fab"))\n'
        f"{body})"
    )


def main() -> None:
    nets = " ".join(f'(net {i} "{n}")' for i, n in enumerate(NETS))
    fps = "\n".join(footprint(i + 1, *p) for i, p in enumerate(PARTS))
    sys.stdout.write(
        f"""(kicad_pcb (version 20240108) (generator "synthetic-8422") (generator_version "8.0")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  {nets}
  (gr_rect (start {OX} {OY}) (end {OX + W} {OY + H}) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{fps}
)
"""
    )


if __name__ == "__main__":
    main()
