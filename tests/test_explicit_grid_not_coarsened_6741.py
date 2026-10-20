"""An explicit routing pitch is never silently coarsened (Issue #6741).

On a 195 x 155 mm board, ``kct route --grid 0.1 --backend python`` used to
print ``Grid: 561x446x2 = 500,412 cells`` (~0.35 mm pitch): the Autorouter's
#972 adaptive cell cap replaced the requested -- and clearance-checked --
pitch with a coarser one, with no message.
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.router.core import Autorouter, worker_router_kwargs
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import DesignRules


def _board(width: float, height: float) -> str:
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "SIG")
      (footprint "Test:A" (layer "F.Cu") (at 10 10)
        (property "Reference" "J1" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
      (footprint "Test:B" (layer "F.Cu") (at 20 15)
        (property "Reference" "J2" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
      (footprint "Test:C" (layer "F.Cu") (at 30 20)
        (property "Reference" "J3" (at 0 -2) (layer "F.SilkS")
          (effects (font (size 1 1) (thickness 0.15))))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG")))
      (gr_rect (start 0 0) (end {width} {height}) (stroke (width 0.05) (type solid))
        (fill none) (layer "Edge.Cuts")))"""


def _rules(grid: float) -> DesignRules:
    return DesignRules(
        grid_resolution=grid,
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.3,
        via_diameter=0.6,
    )


@pytest.mark.parametrize(("width", "height"), [(40, 30), (195, 155)])
def test_loaded_router_honours_explicit_grid(tmp_path, width, height):
    """The issue's repro: the large board must route at 0.1 mm, like the small one."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board(width, height))

    router, _ = load_pcb_for_routing(
        str(path), rules=_rules(0.1), force_python=True, strict_drc=False
    )

    assert router.grid.resolution == pytest.approx(0.1)
    # Before the fix the 195 x 155 board came out at 561 x 446 (~0.35 mm).
    assert router.grid.cols >= round(width / 0.1)
    assert router.grid.rows >= round(height / 0.1)


def test_pinned_pitch_survives_trial_reset_and_parallel_workers():
    router = Autorouter(195, 155, rules=_rules(0.1), force_python=True, adaptive_grid=False)
    assert router.grid.resolution == pytest.approx(0.1)

    router._reset_for_new_trial()
    assert router.grid.resolution == pytest.approx(0.1)

    payload = router._serialize_for_parallel()
    assert worker_router_kwargs(payload)["adaptive_grid"] is False


def test_library_adaptive_coarsening_is_logged_not_silent(caplog):
    """Direct library callers keep #972's adaptive cap, but it now says so."""
    with caplog.at_level(logging.WARNING, logger="kicad_tools.router.core"):
        router = Autorouter(195, 155, rules=_rules(0.1), force_python=True)

    assert router.grid.resolution > 0.1
    assert any("Adaptive grid" in rec.getMessage() for rec in caplog.records)
