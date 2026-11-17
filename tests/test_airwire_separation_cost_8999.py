"""Airwire-separation cost term (Issue #8999).

Covers the acceptance criteria of the issue:

* **Plane purity** -- the integer crossing-count plane is a pure function
  of the unrouted set: identical inputs in different rasterisation /
  iteration orders produce bit-identical planes.
* **Cost scaling + Python/C++ parity** -- a cell on ``k`` unrouted desire
  lines costs ``weight * k`` more than an otherwise identical cell, in
  BOTH ``RoutingGrid.get_negotiated_cost`` and ``Grid3D::get_negotiated_cost``.
* **Flag-off inertness** -- with weight ``0.0`` the plane stays all-zero
  and the negotiated cost is exactly ``present + history``.
* **#6098 fallback-memo stamp** -- ``_py_grid_state_stamp`` does not
  change when only the airwire plane changes (``_COST_ONLY_GRID_ARRAYS``).
* **Stale-.so rejection** -- a stale ``router_cpp`` (one version below the
  required build) is rejected by the ``cpp_backend`` import-time guard.
* **Same-seed determinism with the flag on** -- two runs of the same
  board at the same seed produce identical copper geometry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.airwire_cost import (
    compute_airwire_crossing_plane,
    refresh_airwire_cost,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

needs_cpp = pytest.mark.skipif(
    not cpp_backend.is_cpp_available(), reason="C++ router backend not built"
)

DETOUR = Path(__file__).parent / "fixtures" / "route_6103" / "detour.kicad_pcb"


def _grid(width: float = 20.0, height: float = 20.0) -> RoutingGrid:
    return RoutingGrid(width=width, height=height, rules=DesignRules())


def _pad(x: float, y: float, net: int, name: str) -> Pad:
    return Pad(x=x, y=y, width=0.5, height=0.5, net=net, net_name=name)


class _StubRouter:
    """Duck-typed stand-in for the attributes ``refresh_airwire_cost`` reads."""

    def __init__(self, grid: RoutingGrid):
        self.grid = grid
        self.nets: dict[int, list[str]] = {}
        self.pads: dict[str, Pad] = {}
        self.routes: list[Route] = []
        self._cpp_grid = None

    def _is_pour_net(self, net_id: int) -> bool:
        return False


def _stub_with_three_nets(grid: RoutingGrid) -> _StubRouter:
    """Nets 1 (horizontal desire line), 2 (crossing it), 3 (already routed)."""
    router = _StubRouter(grid)
    res = grid.resolution
    gx = lambda i: grid.origin_x + (i + 0.5) * res  # noqa: E731
    gy = lambda j: grid.origin_y + (j + 0.5) * res  # noqa: E731
    router.nets = {1: ["a1", "a2"], 2: ["b1", "b2"], 3: ["c1", "c2"]}
    router.pads = {
        # Net 1: a long horizontal line through row 10.
        "a1": _pad(gx(2), gy(10), 1, "a1"),
        "a2": _pad(gx(17), gy(10), 1, "a2"),
        # Net 2: a vertical line through column 10, crossing net 1.
        "b1": _pad(gx(10), gy(2), 2, "b1"),
        "b2": _pad(gx(10), gy(17), 2, "b2"),
        # Net 3: another horizontal line, but already committed.
        "c1": _pad(gx(2), gy(15), 3, "c1"),
        "c2": _pad(gx(17), gy(15), 3, "c2"),
    }
    seg = Segment(x1=gx(2), y1=gy(15), x2=gx(17), y2=gy(15), width=0.2, layer=Layer.F_CU, net=3)
    router.routes = [Route(net=3, net_name="N3", segments=[seg], vias=[])]
    return router


class TestPlanePurity:
    """(a) The plane is a pure function of the unrouted set."""

    def _skeletons(self, grid: RoutingGrid) -> dict:
        from kicad_tools.router.crossing_order import minimum_spanning_skeleton

        res = grid.resolution
        pts = lambda x, y: (grid.origin_x + x * res, grid.origin_y + y * res)  # noqa: E731
        return {
            1: minimum_spanning_skeleton([pts(2, 10), pts(17, 10), pts(9, 4)]),
            2: minimum_spanning_skeleton([pts(10, 2), pts(10, 17), pts(4, 12)]),
            7: minimum_spanning_skeleton([pts(3, 3), pts(16, 16)]),
        }

    def test_order_of_nets_and_segments_does_not_matter(self):
        grid = _grid()
        skeletons = self._skeletons(grid)
        base = compute_airwire_crossing_plane(grid, skeletons)

        # Different dict insertion order AND reversed segment order per net.
        shuffled = {net: list(reversed(segs)) for net, segs in reversed(list(skeletons.items()))}
        other = compute_airwire_crossing_plane(grid, shuffled)
        np.testing.assert_array_equal(base, other)
        assert base.dtype == np.int16

    def test_crossing_cell_counts_two_distinct_nets_once_each(self):
        grid = _grid()
        skeletons = self._skeletons(grid)
        plane = compute_airwire_crossing_plane(grid, skeletons)
        # The cell where nets 1 and 2 cross counts 2, not 4: a net's own
        # overlapping skeleton coverage counts once.
        assert int(plane.max()) == 2
        # A cell on net 1's line but off the others counts 1.
        gx, gy = grid.world_to_grid(
            grid.origin_x + 5 * grid.resolution, grid.origin_y + 10 * grid.resolution
        )
        assert plane[gy, gx] == 1

    def test_per_net_dedup_of_overlapping_own_segments(self):
        grid = _grid()
        res = grid.resolution
        pts = lambda x, y: (grid.origin_x + x * res, grid.origin_y + y * res)  # noqa: E731
        # A U-shaped 3-pad net whose MST legs overlap on the middle row.
        from kicad_tools.router.crossing_order import minimum_spanning_skeleton

        segs = minimum_spanning_skeleton([pts(2, 8), pts(10, 8), pts(18, 8)])
        plane = compute_airwire_crossing_plane(grid, {5: segs})
        # Every covered cell counts exactly 1 even where two MST legs share
        # the row.
        covered = np.count_nonzero(plane)
        assert covered > 0
        assert set(np.unique(plane)) <= {0, 1}

    def test_zero_length_skeleton_and_single_cell_desire_line(self):
        grid = _grid()
        res = grid.resolution
        pts = lambda x, y: (grid.origin_x + x * res, grid.origin_y + y * res)  # noqa: E731
        from kicad_tools.router.crossing_order import minimum_spanning_skeleton

        # Pads at identical coordinates: zero-length edges are dropped.
        assert minimum_spanning_skeleton([pts(5, 5), pts(5, 5)]) == []
        plane = compute_airwire_crossing_plane(
            grid, {9: minimum_spanning_skeleton([pts(5, 5), pts(5, 5)])}
        )
        assert not plane.any()
        # A degenerate hand-built segment still covers its one cell: the
        # "2-pin net whose desire line is a single cell" edge case.
        plane2 = compute_airwire_crossing_plane(grid, {9: [(pts(5.5, 5.5), pts(5.5, 5.5))]})
        assert plane2.sum() == 1
        plane3 = compute_airwire_crossing_plane(grid, {9: [(pts(5.5, 5.5), pts(6.5, 5.5))]})
        assert plane3.sum() >= 1  # one-cell step covers its cells


class TestRefresh:
    """The refresh entry point against a duck-typed router."""

    def test_refresh_excludes_current_and_routed_nets(self):
        grid = _grid()
        router = _stub_with_three_nets(grid)
        covered = refresh_airwire_cost(router, weight=2.0, current_net=1)
        assert covered > 0
        assert grid._airwire_weight == 2.0
        # Net 1 (current) excluded: row 10 must carry only net 2 if it
        # crossed there -- it does, so the crossing cell counts 1 (net 2).
        res = grid.resolution
        cx, cy = grid.world_to_grid(grid.origin_x + 10 * res, grid.origin_y + 10 * res)
        assert grid._airwire_cost[cy, cx] == 1
        # Net 3 is routed: row 15 carries nothing.
        rx, ry = grid.world_to_grid(grid.origin_x + 8 * res, grid.origin_y + 15 * res)
        assert grid._airwire_cost[ry, rx] == 0

    def test_refresh_with_weight_zero_is_an_inert_noop(self):
        grid = _grid()
        router = _stub_with_three_nets(grid)
        assert refresh_airwire_cost(router, weight=0.0, current_net=1) == 0
        assert not grid._airwire_cost.any()
        assert grid._airwire_weight == 0.0

    def test_refresh_is_reproducible(self):
        grid_a, grid_b = _grid(), _grid()
        refresh_airwire_cost(_stub_with_three_nets(grid_a), weight=1.5, current_net=1)
        refresh_airwire_cost(_stub_with_three_nets(grid_b), weight=1.5, current_net=3)
        # Different current_net legitimately differs on the excluded net's
        # lines; recompute the SAME call twice and require bit-identity.
        refresh_airwire_cost(_stub_with_three_nets(grid_a), weight=1.5, current_net=3)
        np.testing.assert_array_equal(grid_a._airwire_cost, grid_b._airwire_cost)


class TestPythonCost:
    """(b, c) The term inside RoutingGrid.get_negotiated_cost."""

    def test_flag_off_cost_is_exactly_present_plus_history(self):
        grid = _grid()
        grid._usage_count[0, 5, 5] = 2
        grid._history_cost[0, 5, 5] = 1.5
        assert grid.get_negotiated_cost(5, 5, 0, present_cost_factor=1.0) == 3.5
        assert not grid._airwire_cost.any()

    def test_cell_on_k_desire_lines_costs_weight_k_more(self):
        grid = _grid()
        grid._airwire_weight = 2.0
        grid._airwire_cost[6, 4] = 3  # (gy=6, gx=4)
        base = grid.get_negotiated_cost(4, 6, 0, present_cost_factor=1.0)
        clean = grid.get_negotiated_cost(5, 6, 0, present_cost_factor=1.0)
        assert base == clean + 2.0 * 3
        # Layer-independent: the desire-line column applies to every layer.
        assert grid.get_negotiated_cost(4, 6, 1, present_cost_factor=1.0) == base

    def test_weight_zero_is_byte_identical_even_with_nonzero_plane(self):
        grid = _grid()
        grid._usage_count[0, 5, 5] = 1
        grid._history_cost[0, 5, 5] = 0.25
        expected = grid.get_negotiated_cost(5, 5, 0, present_cost_factor=1.0)
        grid._airwire_cost[5, 5] = 4  # weight is still 0.0
        assert grid.get_negotiated_cost(5, 5, 0, present_cost_factor=1.0) == expected


@needs_cpp
class TestCppParity:
    """(b) Python and C++ cell costs match for a hand-built plane."""

    def _cpp_grid(self, router_cpp):
        return router_cpp.Grid3D(40, 40, 2, 0.5)

    def test_cpp_cost_scales_and_matches_python(self):
        from kicad_tools.router import router_cpp

        py = _grid()
        py._airwire_weight = 2.0
        py._airwire_cost[6, 4] = 3
        py._usage_count[0, 6, 4] = 1

        cpp = self._cpp_grid(router_cpp)
        cell = cpp.at(4, 6, 0)
        cell.usage_count = 1
        cpp.set_airwire_cells([4], [6], [2.0 * 3])

        py_cost = py.get_negotiated_cost(4, 6, 0, present_cost_factor=1.0)
        cpp_cost = cpp.get_negotiated_cost(4, 6, 0, 1.0, 0)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6)
        # Same cells WITHOUT the plane agree at the base value.
        py._airwire_cost[6, 4] = 0
        cpp.clear_airwire_costs()
        assert py.get_negotiated_cost(4, 6, 0, present_cost_factor=1.0) == pytest.approx(
            cpp.get_negotiated_cost(4, 6, 0, 1.0, 0), abs=1e-6
        )

    def test_column_applies_to_every_layer_and_clear_resets(self):
        from kicad_tools.router import router_cpp

        cpp = self._cpp_grid(router_cpp)
        cpp.set_airwire_cells([4], [6], [5.0])
        assert cpp.at(4, 6, 0).airwire_cost == 5.0
        assert cpp.at(4, 6, 1).airwire_cost == 5.0
        assert cpp.at(5, 6, 0).airwire_cost == 0.0
        cpp.clear_airwire_costs()
        assert cpp.at(4, 6, 1).airwire_cost == 0.0

    def test_out_of_bounds_sparse_cells_are_skipped(self):
        from kicad_tools.router import router_cpp

        cpp = self._cpp_grid(router_cpp)
        cpp.set_airwire_cells([4, -3, 999], [6, 2, 1], [5.0, 1.0, 1.0])
        assert cpp.at(4, 6, 0).airwire_cost == 5.0


@needs_cpp
class TestFallbackMemoStamp:
    """(d) The #6098 memo stamp ignores the airwire plane."""

    def test_stamp_unchanged_when_only_airwire_changes(self):
        router, _ = load_pcb_for_routing(
            str(DETOUR), rules=DesignRules(), use_pcb_rules=False, validate_drc=False
        )
        pf = router.router
        grid = router.grid
        before = pf._py_grid_state_stamp(grid)
        grid._airwire_cost[...] = 3
        grid._airwire_weight = 2.0
        assert pf._py_grid_state_stamp(grid) == before
        # Control: a passability-relevant plane DOES change the stamp.
        grid._usage_count[0, 0, 0] = 1
        assert pf._py_grid_state_stamp(grid) != before


