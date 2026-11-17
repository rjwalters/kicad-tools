"""Oracle and parity tests for :func:`kicad_tools.router.search_radius.search_radius_cells`.

Issue #6960 (Epic #5509 Phase 4e slice 2) put the 21 A* search-radius sites
behind one helper with three named rounding rules.  Issue #6994 (slice 4)
collapsed them onto the single guarded rule (Issue #864): the exact ceiling
of ``(half + clearance) / resolution``.  These tests pin

* the helper against the literal guarded expression (an independent oracle);
* the unified tie values (several of which changed in #6994);
* Router / CoupledPathfinder agreement at ``DesignRules()``;
* Python / C++ parity with ``router_cpp.search_radius_cells``, including
  float32-quantized inputs as the native A* sees them.
"""

from __future__ import annotations

import inspect
import itertools
import math
import struct

import pytest

from kicad_tools.router.diffpair_routing import CoupledPathfinder
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.search_radius import search_radius_cells

# ---------------------------------------------------------------------------
# Oracle: the literal guarded expression (do NOT route this through the
# helper -- it is the independent reference).
# ---------------------------------------------------------------------------


def _oracle(a: float, c: float, res: float) -> int:
    return max(1, math.ceil(round((a / 2 + c) / res, 6)))


def _oracle_pairwise(half_mm: float, widen: float, res: float) -> int:
    # pathfinder.py pairwise widening (Issue #4431): half_mm precomputed.
    return max(1, math.ceil(round((half_mm + widen) / res, 6)))


# Full extents (trace widths / via diameters) 0.1-0.8 mm => half extents
# 0.05-0.4 mm, in 0.025 mm steps; clearances 0.1-0.3 mm.
_EXTENTS = [round(0.1 + 0.05 * i, 3) for i in range(15)] + [0.127, 0.15, 0.2, 0.254, 0.6, 0.7]
_CLEARANCES = [round(0.1 + 0.025 * i, 3) for i in range(9)] + [0.127, 0.15, 0.2]
_WIDENINGS = [round(0.05 * i, 3) for i in range(21)]  # 0.0-1.0 mm
_RESOLUTIONS = [0.025, 0.05, 0.0635, 0.1, 0.127]


def test_helper_matches_literal_guarded_expression() -> None:
    checked = 0
    for a, c, res in itertools.product(_EXTENTS, _CLEARANCES, _RESOLUTIONS):
        assert search_radius_cells(a / 2, c, res) == _oracle(a, c, res), (a, c, res)
        checked += 1
    assert checked == len(_EXTENTS) * len(_CLEARANCES) * len(_RESOLUTIONS)


def test_helper_matches_pairwise_widening_oracle() -> None:
    for a, widen, res in itertools.product(_EXTENTS, _WIDENINGS, _RESOLUTIONS):
        half_mm = a / 2.0
        assert search_radius_cells(half_mm, widen, res) == _oracle_pairwise(half_mm, widen, res), (
            a,
            widen,
            res,
        )


def test_rounding_parameter_is_gone() -> None:
    """Issue #6994: one rule left, so no ``rounding`` argument."""
    params = list(inspect.signature(search_radius_cells).parameters)
    assert params == ["half_extent_mm", "clearance_mm", "resolution"]


# ---------------------------------------------------------------------------
# Tie cases from the Issue #6960 / #6994 Problem tables, at unified values.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("half", "clearance", "res", "expected"),
    [
        # (0.1 + 0.2) / 0.1 == 3.0000000000000004: bare ceil gave 4.
        (0.1, 0.2, 0.1, 3),
        (0.1, 0.2, 0.05, 6),  # bare ceil gave 7
        (0.1, 0.2, 0.025, 12),  # bare ceil gave 13
        # x = 2.362: round-to-nearest (old rip-up scan) gave 2.
        (0.1, 0.2, 0.127, 3),
        # via 0.7 / 0.2 @ 0.127: x = 4.331, nearest gave 4.
        (0.35, 0.2, 0.127, 5),
    ],
)
def test_unified_tie_values(half: float, clearance: float, res: float, expected: int) -> None:
    assert search_radius_cells(half, clearance, res) == expected


