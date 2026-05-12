"""C++ <-> Python grid parity gates.

Issue #2709: pin the current Python-only contract for the
corridor-reservation feature added by Issue #2677 / PR #2686.

The Python ``RoutingGrid._mark_via`` consults
``RoutingGrid._reserved_for_nets`` (populated by
``EscapeRouter._reserve_pair_continuation_corridor``) to skip cells that
have been reserved for paired-escape continuation.  The C++ sibling
``router::Grid3D::mark_via`` (cpp/src/grid.cpp) deliberately omits that
check because the escape phase is Python-grid-only today.

This test pins the CURRENT behaviour:

  * Python ``Grid._mark_via`` with a partner-net via SKIPS reserved
    cells (the existing diff-pair gate already covers this in
    ``test_escape_diffpair.py::test_gate_b_partner_vias_do_not_consume_reserved_cells``).
  * C++ ``Grid3D::mark_via`` with the same partner-net via DOES block
    the reserved cell (because it ignores the Python reservation map).

When the escape phase is ported to C++ (e.g. with Epic #2661 Phase 2's
group-of-pairs serpentine), the C++ ``mark_via`` will need an equivalent
reservation API.  At that point this test SHOULD be inverted to assert
the cell remains UNblocked on the C++ side.  A failing test in this
file is a deliberate signal that the parity gap has been closed (or that
someone changed the Python contract without the matching C++ port).

Test is skipped when the C++ backend is not built so it is non-fatal in
Python-only environments.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Via
from kicad_tools.router.rules import DesignRules

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


@pytest.fixture
def grid_4layer(rules: DesignRules) -> RoutingGrid:
    """4-layer all-signal stack so an inner SIGNAL layer exists.

    Mirrors the fixture in ``test_escape_diffpair.py``.
    """
    stack = LayerStack.four_layer_all_signal()
    return RoutingGrid(50, 50, rules, origin_x=0, origin_y=0, layer_stack=stack)


# --------------------------------------------------------------------------- #
# Issue #2709 parity gate                                                     #
# --------------------------------------------------------------------------- #


class TestMarkViaReservationParity:
    """Pin the Python-only nature of corridor reservations.

    These tests document the CURRENT behaviour described in the
    docstring of ``RoutingGrid._mark_via`` and the Issue #2709 comment
    block on ``router::Grid3D::mark_via``.  When the escape phase moves
    into C++ and the reservation logic is ported, the assertions tagged
    with ``# CONTRACT: invert when C++ port lands`` must flip.
    """

    def test_python_grid_skips_partner_via_at_reserved_cell(
        self, grid_4layer: RoutingGrid, rules: DesignRules
    ) -> None:
        """Sanity gate: the Python contract still holds.

        Reserves a single inner-layer cell for nets {1, 2}, then calls
        ``Grid._mark_via`` for a foreign net (42).  The reserved cell
        must remain UNBLOCKED, matching the Issue #2677 contract that
        partner-net through-hole vias do not colonise reserved corridor
        cells on the Python grid.
        """
        # Pick an inner-layer cell well away from the grid edge so the
        # via radius does not run off the grid.
        gx, gy = 25, 25
        # Find a SIGNAL inner layer index for this stack.
        inner_layer_idx = None
        for idx in range(grid_4layer.num_layers):
            layer_enum = grid_4layer.index_to_layer(idx)
            if layer_enum not in (Layer.F_CU.value, Layer.B_CU.value):
                inner_layer_idx = idx
                break
        assert inner_layer_idx is not None, "4-layer stack should expose an inner layer"

        # Reserve a single cell on the inner layer for nets {1, 2}.
        owner_nets = frozenset({1, 2})
        grid_4layer.reserve_corridor_cells(inner_layer_idx, [(gx, gy)], owner_nets)
        assert grid_4layer.is_reserved_for(inner_layer_idx, gx, gy, 1)
        assert not grid_4layer.is_reserved_for(inner_layer_idx, gx, gy, 42)

        # Confirm the cell starts unblocked.
        assert not grid_4layer.grid[inner_layer_idx][gy][gx].blocked

        # Place a foreign-net via at the reserved cell's world coordinate.
        wx, wy = grid_4layer.grid_to_world(gx, gy)
        partner_via = Via(
            x=wx,
            y=wy,
            drill=rules.via_drill,
            diameter=rules.via_diameter,
            layers=(Layer.F_CU, Layer.B_CU),
            net=42,
            net_name="FOREIGN",
        )
        grid_4layer._mark_via(partner_via)

        # Python contract: reserved cell must remain unblocked when the
        # via belongs to a non-owner net.
        assert not grid_4layer.grid[inner_layer_idx][gy][gx].blocked, (
            "Python _mark_via must skip cells reserved for a different net set "
            "(Issue #2677)."
        )

    def test_cpp_grid_ignores_python_reservations(
        self, grid_4layer: RoutingGrid, rules: DesignRules
    ) -> None:
        """C++ ``Grid3D::mark_via`` does NOT consult Python reservations.

        Lock the current behaviour: a foreign-net via marked through the
        C++ binding DOES block a cell that the Python grid has reserved
        for a different net set.

        Issue #2709: this test should be INVERTED (assert ``not blocked``)
        when escape routing is ported to C++ and the reservation logic
        lands on ``Grid3D``.  A failing assertion below means the parity
        gap was closed -- update the assertion and remove the
        contract-locking docstring.
        """
        if not is_cpp_available():
            pytest.skip("C++ router backend not built (run: kct build-native)")

        from kicad_tools.router.cpp_backend import CppGrid

        # Pick the same inner-layer cell as the Python sanity gate.
        gx, gy = 25, 25
        inner_layer_idx = None
        for idx in range(grid_4layer.num_layers):
            layer_enum = grid_4layer.index_to_layer(idx)
            if layer_enum not in (Layer.F_CU.value, Layer.B_CU.value):
                inner_layer_idx = idx
                break
        assert inner_layer_idx is not None, "4-layer stack should expose an inner layer"

        # Reserve the cell on the Python grid BEFORE building the C++
        # mirror.  ``CppGrid.from_routing_grid`` copies blocked cells but
        # has no concept of corridor reservations -- which is exactly the
        # parity gap this test pins.
        owner_nets = frozenset({1, 2})
        grid_4layer.reserve_corridor_cells(inner_layer_idx, [(gx, gy)], owner_nets)
        assert grid_4layer.reserved_cell_count() == 1
        assert not grid_4layer.grid[inner_layer_idx][gy][gx].blocked

        cpp_grid = CppGrid.from_routing_grid(grid_4layer)

        # Confirm the C++ mirror starts with the cell unblocked
        # (reservations don't block, only Python ``_mark_via`` does).
        assert not cpp_grid._impl.at(gx, gy, inner_layer_idx).blocked, (
            "Cell should start unblocked on the C++ side; reservations alone "
            "do not block cells."
        )

        # Place a foreign-net via through the C++ binding.  The radius
        # of 0 cells targets exactly (gx, gy) on every layer, isolating
        # the reservation-skip behaviour from clearance geometry.
        cpp_grid._impl.mark_via(gx, gy, 42, 0)

        # CONTRACT: invert when C++ port lands.
        # Today: C++ ignores the Python reservation map, so the cell IS blocked.
        # Future: when ``Grid3D`` learns about reservations, this should
        # become ``assert not cell.blocked`` and the Python parity gate
        # above will document the symmetric expectation.
        cell = cpp_grid._impl.at(gx, gy, inner_layer_idx)
        assert cell.blocked, (
            "Issue #2709 contract: C++ Grid3D::mark_via deliberately ignores "
            "Python's _reserved_for_nets map.  If this assertion fails, the "
            "C++ port has likely landed -- invert the assertion and update "
            "the rationale comments in cpp/src/grid.cpp and "
            "src/kicad_tools/router/grid.py:_mark_via."
        )
        assert cell.net == 42, (
            "Foreign-net via should claim the cell on the C++ side."
        )
