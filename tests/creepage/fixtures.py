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


# Header for the benign-suspect fixture (issue #4365): net names that *look*
# mains-ish to a naive scanner (LINE / HOT / PRIMARY tokens) but are ordinary
# signals -- SPI_LINE, HOT_SWAP, PRIMARY_CLK.  The tightened MAINS_NAME_RE must
# NOT flag any of these, so a board carrying only these nets (and no mains-level
# working voltage) exits 0 rather than tripping the #4354 vacuity guard.
_BENIGN_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test_creepage")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SPI_LINE")
  (net 2 "GND")
  (net 3 "HOT_SWAP")
  (net 4 "PRIMARY_CLK")
"""


def board_benign_suspect_names_source() -> str:
    """Board whose only suspect-shaped nets are benign (issue #4365).

    ``SPI_LINE`` / ``HOT_SWAP`` / ``PRIMARY_CLK`` carry the LINE / HOT / PRIMARY
    tokens that the *old* broad regex over-matched.  With the tightened
    :data:`kicad_tools.creepage.engine.MAINS_NAME_RE`, none are mains-suspect, so
    a low-voltage audit of this board must exit 0 (no vacuity guard trip).
    """
    parts = [_BENIGN_HEADER, _OUTLINE]
    parts.append(_footprint("U1", 110, 110, 1, "SPI_LINE"))
    parts.append(_footprint("U2", 130, 110, 2, "GND"))
    parts.append(_footprint("U3", 135, 110, 3, "HOT_SWAP"))
    parts.append(_footprint("U4", 105, 110, 4, "PRIMARY_CLK"))
    parts.append(")\n")
    return "".join(parts)


# Header for the mains-named fixture: unmistakable mains net names (AC_LINE /
# AC_NEUTRAL / FUSED_LINE) that the broadened HV name-pattern fallback (#4354)
# must classify as HV without any net-class-map.
_MAINS_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test_creepage")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "AC_LINE")
  (net 2 "GND")
  (net 3 "AC_NEUTRAL")
  (net 4 "FUSED_LINE")
"""


def board_mains_named_source() -> str:
    """Board with mains net names (``AC_LINE`` / ``AC_NEUTRAL`` / ``FUSED_LINE``).

    Deliberately ships **no** net-class-map so the broadened HV name-pattern
    fallback (issue #4354) is the only thing that can classify the mains nets.
    ``AC_LINE`` (x=110) and ``GND`` (x=113) sit ~1 mm apart -- far below any
    realistic mains creepage requirement -- so once the fallback classifies the
    mains nets the resulting census FAILS (rather than silently exiting 0).
    """
    parts = [_MAINS_HEADER, _OUTLINE]
    parts.append(_footprint("U1", 110, 110, 1, "AC_LINE"))
    parts.append(_footprint("U2", 113, 110, 2, "GND"))
    parts.append(_footprint("U3", 135, 110, 3, "AC_NEUTRAL"))
    parts.append(_footprint("U4", 105, 110, 4, "FUSED_LINE"))
    parts.append(")\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Same-footprint classification fixtures (Issue #4403)
# ---------------------------------------------------------------------------

# Header for the same-footprint fixtures.  Nets: L_MAINS (HV via the standard
# net-class map), plus GND / SRC_NEG / DIV_MID as ordinary conductors.
_SAMEFP_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test_creepage")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "L_MAINS")
  (net 2 "GND")
  (net 3 "SRC_NEG")
  (net 4 "DIV_MID")
"""


def _footprint2(ref: str, x: float, y: float, pads: list[tuple]) -> str:
    """A multi-pad SMD footprint at (x, y).

    ``pads`` is a list of ``(number, net_number, net_name, local_x, local_y)``;
    each pad is a 0.6 x 0.6 mm rect so a 1.0 mm pad-centre pitch leaves a 0.4 mm
    copper-edge gap (below any realistic mains creepage requirement).
    """
    pad_lines = "\n".join(
        f'    (pad "{num}" smd rect (at {lx} {ly}) (size 0.6 0.6) (layers "F.Cu")\n'
        f'      (net {nn} "{name}"))'
        for num, nn, name, lx, ly in pads
    )
    # A Reference property is required so ``fp.reference`` is populated -- the
    # same-footprint classification (#4403) keys the binding measurement on it.
    ref_prop = f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    return f'  (footprint "test:pad2" (layer "F.Cu") (at {x} {y})\n{ref_prop}{pad_lines}\n  )\n'


def board_same_footprint_fail_source() -> str:
    """Board exercising all three binding relationships (#4403).

    * **same_footprint** -- ``FET1`` carries an ``L_MAINS`` pad and an
      ``SRC_NEG`` pad 1.0 mm apart (0.4 mm copper gap).  ``SRC_NEG`` exists on
      no other footprint, so the only ``L_MAINS <-> SRC_NEG`` approach is this
      component-internal gap.
    * **board** -- ``P1`` (``L_MAINS``) and ``P2`` (``GND``) sit 1.0 mm apart
      on distinct footprints, so the binding ``L_MAINS <-> GND`` gap is
      board-fixable.
    * **shared-footprint but board-binds** -- ``FET2`` holds ``L_MAINS`` and
      ``DIV_MID`` pads 2.0 mm apart (1.7 mm gap), but ``P3`` (``L_MAINS``) and
      ``P4`` (``DIV_MID``) approach to 0.4 mm elsewhere.  Because the binding
      minimum (0.4 mm) is NOT the intra-footprint gap (1.7 mm), the pair is
      correctly ``board`` -- this is the equality-check guard.

    All three binding gaps are 0.4 mm, so every conductor pair FAILs a
    >=1 mm requirement -- used for the "board fail remains after waiver" path.
    """
    parts = [_SAMEFP_HEADER, _OUTLINE]
    # same_footprint: L_MAINS + SRC_NEG in one package, 0.4 mm gap.
    parts.append(
        _footprint2("FET1", 110, 110, [("1", 1, "L_MAINS", 0, 0), ("2", 3, "SRC_NEG", 0, 1.0)])
    )
    # board: L_MAINS + GND on distinct footprints, 0.4 mm gap.
    parts.append(_footprint2("P1", 120, 110, [("1", 1, "L_MAINS", 0, 0)]))
    parts.append(_footprint2("P2", 120, 111, [("1", 2, "GND", 0, 0)]))
    # shared-footprint but board-binds: FET2 holds both nets 1.7 mm apart, but
    # P3/P4 approach to 0.4 mm, so DIV_MID binds board-level, not intra-FET2.
    parts.append(
        _footprint2("FET2", 110, 115, [("1", 1, "L_MAINS", 0, 0), ("2", 4, "DIV_MID", 0, 2.0)])
    )
    parts.append(_footprint2("P3", 130, 110, [("1", 1, "L_MAINS", 0, 0)]))
    parts.append(_footprint2("P4", 130, 111, [("1", 4, "DIV_MID", 0, 0)]))
    parts.append(")\n")
    return "".join(parts)


def board_same_footprint_only_source() -> str:
    """Board whose ONLY sub-requirement gap is a same-footprint pair (#4403).

    ``FET1`` carries an ``L_MAINS`` pad and an ``SRC_NEG`` pad 0.4 mm apart (the
    same_footprint fail).  ``P1`` (``L_MAINS``) and ``P2`` (``GND``) sit ~7 mm
    apart, so the board-level ``L_MAINS <-> GND`` pair and the board-edge pairs
    all clear a ~1 mm requirement.  With ``--waive-same-footprint`` the gate then
    passes (exit 0); without it, the same-footprint fail exits 1.
    """
    parts = [_SAMEFP_HEADER, _OUTLINE]
    parts.append(
        _footprint2("FET1", 110, 110, [("1", 1, "L_MAINS", 0, 0), ("2", 3, "SRC_NEG", 0, 1.0)])
    )
    # Board-level L_MAINS <-> GND ~7 mm apart -> clears a ~1 mm requirement.
    parts.append(_footprint2("P1", 118, 110, [("1", 1, "L_MAINS", 0, 0)]))
    parts.append(_footprint2("P2", 118, 118, [("1", 2, "GND", 0, 0)]))
    parts.append(")\n")
    return "".join(parts)
