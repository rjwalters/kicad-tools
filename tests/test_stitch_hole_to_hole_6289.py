"""Regression tests for Issue #6289: stitching honours the fab hole-to-hole floor.

Board 05's accepted board had stitch vias 0.457-0.496 mm (drill edge to drill
edge) from neighbouring vias, below the 0.5 mm ``min_hole_to_hole_mm`` of
``jlcpcb-tier1`` that ``kct check --mfr`` enforces.  Two gaps:

* the post-placement filter exempted EVERY same-net via, not just a coincident
  (stacked) one;
* the floor was a hard-coded 0.5 instead of the resolved profile value.
"""

from __future__ import annotations

import math
from pathlib import Path

from kicad_tools.cli.stitch_cmd import (
    MIN_HOLE_TO_HOLE_CLEARANCE,
    _resolve_mfr_min_hole_to_hole,
    run_stitch,
)
from kicad_tools.manufacturers.base import load_design_rules_from_yaml

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

DRILL = 0.3
VIA = 0.6


def _write(tmp_path: Path, extra: str = "") -> Path:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    assert text.endswith(")")
    text = text[:-1] + extra + ")\n"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    return pcb


def _via(x: float, y: float, net: int) -> str:
    return (
        f'  (via (at {x} {y}) (size {VIA}) (drill {DRILL}) (layers "F.Cu" "B.Cu") '
        f'(net {net}) (uuid "00000000-0000-0000-0000-0000000009{net:02d}"))\n'
    )


def _edge(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) - DRILL


def _stitch(pcb: Path, floor: float):
    return run_stitch(pcb, ["GND"], via_size=VIA, drill=DRILL, dry_run=True, min_hole_to_hole=floor)


def test_resolver_matches_check_profile_value():
    rules = next(iter(load_design_rules_from_yaml("jlcpcb_tier1").values()))
    assert _resolve_mfr_min_hole_to_hole("jlcpcb-tier1", 4) == rules.min_hole_to_hole_mm


def test_stitch_via_near_limit_from_own_net_via_is_not_committed(tmp_path):
    base = _stitch(_write(tmp_path), MIN_HOLE_TO_HOLE_CLEARANCE)
    assert len(base.vias_added) == 1
    vx, vy = base.vias_added[0].via_x, base.vias_added[0].via_y

    # An own-net (GND) via 0.457 mm edge-to-edge from the would-be stitch via:
    # the board-05 failure.  Same-net, non-coincident -> must not be exempt.
    near = (vx + DRILL + 0.457, vy)
    pcb = _write(tmp_path, _via(*near, net=1))
    result = _stitch(pcb, MIN_HOLE_TO_HOLE_CLEARANCE)
    for v in result.vias_added:
        assert _edge((v.via_x, v.via_y), near) + 1e-3 >= MIN_HOLE_TO_HOLE_CLEARANCE


def test_resolved_floor_is_applied_to_foreign_via(tmp_path):
    base = _stitch(_write(tmp_path), 0.5)
    vx, vy = base.vias_added[0].via_x, base.vias_added[0].via_y
    # Foreign via 0.6 mm edge-to-edge: legal at 0.5, illegal at a 0.8 floor.
    near = (vx + DRILL + 0.6, vy)
    pcb = _write(tmp_path, _via(*near, net=0))
    for floor in (0.5, 0.8):
        result = _stitch(pcb, floor)
        for v in result.vias_added:
            assert _edge((v.via_x, v.via_y), near) + 1e-3 >= floor


def test_kernel_same_net_via_must_clear_hole_to_hole():
    from kicad_tools.router.via_clearance import point_clear_of_copper

    kw = {
        "via_size": 0.45,
        "clearance": 0.2,
        "other_net_tracks": [],
        "other_net_vias": [],
        "other_net_pads": [],
        "same_net_vias": [(0.0, 0.0)],
        "via_drill": 0.2,
    }
    # 0.65 mm centre spacing: copper-legal (0.45 + 0.2) but only 0.45 mm
    # hole-to-hole -- the board-05 failure.
    assert point_clear_of_copper(x=0.65, y=0.0, **kw)
    assert not point_clear_of_copper(x=0.65, y=0.0, min_hole_to_hole=0.5, **kw)
    assert point_clear_of_copper(x=0.71, y=0.0, min_hole_to_hole=0.5, **kw)


def test_blanket_stitch_vias_clear_floor_between_each_other(tmp_path):
    from kicad_tools.cli.stitch_cmd import run_blanket_stitch

    pcb = _write(tmp_path)
    result = run_blanket_stitch(pcb, ["GND"], via_size=0.45, drill=0.2, spacing=0.66, dry_run=True)
    pts = [(v.via_x, v.via_y) for v in result.vias_added]
    assert len(pts) > 1
    for i, a in enumerate(pts):
        for b in pts[i + 1 :]:
            assert math.hypot(a[0] - b[0], a[1] - b[1]) - 0.2 + 1e-3 >= 0.5
