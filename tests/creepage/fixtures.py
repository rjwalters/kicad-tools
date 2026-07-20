"""Synthetic PCB fixtures for the creepage engine/CLI tests (Issue #4327).

Each fixture is a minimal-but-real KiCad S-expression board so the tests
double as end-to-end ``PCB.load`` smoke tests.  The boards deliberately use
large millimetre gaps so the straight clearance and the slot-detour creepage
are numerically obvious and deterministic.
"""

from __future__ import annotations

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test_creepage")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user)
    (37 "F.SilkS" user)
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "L_MAINS")
  (net 2 "GND")
"""

# Outer board outline: rectangle (100,100) -> (140,120).
_OUTLINE = """\
  (gr_line (start 100 100) (end 140 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 140 100) (end 140 120) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 140 120) (end 100 120) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 120) (end 100 100) (layer "Edge.Cuts") (width 0.1))
"""

# Interior milled slot: a tall thin rectangle at x in [119.8, 120.2],
# y in [103, 117], lying directly between the two pads (which sit at y=110).
_SLOT = """\
  (gr_line (start 119.8 103) (end 120.2 103) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 120.2 103) (end 120.2 117) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 120.2 117) (end 119.8 117) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 119.8 117) (end 119.8 103) (layer "Edge.Cuts") (width 0.1))
"""


def _footprint(ref: str, x: float, y: float, net_number: int, net_name: str) -> str:
    """A single-pad SMD footprint: 2x2 mm rect pad centered at (x, y)."""
    return f"""\
  (footprint "test:pad" (layer "F.Cu") (at {x} {y})
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu")
      (net {net_number} "{net_name}"))
  )
"""


def board_source(with_slot: bool) -> str:
    """Two 2x2 pads (L_MAINS at x=110, GND at x=130) inside a 40x20 board.

    Pad copper edges sit at x=111 (L_MAINS) and x=129 (GND), so the
    straight-line clearance is 18 mm.  When ``with_slot`` is True a milled
    slot lies on the straight path between them, so the creepage surface path
    must detour around it (creepage > clearance); otherwise creepage == 18.
    """
    parts = [_HEADER, _OUTLINE]
    if with_slot:
        parts.append(_SLOT)
    parts.append(_footprint("U1", 110, 110, 1, "L_MAINS"))
    parts.append(_footprint("U2", 130, 110, 2, "GND"))
    parts.append(")\n")
    return "".join(parts)


def board_close_hv_source() -> str:
    """Two 2x2 pads (L_MAINS at x=110, GND at x=113) -- a ~1 mm HV gap.

    Pad copper edges sit at x=111 (L_MAINS) and x=112 (GND), so the
    straight-line clearance (and, with no slot, the creepage) is ~1 mm -- far
    below any realistic IEC-derived mains requirement.  Used for the
    below-standard gate-FAIL path.
    """
    parts = [_HEADER, _OUTLINE]
    parts.append(_footprint("U1", 110, 110, 1, "L_MAINS"))
    parts.append(_footprint("U2", 113, 110, 2, "GND"))
    parts.append(")\n")
    return "".join(parts)


def board_no_hv_source() -> str:
    """Board whose only assigned nets are GND / SIG -- no HV net exists."""
    header = _HEADER.replace('(net 1 "L_MAINS")', '(net 1 "SIG")')
    parts = [header, _OUTLINE]
    parts.append(_footprint("U1", 110, 110, 1, "SIG"))
    parts.append(_footprint("U2", 130, 110, 2, "GND"))
    parts.append(")\n")
    return "".join(parts)
