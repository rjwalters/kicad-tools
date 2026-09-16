"""Spacing-preserving loops add the same length to translated/rotated hosts."""

import math
from dataclasses import replace

import pytest
from shapely.geometry import LineString

from kicad_tools.router.coordinated_tuning import coordinated_pair_loop
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment


@pytest.mark.parametrize("angle", [0, math.pi / 4, math.pi / 2, math.pi])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("num_loops", [1, 2, 3])
def test_equal_length_and_spacing_with_exact_connection_anchors(angle, reverse, num_loops):
    def transform(x, y):
        return (
            100.089 + x * math.cos(angle) - y * math.sin(angle),
            100.051 + x * math.sin(angle) + y * math.cos(angle),
        )

    def host(net, y):
        a, b = transform(0, y), transform(12, y)
        return Segment(*a, *b, 0.225, Layer.IN1_CU, net=net, net_name=str(net))

    p, n = host(1, 0), host(2, 0.381)
    if reverse:
        n = replace(n, x1=n.x2, y1=n.y2, x2=n.x1, y2=n.y1)
    originals = replace(p), replace(n)
    result = coordinated_pair_loop(
        p, n, added_length=7.387495865, window_start=3, window_end=9, num_loops=num_loops
    )
    assert result is not None
    for old, chain in zip((p, n), result, strict=True):
        assert chain[0].start == old.start and chain[-1].end == old.end
        assert sum(math.dist(s.start, s.end) for s in chain) - math.dist(
            old.start, old.end
        ) == pytest.approx(7.387495865)
        assert all(
            (s.net, s.net_name, s.width, s.layer) == (old.net, old.net_name, old.width, old.layer)
            for s in chain
        )
        for a, b in zip(chain[:-1], chain[1:], strict=True):
            assert a.end == b.start
    pl, nl = [LineString([chain[0].start] + [s.end for s in chain]) for chain in result]
    assert pl.is_simple and nl.is_simple
    assert pl.distance(nl) == pytest.approx(0.381)
    assert (p, n) == originals


@pytest.mark.parametrize(
    "bad", ["crossing", "overlap", "layer", "window", "fixed_end", "nonfinite"]
)
def test_invalid_hosts_or_windows_are_rejected(bad):
    p = Segment(0, 0, 12, 0, 0.225, Layer.F_CU, net=1)
    n = Segment(0, 0.381, 12, 0.381, 0.225, Layer.F_CU, net=2)
    kwargs = {"added_length": 4, "window_start": 3, "window_end": 7}
    if bad == "crossing":
        n = replace(n, x1=6, y1=-6, x2=6, y2=6)
    if bad == "overlap":
        n = replace(n, y1=0.1, y2=0.1)
    if bad == "layer":
        n = replace(n, layer=Layer.B_CU)
    if bad == "window":
        kwargs["window_end"] = 3.7
    if bad == "fixed_end":
        kwargs["window_start"] = -1
    if bad == "nonfinite":
        kwargs["added_length"] = math.inf
    assert coordinated_pair_loop(p, n, **kwargs) is None


@pytest.mark.parametrize("count", [0, -1, 4, 1.5, True])
def test_invalid_or_unbounded_loop_count_is_rejected(count):
    p = Segment(0, 0, 12, 0, 0.15, Layer.F_CU, net=1)
    n = Segment(0, 0.381, 12, 0.381, 0.15, Layer.F_CU, net=2)
    assert (
        coordinated_pair_loop(p, n, added_length=4, window_start=3, window_end=7, num_loops=count)
        is None
    )
