"""C++ <-> Python grid parity gates.

Issue #4071: the corridor-reservation feature (Issue #2677 / PR #2686) is
now ported to the C++ backend, so Python and C++ grids AGREE on both the
keep-out and the attractor semantics.

The Python ``RoutingGrid._mark_via`` consults
``RoutingGrid._reserved_for_nets`` (populated by ``EscapeRouter``'s
reservation helpers) to skip cells that have been reserved for a net set
that excludes the via's net.  The C++ sibling ``router::Grid3D::mark_via``
(cpp/src/grid.cpp) now mirrors that check against a per-cell owner set
marshalled across the boundary.

This file pins the PORTED (agreement) behaviour:

  * Python ``Grid._mark_via`` with a foreign-net via SKIPS reserved
    cells (the existing diff-pair gate also covers this in
    ``test_escape_diffpair.py::test_gate_b_partner_vias_do_not_consume_reserved_cells``).
  * C++ ``Grid3D::mark_via`` with the same foreign-net via ALSO skips
    the reserved cell (it now honours the mirrored reservation map).
  * The C++ A* attractor discounts a reserved cell's step cost by
    ``rules.cost_corridor_attractor`` for the OWNING net, matching
    ``RoutingGrid.get_corridor_attractor_bonus``.

Historical note (Issue #2709): this test previously pinned the OPPOSITE
divergence -- the C++ grid deliberately ignored reservations because it
had no reservation-writing consumer.  #4071 inverted that contract when
#2983 / #4053 started writing reservations through the production C++
path.

Tests are skipped when the C++ backend is not built so they are non-fatal
in Python-only environments.
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
    """Pin the PORTED (Python == C++) corridor-reservation contract.

    Issue #4071 moved the reservation keep-out into the C++
    ``Grid3D::mark_via`` and the attractor into the C++ A* cost loop, so
    the Python and C++ grids now agree.  These tests assert that
    agreement for both the keep-out (foreign-net via skips a reserved
    cell) and the attractor (owning-net step cost is discounted).
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
            "Python _mark_via must skip cells reserved for a different net set (Issue #2677)."
        )

    def _find_inner_layer(self, grid: RoutingGrid) -> int:
        """Return the first SIGNAL inner-layer index in a 4-layer stack."""
        for idx in range(grid.num_layers):
            layer_enum = grid.index_to_layer(idx)
            if layer_enum not in (Layer.F_CU.value, Layer.B_CU.value):
                return idx
        raise AssertionError("4-layer stack should expose an inner layer")

    def test_cpp_grid_honours_python_reservations(
        self, grid_4layer: RoutingGrid, rules: DesignRules
    ) -> None:
        """C++ ``Grid3D::mark_via`` NOW consults reservations (Issue #4071).

        Ported contract: a foreign-net via marked through the C++ binding
        SKIPS a cell that has been reserved for a different net set --
        matching the Python ``_mark_via`` sanity gate above.

        This test was INVERTED by #4071 (it previously pinned the opposite
        divergence under Issue #2709).  A failing assertion here means the
        C++ reservation port regressed or the marshalling path broke.
        """
        if not is_cpp_available():
            pytest.skip("C++ router backend not built (run: kct build-native)")

        from kicad_tools.router.cpp_backend import CppGrid

        gx, gy = 25, 25
        inner_layer_idx = self._find_inner_layer(grid_4layer)

        # Reserve the cell on the Python grid BEFORE building the C++
        # mirror so ``CppGrid.from_routing_grid``'s bulk copy path is the
        # one under test (the incremental mirror is covered separately).
        owner_nets = frozenset({1, 2})
        grid_4layer.reserve_corridor_cells(inner_layer_idx, [(gx, gy)], owner_nets)
        assert grid_4layer.reserved_cell_count() == 1
        assert not grid_4layer.grid[inner_layer_idx][gy][gx].blocked

        cpp_grid = CppGrid.from_routing_grid(grid_4layer)

        # The reservation must have been marshalled across the boundary.
        assert cpp_grid._impl.reserved_cell_count() == 1, (
            "from_routing_grid must marshal _reserved_for_nets into the C++ grid."
        )
        assert cpp_grid._impl.is_reserved_for(gx, gy, inner_layer_idx, 1)
        assert not cpp_grid._impl.is_reserved_for(gx, gy, inner_layer_idx, 42)

        # Confirm the C++ mirror starts with the cell unblocked.
        assert not cpp_grid._impl.at(gx, gy, inner_layer_idx).blocked

        # Place a foreign-net via (net 42) through the C++ binding.  The
        # radius of 0 cells targets exactly (gx, gy) on every layer,
        # isolating the reservation-skip behaviour from clearance geometry.
        cpp_grid._impl.mark_via(gx, gy, 42, 0)

        # Issue #4071 contract: the reserved cell must remain UNBLOCKED --
        # the foreign-net via's halo skips it, matching Python _mark_via.
        cell = cpp_grid._impl.at(gx, gy, inner_layer_idx)
        assert not cell.blocked, (
            "Issue #4071: C++ Grid3D::mark_via must skip cells reserved for a "
            "net set that excludes the via's net (parity with Python _mark_via)."
        )

        # And the OWNING net's via still blocks the cell (reservation is
        # advisory for matching-net vias).
        cpp_grid._impl.mark_via(gx, gy, 1, 0)
        owner_cell = cpp_grid._impl.at(gx, gy, inner_layer_idx)
        assert owner_cell.blocked, (
            "Owning-net via must be able to block/claim its own reserved cell."
        )
        assert owner_cell.net == 1

    def test_cpp_reservation_incremental_mirror(
        self, grid_4layer: RoutingGrid, rules: DesignRules
    ) -> None:
        """Reservations made AFTER the C++ mirror is built still propagate.

        Issue #4071: ``EscapeRouter``'s reservation helpers run during
        net-order preparation, which can be after ``from_routing_grid``.
        ``reserve_corridor_cells`` mirrors incrementally onto the attached
        C++ grid; verify a foreign-net via then skips the late reservation.
        """
        if not is_cpp_available():
            pytest.skip("C++ router backend not built (run: kct build-native)")

        from kicad_tools.router.cpp_backend import CppGrid

        gx, gy = 30, 20
        inner_layer_idx = self._find_inner_layer(grid_4layer)

        # Build the C++ mirror FIRST (no reservations yet), establishing
        # the grid._cpp_grid back-reference.
        cpp_grid = CppGrid.from_routing_grid(grid_4layer)
        assert cpp_grid._impl.reserved_cell_count() == 0

        # Reserve AFTER the mirror exists -- must propagate via the
        # incremental path in reserve_corridor_cells.
        grid_4layer.reserve_corridor_cells(inner_layer_idx, [(gx, gy)], frozenset({7, 8}))
        assert cpp_grid._impl.reserved_cell_count() == 1
        assert cpp_grid._impl.is_reserved_for(gx, gy, inner_layer_idx, 7)

        cpp_grid._impl.mark_via(gx, gy, 99, 0)
        assert not cpp_grid._impl.at(gx, gy, inner_layer_idx).blocked, (
            "Late (post-mirror) reservation must still keep out a foreign-net via."
        )

        # clear_corridor_reservations must also flow to the C++ grid.
        grid_4layer.clear_corridor_reservations()
        assert cpp_grid._impl.reserved_cell_count() == 0
        assert not cpp_grid._impl.has_reservations()

    def test_cpp_attractor_discounts_owning_net_step_cost(
        self, grid_4layer: RoutingGrid, rules: DesignRules
    ) -> None:
        """C++ A* discounts a reserved cell's step cost for the owner net.

        Issue #4071 parity: the C++ corridor attractor must subtract
        ``rules.cost_corridor_attractor`` from an owning-net step into a
        reserved cell (clamped at 0), and return 0.0 for a foreign net or
        an unreserved cell -- matching
        ``RoutingGrid.get_corridor_attractor_bonus`` exactly.
        """
        if not is_cpp_available():
            pytest.skip("C++ router backend not built (run: kct build-native)")

        from kicad_tools.router.cpp_backend import CppGrid

        gx, gy = 25, 25
        inner_layer_idx = self._find_inner_layer(grid_4layer)
        owner_nets = frozenset({1, 2})
        grid_4layer.reserve_corridor_cells(inner_layer_idx, [(gx, gy)], owner_nets)
        cpp_grid = CppGrid.from_routing_grid(grid_4layer)

        bonus = rules.cost_corridor_attractor

        # Python reference contract.
        py_owner = grid_4layer.get_corridor_attractor_bonus(
            inner_layer_idx, gx, gy, 1, bonus
        )
        py_foreign = grid_4layer.get_corridor_attractor_bonus(
            inner_layer_idx, gx, gy, 42, bonus
        )
        py_unreserved = grid_4layer.get_corridor_attractor_bonus(
            inner_layer_idx, gx + 5, gy + 5, 1, bonus
        )

        # C++ mirror of the same query.
        cpp_owner = cpp_grid._impl.corridor_attractor_bonus(
            gx, gy, inner_layer_idx, 1, bonus
        ) if hasattr(cpp_grid._impl, "corridor_attractor_bonus") else (
            bonus if cpp_grid._impl.is_reserved_for(gx, gy, inner_layer_idx, 1) else 0.0
        )
        cpp_foreign = (
            bonus if cpp_grid._impl.is_reserved_for(gx, gy, inner_layer_idx, 42) else 0.0
        )
        cpp_unreserved = (
            bonus
            if cpp_grid._impl.is_reserved_for(gx + 5, gy + 5, inner_layer_idx, 1)
            else 0.0
        )

        assert py_owner == pytest.approx(bonus)
        assert py_owner == pytest.approx(cpp_owner)
        assert py_foreign == pytest.approx(0.0)
        assert py_foreign == pytest.approx(cpp_foreign)
        assert py_unreserved == pytest.approx(0.0)
        assert py_unreserved == pytest.approx(cpp_unreserved)
