"""The coupled pose search measures its partner's pads exactly (Issues #7491, #7401).

On board 06 the pose trunk for USB2_D ran its rails through the 0.7 mm gap
between J1.B7 and J1.B6, about 0.02 mm from the partner's pads.  The search's
per-step gate, ``CoupledPathfinder::rail_segment_clear``, saw pads only through
the raster halo (about 0.05 mm past the pad edge), and ``rail_clear_world``
measures stored route copper, not pads.  The finished-copper check measures
partner pads exactly and refused the trunk (``derived-rails-blocked``), so the
pair fell back to single-ended routing, 0.091 mm over its uncoupled-length
limit.

``rail_segment_clear`` now also calls ``Grid3D.pad_geometry_clear`` restricted
to the partner net (``only_net``), so the search and the verification agree.
Board 06's USB2_D then routes as a coupled trunk (the ``check_diffpair_coverage``
gate pins that end to end: its #7401 uncoupled-length allowance is deleted).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.skipif(
    not is_cpp_available(), reason="C++ router backend not available; run `kct build-native`"
)

CLEARANCE = 0.15


def _native_grid_with_pad():
    """A native grid holding one 0.6 x 0.6 mm pad of net 2 at (3, 2)."""
    from kicad_tools.router.cpp_backend import CppGrid

    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=CLEARANCE,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=CLEARANCE,
        grid_resolution=0.05,
    )
    grid = RoutingGrid(width=6.0, height=4.0, rules=rules, layer_stack=LayerStack.two_layer())
    impl = CppGrid.from_routing_grid(grid)._impl
    impl.add_pad(3.0, 2.0, 0.6, 0.6, 2, 0, 0, 0.0, False, 0.0, False)
    return impl


# A 0.2 mm horizontal trace whose edge passes 0.02 mm from the pad's top edge.
_Y_TOO_CLOSE = 2.0 + 0.3 + 0.1 + 0.02


def test_only_net_measures_that_nets_pads() -> None:
    """The partner probe sees the partner's pad exactly."""
    impl = _native_grid_with_pad()
    y = _Y_TOO_CLOSE
    assert not impl.pad_geometry_clear(2.0, y, 4.0, y, 0, 1, 0.1, CLEARANCE, 2)


def test_only_net_ignores_other_nets_pads() -> None:
    """Restricted to net 3, net 2's pad is not this probe's business."""
    impl = _native_grid_with_pad()
    y = _Y_TOO_CLOSE
    assert impl.pad_geometry_clear(2.0, y, 4.0, y, 0, 1, 0.1, CLEARANCE, 3)


def test_default_still_checks_every_foreign_pad() -> None:
    """``only_net`` defaults to -1: the #6315 single-ended callers are unchanged."""
    impl = _native_grid_with_pad()
    y = _Y_TOO_CLOSE
    assert not impl.pad_geometry_clear(2.0, y, 4.0, y, 0, 1, 0.1, CLEARANCE)
    clear_y = 2.0 + 0.3 + 0.1 + 0.2
    assert impl.pad_geometry_clear(2.0, clear_y, 4.0, clear_y, 0, 1, 0.1, CLEARANCE)
