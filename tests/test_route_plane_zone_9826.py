"""Plane-zone marks survive rebuilt C++ grids and trial resets (Issue #9826).

Follow-up to #9597.  Fixture: a 4-layer stack
(``LayerStack.four_layer_sig_gnd_pwr_sig``, ``In2.Cu`` is ``PLANE``) with a
``+3V3`` *island* zone in the middle of ``In2.Cu`` and two ``SIG`` pads on
``In2.Cu`` either side of it.  ``SIG`` may only use ``In2.Cu``, and there is
room to go around the island, so a router that sees the zone cost takes the
around-path and one that does not crosses it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, create_hybrid_router
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.layer_advisories import grid_plane_layer_indices, plane_zone_crossings
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules, NetClassRouting

pytestmark = pytest.mark.requires_router_cpp

_STACK = LayerStack.four_layer_sig_gnd_pwr_sig()


def _pad(number: str, x: float, y: float, net: int, net_name: str, layer: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 1 1) '
        f'(layers "{layer}") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: str, pads: str) -> str:
    return f"""  (footprint "Test:P" (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid}")
    (at 0 0)
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (attr smd)
    {pads}
  )
"""


def _board_text(pad_layer: str = "In2.Cu") -> str:
    sig = _fp("S1", "b1", _pad("1", 6.0, 10.0, 2, "SIG", pad_layer)) + _fp(
        "S2", "b2", _pad("1", 34.0, 10.0, 2, "SIG", pad_layer)
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "SIG")
  (gr_rect (start 0 0) (end 40 20)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{sig}
  (zone (net 1) (net_name "+3V3") (layer "In2.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 16 7) (xy 24 7) (xy 24 13) (xy 16 13)))
  )
)
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "plane_island.kicad_pcb"
    pcb.write_text(_board_text())
    return pcb


def _load(pcb: Path, *, force_python: bool = False, avoid=(0, 1, 3)):
    rules = DesignRules(cost_via=100000.0)
    rules.strict_layers = True
    router, _ = load_pcb_for_routing(
        str(pcb),
        strategy="grid",
        rules=rules,
        layer_stack=_STACK,
        skip_nets=["+3V3"],
        force_python=force_python,
    )
    router.rules.strict_layers = True
    router.net_class_map["SIG"] = NetClassRouting(name="SIG", avoid_layers=list(avoid))
    return router


def _sig_id(router) -> int:
    return next(n for n, nm in router.net_names.items() if nm == "SIG")


def _sig_pads(router):
    sig = _sig_id(router)
    pads = [p for p in (router.all_pads or router.pads.values()) if p.net == sig]
    assert len(pads) == 2
    return sorted(pads, key=lambda p: p.x)


def _layer(router, name: str) -> int:
    return router.grid.layer_to_index(Layer.from_kicad_name(name).value)


def _route_sig(router, pathfinder):
    start, end = _sig_pads(router)
    return pathfinder.route(start, end, net_class=router.net_class_map["SIG"])


def _crossings(router, route) -> list:
    assert route is not None, "SIG must route"
    return plane_zone_crossings([route], router.plane_zones)


def _cpp_zone_state(cpp_grid: CppGrid, layer: int, cols: int, rows: int):
    impl = cpp_grid._impl
    is_zone = np.zeros((rows, cols), dtype=bool)
    net = np.zeros((rows, cols), dtype=np.int64)
    for y in range(rows):
        for x in range(cols):
            cell = impl.at(x, y, layer)
            is_zone[y, x] = cell.is_zone
            net[y, x] = cell.net
    return is_zone, net


class TestFreshCppGridCopiesZoneMarks:
    def test_from_routing_grid_copies_marks_and_plane_mask(self, board: Path) -> None:
        router = _load(board)
        grid = router.grid
        in2 = _layer(router, "In2.Cu")
        assert bool(np.any(grid._is_zone[in2])), "fixture zone not marked"

        cpp_grid = CppGrid.from_routing_grid(grid)
        is_zone, net = _cpp_zone_state(cpp_grid, in2, grid.cols, grid.rows)
        np.testing.assert_array_equal(is_zone, grid._is_zone[in2])
        np.testing.assert_array_equal(net[is_zone], grid._net[in2][is_zone])

        planes = set(grid_plane_layer_indices(grid))
        assert in2 in planes
        for layer in range(grid.num_layers):
            assert cpp_grid._impl.is_plane_layer(layer) is (layer in planes)

    def test_fresh_cpp_router_takes_around_path(self, board: Path) -> None:
        # The relaxed-clearance retry builds exactly this: a new C++ router
        # over the already-marked ``self.grid``.
        router = _load(board)
        fresh = create_hybrid_router(router.grid, router.rules, net_class_map=router.net_class_map)
        assert isinstance(fresh, CppPathfinder)
        assert _crossings(router, _route_sig(router, fresh)) == []

    def test_fresh_cpp_router_crosses_at_zero_cost(self, board: Path) -> None:
        # Control: with the cost switched off the straight line wins, so the
        # around-path above really is the zone cost at work.
        router = _load(board)
        router.rules.cost_zone_foreign_plane = 0.0
        fresh = create_hybrid_router(router.grid, router.rules, net_class_map=router.net_class_map)
        assert isinstance(fresh, CppPathfinder)
        assert len(_crossings(router, _route_sig(router, fresh))) == 1

    def test_python_takes_around_path_too(self, board: Path) -> None:
        router = _load(board, force_python=True)
        assert _crossings(router, _route_sig(router, router.router)) == []

    def test_fine_grid_pass_marks_plane_zones(self, board: Path, monkeypatch) -> None:
        # The fine-grid pass builds a brand-new RoutingGrid; capture it at
        # router construction and check it carries the zone (Python and C++).
        router = _load(board)
        import kicad_tools.router.core as core_mod

        seen: list = []
        real = core_mod.create_hybrid_router

        def spy(grid, *a, **k):
            r = real(grid, *a, **k)
            seen.append((grid, r))
            return r

        monkeypatch.setattr(core_mod, "create_hybrid_router", spy)
        sig = _sig_id(router)
        from types import SimpleNamespace

        # Force the fine-grid pass: the coarse pass "fails" SIG.
        def coarse(*_a, **_k):
            router.routing_failures = [SimpleNamespace(net=sig)]
            return []

        monkeypatch.setattr(router, "route_all", coarse)
        router.route_all_multi_resolution(use_negotiated=False, timeout=60.0)
        assert seen, "fine-grid pass built no router"
        fine_grid, fine_router = seen[0]
        assert fine_grid is not router.grid
        in2 = fine_grid.layer_to_index(Layer.from_kicad_name("In2.Cu").value)
        assert bool(np.any(fine_grid._is_zone[in2]))
        assert isinstance(fine_router, CppPathfinder)
        is_zone, _ = _cpp_zone_state(fine_router._grid, in2, fine_grid.cols, fine_grid.rows)
        np.testing.assert_array_equal(is_zone, fine_grid._is_zone[in2])


class TestTrialReset:
    @pytest.mark.parametrize("force_python", [False, True], ids=["cpp", "py"])
    def test_reset_keeps_zone_cost_over_two_trials(self, board: Path, force_python: bool) -> None:
        router = _load(board, force_python=force_python)
        in2 = _layer(router, "In2.Cu")
        marked = int(np.count_nonzero(router.grid._is_zone[in2]))
        assert marked > 0
        for _trial in range(2):
            router._reset_for_new_trial()
            assert int(np.count_nonzero(router.grid._is_zone[in2])) == marked
            if not force_python:
                assert isinstance(router.router, CppPathfinder)
                cpp_grid = router.router._grid
                is_zone, _ = _cpp_zone_state(cpp_grid, in2, router.grid.cols, router.grid.rows)
                np.testing.assert_array_equal(is_zone, router.grid._is_zone[in2])
            assert _crossings(router, _route_sig(router, router.router)) == []


class TestLayerScopedClearParity:
    def test_clear_one_layer_matches_python(self, board: Path) -> None:
        router = _load(board)
        grid = router.grid
        cpp_grid = CppGrid.from_routing_grid(grid)
        in1 = _layer(router, "In1.Cu")
        in2 = _layer(router, "In2.Cu")
        from types import SimpleNamespace

        # A second zone on In1.Cu so a layer-scoped clear has something to keep.
        grid.add_zone_cells(
            SimpleNamespace(uuid="z-in1", net_number=1),
            {(x, y) for x in range(5, 10) for y in range(5, 10)},
            in1,
        )
        before_in1, _ = _cpp_zone_state(cpp_grid, in1, grid.cols, grid.rows)
        assert before_in1.sum() == 25
        former = grid._is_zone[in2].copy()

        grid.clear_zones(layer_index=in2)

        for layer in (in1, in2):
            is_zone, net = _cpp_zone_state(cpp_grid, layer, grid.cols, grid.rows)
            np.testing.assert_array_equal(is_zone, grid._is_zone[layer])
            np.testing.assert_array_equal(net, grid._net[layer])
        assert not bool(np.any(grid._is_zone[in2]))
        assert int(grid._is_zone[in1].sum()) == 25
        # Unblocked former zone cells drop the zone's net on both backends.
        free = former & ~grid._blocked[in2] & ~grid._is_obstacle[in2]
        assert bool(np.any(free))
        assert not bool(np.any(grid._net[in2][free]))

        grid.clear_zones()
        is_zone, net = _cpp_zone_state(cpp_grid, in1, grid.cols, grid.rows)
        assert not bool(np.any(is_zone))
        np.testing.assert_array_equal(net, grid._net[in1])


class TestNonPlaneZoneParity:
    """Item 2: a foreign zone on a non-PLANE layer blocks on both backends."""

    @pytest.mark.parametrize("force_python", [False, True], ids=["cpp", "py"])
    def test_full_width_signal_layer_zone_blocks(self, tmp_path: Path, force_python: bool) -> None:
        pcb = tmp_path / "signal_zone.kicad_pcb"
        pcb.write_text(_board_text(pad_layer="F.Cu"))
        router = _load(pcb, force_python=force_python, avoid=(1, 2, 3))
        grid = router.grid
        fcu = _layer(router, "F.Cu")
        assert fcu not in set(grid_plane_layer_indices(grid))
        from types import SimpleNamespace

        # A full-height foreign zone stripe on F.Cu (a signal layer).
        x0, _ = grid.world_to_grid(18.0, 0.0)
        x1, _ = grid.world_to_grid(22.0, 0.0)
        cells = {
            (x, y)
            for x in range(x0, x1 + 1)
            for y in range(grid.rows)
            if not grid._blocked[fcu, y, x]
        }
        grid.add_zone_cells(SimpleNamespace(uuid="z-fcu", net_number=1), cells, fcu)
        if not force_python:
            assert isinstance(router.router, CppPathfinder)
        # Python blocks a foreign non-plane zone; C++ must too (it would
        # have charged ``cost_zone_foreign_plane`` and crossed before #9826).
        assert _route_sig(router, router.router) is None