class TestStaleSoRejected:
    """(e) A stale .so (any version below the required one) is rejected by the import-time guard."""

    def test_required_version_is_68(self):
        from kicad_tools.router import router_cpp

        assert cpp_backend._REQUIRED_CPP_BUILD_VERSION == 68
        assert router_cpp.BUILD_VERSION == 68

    def test_stale_v66_module_disables_backend(self):
        script = (
            "import sys, types\n"
            "fake = types.ModuleType('kicad_tools.router.router_cpp')\n"
            "fake.BUILD_VERSION = 66\n"
            "fake.__file__ = '/nonexistent/router_cpp.so'\n"
            "sys.modules['kicad_tools.router.router_cpp'] = fake\n"
            "from kicad_tools.router import cpp_backend\n"
            "assert not cpp_backend._CPP_AVAILABLE\n"
            "assert 'stale' in cpp_backend._CPP_IMPORT_ERROR\n"
            "print('STALE-REJECTED OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        assert "STALE-REJECTED OK" in proc.stdout


class TestDeterminismSameSeed:
    """Two same-seed runs with the flag on produce identical copper geometry."""

    @staticmethod
    def _copper_multiset(router) -> list:
        rows = []
        for route in router.routes:
            for seg in route.segments:
                rows.append(
                    (
                        route.net,
                        round(seg.x1, 6),
                        round(seg.y1, 6),
                        round(seg.x2, 6),
                        round(seg.y2, 6),
                        getattr(seg.layer, "value", seg.layer),
                    )
                )
            for via in route.vias:
                rows.append((route.net, round(via.x, 6), round(via.y, 6)))
        return sorted(rows)

    @staticmethod
    def _routed(seed: int, weight: float):
        router, _ = load_pcb_for_routing(
            str(DETOUR), rules=DesignRules(), use_pcb_rules=False, validate_drc=False
        )
        router.route_all_negotiated(
            max_iterations=3, seed=seed, airwire_weight=weight, timeout=300.0
        )
        return router

    def test_same_seed_flag_on_identical_copper(self):
        a = self._routed(42, 1.0)
        b = self._routed(42, 1.0)
        assert a.grid._airwire_weight == 1.0
        assert self._copper_multiset(a) == self._copper_multiset(b)

    def test_flag_off_matches_absent_kwarg(self):
        a = self._routed(42, 0.0)
        router, _ = load_pcb_for_routing(
            str(DETOUR), rules=DesignRules(), use_pcb_rules=False, validate_drc=False
        )
        router.route_all_negotiated(max_iterations=3, seed=42, timeout=300.0)
        assert self._copper_multiset(a) == self._copper_multiset(router)
        assert not router.grid._airwire_cost.any()