def test_minimum_is_one_cell() -> None:
    assert search_radius_cells(0.0, 0.0, 0.1) == 1


def test_router_and_coupled_pathfinder_agree_at_default_rules() -> None:
    """Issue #6994: both use 3 cells at ``DesignRules()`` on a 0.1 mm grid.

    Before #6994 ``CoupledPathfinder`` used a bare ceil and searched 4 cells.
    """
    rules = DesignRules()
    grid = RoutingGrid(width=12.7, height=12.7, rules=rules)
    assert grid.resolution == 0.1
    router_r = Router(grid, rules)._trace_half_width_cells
    pf = CoupledPathfinder(grid=grid, rules=rules, target_spacing_cells=1)
    assert router_r == pf._trace_half_width_cells == 3


# ---------------------------------------------------------------------------
# Python / C++ parity (router_cpp.search_radius_cells, types.hpp).
# ---------------------------------------------------------------------------


def _router_cpp():
    from kicad_tools.router.cpp_backend import is_cpp_available

    if not is_cpp_available():
        pytest.skip("C++ router backend not built")
    from kicad_tools.router import router_cpp  # type: ignore[attr-defined]

    return router_cpp


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


# Half extents 0.05-1.0 mm in 0.005 mm steps, plus common odd values;
# clearances 0.1-0.3 mm in 0.005 mm steps plus 0.127 / 0.1524 / 0.254.
_PARITY_HALVES = sorted(
    {round(0.05 + 0.005 * i, 4) for i in range(191)} | {0.0635, 0.127, 0.1524, 0.225, 0.275}
)
_PARITY_CLEARANCES = sorted(
    {round(0.1 + 0.005 * i, 4) for i in range(41)} | {0.127, 0.1524, 0.254, 0.13, 0.17}
)


def test_cpp_helper_matches_python_on_double_inputs() -> None:
    cpp = _router_cpp()
    checked = 0
    for h, c, res in itertools.product(_PARITY_HALVES, _PARITY_CLEARANCES, _RESOLUTIONS):
        assert cpp.search_radius_cells(h, c, res) == search_radius_cells(h, c, res), (h, c, res)
        checked += 1
    assert checked >= 330 + 150


def test_cpp_helper_matches_python_on_float32_inputs() -> None:
    """The native A* holds rules and resolution as float32 (~1e-7 noise).

    Feed the helper the float32-rounded values the C++ call sites actually
    pass (``rules.trace_width / 2`` computed in float32) and require the
    exact Python answer on the real-valued input.
    """
    cpp = _router_cpp()
    for h, c, res in itertools.product(_PARITY_HALVES, _PARITY_CLEARANCES, _RESOLUTIONS):
        h32 = _f32(_f32(2 * h) / 2)
        got = cpp.search_radius_cells(h32, _f32(c), _f32(res))
        assert got == search_radius_cells(h, c, res), (h, c, res)


@pytest.mark.parametrize(
    ("full", "clearance", "res", "expected"),
    [
        (0.8, 0.3, 0.025, 28),  # trace: float32 x = 28.0000x
        (0.6, 0.2, 0.1, 5),  # via: float32 x = 5.0000000745 (old C++ gave 6)
        (0.3, 0.2, 0.05, 7),  # trace: old C++ float32 ceil gave 8
        (0.2, 0.2, 0.1, 3),  # DesignRules() trace @ 0.1 mm
    ],
)
def test_cpp_helper_float32_noise_cases(
    full: float, clearance: float, res: float, expected: int
) -> None:
    cpp = _router_cpp()
    assert search_radius_cells(full / 2, clearance, res) == expected
    assert cpp.search_radius_cells(full / 2, clearance, res) == expected
    assert cpp.search_radius_cells(_f32(full) / 2, _f32(clearance), _f32(res)) == expected
