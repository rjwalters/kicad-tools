"""Oracle tests for ``routed_halo_radius_cells`` (Issue #5858).

The helper replaced 17 hand-derived routed-copper halo radii in
``router/grid.py`` and ``router/core.py``.  The oracles below are the
literal pre-refactor formulas, copied from those call sites, so any drift in
float order, rounding or margin fails here.
"""

from __future__ import annotations

import itertools
import math

import pytest

from kicad_tools.router.grid import routed_halo_radius_cells
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment, Via
from kicad_tools.router.rules import DesignRules

SEG_WIDTHS = (0.1, 0.127, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
VIA_DIAMETERS = (0.4, 0.45, 0.5, 0.6, 0.7, 0.8)
CLEARANCES = (0.1, 0.127, 0.15, 0.2, 0.25)
TRACE_WIDTHS = (0.127, 0.2, 0.25)
RESOLUTIONS = (0.025, 0.05, 0.1, 0.127)
MAX_TRACE_WIDTHS = (None, 0.3)


def _rules(clearance: float, trace_width: float) -> DesignRules:
    return DesignRules(
        trace_width=trace_width,
        trace_clearance=clearance,
        via_clearance=clearance,
    )


def _seg(width: float) -> Segment:
    return Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=width, layer=Layer.F_CU, net=1)


def _via(diameter: float) -> Via:
    return Via(x=0.0, y=0.0, drill=diameter / 2, diameter=diameter, layers=(Layer.F_CU, Layer.B_CU))


# --- literal pre-#5858 formulas (the behaviour contract) -------------------


def _oracle_py(seg, rules, resolution):
    # grid.py mark_route / unmark_route / resync_route_occupancy
    total_clearance = seg.width / 2 + rules.trace_clearance
    clearance_cells = int(total_clearance / resolution) + 1
    clearance_cells += 1
    return clearance_cells


def _oracle_py_inline(seg, rules, resolution):
    # grid.py _segment_halo_reach_mm / _restamp_surviving_halos
    return int((seg.width / 2 + rules.trace_clearance) / resolution) + 2


def _oracle_cpp(seg, rules, resolution):
    # core.py _mark_route_on_cpp_grid / grid.py _{un,}mark_route_on_cpp_cells
    total_clearance = seg.width / 2 + rules.trace_clearance
    return int(total_clearance / resolution) + 1


def _oracle_pyv(via, rules, resolution, max_trace_width):
    # grid.py _mark_via / _unmark_via / _via_halo_reach_mm
    trace_w = max_trace_width if max_trace_width is not None else rules.trace_width
    radius = int((via.diameter / 2 + rules.via_clearance + trace_w / 2) / resolution)
    radius += 1
    return radius


def _oracle_cppv(via, rules, resolution):
    # core.py _mark_route_on_cpp_grid / grid.py _{un,}mark_route_on_cpp_cells
    return (
        math.ceil((via.diameter / 2 + rules.via_clearance + rules.trace_width / 2) / resolution) + 1
    )


@pytest.mark.parametrize("resolution", RESOLUTIONS)
def test_segment_variants_match_oracle(resolution: float) -> None:
    for width, clearance, tw in itertools.product(SEG_WIDTHS, CLEARANCES, TRACE_WIDTHS):
        rules = _rules(clearance, tw)
        seg = _seg(width)
        py = routed_halo_radius_cells(seg, rules, resolution)
        assert py == _oracle_py(seg, rules, resolution)
        assert py == _oracle_py_inline(seg, rules, resolution)
        assert routed_halo_radius_cells(seg, rules, resolution, cpp=True) == _oracle_cpp(
            seg, rules, resolution
        )


@pytest.mark.parametrize("resolution", RESOLUTIONS)
@pytest.mark.parametrize("max_trace_width", MAX_TRACE_WIDTHS)
def test_via_variants_match_oracle(resolution: float, max_trace_width: float | None) -> None:
    for diameter, clearance, tw in itertools.product(VIA_DIAMETERS, CLEARANCES, TRACE_WIDTHS):
        rules = _rules(clearance, tw)
        via = _via(diameter)
        assert routed_halo_radius_cells(
            via, rules, resolution, max_trace_width=max_trace_width
        ) == _oracle_pyv(via, rules, resolution, max_trace_width)
        assert routed_halo_radius_cells(via, rules, resolution, cpp=True) == _oracle_cppv(
            via, rules, resolution
        )


def test_segment_py_and_cpp_differ_by_one_cell() -> None:
    """The deliberate +2 (Python) vs +1 (C++) segment asymmetry survives."""
    rules = _rules(0.2, 0.2)
    seg = _seg(0.25)
    py = routed_halo_radius_cells(seg, rules, 0.1)
    cpp = routed_halo_radius_cells(seg, rules, 0.1, cpp=True)
    assert py - cpp == 1


def test_cppv_rounds_up_where_pyv_rounds_down() -> None:
    """A case where ``int`` and ``ceil`` differ: CPPV must keep its ``ceil``.

    0.6/2 + 0.15 + 0.2/2 = 0.55 mm at 0.1 mm/cell is 5.5 cells: PYV
    truncates to 5 (+1 = 6), CPPV rounds up to 6 (+1 = 7).
    """
    rules = _rules(0.15, 0.2)
    via = _via(0.6)
    raw = (via.diameter / 2 + rules.via_clearance + rules.trace_width / 2) / 0.1
    assert int(raw) != math.ceil(raw)
    assert routed_halo_radius_cells(via, rules, 0.1) == int(raw) + 1 == 6
    assert routed_halo_radius_cells(via, rules, 0.1, cpp=True) == math.ceil(raw) + 1 == 7


def test_cppv_ignores_rules_override_only_via_max_trace_width() -> None:
    """PYV honours ``max_trace_width``; CPPV always uses ``rules.trace_width``."""
    rules = _rules(0.15, 0.2)
    via = _via(0.6)
    widened = routed_halo_radius_cells(via, rules, 0.05, max_trace_width=0.5)
    assert widened == _oracle_pyv(via, rules, 0.05, 0.5)
    assert widened > routed_halo_radius_cells(via, rules, 0.05)


def test_cpp_with_max_trace_width_raises() -> None:
    rules = _rules(0.15, 0.2)
    with pytest.raises(ValueError):
        routed_halo_radius_cells(_via(0.6), rules, 0.1, cpp=True, max_trace_width=0.3)
    with pytest.raises(ValueError):
        routed_halo_radius_cells(_seg(0.2), rules, 0.1, cpp=True, max_trace_width=0.3)
