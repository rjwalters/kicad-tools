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

Operator ruling Q12 (recorded on #6858): the trim is accepted only if it
leaves the pair's combined P+N uncoupled length no higher than before.  That
is the diff-pair bench's ``uncoupled_mm``
(``_pair_combined_uncoupled_mm``).  A bulge on the shorter half always fails
that gate on a pair coupled end to end, because the bulge's legs are
uncoupled.  So the partner-aware path falls back to straightening the longer
half, which can lower the figure instead.

These tests build pairs with those shapes on a real grid and assert on the
combined figure.
"""

from __future__ import annotations

import copy
import math

from kicad_tools.router.diffpair_length_tuning import (
    _pair_combined_uncoupled_mm,
    _pair_uncoupled_mm,
)
from kicad_tools.router.diffpair_routing import (
    _serpentine_segments_ok,
    create_serpentine,
    match_pair_lengths,
)
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


def test_no_room_leaves_route_untouched():
    p, n = _pair()
    foreign = _route(FOREIGN, "MIPI_DAT1_P", [(1.7, 2.85), (15.3, 2.85)])
    grid = _grid(p, n, foreign)
    before = copy.deepcopy(n.segments)

    assert not match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
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


def _detour_pair() -> tuple[Route, Route]:
    """P makes a 45-degree trapezoid detour away from N: +0.414 mm, all uncoupled."""
    p = _route(
        P_NET,
        "A_P",
        [(1.0, 2.09), (5.0, 2.09), (5.5, 1.59), (6.5, 1.59), (7.0, 2.09), (16.0, 2.09)],
    )
    n = _route(N_NET, "A_N", [(1.0, 2.47), (16.0, 2.47)])
    return p, n


def test_combined_figure_is_the_bench_uncoupled_mm():
    """P+N, each half's length minus what the continuity rule counts coupled."""
    p, n = _detour_pair()
    # The two 45-degree legs (2 x 0.7071) are not parallel to N, and the
    # detour top sits 0.655 mm edge to edge from N, outside the 0.5 mm window.
    # Every other segment is coupled.
    assert math.isclose(_pair_combined_uncoupled_mm(p, n), 2.0 * math.sqrt(0.5) + 1.0, abs_tol=1e-6)
    assert math.isclose(_pair_combined_uncoupled_mm(n, p), _pair_combined_uncoupled_mm(p, n))
    tight_p, tight_n = _pair()
    assert _pair_combined_uncoupled_mm(tight_p, tight_n) <= 1e-6


def test_tight_pair_bulge_is_refused_by_the_combined_gate():
    """Board 07 MIPI shape: any bulge on N raises P+N uncoupled, so nothing commits.

    Before ruling Q12 the sized comb committed here and took the combined
    figure from 0 to >= 0.4 mm while matching the skew.
    """
    p, n = _pair()
    grid = _grid(p, n)
    combined_before = _pair_combined_uncoupled_mm(p, n)
    p_before, n_before = copy.deepcopy(p.segments), copy.deepcopy(n.segments)

    assert not match_pair_lengths(
        p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )

    assert _pair_combined_uncoupled_mm(p, n) <= combined_before + 1e-6
    assert n.segments == n_before
    assert p.segments == p_before


def test_legacy_comb_overshoot_is_refused():
    """Asked for 0.373 mm, the legacy comb's 1.79 mm is refused, not committed."""
    p, n = _pair()
    before = copy.deepcopy(n.segments)
    combined_before = _pair_combined_uncoupled_mm(p, n)
    assert not create_serpentine(
        n, 0.373, partner_route=p, intra_pair_clearance_mm=INTRA, max_length_to_add=0.873
    )
    assert n.segments == before
    assert _pair_combined_uncoupled_mm(p, n) <= combined_before + 1e-6


def test_longer_half_detour_is_straightened():
    """Shortening the longer half lowers the skew AND the combined figure."""
    p, n = _detour_pair()
    grid = _grid(p, n)
    skew_before = _skew(p, n)
    combined_before = _pair_combined_uncoupled_mm(p, n)
    per_net_before = _pair_uncoupled_mm(p, n)
    n_before = copy.deepcopy(n.segments)

    assert match_pair_lengths(
        p, n, 0.1, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )

    assert _skew(p, n) <= 0.1 + 1e-6 < skew_before
    assert _pair_combined_uncoupled_mm(p, n) < combined_before - 1.0
    assert _pair_uncoupled_mm(p, n) <= per_net_before + 1e-6
    assert n.segments == n_before
    # Connectivity: still one chain from the same start to the same end.
    assert (p.segments[0].x1, p.segments[0].y1) == (1.0, 2.09)
    assert (p.segments[-1].x2, p.segments[-1].y2) == (16.0, 2.09)
    for a, b in zip(p.segments, p.segments[1:], strict=False):
        assert math.isclose(a.x2, b.x1) and math.isclose(a.y2, b.y1)


def _step_pair() -> tuple[Route, Route]:
    """Both halves step up 1 mm together; N stops 0.5 mm short, so P is longer."""
    p = _route(P_NET, "A_P", [(1.0, 2.09), (6.0, 2.09), (6.0, 1.09), (16.0, 1.09)])
    n = _route(N_NET, "A_N", [(1.0, 2.47), (6.38, 2.47), (6.38, 1.47), (15.5, 1.47)])
    return p, n


def test_shortening_that_raises_the_combined_figure_is_refused():
    """A clearance-legal chord that uncouples N's step is rejected by the gate."""
    p, n = _step_pair()
    grid = _grid(p, n)
    # The only chord the clearance checks allow cuts P's lower corner.  It is
    # legal copper, but N's vertical step then has no P partner.
    chord = Segment(
        x1=1.0, y1=2.09, x2=6.0, y2=1.09, width=WIDTH, layer=Layer.F_CU, net=P_NET, net_name="A_P"
    )
    assert _serpentine_segments_ok([chord], p, n, INTRA, grid, partner_cells_passable=True)
    cut = Route(net=P_NET, net_name="A_P")
    cut.segments = [chord, p.segments[2]]
    assert _pair_combined_uncoupled_mm(cut, n) > _pair_combined_uncoupled_mm(p, n) + 0.5

    p_before = copy.deepcopy(p.segments)
    assert not match_pair_lengths(
        p, n, 0.1, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid
    )
    assert p.segments == p_before


def test_partner_via_is_checked_geometrically():
    """Partner cells are passable in the relaxed grid scan, so partner vias are checked exactly."""
    p, n = _pair()
    grid = _grid(p, n)
    # A bulge leg on N's trunk, rising away from P (+y).
    leg = Segment(
        x1=9.0, y1=2.47, x2=9.0, y2=2.9, width=WIDTH, layer=Layer.F_CU, net=N_NET, net_name="N"
    )
    assert _serpentine_segments_ok([leg], n, p, INTRA, grid, partner_cells_passable=True)
    p.vias = [
        Via(x=9.3, y=2.95, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=P_NET)
    ]
    assert not _serpentine_segments_ok([leg], n, p, INTRA, grid, partner_cells_passable=True)


def test_legacy_comb_on_coupled_pair_is_gated():
    """The legacy comb zigzags the whole host and must pass the Q12 gates too.

    A 0.6 mm pitch pair (0.375 mm gap, coupled): the ungated comb matched the
    skew (1.60 -> 0.16 mm) but took combined P+N uncoupled from 1.60 to 17.21 mm.
    """
    p = _route(P_NET, "A_P", [(1.0, 1.29), (1.0, 2.09), (16.0, 2.09), (16.0, 1.29)])
    n = _route(N_NET, "A_N", [(1.0, 2.69), (16.0, 2.69)])
    grid = _grid(p, n)
    combined_before = _pair_combined_uncoupled_mm(p, n)
    per_net_before = _pair_uncoupled_mm(p, n)
    p_before, n_before = copy.deepcopy(p.segments), copy.deepcopy(n.segments)

    match_pair_lengths(p, n, 0.5, add_serpentines=True, intra_pair_clearance_mm=INTRA, grid=grid)

    assert _pair_combined_uncoupled_mm(p, n) <= combined_before + 1e-6
    assert _pair_uncoupled_mm(p, n) <= per_net_before + 1e-6
    assert n.segments == n_before
    assert p.segments == p_before
