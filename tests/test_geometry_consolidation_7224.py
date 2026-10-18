"""Regression suite pinning the point-in-polygon / point-to-segment copies (#7224).

Golden tables were captured from ``origin/main`` before any delegation.  Each
scalar ray-cast copy must keep returning its pinned result; the only
intentional verdict change is ``validate.rules.placement.point_in_polygon``
becoming boundary-inclusive.
"""

from __future__ import annotations

import pytest

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
POLYS = {
    "square": SQUARE,
    # KiCad outline as returned by PCB.get_board_outline(): closed ring with a
    # repeated final vertex (zero-length closing edge).
    "ring5": SQUARE + [(0.0, 0.0)],
    "concave_L": [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 10.0), (0.0, 10.0)],
    "triangle": [(0.0, 0.0), (10.0, 0.0), (5.0, 8.0)],
    "two": [(0.0, 0.0), (10.0, 10.0)],
    "empty": [],
}

# Interior, exterior, edge midpoints, vertices, collinear-with-horizontal-edge
# rays, just-outside points.
POINTS = [
    (5.0, 5.0),
    (2.0, 2.0),
    (8.0, 2.0),
    (2.0, 8.0),
    (8.0, 8.0),
    (15.0, 5.0),
    (-5.0, 5.0),
    (5.0, 15.0),
    (5.0, -5.0),
    (5.0, 0.0),
    (0.0, 5.0),
    (5.0, 10.0),
    (10.0, 5.0),
    (0.0, 0.0),
    (10.0, 0.0),
    (10.0, 10.0),
    (0.0, 10.0),
    (-5.0, 0.0),
    (15.0, 0.0),
    (-5.0, 10.0),
    (15.0, 10.0),
    (-5.0, 4.0),
    (7.0, 4.0),
    (4.0, 7.0),
    (10.001, 5.0),
    (-0.001, 5.0),
    (5.0, 10.001),
    (5.0, -0.001),
    (4.0, 4.0),
    (5.0, 8.0),
    (2.5, 4.0),
]

# (px, py, x1, y1, x2, y2): interior projection, endpoint clamps, degenerate
# segments (zero and sub-micron length), on-segment, vertical/horizontal.
DIST_CASES = [
    (5.0, 5.0, 0.0, 0.0, 10.0, 0.0),
    (-3.0, 4.0, 0.0, 0.0, 10.0, 0.0),
    (13.0, -4.0, 0.0, 0.0, 10.0, 0.0),
    (5.0, 0.0, 0.0, 0.0, 10.0, 0.0),
    (0.0, 0.0, 0.0, 0.0, 10.0, 0.0),
    (3.0, 4.0, 1.0, 1.0, 1.0, 1.0),
    (3.0, 4.0, 1.0, 1.0, 1.0 + 1e-7, 1.0),
    (2.0, 7.0, 0.0, 0.0, 0.0, 10.0),
    (5.0, 5.0, 0.0, 0.0, 10.0, 10.0),
    (5.0, 6.0, 0.0, 0.0, 10.0, 10.0),
    (150.3, 149.1, 148.0, 150.0, 152.5, 147.5),
    (-2.0, -2.0, 10.0, 0.0, 0.0, 10.0),
]

# Golden tables captured from origin/main before any edit: one char per entry
# of POINTS ("1" = inside).  (half_open, inclusive).  ``half_open`` is what
# every scalar ray-cast copy returned; ``inclusive`` is the router mesh copy.
GOLDEN = {
    "square": ("1111100001100100000000110000111", "1111100001111111100000110000111"),
    "ring5": ("1111100001100100000000110000111", "1111100001111111100000110000111"),
    "concave_L": ("0111000001100100000000000000001", "0111000001100110100000110000101"),
    "triangle": ("1110000001000100000000100000101", "1110000001000110000000100000111"),
    "two": ("0" * 31, "0" * 31),
    "empty": ("0" * 31, "0" * 31),
}

GOLDEN_DIST = [
    5.0,
    5.0,
    5.0,
    0.0,
    0.0,
    3.605551275,
    3.60555122,
    2.0,
    0.0,
    0.707106781,
    0.330237193,
    9.899494937,
]


def _bits(fn, poly) -> str:
    return "".join("1" if fn(x, y, poly) else "0" for x, y in POINTS)


def _stitch(x, y, poly):
    from kicad_tools.cli.stitch_cmd import point_in_polygon

    return point_in_polygon(x, y, poly)


