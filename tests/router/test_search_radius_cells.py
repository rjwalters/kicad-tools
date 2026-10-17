"""Oracle tests for :func:`kicad_tools.router.search_radius.search_radius_cells`.

Issue #6960 (Epic #5509 Phase 4e slice 2) replaced 21 hand-computed A*
search-radius expressions with one helper, **without changing any radius**.
These tests pin that contract: for each of the three rounding variants the
helper must equal the literal pre-refactor expression (inlined below as the
oracle, in its original ``(a / 2 + c) / res`` form), and the known
cross-variant disagreements are pinned as *current* behaviour.
"""

from __future__ import annotations

import itertools
import math

import pytest

from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.search_radius import search_radius_cells

# ---------------------------------------------------------------------------
# Oracles: the literal pre-refactor expressions (do NOT route these through
# the helper -- they are the independent reference).
# ---------------------------------------------------------------------------


def _oracle_guarded(a: float, c: float, res: float) -> int:
    # pathfinder.py Router sites / find_blocking_nets_relaxed (Issue #864).
    return max(1, math.ceil(round((a / 2 + c) / res, 6)))


def _oracle_ceil(a: float, c: float, res: float) -> int:
    # CppPathfinder._route_impl / CoupledPathfinder.__init__.
    return max(1, math.ceil((a / 2 + c) / res))


def _oracle_nearest(a: float, c: float, res: float) -> int:
    # CppPathfinder.find_blocking_nets.
    return max(1, int((a / 2 + c) / res + 0.5))


def _oracle_pairwise_guarded(half_mm: float, widen: float, res: float) -> int:
    # pathfinder.py pairwise widening (Issue #4431): half_mm precomputed.
    return max(1, math.ceil(round((half_mm + widen) / res, 6)))


_ORACLES = {
    "guarded": _oracle_guarded,
    "ceil": _oracle_ceil,
    "nearest": _oracle_nearest,
}

# Full extents (trace widths / via diameters) 0.1-0.8 mm => half extents
# 0.05-0.4 mm, in 0.025 mm steps; clearances 0.1-0.3 mm.
_EXTENTS = [round(0.1 + 0.05 * i, 3) for i in range(15)] + [0.127, 0.15, 0.2, 0.254, 0.6, 0.7]
_CLEARANCES = [round(0.1 + 0.025 * i, 3) for i in range(9)] + [0.127, 0.15, 0.2]
_WIDENINGS = [round(0.05 * i, 3) for i in range(21)]  # 0.0-1.0 mm
_RESOLUTIONS = [0.025, 0.05, 0.0635, 0.1, 0.127]


@pytest.mark.parametrize("rounding", ["guarded", "ceil", "nearest"])
def test_helper_matches_literal_pre_refactor_expression(rounding: str) -> None:
    oracle = _ORACLES[rounding]
    checked = 0
    for a, c, res in itertools.product(_EXTENTS, _CLEARANCES, _RESOLUTIONS):
        got = search_radius_cells(a / 2, c, res, rounding=rounding)  # type: ignore[arg-type]
        assert got == oracle(a, c, res), (rounding, a, c, res)
        checked += 1
    assert checked == len(_EXTENTS) * len(_CLEARANCES) * len(_RESOLUTIONS)


def test_helper_matches_pairwise_widening_oracle() -> None:
    for a, widen, res in itertools.product(_EXTENTS, _WIDENINGS, _RESOLUTIONS):
        half_mm = a / 2.0
        got = search_radius_cells(half_mm, widen, res, rounding="guarded")
        assert got == _oracle_pairwise_guarded(half_mm, widen, res), (a, widen, res)


def test_variants_actually_disagree_somewhere_in_the_grid() -> None:
    """Guard against a refactor that silently collapsed CEIL/NEAREST to GUARDED."""
    ceil_diff = nearest_diff = 0
    for a, c, res in itertools.product(_EXTENTS, _CLEARANCES, _RESOLUTIONS):
        g = search_radius_cells(a / 2, c, res, rounding="guarded")
        ceil_diff += search_radius_cells(a / 2, c, res, rounding="ceil") != g
        nearest_diff += search_radius_cells(a / 2, c, res, rounding="nearest") != g
    assert ceil_diff > 0
    assert nearest_diff > 0


# ---------------------------------------------------------------------------
# Named tie cases from the Issue #6960 Problem table.
# ---------------------------------------------------------------------------


def test_tie_default_rules_on_0p1mm_grid() -> None:
    # (0.1 + 0.2) / 0.1 == 3.0000000000000004
    assert search_radius_cells(0.1, 0.2, 0.1, rounding="guarded") == 3
    assert search_radius_cells(0.1, 0.2, 0.1, rounding="ceil") == 4
    assert search_radius_cells(0.1, 0.2, 0.1, rounding="nearest") == 3


@pytest.mark.parametrize(("res", "guarded", "ceil"), [(0.05, 6, 7), (0.025, 12, 13)])
def test_tie_default_rules_on_finer_grids(res: float, guarded: int, ceil: int) -> None:
    assert search_radius_cells(0.1, 0.2, res, rounding="guarded") == guarded
    assert search_radius_cells(0.1, 0.2, res, rounding="ceil") == ceil


def test_tie_0p127mm_grid_nearest_rounds_down() -> None:
    # x = 2.362...: nearest gives 2, guarded and ceil give 3.
    assert search_radius_cells(0.1, 0.2, 0.127, rounding="nearest") == 2
    assert search_radius_cells(0.1, 0.2, 0.127, rounding="guarded") == 3
    assert search_radius_cells(0.1, 0.2, 0.127, rounding="ceil") == 3


def test_tie_via_on_0p127mm_grid() -> None:
    # via 0.7 mm / clearance 0.2 mm: x = 4.331...
    assert search_radius_cells(0.7 / 2, 0.2, 0.127, rounding="guarded") == 5
    assert search_radius_cells(0.7 / 2, 0.2, 0.127, rounding="ceil") == 5
    assert search_radius_cells(0.7 / 2, 0.2, 0.127, rounding="nearest") == 4


def test_minimum_is_one_cell() -> None:
    for rounding in ("guarded", "ceil", "nearest"):
        assert search_radius_cells(0.0, 0.0, 0.1, rounding=rounding) == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Current cross-backend disagreement, pinned as CURRENT behaviour.
# ---------------------------------------------------------------------------


def test_router_and_coupled_pathfinder_disagree_at_default_rules() -> None:
    """Python Router uses 3 cells; CoupledPathfinder uses 4 (same grid, same rules).

    This is pinned as CURRENT behaviour only.  Issue #6960 was a
    behaviour-preserving refactor; the Epic #5509 follow-up that unifies the
    rounding variants WILL change these numbers (CoupledPathfinder, and the
    C++ wrapper's search radius, are expected to drop to 3), and this test
    must be updated together with that change.
    """
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    assert grid.resolution == 0.1
    assert Router(grid, rules)._trace_half_width_cells == 3
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=1)
    assert pf._trace_half_width_cells == 4


# ---------------------------------------------------------------------------
# Argument contract.
# ---------------------------------------------------------------------------


def test_unknown_rounding_raises_value_error() -> None:
    with pytest.raises(ValueError, match="rounding"):
        search_radius_cells(0.1, 0.2, 0.1, rounding="floor")  # type: ignore[arg-type]


def test_rounding_is_required_keyword() -> None:
    with pytest.raises(TypeError):
        search_radius_cells(0.1, 0.2, 0.1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        search_radius_cells(0.1, 0.2, 0.1, "guarded")  # type: ignore[misc]
