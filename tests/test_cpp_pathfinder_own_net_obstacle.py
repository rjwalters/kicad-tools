"""Regression tests for Issue #2989 / #2990: C++ pathfinder must admit
own-net obstacle cells in negotiated mode.

Background
==========

PR #2928 marks isolated pad-metal cells as ``is_obstacle=True`` on first
touch.  PR #2972 (closes #2963) added the own-net gate to the Python
mirror (``pathfinder.py::Pathfinder._is_via_blocked`` SoA branch at
line 1442, plus ``_is_trace_blocked``'s rect-mask which was already
net-gated).

The PR #2972 description listed the C++ mirror as fixed for the cost
function (``grid.cpp::get_negotiated_cost``) but the *blocking* checks
in ``pathfinder.cpp`` (``is_via_blocked_diag``, ``is_trace_blocked``,
``is_diagonal_blocked``) were not updated.  All three retained the
pattern::

    if (allow_sharing && !cell.is_obstacle) {
        // negotiated branch (own-net obstacle skipped entirely)
    } else {
        if (cell.is_obstacle || cell.net != net) return true;  // <- bug
    }

This branch rejects own-net obstacle cells unconditionally because the
``else`` arm fires whenever ``cell.is_obstacle`` is true -- even when
``allow_sharing`` is true and the cell belongs to the routing net.

The C++ backend is the default backend, so this is the live bug behind:

* Board 03 USB_D-/USB_CC2/JOY_Y diff-pair partial completion (#2990).
* Board 06 USB3 / PCIE / MIPI diff-pair partial completion (#2989).

The fix mirrors the Python SoA branch pattern: split first on
``allow_sharing``, then in the negotiated arm only reject obstacle
cells when ``cell.net != net``.

These tests exercise the three C++ predicates directly through the
pybind interface so the regression is caught without needing a full
board route.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_grid_and_rules() -> tuple[RoutingGrid, DesignRules]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=5.0,
        height=5.0,
        rules=rules,
        layer_stack=LayerStack.four_layer_all_signal(),
    )
    return grid, rules


def _paint_own_net_obstacle(
    cpp_grid: CppGrid, gx: int, gy: int, net: int
) -> None:
    """Mark the cell at grid (gx, gy) as a same-net obstacle on every
    layer.  Mirrors the post-PR #2928 isolated-pad first-touch
    bookkeeping the Python ``EscapeRouter`` does.
    """
    for layer_idx in range(cpp_grid._impl.layers):
        cpp_grid._impl.mark_blocked(gx, gy, layer_idx, net, True)


@requires_cpp
class TestIsViaBlockedOwnNetObstacle:
    """Issue #2989 sibling of #2963: ``Pathfinder::is_via_blocked`` must
    admit a via candidate whose cell is an own-net ``is_obstacle`` in
    negotiated (``allow_sharing=True``) mode.
    """

    def test_own_net_obstacle_admits_via_negotiated(self) -> None:
        """Negotiated-mode via probe at an own-net obstacle cell must
        NOT be rejected.  This is the diff-pair partner B pad case --
        partner B's pad is own-net but is_obstacle=True (painted by
        PR #2942 rect-aware halo + PR #2928 first-touch), and without
        the own-net gate the via probe rejects unconditionally, leaving
        the partner endpoint unreachable.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        pad_net = 7
        gx, gy = cpp_grid._impl.world_to_grid(2.5, 2.5)
        _paint_own_net_obstacle(cpp_grid, gx, gy, pad_net)

        # allow_sharing=True (negotiated mode) is the regression site.
        assert not pathfinder._impl.is_via_blocked(
            gx, gy, pad_net, True, 0
        ), (
            "Issue #2989: same-net via must be admitted on an own-net "
            "is_obstacle cell in negotiated mode (diff-pair partner B "
            "pad reachability for USB3/PCIE/MIPI escapes)."
        )

    def test_foreign_net_obstacle_rejects_via_negotiated(self) -> None:
        """Foreign-net obstacle cells must STILL be rejected.  The fix
        is a refinement, not a relaxation -- PR #2928's invariant
        (foreign isolated pad metal blocks foreign vias) is preserved.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        obstacle_net = 7
        probe_net = 99
        gx, gy = cpp_grid._impl.world_to_grid(2.5, 2.5)
        _paint_own_net_obstacle(cpp_grid, gx, gy, obstacle_net)

        assert pathfinder._impl.is_via_blocked(
            gx, gy, probe_net, True, 0
        ), (
            "Issue #2989: foreign-net obstacle cells must still reject "
            "the via (preserves PR #2928's invariant)."
        )

    def test_own_net_obstacle_admits_via_standard(self) -> None:
        """Issue #3622: In standard (non-negotiated) mode an own-net
        ``is_obstacle`` cell must NOT reject the via -- parity with the
        Python ``_is_via_blocked`` standard branch (Issue #864: same-net
        cells are passable regardless of the obstacle flag) and with the
        sibling ``is_trace_blocked`` / ``is_diagonal_blocked`` standard
        predicates aligned in #3456.

        This flips the previously-pinned strict-reject contract.  Its
        rationale ("A* in standard mode never enters obstacle metal
        anyway") was shown false during #3456 -- standard-mode
        ``route_all`` does traverse own-pad copper -- so a board that
        routes a via through its own destination pad in the Python
        fallback but not in C++ was a silent-fallback seed of exactly
        the #3456 class.  Both backends now agree.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        pad_net = 7
        gx, gy = cpp_grid._impl.world_to_grid(2.5, 2.5)
        _paint_own_net_obstacle(cpp_grid, gx, gy, pad_net)

        # Standard mode now admits the own-net via, matching the Python
        # ``_is_via_blocked`` standard branch (same-net passable).
        assert not pathfinder._impl.is_via_blocked(
            gx, gy, pad_net, False, 0
        ), (
            "Issue #3622: standard-mode via probe at an own-net "
            "is_obstacle cell must be admitted (Python #864 parity); "
            "foreign-net obstacles still reject."
        )

    def test_foreign_net_obstacle_rejects_via_standard(self) -> None:
        """Counterpart to the own-net admit case above: in standard mode
        a FOREIGN-net cell must still reject the via.  The #3622 fix is a
        same-net relaxation, not a blanket one -- foreign metal continues
        to hard-block.
        """
        grid, rules = _make_grid_and_rules()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        obstacle_net = 7
        probe_net = 99
        gx, gy = cpp_grid._impl.world_to_grid(2.5, 2.5)
        _paint_own_net_obstacle(cpp_grid, gx, gy, obstacle_net)

        assert pathfinder._impl.is_via_blocked(
            gx, gy, probe_net, False, 0
        ), (
            "Issue #3622: foreign-net cells must still reject the via in "
            "standard mode (same-net relaxation only)."
        )


@requires_cpp
class TestSingleLayerOwnNetObstacleNoVias:
    """Issue #3622 follow-up: the standard-mode own-net-obstacle via admit
    must NOT introduce vias on a single-layer (``allowed_layers``) route.

    The #864 parity relaxation made own-net ``is_obstacle`` cells passable
    for vias in standard mode.  That removed an incidental suppression that
    had been keeping vias off single-layer boards: the C++ pathfinder's
    via-expansion loop iterates every grid layer, and only the strict
    obstacle reject (now relaxed) had been blocking the layer change when a
    candidate via's clearance disc touched the routing net's own pad copper.

    The fix restricts the C++ ``routable_layers_`` set to the
    ``allowed_layers`` permitted indices (see
    ``CppPathfinder._apply_allowed_layers_to_routable``), so a single-layer
    route has no second layer to land a via on.  These tests pin that the
    own-net admit and the single-layer invariant coexist.
    """

    def test_single_layer_route_emits_no_vias_with_own_net_obstacle(self) -> None:
        """A same-net two-pad route constrained to F.Cu must emit zero vias
        even though the destination pad copper is an own-net ``is_obstacle``
        that the #864 relaxation now admits for via landing.
        """
        rules = DesignRules(allowed_layers=["F.Cu"])
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        pads = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 40.0, "y": 20.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)

        assert routes, "Single-layer same-net route should still connect"
        for route in routes:
            assert len(route.vias) == 0, (
                "Issue #3622: own-net-obstacle via admit must not introduce "
                "vias on a single-layer (allowed_layers=['F.Cu']) route"
            )

    def test_routable_layers_restricted_to_single_allowed_layer(self) -> None:
        """White-box check: the C++ pathfinder's routable-layer set is
        restricted to the single allowed copper index, leaving the via loop
        with no alternate layer to expand onto.
        """
        rules = DesignRules(allowed_layers=["F.Cu"], grid_resolution=0.1)
        grid = RoutingGrid(
            width=5.0,
            height=5.0,
            rules=rules,
            layer_stack=LayerStack.four_layer_all_signal(),
        )
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)

        f_cu_idx = grid.layer_to_index(0)  # F.Cu is enum value 0
        assert pathfinder._routable_layers == [f_cu_idx], (
            "Issue #3622: allowed_layers=['F.Cu'] must restrict the C++ "
            "routable-layer set to the single F.Cu index so the via loop "
            "has no second routable layer to land on"
        )
