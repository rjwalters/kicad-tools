"""The inline diff-pair comb is undone when it leaves the pair no closer (Issue #7548).

Board 06's MIPI_D0 routed as a coupled trunk with P 0.663 mm longer than N.
The legacy comb (:func:`create_serpentine`) floors its triangular bulges at a
0.3 mm amplitude and spreads twelve of them along the 16.5 mm host, so it
added 1.649 mm to N: the pair came back 0.986 mm skewed the other way, and the
off-angle teeth grew a further 1.3 mm under 45-degree quantization (2.320 mm
against a 0.300 mm limit).  :func:`match_pair_lengths` now restores the route
when the comb makes the skew no smaller, so the caller falls through to the
audited skew-bump tuner on untouched copper.
"""

from __future__ import annotations

from kicad_tools.router.diffpair_routing import calculate_route_length, match_pair_lengths
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _route(net: int, name: str, y: float, x2: float) -> Route:
    route = Route(net=net, net_name=name)
    route.segments.append(
        Segment(x1=0.0, y1=y, x2=x2, y2=y, width=0.225, layer=Layer.F_CU, net=net, net_name=name)
    )
    return route


def _skew(p: Route, n: Route) -> float:
    return abs(calculate_route_length([p]) - calculate_route_length([n]))


def test_overshooting_comb_is_rolled_back():
    """MIPI_D0's shape: a 16.5 mm host and a 0.663 mm skew against 0.3 mm."""
    p = _route(1, "MIPI_D0+", 0.0, 16.466 + 0.663)
    n = _route(2, "MIPI_D0-", 0.4, 16.466)
    before = n.segments
    skew_before = _skew(p, n)

    assert not match_pair_lengths(p, n, 0.3, add_serpentines=True, intra_pair_clearance_mm=0.135)

    # Byte-for-byte: the same segment list object, so nothing is half-applied.
    assert n.segments is before
    assert len(n.segments) == 1
    assert abs(_skew(p, n) - skew_before) < 1e-9


def test_overshoot_also_rolled_back_without_partner_constraint():
    """The legacy (no intra-pair clearance) path is held to the same bar."""
    p = _route(1, "A+", 0.0, 16.466 + 0.663)
    n = _route(2, "A-", 0.4, 16.466)
    before = n.segments
    assert not match_pair_lengths(p, n, 0.3, add_serpentines=True)
    assert n.segments is before


def test_comb_that_reduces_skew_is_kept():
    """A comb that brings the pair closer still commits (no behaviour change)."""
    p = _route(1, "B+", 0.0, 12.0)
    n = _route(2, "B-", 1.0, 10.0)
    skew_before = _skew(p, n)
    assert match_pair_lengths(p, n, 1.0, add_serpentines=True)
    assert len(n.segments) > 1
    assert _skew(p, n) < skew_before
