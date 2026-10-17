"""``DiffPairRouter._remark_route_cells`` uses the shared routed halo (Issue #6982).

#5858 moved the routed-copper halo radius in ``grid.py`` / ``core.py`` behind
``routed_halo_radius_cells``.  The last hand-derived copy lived in
``diffpair_routing.py``'s ``_remark_route_cells``.  The oracle below is that
site's literal pre-refactor formula, so any drift in float order, rounding or
the #1666 margin fails here.  The boundary check extends #5858's grep to
``diffpair_routing.py`` so the pattern cannot come back.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

SEG_WIDTHS = (0.1, 0.127, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
CLEARANCES = (0.1, 0.127, 0.15, 0.2, 0.25)
RESOLUTIONS = (0.025, 0.05, 0.1, 0.127)

ROUTER_DIR = Path(__file__).resolve().parents[2] / "src" / "kicad_tools" / "router"


def _oracle_remark(seg, rules, resolution):
    # Literal pre-#6982 diffpair_routing.py _remark_route_cells formula.
    total_clearance = seg.width / 2 + rules.trace_clearance
    clearance_cells = int(total_clearance / resolution) + 1
    clearance_cells += 1
    return clearance_cells


def _router_with_recording_grid(rules: DesignRules, resolution: float):
    marked: list[tuple[Segment, int]] = []
    cpp_marked: list[Route] = []
    grid = SimpleNamespace(
        rules=rules,
        resolution=resolution,
        _mark_segment=lambda seg, clearance_cells: marked.append((seg, clearance_cells)),
    )
    router = object.__new__(DiffPairRouter)
    router.autorouter = SimpleNamespace(grid=grid, _mark_route_on_cpp_grid=cpp_marked.append)
    return router, marked, cpp_marked


@pytest.mark.parametrize("resolution", RESOLUTIONS)
def test_remark_route_cells_matches_pre_refactor_oracle(resolution: float) -> None:
    for clearance in CLEARANCES:
        rules = DesignRules(trace_width=0.2, trace_clearance=clearance, via_clearance=clearance)
        segs = [
            Segment(x1=0.0, y1=0.0, x2=1.0, y2=0.0, width=w, layer=Layer.F_CU, net=1)
            for w in SEG_WIDTHS
        ]
        route = Route(net=1, net_name="LVDS0_P", segments=segs)
        router, marked, cpp_marked = _router_with_recording_grid(rules, resolution)

        router._remark_route_cells(route)

        assert [s for s, _ in marked] == segs
        for seg, cells in marked:
            assert cells == _oracle_remark(seg, rules, resolution), (
                seg.width,
                clearance,
                resolution,
            )
        assert cpp_marked == [route]


# #5858's deletion-boundary grep, extended to diffpair_routing.py (which
# reaches the grid through a local ``grid`` rather than ``self``).
_BOUNDARY = re.compile(
    r"seg\.width / 2 \+ (self|grid)\.rules\.trace_clearance"
    r"|via\.diameter / 2 \+ (self|grid)\.rules\.via_clearance \+ "
    r"(trace_w|(self|grid)\.rules\.trace_width) / 2"
)


@pytest.mark.parametrize("name", ["grid.py", "core.py", "diffpair_routing.py"])
def test_no_hand_derived_routed_halo_radius(name: str) -> None:
    lines = (ROUTER_DIR / name).read_text().splitlines()
    hits = [
        f"{name}:{i}: {line.strip()}" for i, line in enumerate(lines, 1) if _BOUNDARY.search(line)
    ]
    assert hits == []


def test_boundary_pattern_matches_the_removed_site() -> None:
    # Guard against the regex silently matching nothing.
    removed = "total_clearance = seg.width / 2 + grid.rules.trace_clearance"
    assert _BOUNDARY.search(removed)
    assert not _BOUNDARY.search(
        "cells = int((item.width / 2 + rules.trace_clearance) / resolution)"
    )
