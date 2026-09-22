"""Unknown static overlaps cannot inherit registered pad-halo provenance."""

import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


def _grid():
    return RoutingGrid(10, 10, DesignRules(grid_resolution=0.1, trace_clearance=0.2))


def _pad(net=1):
    return Pad(5, 5, 1, 1, net, str(net))


def test_registered_pad_marks_are_known():
    grid = _grid()
    grid.add_pad(_pad())
    assert (0, 50, 56) in grid._pad_geometry_cells


@pytest.mark.parametrize("before", [False, True])
def test_unknown_keepout_overlap_stays_unknown(before):
    grid = _grid()
    if before:
        grid.add_keepout(5.6, 5, 5.6, 5, [Layer.F_CU])
    grid.add_pad(_pad())
    if not before:
        grid.add_keepout(5.6, 5, 5.6, 5, [Layer.F_CU])
    assert (0, 50, 56) not in grid._pad_geometry_cells
    # A later pad cannot launder the unknown overlap into known geometry.
    grid.add_pad(_pad(2))
    assert (0, 50, 56) not in grid._pad_geometry_cells


def test_overlapping_registered_pads_remain_known():
    grid = _grid()
    grid.add_pad(_pad())
    grid.add_pad(_pad(2))
    assert (0, 50, 56) in grid._pad_geometry_cells


@pytest.mark.parametrize(
    "field,value", [("blocked", True), ("net", 1), ("is_obstacle", True), ("pad_blocked", True)]
)
def test_generic_cell_write_revokes_provenance(field, value):
    grid = _grid()
    grid.add_pad(_pad())
    setattr(grid.cell_at(0, 50, 56), field, value)
    assert (0, 50, 56) not in grid._pad_geometry_cells


def test_region_bound_revokes_already_blocked_pad_cells():
    grid = _grid()
    grid.add_pad(_pad())
    grid.mark_region_bound(0, 0, 5.5, 10)
    assert (0, 50, 56) not in grid._pad_geometry_cells


def test_edge_keepout_revokes_already_blocked_pad_cells():
    grid = _grid()
    grid.add_pad(_pad())
    grid._mark_edge_segment_keepout(5.6, 4, 5.6, 6, 0, [0])
    assert (0, 50, 56) not in grid._pad_geometry_cells


@pytest.mark.parametrize("incremental", [False, True])
def test_native_pad_proof_is_revoked_by_later_keepout(incremental):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    grid = _grid()
    if incremental:
        native = CppGrid.from_routing_grid(grid)
        grid.add_pad(_pad())
    else:
        grid.add_pad(_pad())
        native = CppGrid.from_routing_grid(grid)
    assert native._impl.pad_cell_has_geometry(56, 50, 0)
    grid.add_keepout(5.6, 5, 5.6, 5, [Layer.F_CU])
    assert not native._impl.pad_cell_has_geometry(56, 50, 0)


def test_native_unknown_mark_revokes_pad_proof():
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    grid = _grid()
    grid.add_pad(_pad())
    native = CppGrid.from_routing_grid(grid)
    assert native._impl.pad_cell_has_geometry(56, 50, 0)
    native._impl.mark_blocked(56, 50, 0, 1, True, False)
    assert not native._impl.pad_cell_has_geometry(56, 50, 0)


@pytest.mark.parametrize("mark", ["segment", "via"])
def test_unknown_native_route_overlap_revokes_pad_proof(mark):
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    grid = _grid()
    grid.add_pad(_pad())
    native = CppGrid.from_routing_grid(grid)
    assert native._impl.pad_cell_has_geometry(56, 50, 0)
    if mark == "segment":
        native._impl.mark_segment(56, 50, 57, 50, 0, 2, 1)
    else:
        native._impl.mark_via(56, 50, 2, 1)
    assert not native._impl.pad_cell_has_geometry(56, 50, 0)


def test_native_unknown_mark_before_pad_sync_stays_unknown():
    from kicad_tools.router.cpp_backend import CppGrid, is_cpp_available

    if not is_cpp_available():
        pytest.skip("native build required")
    grid = _grid()
    native = CppGrid.from_routing_grid(grid)
    native._impl.mark_blocked(56, 50, 0, 1, True, False)
    grid.add_pad(_pad())
    assert not native._impl.pad_cell_has_geometry(56, 50, 0)
