"""Rip-up must remove current occupancy cost without changing history."""

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="native router required")


@pytest.fixture
def grid():
    from kicad_tools.router import router_cpp

    return router_cpp.Grid3D(24, 24, 2, 0.1, 0, 0)


@pytest.mark.parametrize("kind", ["segment", "via"])
def test_repeated_ripup_restores_cost_and_preserves_history(grid, kind):
    args = (3, 3, 5, 3, 0, 7, 1) if kind == "segment" else (4, 3, 7, 1)
    mark = getattr(grid, f"mark_{kind}")
    unmark = getattr(grid, f"unmark_{kind}")
    grid.at(4, 3, 0).history_cost = 2.5
    grid.at(4, 3, 0).usage_count = 3
    for _ in range(4):
        mark(*args)
        expected = 15 / 64 if kind == "segment" else 9 / 64
        assert grid.get_congestion(4, 3, 0) == expected
        unmark(*args)
        assert grid.get_congestion(4, 3, 0) == 0
        assert grid.count_blocked() == 0
        assert grid.at(4, 3, 0).history_cost == 2.5
        assert grid.at(4, 3, 0).usage_count == 3


def test_overlap_nonowner_and_static_cells(grid):
    grid.mark_blocked(3, 3, 0, 7, False, True)
    grid.mark_segment(2, 3, 5, 3, 0, 7, 0)
    grid.mark_segment(2, 3, 5, 3, 0, 8, 0)
    assert grid.get_congestion(3, 3, 0) == 3 / 64
    grid.unmark_segment(2, 3, 5, 3, 0, 8, 0)
    assert grid.get_congestion(3, 3, 0) == 3 / 64
    grid.unmark_segment(2, 3, 5, 3, 0, 7, 0)
    grid.unmark_segment(2, 3, 5, 3, 0, 7, 0)
    assert grid.get_congestion(3, 3, 0) == 0
    assert grid.count_blocked() == 1
    assert grid.at(3, 3, 0).net == 7


def test_uncounted_import_does_not_decrement(grid):
    grid.at(3, 3, 0).blocked = True
    grid.at(3, 3, 0).net = 7
    grid.unmark_segment(3, 3, 3, 3, 0, 7, 0)
    assert grid.get_congestion(3, 3, 0) == 0


def test_static_conversion_removes_route_contribution(grid):
    grid.mark_segment(3, 3, 3, 3, 0, 7, 0)
    assert grid.get_congestion(3, 3, 0) == 1 / 64
    grid.mark_blocked(3, 3, 0, 99, True, False)
    assert grid.get_congestion(3, 3, 0) == 0
    grid.unmark_segment(3, 3, 3, 3, 0, 7, 0)
    assert grid.at(3, 3, 0).blocked
    assert grid.at(3, 3, 0).net == 99
