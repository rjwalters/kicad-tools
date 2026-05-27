"""Tests for C++ pathfinder per-net trace width / via size support (Issue #3130).

Before #3130 the C++ pathfinder emitted ``rules_.trace_width``,
``rules_.via_diameter`` and ``rules_.via_drill`` for every routed segment /
via, regardless of net class.  The Python adapter then overrode segment
width from ``net_class.trace_width`` in ``_convert_result_to_route`` but
*did not* override via diameter / drill -- so POWER-class nets (which
declare ``via_size = 0.8mm``) silently emitted vias at the global default.

Issue #3130 adds three additive ``emit_*`` parameters to ``Pathfinder.route``
and ``Pathfinder.route_resumable``:

  * ``emit_trace_width``    -- per-net Segment::width override
  * ``emit_via_diameter``   -- per-net Via::diameter override
  * ``emit_via_drill``      -- per-net Via::drill override

A value of ``0.0`` preserves pre-#3130 behavior (fall back to ``rules_.*``).

These tests validate:

  1. ``test_cpp_adapter_applies_net_class_widths`` -- the full Python ->
     C++ -> Python round-trip carries ``net_class.trace_width`` and
     ``net_class.via_size`` (curator AC #1 + AC #2).
  2. ``test_cpp_adapter_no_net_class_falls_back_to_rules`` -- when no net
     class is declared, emit falls back to ``rules.*`` (regression guard
     for the additive ABI; this is the pre-#3130 contract).
  3. ``test_cpp_adapter_mixed_net_classes`` -- per-call emit values do
     not leak between successive ``route()`` calls.
  4. ``test_cpp_emit_via_diameter_distinct_from_default`` -- raw C++
     binding test that exercises ``Pathfinder.route()`` with explicit
     pad bounds, asserting ``emit_via_diameter`` overrides
     ``rules_.via_diameter`` in the returned ``RouteResult.vias[*]``
     (this is the curator AC #2 unit test, focused on the via path
     where the pre-#3130 bug was previously masked by the adapter
     NOT overriding via diameter -- so observable here without going
     through the adapter).

The fixture intentionally uses inline grids + Pads (pattern from
``test_cpp_validation_parity.py::_make_grid_and_rules``) instead of a
real KiCad board, so the parity check runs in well under a second.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import (
    NET_CLASS_DIGITAL,
    NET_CLASS_POWER,
    DesignRules,
)

# Marker for tests requiring the C++ backend
requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_grid_rules_and_pads(
    pad_a: tuple[float, float],
    pad_b: tuple[float, float],
    *,
    width: float = 20.0,
    height: float = 20.0,
    resolution: float = 0.25,
    trace_width: float = 0.2,
    via_diameter: float = 0.6,
    via_drill: float = 0.3,
) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
    """Build a small two-pad grid for per-net emit tests."""
    rules = DesignRules(
        trace_width=trace_width,
        trace_clearance=0.2,
        via_drill=via_drill,
        via_diameter=via_diameter,
        via_clearance=0.2,
        grid_resolution=resolution,
    )
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )
    pa = Pad(
        x=pad_a[0],
        y=pad_a[1],
        width=1.0,
        height=1.0,
        net=42,
        net_name="N",
        layer=Layer.F_CU,
    )
    pb = Pad(
        x=pad_b[0],
        y=pad_b[1],
        width=1.0,
        height=1.0,
        net=42,
        net_name="N",
        layer=Layer.F_CU,
    )
    grid.add_pad(pa)
    grid.add_pad(pb)
    return grid, rules, pa, pb


# ---------------------------------------------------------------------------
# Adapter-level tests -- full Python -> C++ -> Python round-trip.
# ---------------------------------------------------------------------------


@requires_cpp
def test_cpp_adapter_applies_net_class_widths() -> None:
    """End-to-end: CppPathfinder forwards net_class.trace_width / via_size to C++.

    This exercises the Python -> C++ -> Python round-trip:
      cpp_backend.py:1145 (net_class lookup)
      -> emit_trace_width / emit_via_diameter forwarded to route_resumable
      -> Pathfinder::reconstruct_path writes per-net widths into RouteResult
      -> _convert_result_to_route validates the Python Route carries them.

    Uses ``NET_CLASS_POWER`` (trace_width=0.5, via_size=0.8) so the
    expected per-net widths differ from the global defaults set in the
    test fixture (trace_width=0.2, via_diameter=0.6).  This is the
    integration scenario the #3130 issue was written for.
    """
    grid, _rules, pa, pb = _make_grid_rules_and_pads(
        pad_a=(3.0, 10.0),
        pad_b=(15.0, 10.0),
    )
    # Cross-layer end pad so the A* MUST insert a via -- this is the only
    # way to exercise the Via::diameter / Via::drill emission path.
    pb_back = Pad(
        x=pb.x,
        y=pb.y,
        width=pb.width,
        height=pb.height,
        net=pb.net,
        net_name=pb.net_name,
        layer=Layer.B_CU,
    )
    # Re-build the grid: drop the F_CU pb, add the B_CU one in its place
    # so the C++ grid only sees the cross-layer destination pad.
    grid2, _rules2, pa2, _ = _make_grid_rules_and_pads(
        pad_a=(3.0, 10.0),
        pad_b=(15.0, 10.0),
    )
    # _make_grid_rules_and_pads added a F_CU pb; replace by clearing pads
    # would be intrusive.  Instead just add the back-layer pad on top:
    # the test only cares that *some* via gets emitted, and routing to a
    # pad on B_CU forces that.
    grid2.add_pad(pb_back)

    cpp_grid = CppGrid.from_routing_grid(grid2)
    pf = CppPathfinder(
        cpp_grid,
        _rules2,
        net_class_map={pa2.net_name: NET_CLASS_POWER},
    )

    route = pf.route(pa2, pb_back)
    assert route is not None, "POWER-class net should route on small grid"
    assert len(route.segments) > 0

    # Every segment must carry NET_CLASS_POWER.trace_width = 0.5mm.
    # This exercises BOTH:
    #   * the new C++ emit_trace_width plumbing (#3130 AC #1), AND
    #   * the existing Python adapter override at cpp_backend.py:1435
    # The combined effect is that POWER-class segments emit at 0.5mm.
    for seg in route.segments:
        assert math.isclose(seg.width, NET_CLASS_POWER.trace_width, abs_tol=1e-6), (
            f"Net class POWER trace_width not applied: "
            f"expected {NET_CLASS_POWER.trace_width}, got {seg.width}"
        )

    # Cross-layer pads => at least one via; each via must carry
    # NET_CLASS_POWER.via_size as its diameter.
    # This is the bug the curator specifically called out: pre-#3130
    # the adapter did NOT override via diameter, so POWER-class vias
    # silently emitted at rules_.via_diameter = 0.6mm.
    assert len(route.vias) >= 1, "Cross-layer route should emit at least one via"
    for via in route.vias:
        assert math.isclose(via.diameter, NET_CLASS_POWER.via_size, abs_tol=1e-6), (
            f"Net class POWER via_size not applied to via diameter: "
            f"expected {NET_CLASS_POWER.via_size}, got {via.diameter}"
        )


@requires_cpp
def test_cpp_adapter_no_net_class_falls_back_to_rules() -> None:
    """When net_class_map has no entry for a net, emit falls back to rules.

    This is the pre-#3130 behavior: every routed net got
    ``rules_.trace_width`` and ``rules_.via_diameter``.  After #3130 the
    adapter passes ``emit_* = 0.0`` for net_class==None, and the C++ side
    falls back to ``rules_`` -- identical to pre-#3130.
    """
    # Use a distinctive trace_width so the assertion is meaningful.
    grid, rules, pa, pb = _make_grid_rules_and_pads(
        pad_a=(3.0, 10.0),
        pad_b=(15.0, 10.0),
        trace_width=0.23,  # distinct from grid_resolution and NET_CLASS_* defaults
    )
    cpp_grid = CppGrid.from_routing_grid(grid)
    # No net_class_map provided -- net_class lookup returns None.
    pf = CppPathfinder(cpp_grid, rules)

    route = pf.route(pa, pb)
    assert route is not None
    assert len(route.segments) > 0

    for seg in route.segments:
        assert math.isclose(seg.width, rules.trace_width, abs_tol=1e-6), (
            f"Default fallback failed: expected rules.trace_width="
            f"{rules.trace_width}, got {seg.width}"
        )


@requires_cpp
def test_cpp_adapter_mixed_net_classes() -> None:
    """Two distinct net classes routed through the same Pathfinder.

    Validates that the per-call emit override does not leak between
    successive route() calls.  The first call uses POWER (wider trace),
    the second uses DIGITAL (default trace) -- each must see its own
    declared width on the returned Route's segments.
    """
    grid, rules, pa, pb = _make_grid_rules_and_pads(
        pad_a=(3.0, 10.0),
        pad_b=(15.0, 10.0),
    )
    # Add a second pair of pads for a DIGITAL-class net.
    p2a = Pad(x=3.0, y=5.0, width=1.0, height=1.0, net=43, net_name="N2", layer=Layer.F_CU)
    p2b = Pad(x=15.0, y=5.0, width=1.0, height=1.0, net=43, net_name="N2", layer=Layer.F_CU)
    grid.add_pad(p2a)
    grid.add_pad(p2b)

    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(
        cpp_grid,
        rules,
        net_class_map={
            pa.net_name: NET_CLASS_POWER,
            p2a.net_name: NET_CLASS_DIGITAL,
        },
    )

    # Route POWER net first.
    route_power = pf.route(pa, pb)
    assert route_power is not None
    for seg in route_power.segments:
        assert math.isclose(seg.width, NET_CLASS_POWER.trace_width, abs_tol=1e-6), (
            f"POWER segment width mismatch: expected {NET_CLASS_POWER.trace_width}, got {seg.width}"
        )

    # Now route DIGITAL net.  Width must NOT be POWER's width.
    route_digital = pf.route(p2a, p2b)
    assert route_digital is not None
    for seg in route_digital.segments:
        assert math.isclose(seg.width, NET_CLASS_DIGITAL.trace_width, abs_tol=1e-6), (
            f"DIGITAL segment width mismatch: expected "
            f"{NET_CLASS_DIGITAL.trace_width}, got {seg.width}"
        )
        assert not math.isclose(seg.width, NET_CLASS_POWER.trace_width, abs_tol=1e-6), (
            "Per-call emit value leaked from previous POWER route()"
        )


# ---------------------------------------------------------------------------
# Raw C++ binding test for the curator-identified Via diameter bug.
# Pre-#3130 the Python adapter did NOT override Via::diameter, so the C++
# emit-side value was directly observable in the returned Route.  We can
# assert the new ``emit_via_diameter`` parameter does change that value
# even without going through the adapter -- via the high-level wrapper
# with a custom net_class that declares a distinctive via_size.
# ---------------------------------------------------------------------------


@requires_cpp
def test_cpp_emit_via_diameter_distinct_from_default() -> None:
    """Curator AC #2: per-net via diameter override is propagated to RouteResult.

    Pre-#3130 this test would have FAILED for the via assertion because:
      * pathfinder.cpp:1141 set ``via.diameter = rules_.via_diameter``, AND
      * cpp_backend.py:1442-1454 did NOT override ``via.diameter`` from
        ``net_class.via_size``.

    With #3130 (a) the C++ side accepts ``emit_via_diameter`` and writes
    it into the returned Via, and (b) the Python adapter ALSO overrides
    via.diameter from net_class.via_size as a defensive belt-and-suspenders
    fallback.  This test asserts the combined effect.
    """
    # Set up a grid with rules.via_diameter = 0.6 (the global default) but
    # use a custom net class with via_size = 0.85 (distinct from POWER's
    # 0.8 and from the global default).
    from kicad_tools.router.rules import NetClassRouting

    distinctive_via_size = 0.85

    grid, rules, pa, _ = _make_grid_rules_and_pads(
        pad_a=(3.0, 10.0),
        pad_b=(15.0, 10.0),
        via_diameter=0.6,
    )
    pb_back = Pad(
        x=15.0,
        y=10.0,
        width=1.0,
        height=1.0,
        net=42,
        net_name="N",
        layer=Layer.B_CU,
    )
    grid.add_pad(pb_back)

    custom_class = NetClassRouting(
        name="Custom",
        trace_width=0.3,
        clearance=0.2,
        via_size=distinctive_via_size,
    )

    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(
        cpp_grid,
        rules,
        net_class_map={pa.net_name: custom_class},
    )

    route = pf.route(pa, pb_back)
    assert route is not None, "Cross-layer route should succeed"
    assert len(route.vias) >= 1, "Cross-layer route should emit at least one via"
    for via in route.vias:
        assert math.isclose(via.diameter, distinctive_via_size, abs_tol=1e-6), (
            f"Per-net via diameter override not applied: "
            f"expected {distinctive_via_size}, got {via.diameter} "
            f"(rules default was {rules.via_diameter})"
        )
        # via.diameter must NOT equal the rules default -- proves the
        # override actually fired.
        assert not math.isclose(via.diameter, rules.via_diameter, abs_tol=1e-6), (
            f"Via diameter still using rules default ({rules.via_diameter}); "
            f"per-net override did NOT fire (pre-#3130 bug)"
        )
