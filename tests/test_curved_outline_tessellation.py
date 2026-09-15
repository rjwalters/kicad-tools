"""Certified curve chains, compared with independently sampled authored geometry."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point

from kicad_tools.core.outline_tessellation import tessellate_arc, tessellate_cubic


def _cubic(points, t):
    u = 1 - t
    return tuple(
        u**3 * points[0][i]
        + 3 * u * u * t * points[1][i]
        + 3 * u * t * t * points[2][i]
        + t**3 * points[3][i]
        for i in range(2)
    )


@pytest.mark.parametrize(
    "points",
    [
        [(0, 0), (0, 10), (10, 10), (10, 0)],
        [(0, 0), (10, 10), (-10, -10), (1, 0)],  # inflection/backtracking
        [(0, 0), (10, 0), (-10, 0), (1, 0)],  # collinear overshoot is not a chord
        [(0, 0), (1, 1e-10), (2, -1e-10), (3, 0)],
        [(1e9, 1e9), (1e9, 1e9 + 10), (1e9 + 10, 1e9 + 10), (1e9 + 10, 1e9)],
        [(2, 3)] * 4,
        [(0, 0), (1, 2), (-1, 2), (0, 0)],  # authored closed cubic loop
    ],
)
def test_cubic_preserves_endpoints_and_error(points):
    chain = tessellate_cubic(points, max_error=1e-3)
    assert chain[0] == points[0] and chain[-1] == points[-1]
    chords = LineString(chain)
    samples = [_cubic(points, i / 10000) for i in range(10001)]
    assert max(chords.distance(Point(p)) for p in samples) <= 1e-3
    # Check reverse direction too; collinear/backtracking geometry cannot be
    # certified merely by distances to the infinite endpoint line.
    dense = LineString(samples)
    assert (
        max(
            dense.distance(Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
            for a, b in zip(chain[:-1], chain[1:], strict=True)
        )
        <= 1e-3
    )


@pytest.mark.parametrize(
    "angles", [(0, 45, 90), (0, 180, 270), (0, -45, -90), (350, 360, 370), (10, -170, -260)]
)
def test_arc_preserves_midpoint_sweep_and_error(angles):
    radius = 7.0
    authored = [
        (13 + radius * math.cos(math.radians(a)), -3 + radius * math.sin(math.radians(a)))
        for a in angles
    ]
    chain = tessellate_arc(*authored, max_error=1e-4)
    assert chain[0] == authored[0] and chain[-1] == authored[-1]
    assert authored[1] in chain
    dense = LineString(
        [
            (
                13
                + radius * math.cos(math.radians(angles[0] + (angles[-1] - angles[0]) * i / 10000)),
                -3
                + radius * math.sin(math.radians(angles[0] + (angles[-1] - angles[0]) * i / 10000)),
            )
            for i in range(10001)
        ]
    )
    chords = LineString(chain)
    assert dense.hausdorff_distance(chords) <= 1e-4
    assert chords.length == pytest.approx(
        radius * abs(math.radians(angles[-1] - angles[0])), abs=0.002
    )


def test_resource_caps_refuse_instead_of_weakening_bound():
    with pytest.raises(ValueError, match="budget|resource|certif"):
        tessellate_cubic([(0, 0), (0, 100), (100, 100), (100, 0)], max_error=1e-8, max_segments=2)
    with pytest.raises(ValueError, match="budget|resource|certif"):
        tessellate_cubic([(0, 0), (0, 100), (100, 100), (100, 0)], max_error=1e-8, max_depth=1)
    with pytest.raises(ValueError, match="budget|resource|certif"):
        tessellate_arc((1000, 0), (0, 1000), (-1000, 0), max_error=1e-8, max_segments=2)


@pytest.mark.parametrize(
    "points",
    [
        [(0, 0)] * 3,
        [(0, 0), (0, float("nan")), (1, 1), (2, 2)],
        [(0, 0), (1e300, 1e300), (-1e300, 0), (2, 2)],
    ],
)
def test_malformed_or_uncertifiable_cubic_refuses(points):
    with pytest.raises(ValueError):
        tessellate_cubic(points)


def test_collinear_arc_refuses():
    with pytest.raises(ValueError, match="collinear"):
        tessellate_arc((0, 0), (1, 0), (2, 0))


@pytest.mark.parametrize("sweep", [-270, -90, 90, 270])
def test_legacy_parser_preserves_signed_sweep(sweep):
    from kicad_tools.core.board_outline import _routing_curved_chain
    from kicad_tools.sexp import parse_string

    node = parse_string(f'(gr_arc (start 0 0) (end 10 0) (angle {sweep}) (layer "Edge.Cuts"))')
    chain = _routing_curved_chain(node)
    assert chain[0] == (10, 0)
    theta = math.radians(sweep)
    assert chain[-1] == pytest.approx((10 * math.cos(theta), 10 * math.sin(theta)))
    angles = [
        math.atan2(b[1], b[0]) - math.atan2(a[1], a[0])
        for a, b in zip(chain[:-1], chain[1:], strict=True)
    ]
    signed = sum((a + math.pi) % math.tau - math.pi for a in angles)
    assert signed == pytest.approx(theta)


@pytest.mark.parametrize("angle", ["0", "360", "-360", "720", "nan", "90 180"])
def test_legacy_parser_refuses_unrepresentable_sweep(angle):
    from kicad_tools.core.board_outline import _routing_curved_chain
    from kicad_tools.sexp import parse_string

    node = parse_string(f'(gr_arc (start 0 0) (end 10 0) (angle {angle}) (layer "Edge.Cuts"))')
    with pytest.raises(ValueError, match="Edge.Cuts"):
        _routing_curved_chain(node)


@pytest.mark.parametrize(
    "coords",
    [
        "",
        "(xy 0 0) (xy 1 1) (xy 2 2)",
        "(xy 0 0) (xy 1) (xy 2 2) (xy 3 3)",
        "(xy 0 0) (xy inf 1) (xy 2 2) (xy 3 3)",
    ],
)
def test_cubic_parser_refuses_malformed_controls(coords):
    from kicad_tools.core.board_outline import _routing_curved_chain
    from kicad_tools.sexp import parse_string

    node = parse_string(f'(gr_curve (pts {coords}) (layer "Edge.Cuts"))')
    with pytest.raises(ValueError, match="Edge.Cuts"):
        _routing_curved_chain(node)


def test_nearly_collinear_arc_retains_authored_midpoint():
    # Very shallow genuine arc: exact determinant avoids classifying it as a
    # line, while its certified chord budget permits just two small chords.
    chain = tessellate_arc((-1, 0), (0, 1e-6), (1, 0))
    assert chain == [(-1, 0), (0, 1e-6), (1, 0)]


def test_arc_large_sheet_offset_has_bounded_error():
    origin = 1e9
    chain = tessellate_arc((origin + 10, origin), (origin, origin + 10), (origin - 10, origin))
    centered = LineString([(x - origin, y - origin) for x, y in chain])
    dense = LineString(
        [
            (10 * math.cos(math.pi * i / 10000), 10 * math.sin(math.pi * i / 10000))
            for i in range(10001)
        ]
    )
    assert centered.hausdorff_distance(dense) <= 1e-4


@pytest.mark.parametrize("radius", [1e8, 1e16])
def test_arc_default_resource_or_precision_limit_refuses(radius):
    with pytest.raises(ValueError, match="certif|budget"):
        tessellate_arc((radius, 0), (0, radius), (-radius, 0))


def test_subnormal_scale_arc_preserves_endpoints_without_unbounded_sampling():
    radius = 1e-300
    chain = tessellate_arc((radius, 0), (0, radius), (-radius, 0))
    assert chain == [(radius, 0), (0, radius), (-radius, 0)]
