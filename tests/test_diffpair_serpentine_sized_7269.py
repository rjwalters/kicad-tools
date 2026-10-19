"""Issue #7269: the coupled-commit skew trim on a tightly coupled pair.

Board 07's MIPI_DAT0 commits as a pose-centreline trunk: short, 0 vias, one
long straight run per half at a 0.38 mm centre pitch, skew 0.623 mm.  The
diff-pair skew tuner (``tune_diff_pair_skew``) never runs on that board (the
grid engine does not engage it), so the only trim is the inline serpentine
in ``route_differential_pair_coupled`` -> ``match_pair_lengths`` ->
``create_serpentine``.  Measured on a seed-42 board 07 route, it rejected
every attempt for two reasons:

1. **The grid check.**  The partner's clearance envelope covers the shorter
   half's own centreline, so the very first cell of any bulge reads "blocked
   by a foreign net" (the partner) and the comb is refused before it leaves
   the trunk.
2. **The size.**  The legacy comb spans the whole host segment and floors
   its amplitude at 0.3 mm: asked for 0.373 mm, it would add 1.79 mm and
   leave skew 1.169 mm, worse than the 0.623 mm it started from.

These tests build a pair with that shape on a real grid.
"""

from __future__ import annotations

import copy
import math

from kicad_tools.router.diffpair_length_tuning import _pair_uncoupled_mm
from kicad_tools.router.diffpair_routing import create_serpentine, match_pair_lengths
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.path import calculate_route_length
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

WIDTH = 0.225
#: The floored intra-pair gap board 07's MIPI class resolves to.
INTRA = 0.135
P_NET, N_NET, FOREIGN = 17, 18, 19


def _route(net: int, name: str, pts: list[tuple[float, float]]) -> Route:
    r = Route(net=net, net_name=name)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        r.segments.append(
            Segment(
                x1=x1, y1=y1, x2=x2, y2=y2, width=WIDTH, layer=Layer.F_CU, net=net, net_name=name
            )
        )
    return r


def _pair() -> tuple[Route, Route]:
    """MIPI_DAT0-shaped: one straight run each at a 0.38 mm pitch, P longer by 0.62 mm."""
    p = _route(P_NET, "MIPI_DAT0_P", [(1.0, 2.55), (1.0, 2.09), (16.0, 2.09), (16.0, 2.55)])
    n = _route(N_NET, "MIPI_DAT0_N", [(1.38, 3.0), (1.38, 2.47), (15.62, 2.47), (15.62, 3.0)])
    return p, n


def _grid(*routes: Route) -> RoutingGrid:
    """Mark in order; as on board 07, the first-marked P owns N's trunk cells."""
    rules = DesignRules(trace_width=WIDTH, trace_clearance=0.15, grid_resolution=0.127)
    grid = RoutingGrid(width=20.0, height=8.0, rules=rules)
    for r in routes:
        grid.mark_route(r)
    return grid


def _skew(p: Route, n: Route) -> float:
    return abs(calculate_route_length([p]) - calculate_route_length([n]))


def _n_trunk_cell_owner(grid: RoutingGrid) -> int:
    gx, gy = grid.world_to_grid(9.0, 2.47)
    return grid.cell_at(grid.layer_to_index(Layer.F_CU.value), gy, gx).net


def test_fixture_reproduces_partner_envelope_over_own_trunk():
    """Precondition: the grid reports the N trunk's own cells as the partner's."""
    p, n = _pair()
    grid = _grid(p, n)
    assert _n_trunk_cell_owner(grid) == P_NET
    assert math.isclose(_skew(p, n), 0.62, abs_tol=1e-6)


def test_tight_pair_trim_commits_and_matches():
    p, n = _pair()
    grid = _grid(p, n)
    skew_before = _skew(p, n)
    uncoupled_before = _pair_uncoupled_mm(n, p)

    assert match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )

    assert _skew(p, n) <= 0.25 + 1e-6 < skew_before
    # The KiCad uncoupled figure (``kct check``'s) does not grow.
    assert _pair_uncoupled_mm(n, p) <= uncoupled_before + 1e-6
    # One-sided (away from P, i.e. +y), axis-aligned copper only.
    for s in n.segments[1:-1]:
        assert s.y1 >= 2.47 - 1e-9 and s.y2 >= 2.47 - 1e-9
    assert all(abs(s.x2 - s.x1) < 1e-9 or abs(s.y2 - s.y1) < 1e-9 for s in n.segments[:-1])


def test_trim_slides_past_foreign_copper():
    """Foreign copper over the trunk's centre: the comb moves along the run."""
    p, n = _pair()
    foreign = _route(FOREIGN, "MIPI_DAT1_P", [(6.0, 3.15), (12.0, 3.15)])
    grid = _grid(p, n, foreign)

    assert match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )
    bulge_x = [s.x1 for s in n.segments if abs(s.x2 - s.x1) < 1e-9 and abs(s.y2 - s.y1) > 1e-9]
    assert bulge_x and all(x < 6.0 or x > 12.0 for x in bulge_x[1:-1])


def test_no_room_leaves_route_untouched():
    p, n = _pair()
    foreign = _route(FOREIGN, "MIPI_DAT1_P", [(1.7, 2.85), (15.3, 2.85)])
    grid = _grid(p, n, foreign)
    before = copy.deepcopy(n.segments)

    assert not match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )
    assert n.segments == before


def test_partner_via_is_checked_geometrically():
    """Partner cells are passable in the grid scan, so partner vias are checked exactly."""
    p, n = _pair()
    grid = _grid(p, n)
    # A partner via just outside N, along the whole bulge-able span: no comb fits.
    p.vias = [
        Via(x=x / 10.0, y=2.95, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=P_NET)
        for x in range(20, 152, 5)
    ]
    before = copy.deepcopy(n.segments)
    assert not create_serpentine(
        n,
        0.373,
        partner_route=p,
        intra_pair_clearance_mm=INTRA,
        grid=grid,
        max_length_to_add=0.873,
    )
    assert n.segments == before


def test_within_tolerance_is_not_touched():
    p = _route(P_NET, "A_P", [(1.0, 2.09), (16.0, 2.09)])
    n = _route(N_NET, "A_N", [(1.0, 2.47), (15.7, 2.47)])
    before = copy.deepcopy(n.segments)
    assert match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=_grid(p, n)
    )
    assert n.segments == before


def test_legacy_comb_overshoot_is_refused():
    """Asked for 0.373 mm, the partner-aware path never adds more than the ceiling."""
    p, n = _pair()
    before = calculate_route_length([n])
    assert create_serpentine(
        n, 0.373, partner_route=p, intra_pair_clearance_mm=INTRA, max_length_to_add=0.873
    )
    added = calculate_route_length([n]) - before
    assert 0.373 - 1e-6 <= added <= 0.873 + 1e-6
    assert not math.isclose(added, 1.79, abs_tol=0.05)
