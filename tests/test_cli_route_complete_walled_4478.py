"""Walled-SMD completion proof for the rescue rewiring (Issue #4478, epic #4465).

Phase 5 points the completion/rescue driver at ``kct route --complete`` (the
lattice engine) instead of the coarse uniform-grid A* it used to shell via
``--skip-nets`` (gap G2).  The grid engine cannot thread a walled SMD pocket or
place a via-in-pad, so ``#4434``'s batch route traded stranded walled pads 1:1
forever.  This module is the end-to-end proof of the mechanism the rewiring
unlocks (AC item 3):

* a deliberately **walled** SMD pad the GRID engine cannot close,
* the same pad closed by ``--complete`` (lattice) **only** on a fab tier that
  supports via-in-pad (``jlcpcb-tier1``) -- a tier without it (plain ``jlcpcb``)
  leaves the pad open rather than fabricating an unmanufacturable via,
* and the ``#4413`` ``guard_copper_loss`` invariant: an unrelated net's
  pre-existing copper is never deleted by the completion pass, even when the
  walled link fails to close.

Fixture geometry mirrors the Phase 3 pathfinder fixture
(``tests/router/lattice/test_via_in_pad_last_resort.py``) lifted to a real
``.kicad_pcb``: pad ``A`` (NET_A) on F.Cu is ringed by four single-pad wall
nets on F.Cu with a 0.7 mm gap, leaving a via-in-pad straight down to pad ``B``
(NET_A) on B.Cu as the only escape.  NET_KEEP is an unrelated,
already-routed net whose segment must survive every pass untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main

# Pad A center; the walls box it on F.Cu.
_CX, _CY = 100.0, 100.0
# Ring standoff / thickness / length -- the Phase 3 fixture's tuned geometry.
_D, _WW, _WL = 0.7, 1.0, 3.0
# NET_KEEP's pre-existing segment endpoints (unrelated to the walled pocket).
# Written on one line on input; KiCad re-serializes it multi-line on output, so
# tests probe for a surviving *net-6 segment* rather than this literal string.
_KEEP_SEG = '(segment (start 106 105) (end 108 105) (width 0.2) (layer "F.Cu") (net 6))'


def _net6_segment_preserved(text: str) -> bool:
    """True iff exactly one NET_KEEP (net 6) segment survives the pass.

    guard_copper_loss (#4413): the pre-existing unrelated segment must be
    re-emitted (never deleted), and no new net-6 copper is fabricated.
    """
    return _seg_nets(text).count(6) == 1


def _pad(ref: str, x: float, y: float, w: float, h: float, net: int, name: str, layer: str) -> str:
    mask = "F.Paste F.Mask" if layer == "F.Cu" else "B.Paste B.Mask"
    fp_layer = "F.Cu" if layer == "F.Cu" else "B.Cu"
    return f"""  (footprint "test:P" (layer "{fp_layer}") (uuid "{ref}") (at {x} {y})
    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS"))
    (property "Value" "v" (at 0 2 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size {w} {h}) (layers "{layer}" "{mask}") (net {net} "{name}"))
  )"""


def _walled_board() -> str:
    parts = [
        _pad("U1", _CX, _CY, 0.3, 0.3, 1, "NET_A", "F.Cu"),
        _pad("U2", _CX + 5, _CY, 0.3, 0.3, 1, "NET_A", "B.Cu"),
        _pad("WR", _CX + _D + _WW / 2, _CY, _WW, _WL, 2, "WALL_R", "F.Cu"),
        _pad("WL", _CX - _D - _WW / 2, _CY, _WW, _WL, 3, "WALL_L", "F.Cu"),
        _pad("WT", _CX, _CY + _D + _WW / 2, _WL, _WW, 4, "WALL_T", "F.Cu"),
        _pad("WB", _CX, _CY - _D - _WW / 2, _WL, _WW, 5, "WALL_B", "F.Cu"),
        # NET_KEEP: an unrelated, already-routed net (guard_copper_loss probe).
        _pad("K1", 106.0, 105.0, 0.3, 0.3, 6, "NET_KEEP", "F.Cu"),
        _pad("K2", 108.0, 105.0, 0.3, 0.3, 6, "NET_KEEP", "F.Cu"),
    ]
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET_A")
  (net 2 "WALL_R")
  (net 3 "WALL_L")
  (net 4 "WALL_T")
  (net 5 "WALL_B")
  (net 6 "NET_KEEP")
  (gr_rect (start 90 90) (end 115 110)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{chr(10).join(parts)}
  {_KEEP_SEG}
)
"""


def _seg_nets(text: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\(segment.*?\(net (\d+)\)", text, re.S)]


def _via_nets(text: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\(via.*?\(net (\d+)\)", text, re.S)]


def _net1_via_in_pad(text: str) -> bool:
    """True iff a NET_A (net 1) via sits inside pad A's footprint (a via-in-pad)."""
    for m in re.finditer(r"\(via[^)]*?\(at ([-\d.]+) ([-\d.]+)\)(?:.*?)?\(net (\d+)\)", text, re.S):
        vx, vy, net = float(m.group(1)), float(m.group(2)), int(m.group(3))
        if net == 1 and abs(vx - _CX) <= 0.5 and abs(vy - _CY) <= 0.5:
            return True
    return False


@pytest.fixture
def walled_board(tmp_path: Path) -> Path:
    board = tmp_path / "walled.kicad_pcb"
    board.write_text(_walled_board())
    return board


def test_grid_engine_cannot_close_walled_pad(walled_board: Path, tmp_path: Path) -> None:
    """Baseline: the coarse uniform-grid A* the old rescue shelled cannot close
    the walled pad -- the exact failure mode #4478 fixes."""
    out = tmp_path / "grid.kicad_pcb"
    rc = route_main(
        [
            str(walled_board),
            "-o",
            str(out),
            "--route-engine",
            "grid",
            "--nets",
            "NET_A",
            "--preserve-existing",
            "--manufacturer",
            "jlcpcb-tier1",
            "--backend",
            "cpp",
        ]
    )
    assert out.exists()
    # The grid engine lands NO copper for NET_A: the pad stays stranded.
    assert 1 not in _seg_nets(out.read_text())
    assert 1 not in _via_nets(out.read_text())
    # And it did not return the clean-success code.
    assert rc != 0


def test_complete_closes_walled_pad_via_via_in_pad_on_supporting_tier(
    walled_board: Path, tmp_path: Path
) -> None:
    """``--complete`` (lattice) closes the walled pad the grid engine could not,
    using a via-in-pad, on a via-in-pad-capable tier -- AND preserves the
    unrelated NET_KEEP copper (#4413 guard_copper_loss)."""
    out = tmp_path / "complete_tier1.kicad_pcb"
    rc = route_main(
        [
            str(walled_board),
            "-o",
            str(out),
            "--complete",
            "--via-in-pad-last-resort",
            "--manufacturer",
            "jlcpcb-tier1",
            "--backend",
            "cpp",
        ]
    )
    # Routing succeeded even if the tiny synthetic fixture trips DRC (rc 3):
    # the mechanism proof is that the previously-unroutable link now has copper.
    assert rc in (0, 3), f"expected routing success (rc 0 or 3), got {rc}"
    text = out.read_text()
    # NET_A (the walled link) is now routed with a via-in-pad.
    assert 1 in _via_nets(text), "NET_A must gain a via to escape the walled pocket"
    assert _net1_via_in_pad(text), "the escape via must land inside pad A (a via-in-pad)"
    # #4413 guard_copper_loss: NET_KEEP's pre-existing segment is untouched.
    assert _net6_segment_preserved(text), "unrelated NET_KEEP copper must never be deleted"
    # No other-net (wall) copper was fabricated.
    assert set(_seg_nets(text)) | set(_via_nets(text)) <= {1, 6}


def test_complete_does_not_fabricate_via_in_pad_on_unsupported_tier(
    walled_board: Path, tmp_path: Path
) -> None:
    """On a tier WITHOUT via-in-pad support the walled pad is left open rather
    than closed with an unmanufacturable via -- and NET_KEEP still survives."""
    out = tmp_path / "complete_plain.kicad_pcb"
    rc = route_main(
        [
            str(walled_board),
            "-o",
            str(out),
            "--complete",
            "--via-in-pad-last-resort",
            "--manufacturer",
            "jlcpcb",  # via_in_pad_supported=False
            "--backend",
            "cpp",
        ]
    )
    text = out.read_text()
    # The walled link is NOT closed: no via-in-pad was fabricated.
    assert not _net1_via_in_pad(text), "no via-in-pad may appear on an unsupporting tier"
    assert 1 not in _via_nets(text)
    # --complete signals the unroutable link (exit 8) or a fatal no-route (1);
    # either way it did NOT report clean success.
    assert rc != 0
    # #4413 guard_copper_loss holds even on the failed-completion path.
    assert _net6_segment_preserved(text), "unrelated NET_KEEP copper must never be deleted"
