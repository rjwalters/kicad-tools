"""Via search preserves physical drill spacing, including same-net PTH pads."""

import math

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def context():
    rules = DesignRules(grid_resolution=0.1, via_drill=0.3, min_hole_to_hole=0.5)
    grid = RoutingGrid(20, 20, rules, origin_x=160, origin_y=110)
    pad = Pad(169.04, 122.5, 2, 2, 8, "VCC", through_hole=True, drill=1.0, ref="J2", pin="2")
    return grid, rules, pad


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("backend", ["python", "cpp"])
def test_board02_same_net_hole_blocks_via_but_not_same_net_copper(backend, sharing):
    grid, rules, pad = context()
    if backend == "cpp" and not is_cpp_available():
        pytest.skip("native router required")
    native = CppGrid.from_routing_grid(grid) if backend == "cpp" else None
    # Exercise incremental synchronization after the native snapshot exists.
    grid.add_pad(pad)
    router = CppPathfinder(native, rules) if native else Router(grid, rules)
    gx, gy = grid.world_to_grid(169.7, 121.6)
    gap = math.hypot(169.7 - 169.04, 121.6 - 122.5) - (0.3 + 1.0) / 2
    assert gap == pytest.approx(0.4660645, abs=1e-6)
    if native:
        assert router._impl.is_via_blocked(gx, gy, 8, sharing, 4)
        assert not router._impl.is_trace_blocked(gx, gy, 0, 8, sharing, 2)
        assert native._impl.component_holes_clear(169.8, 121.6, 0.3, 0.5)
    else:
        assert router._is_via_blocked(gx, gy, 0, 8, sharing, radius=4)
        assert not router._is_trace_blocked(gx, gy, 0, 8, sharing, radius=2)
        assert grid._component_hole_index.clear(169.8, 121.6, 0.3, 0.5)


def test_python_new_hole_invalidates_prior_positive_via_answer():
    grid, rules, pad = context()
    router = Router(grid, rules)
    gx, gy = grid.world_to_grid(169.7, 121.6)
    assert router._check_via_placement_cached(gx, gy, 8)
    grid.add_component_hole(pad)
    assert not router._check_via_placement_cached(gx, gy, 8)


@pytest.mark.parametrize("angle", [0, 90, 45])
def test_rotated_slot_physical_hole_crosses_index_bins(angle):
    grid, rules, pad = context()
    pad.x, pad.y = 166, 116
    pad.drill_size, pad.drill_rotation = (4, 1), angle
    grid.add_component_hole(pad)  # No copper obstacle installed.
    a = math.radians(angle)
    x, y = pad.x + 2.6 * math.cos(a), pad.y - 2.6 * math.sin(a)
    assert not grid._component_hole_index.clear(x, y, 0.3, 0.5)
    if is_cpp_available():
        native = CppGrid.from_routing_grid(grid)
        assert not native._impl.component_holes_clear(x, y, 0.3, 0.5)


@pytest.mark.parametrize("method", ["route", "route_resumable"])
def test_native_search_checks_emitted_drill_override(method):
    if not is_cpp_available():
        pytest.skip("native router required")
    grid, rules, pad = context()
    grid.add_component_hole(pad)
    native = CppGrid.from_routing_grid(grid)
    router = CppPathfinder(native, rules)
    result = getattr(router._impl, method)(
        169.8,
        121.6,
        0,
        169.8,
        121.6,
        1,
        8,
        emit_via_diameter=1.0,
        emit_via_drill=0.6,
        max_search_iterations=2000,
    )
    assert result.success
    assert result.vias
    for via in result.vias:
        assert via.drill == pytest.approx(0.6)
        assert math.hypot(via.x - pad.x, via.y - pad.y) - (via.drill + pad.drill) / 2 >= 0.5 - 1e-4


@pytest.mark.parametrize("backend", ["python", "cpp"])
def test_hole_index_refreshes_moved_pad_and_deduplicates(backend):
    if backend == "cpp" and not is_cpp_available():
        pytest.skip("native router required")
    grid, rules, pad = context()
    from dataclasses import replace

    # Loader census and routed copper pads are distinct objects.
    grid.add_component_hole(replace(pad))
    grid.add_pad(pad)
    assert len(grid._component_hole_index.holes) == 1
    native = CppGrid.from_routing_grid(grid) if backend == "cpp" else None
    router = CppPathfinder(native, rules) if native else Router(grid, rules)
    pad.x += 5
    router.invalidate_pad_geometry_cache()
    assert grid._component_hole_index.clear(169.7, 121.6, 0.3, 0.5)
    assert not grid._component_hole_index.clear(174.7, 121.6, 0.3, 0.5)
    if native:
        assert native._impl.component_holes_clear(169.7, 121.6, 0.3, 0.5)
        assert not native._impl.component_holes_clear(174.7, 121.6, 0.3, 0.5)


@pytest.mark.parametrize("known", [True, False])
def test_loader_keeps_unconnected_npth_hole_outside_routing_pads(tmp_path, monkeypatch, known):
    from kicad_tools.router.io import load_pcb_for_routing

    if not known:
        monkeypatch.setattr(
            "kicad_tools.router.via_in_pad_eligibility.component_holes_from_document",
            lambda *args, **kwargs: None,
        )
    pcb = tmp_path / "holes.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "N")
      (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default))
               (fill none) (layer "Edge.Cuts"))
      (footprint "Connector" (layer "F.Cu") (at 10 10)
        (property "Reference" "J1")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N"))
        (pad "2" smd rect (at 5 0) (size 1 1) (layers "F.Cu") (net 1 "N"))
        (pad "" np_thru_hole circle (at 2 2) (size 1 1) (drill 1)
             (layers "*.Cu" "*.Mask"))))""")
    router, _ = load_pcb_for_routing(
        str(pcb),
        rules=DesignRules(grid_resolution=0.1),
        use_pcb_rules=False,
        validate_drc=False,
        force_python=True,
    )
    assert not any(p.through_hole for p in router.grid._pads)
    assert not router.grid._component_hole_index.clear(12.7, 12, 0.3, 0.5)
    assert router.grid._component_hole_index.clear(17, 17, 0.3, 0.5) == known
    if is_cpp_available():
        native = CppGrid.from_routing_grid(router.grid)
        assert not native._impl.component_holes_clear(12.7, 12, 0.3, 0.5)
        assert native._impl.component_holes_clear(17, 17, 0.3, 0.5) == known


def test_duplicate_reference_pin_never_moves_another_footprints_hole():
    from dataclasses import replace

    grid, _, first = context()
    first.x, first.y, first.ref, first.pin = 165, 115, "J1", "1"
    second = replace(first, x=175, net=9)
    for pad in (first, second):
        grid.add_component_hole(replace(pad))
        grid.add_pad(pad)
    native = CppGrid.from_routing_grid(grid) if is_cpp_available() else None
    first.x += 2
    grid.refresh_component_holes()
    for x in (165, 167, 175):
        assert not grid._component_hole_index.clear(x, 115, 0.3, 0.5)
        if native:
            assert not native._impl.component_holes_clear(x, 115, 0.3, 0.5)