def _via_clearance(x, y, poly):
    from kicad_tools.router.via_clearance import _point_in_polygon

    return _point_in_polygon(x, y, poly)


def _zones(x, y, poly):
    from kicad_tools.router.zones import ZoneFiller

    return ZoneFiller.point_in_polygon(None, x, y, poly)  # type: ignore[arg-type]


def _net_status(x, y, poly):
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    return NetStatusAnalyzer._point_in_polygon(None, (x, y), poly)  # type: ignore[arg-type]


def _connectivity(x, y, poly):
    from kicad_tools.validate.connectivity import ConnectivityValidator

    return ConnectivityValidator._point_in_polygon((x, y), poly)


def _mesh(x, y, poly):
    from kicad_tools.router.mesh.geometry import point_in_polygon

    return point_in_polygon((x, y), poly)


def _placement(x, y, poly):
    from kicad_tools.validate.rules.placement import point_in_polygon

    return point_in_polygon(x, y, poly)


HALF_OPEN_COPIES = [_stitch, _via_clearance, _zones, _net_status, _connectivity]


@pytest.mark.parametrize("name", list(POLYS))
@pytest.mark.parametrize("fn", HALF_OPEN_COPIES, ids=lambda f: f.__name__)
def test_half_open_copies_pinned(fn, name):
    assert _bits(fn, POLYS[name]) == GOLDEN[name][0]


@pytest.mark.parametrize("name", list(POLYS))
def test_mesh_copy_pinned_inclusive(name):
    assert _bits(_mesh, POLYS[name]) == GOLDEN[name][1]


@pytest.mark.parametrize("name", list(POLYS))
def test_placement_copy_pinned(name):
    # Intentionally flipped by #7224: the placement rule is boundary-inclusive,
    # so it now matches the inclusive column (top/right edges and the
    # (max, max) corner are inside).
    assert _bits(_placement, POLYS[name]) == GOLDEN[name][1]


@pytest.mark.parametrize("case,expected", list(zip(DIST_CASES, GOLDEN_DIST, strict=True)))
def test_core_distance_pinned(case, expected):
    from kicad_tools.core.geometry import point_to_segment_distance

    assert point_to_segment_distance(*case) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("case,expected", list(zip(DIST_CASES, GOLDEN_DIST, strict=True)))
def test_copper_distance_pinned(case, expected):
    from kicad_tools.geometry.copper import point_segment_distance

    px, py, x1, y1, x2, y2 = case
    assert point_segment_distance((px, py), (x1, y1), (x2, y2)) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# core.geometry.point_in_polygon
# ---------------------------------------------------------------------------


def _core(x, y, poly, inclusive):
    from kicad_tools.core.geometry import point_in_polygon

    return point_in_polygon(x, y, poly, boundary_inside=inclusive)


@pytest.mark.parametrize("name", list(POLYS))
def test_core_point_in_polygon_matches_golden(name):
    poly = POLYS[name]
    half = "".join("1" if _core(x, y, poly, False) else "0" for x, y in POINTS)
    incl = "".join("1" if _core(x, y, poly, True) else "0" for x, y in POINTS)
    assert half == GOLDEN[name][0]
    assert incl == GOLDEN[name][1]


def test_core_point_in_polygon_boundary_keyword_is_required():
    from kicad_tools.core.geometry import point_in_polygon

    with pytest.raises(TypeError):
        point_in_polygon(1.0, 1.0, SQUARE)  # type: ignore[call-arg]


@pytest.mark.parametrize("poly_name", ["square", "ring5"])
@pytest.mark.parametrize(
    "pt", [(5, 0), (0, 5), (5, 10), (10, 5), (0, 0), (10, 0), (10, 10), (0, 10)]
)
def test_core_inclusive_accepts_every_edge_and_vertex(poly_name, pt):
    assert _core(pt[0], pt[1], POLYS[poly_name], True)


@pytest.mark.parametrize("pt", [(10.001, 5), (-0.001, 5), (5, 10.001), (5, -0.001)])
def test_core_inclusive_rejects_just_outside(pt):
    assert not _core(pt[0], pt[1], SQUARE, True)


def test_core_half_open_excludes_top_right():
    assert _core(5, 0, SQUARE, False)
    assert not _core(5, 10, SQUARE, False)
    assert not _core(10, 5, SQUARE, False)
