"""One coordinate contract for routing, planning and schema (#4978)."""

import math
from dataclasses import dataclass, field

import pytest

from kicad_tools.core.board_outline import (
    _CIRCLE_MAX_SEGMENTS,
    _CIRCLE_MIN_SEGMENTS,
    _CIRCLE_TESSELLATION_MAX_ERROR_MM,
    OutlineSegments,
    board_outline_bounds,
    board_outline_segments,
    circle_sagitta,
    circle_segment_count,
    circle_tessellation_points,
)
from kicad_tools.router import DesignRules
from kicad_tools.router.io import (
    _extract_edge_segments,
    extract_board_dimensions,
    extract_board_origin,
    load_pcb_for_routing,
    validate_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string


def _board(outline, origin=(68.5, 55)):
    x, y = origin
    return f"""(kicad_pcb (version 20241229) (generator "test")
    (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "SIGNAL")
    (gr_rect (start 1 2) (end 900 900) (layer "F.SilkS"))
    {outline}
    (footprint "R" (layer "F.Cu") (at {x + 10} {y + 10})
      (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))
      (fp_rect (start -900 -900) (end 900 900) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))
      (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))))"""


@pytest.mark.parametrize("origin", [(68.5, 55), (-68.5, -55)])
@pytest.mark.parametrize("kind", ["lines", "rect", "reversed"])
def test_same_outline_across_consumers(tmp_path, origin, kind):
    x, y = origin
    corners = [(x, y), (x + 160, y), (x + 160, y + 100), (x, y + 100)]
    if kind == "lines":
        outline = "\n".join(
            f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'
            for a, b in zip(corners, corners[1:] + corners[:1], strict=True)
        )
    else:
        a, b = (corners[2], corners[0]) if kind == "reversed" else (corners[0], corners[2])
        outline = f'(gr_rect (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))'
    text = _board(outline, origin)
    path = tmp_path / "board.kicad_pcb"
    path.write_text(text)
    assert extract_board_dimensions("  \n" + text) == (160, 100)
    assert extract_board_origin(path) == origin
    edges = _extract_edge_segments(text)
    assert len(edges) == 4
    assert all(px in (x, x + 160) and py in (y, y + 100) for edge in edges for px, py in edge)
    pcb = PCB.load(path)
    assert pcb.board_origin == origin
    assert pcb.board_size == (160, 100)
    assert pcb.footprints[0].position == (10, 10)
    assert pcb.get_pad_position("R1", "1") == (9, 10)
    saved = tmp_path / "roundtrip.kicad_pcb"
    pcb.save(saved)
    reread = PCB.load(saved)
    assert reread.board_origin == origin
    assert reread.get_pad_position("R1", "1") == (9, 10)
    router, _ = load_pcb_for_routing(
        path, rules=DesignRules(grid_resolution=0.5, trace_clearance=0.5), validate_drc=False
    )
    assert (router.grid.width, router.grid.height) == (160, 100)
    # Router pads retain sheet-absolute coordinates; the grid carries the origin.
    pads = list(router.pads.values())
    assert sorted((p.x, p.y) for p in pads) == [(x + 9, y + 10), (x + 11, y + 10)]
    assert (router.grid.origin_x, router.grid.origin_y) == origin


@pytest.mark.parametrize("outline", ["", '(gr_rect (start 0 0) (end 10 10) (layer "F.SilkS"))'])
def test_missing_outline_is_explicit(tmp_path, outline):
    text = _board(outline)
    assert extract_board_dimensions(text) is None
    assert extract_board_origin(text) is None
    path = tmp_path / "missing.kicad_pcb"
    path.write_text(text)
    with pytest.raises(ValueError, match="missing supported Edge.Cuts"):
        load_pcb_for_routing(path, validate_drc=False)


@pytest.mark.parametrize(
    "outline",
    [
        '(gr_rect (start 0 0) (layer "Edge.Cuts"))',
        '(gr_text "unsupported" (at 1 1) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (mid 1 1) (end 2 2) (layer "Edge.Cuts"))',
    ],
)
def test_malformed_or_unsupported_outline_is_explicit(outline):
    with pytest.raises(ValueError, match="Edge.Cuts"):
        extract_board_dimensions(_board(outline))


@pytest.mark.parametrize(
    "outline",
    [
        '(gr_rect (start 0 0) (layer "Edge.Cuts"))',
        '(gr_line (start nan 0) (end 12 12) (layer "Edge.Cuts"))',
        '(gr_poly (pts (xy 0 0) (xy 12) (xy 12 12)) (layer "Edge.Cuts"))',
        '(gr_text "unsupported" (at 1 1) (layer "Edge.Cuts"))',
    ],
)
def test_malformed_outline_is_fail_loud_for_routing_but_tolerated_by_pcb_load(tmp_path, outline):
    """Issue #5274: one reader, two contracts, split at ``PCB.load()``.

    ``board_outline_bounds`` (and therefore every routing/bounds consumer that
    calls it directly) stays fail-loud.  ``PCB.load()`` must **not** inherit
    that: repair/inspection tools have to be able to open a board a human
    already knows is imperfect and refuse on their own proof-based terms
    (``fix-vias --relocate-in-pad --search-alternatives`` refuses atomically).
    A tolerated load leaves the origin unproven at ``(0, 0)`` -- no coordinate
    is rewritten -- and records the reason on ``PCB.outline_error``.
    """
    text = _board(outline)
    path = tmp_path / "malformed.kicad_pcb"
    path.write_text(text)

    # Fail loud for routing and for anything asking for bounds directly.
    with pytest.raises(ValueError, match="Edge.Cuts"):
        board_outline_bounds(parse_string(text))
    with pytest.raises(ValueError, match="Edge.Cuts"):
        load_pcb_for_routing(path, validate_drc=False)

    # Tolerant at load, with the reason retained for consumers that fail closed.
    pcb = PCB.load(path)
    assert "Edge.Cuts" in pcb.outline_error
    assert pcb.board_origin == (0.0, 0.0)
    # Coordinates are left exactly as written -- no guessed-origin subtraction.
    assert pcb.footprints[0].position == (78.5, 65)
    # Consumers that explicitly ask for bounds still get the loud failure.
    with pytest.raises(ValueError, match="Edge.Cuts"):
        _ = pcb.board_size


def test_readable_outline_clears_outline_error(tmp_path):
    path = tmp_path / "ok.kicad_pcb"
    path.write_text(_board('(gr_rect (start 68.5 55) (end 228.5 155) (layer "Edge.Cuts"))'))
    pcb = PCB.load(path)
    assert pcb.outline_error == ""
    assert pcb.board_origin == (68.5, 55)


def test_circle_uses_radius_not_center_end_bbox():
    root = parse_string('(kicad_pcb (gr_circle (center -4 8) (end -1 12) (layer "Edge.Cuts")))')
    assert board_outline_bounds(root) == (-9, 3, 1, 13)


@pytest.mark.parametrize("reverse", [False, True])
def test_arc_includes_extrema_between_control_points(reverse):
    points = [(10 * math.cos(a), 10 * math.sin(a)) for a in (math.pi / 4, math.pi, 7 * math.pi / 4)]
    if reverse:
        points.reverse()
    coords = " ".join(
        f"({tag} {p[0]} {p[1]})" for tag, p in zip(("start", "mid", "end"), points, strict=True)
    )
    root = parse_string(f'(kicad_pcb (gr_arc {coords} (layer "Edge.Cuts")))')
    assert board_outline_bounds(root) == pytest.approx((-10, -10, math.sqrt(50), 10))


def test_cubic_bounds_use_curve_extrema_not_control_polygon():
    root = parse_string(
        '(kicad_pcb (gr_curve (pts (xy 0 0) (xy 0 10) (xy 10 10) (xy 10 0)) (layer "Edge.Cuts")))'
    )
    assert board_outline_bounds(root) == (0, 0, 10, 7.5)


@pytest.mark.parametrize(
    "outline",
    [
        '(gr_arc (start 0 0) (mid 5 5) (end 10 0) (layer "Edge.Cuts"))',
        '(gr_arc (start 0 0) (end 10 0) (angle -90) (layer "Edge.Cuts"))',
        '(gr_curve (pts (xy 0 0) (xy 0 10) (xy 10 10) (xy 10 0)) (layer "Edge.Cuts"))',
    ],
)
def test_routing_rejects_unsupported_curved_edge_obstacles(tmp_path, outline):
    path = tmp_path / "curved.kicad_pcb"
    path.write_text(_board(outline))
    with pytest.raises(ValueError, match="Unsupported routing Edge.Cuts"):
        load_pcb_for_routing(path, validate_drc=False)


@pytest.mark.parametrize("sweep, expected", [(270, (-10, -10, 10, 10)), (-90, (0, -10, 10, 0))])
def test_legacy_arc_bounds_preserve_signed_sweep(sweep, expected):
    root = parse_string(
        f'(kicad_pcb (gr_arc (start 0 0) (end 10 0) (angle {sweep}) (layer "Edge.Cuts")))'
    )
    assert board_outline_bounds(root) == pytest.approx(expected)


@pytest.mark.parametrize("angle", ["", "(angle nan)", "(angle 90 180)", "(angle 0)"])
def test_invalid_legacy_arc_is_rejected(angle):
    root = parse_string(f'(kicad_pcb (gr_arc (start 0 0) (end 10 0) {angle} (layer "Edge.Cuts")))')
    with pytest.raises(ValueError, match="Malformed Edge.Cuts"):
        board_outline_bounds(root)


# ---------------------------------------------------------------------------
# Issue #5367: gr_circle Edge.Cuts must reach the router pad stage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("radius", [0.05, 1.0, 5.0, 50.0, 500.0, 1200.0])
def test_circle_tessellation_chord_error_bounded(radius):
    """Every chord's midpoint sagitta stays within the documented bound.

    ``circle_tessellation_points`` returns vertices exactly on the circle
    (radius unchanged), so the only approximation error is the gap between
    each chord and the arc it replaces (maximal at the chord midpoint).
    """
    center = (3.0, -7.0)
    ring = circle_tessellation_points(center, radius)
    assert len(ring) >= 12
    for a, b in zip(ring, ring[1:] + ring[:1], strict=True):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dist_to_center = math.hypot(mx - center[0], my - center[1])
        sagitta = radius - dist_to_center
        assert -1e-9 <= sagitta <= _CIRCLE_TESSELLATION_MAX_ERROR_MM + 1e-9


def test_circle_segment_count_rejects_bad_radius():
    with pytest.raises(ValueError, match="radius"):
        circle_segment_count(0.0)
    with pytest.raises(ValueError, match="radius"):
        circle_segment_count(-5.0)
    with pytest.raises(ValueError, match="radius"):
        circle_segment_count(math.nan)


def test_circle_segments_form_closed_chain_on_the_true_circle():
    radius = 12.5
    center = (2.0, 4.0)
    root = parse_string(
        f"(kicad_pcb (gr_circle (center {center[0]} {center[1]}) "
        f'(end {center[0] + radius} {center[1]}) (layer "Edge.Cuts")))'
    )
    segments = board_outline_segments(root)
    assert len(segments) >= 12
    for (x1, y1), (x2, y2) in segments:
        assert math.isclose(math.hypot(x1 - center[0], y1 - center[1]), radius, abs_tol=1e-9)
        assert math.isclose(math.hypot(x2 - center[0], y2 - center[1]), radius, abs_tol=1e-9)
    for (_, end), (start, _) in zip(segments, segments[1:] + segments[:1], strict=True):
        assert end == start


def test_circle_zero_radius_is_rejected():
    root = parse_string('(kicad_pcb (gr_circle (center 5 5) (end 5 5) (layer "Edge.Cuts")))')
    with pytest.raises(ValueError, match="zero-radius"):
        board_outline_segments(root)


@pytest.mark.parametrize(
    "circle",
    [
        '(gr_circle (center 5 5) (layer "Edge.Cuts"))',
        '(gr_circle (end 5 5) (layer "Edge.Cuts"))',
        '(gr_circle (center nan 5) (end 10 5) (layer "Edge.Cuts"))',
        '(gr_circle (center 5 5) (end nan 5) (layer "Edge.Cuts"))',
    ],
)
def test_circle_missing_or_nonfinite_coordinate_is_rejected(circle):
    root = parse_string(f"(kicad_pcb {circle})")
    with pytest.raises(ValueError, match="Malformed Edge.Cuts"):
        board_outline_segments(root)


def test_circular_outline_reaches_pad_stage_via_load_pcb_for_routing(tmp_path):
    """Issue #5367: the loader must not refuse a circular Edge.Cuts outline.

    Regression for the reported failure: ``load_pcb_for_routing`` calls
    ``_extract_edge_segments`` (-> ``board_outline_segments``) before any
    footprint/pad parsing, so a ``gr_circle`` outline previously aborted
    the whole load with ``ValueError`` before pads were ever reached.
    """
    cx, cy, radius = 60.0, 60.0, 50.0
    text = f"""(kicad_pcb (version 20241229) (generator "test")
    (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "SIGNAL")
    (gr_circle (center {cx} {cy}) (end {cx + radius} {cy}) (layer "Edge.Cuts"))
    (footprint "R" (layer "F.Cu") (at {cx} {cy})
      (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))
      (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))))"""
    path = tmp_path / "circle.kicad_pcb"
    path.write_text(text)

    edges = _extract_edge_segments(text)
    assert len(edges) >= 12

    router, _ = load_pcb_for_routing(
        path, rules=DesignRules(grid_resolution=0.5, trace_clearance=0.5), validate_drc=False
    )
    assert len(router.pads) == 2


def test_circular_cutout_alongside_rectangular_outer_boundary(tmp_path):
    """A circular internal cutout (e.g. a round mounting hole) on Edge.Cuts
    coexists with a straight outer boundary; both are tessellated/returned
    without the loader treating outline topology specially (#5367).
    """
    text = """(kicad_pcb (version 20241229) (generator "test")
    (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "SIGNAL")
    (gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))
    (gr_circle (center 50 40) (end 55 40) (layer "Edge.Cuts"))
    (footprint "R" (layer "F.Cu") (at 10 10)
      (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))
      (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIGNAL"))))"""
    path = tmp_path / "cutout.kicad_pcb"
    path.write_text(text)

    edges = _extract_edge_segments(text)
    # 4 from the rectangle plus >= 12 tessellated chords from the cutout.
    assert len(edges) >= 16

    router, _ = load_pcb_for_routing(
        path, rules=DesignRules(grid_resolution=0.5, trace_clearance=0.5), validate_drc=False
    )
    assert len(router.pads) == 2


# ---------------------------------------------------------------------------
# Issue #5367 follow-up: the tessellation error must never *hide* a real
# edge-clearance violation.  An inscribed chord chain sits inside the true
# circle, so for an interior cutout the chords are *farther* from the copper
# than the real hole edge -- the direction that under-reports.  The extractor
# therefore certifies the worst-case sagitta on ``OutlineSegments`` and the
# clearance consumers charge it against the measured distance.
# ---------------------------------------------------------------------------

_CUTOUT_CENTER = (10.0, 10.0)
_CUTOUT_RADIUS = 5.0
_EDGE_CLEARANCE = 0.3
_TRACE_HALF_WIDTH = 0.1


@dataclass
class _StubRouter:
    """Minimal stand-in for ``Autorouter`` accepted by ``validate_routes``."""

    routes: list = field(default_factory=list)
    existing_routes: list = field(default_factory=list)
    rules: DesignRules = field(default_factory=DesignRules)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)
    net_class_map: dict | None = None


def _radial_trace(center, start_radius, angle, *, length=2.0):
    """A radial trace whose nearest point sits ``start_radius`` from ``center``."""
    cx, cy = center
    return Segment(
        x1=cx + start_radius * math.cos(angle),
        y1=cy + start_radius * math.sin(angle),
        x2=cx + (start_radius + length) * math.cos(angle),
        y2=cy + (start_radius + length) * math.sin(angle),
        width=2 * _TRACE_HALF_WIDTH,
        layer=Layer.F_CU,
        net=1,
    )


def _edge_violations(edges, segment, clearance=_EDGE_CLEARANCE):
    router = _StubRouter(routes=[Route(net=1, net_name="N", segments=[segment])])
    router._edge_segments = edges
    router._edge_clearance = clearance
    return [v for v in validate_routes(router) if v.obstacle_type == "edge"]


def _cutout_edges():
    """Rectangular outer boundary with a circular cutout in the middle."""
    cx, cy = _CUTOUT_CENTER
    return _extract_edge_segments(
        f'(kicad_pcb (gr_rect (start -50 -50) (end 80 80) (layer "Edge.Cuts")) '
        f"(gr_circle (center {cx} {cy}) (end {cx + _CUTOUT_RADIUS} {cy}) "
        f'(layer "Edge.Cuts")))'
    )


def _chord_midpoint_angle(segments):
    """Direction of the first chord's midpoint -- the worst-case error point."""
    return math.pi / segments


def test_circular_cutout_violation_is_not_hidden_by_chord_offset():
    """The exact case reported in review of #5373.

    Copper outside a 5 mm circular cutout, aimed at the first chord's
    midpoint (where the chord is farthest from the true hole edge), with a
    true clearance of 0.299895001298 mm -- below the effective threshold of
    ``0.3 - _CLEARANCE_EPSILON_MM == 0.2999``.  Measured against the raw
    chords the trace looks like it clears 0.299905 mm and the violation
    vanished; charging the certified sagitta restores it.
    """
    edges = _cutout_edges()
    n = circle_segment_count(_CUTOUT_RADIUS)
    sagitta = circle_sagitta(_CUTOUT_RADIUS, n)

    # The certified error is the real sagitta, and it honours the bound.
    assert edges.max_error_mm == pytest.approx(sagitta, rel=1e-12)
    assert 0 < sagitta <= _CIRCLE_TESSELLATION_MAX_ERROR_MM

    true_clearance = 0.299895001298
    assert true_clearance < _EDGE_CLEARANCE - 1e-4, "scenario must be a real violation"
    # Raw chord distance would read *above* the threshold -- the hiding effect.
    assert true_clearance + sagitta > _EDGE_CLEARANCE - 1e-4

    trace = _radial_trace(
        _CUTOUT_CENTER,
        _CUTOUT_RADIUS + _TRACE_HALF_WIDTH + true_clearance,
        _chord_midpoint_angle(n),
    )
    violations = _edge_violations(edges, trace)
    assert violations, (
        "a trace 0.2999 mm from a circular cutout must report an edge "
        "violation; the inscribed chords must not hide it"
    )
    # The reported distance is the true clearance, not the chord's.
    assert violations[0].distance == pytest.approx(true_clearance, abs=1e-9)


@pytest.mark.parametrize("at_chord_midpoint", [True, False])
def test_circular_cutout_clearance_controls_on_both_sides_of_the_threshold(at_chord_midpoint):
    """Both sides of a chord (midpoint and vertex) keep verdicts correct."""
    edges = _cutout_edges()
    n = circle_segment_count(_CUTOUT_RADIUS)
    # A vertex lies exactly on the circle (zero error); a chord midpoint is
    # the worst case.  Both must agree with the true-circle verdict.
    angle = _chord_midpoint_angle(n) if at_chord_midpoint else 0.0

    def violations_for(true_clearance):
        return _edge_violations(
            edges,
            _radial_trace(
                _CUTOUT_CENTER,
                _CUTOUT_RADIUS + _TRACE_HALF_WIDTH + true_clearance,
                angle,
            ),
        )

    assert violations_for(0.25), "clearly-violating trace must still fail"
    assert not violations_for(0.35), "clearly-passing trace must still pass"
    assert not violations_for(_EDGE_CLEARANCE), (
        "a trace at exactly the nominal clearance must not become a false "
        "positive: the certified error is far below the comparison epsilon"
    )


@pytest.mark.parametrize("at_chord_midpoint", [True, False])
def test_circular_outer_boundary_clearance_verdicts_unchanged(at_chord_midpoint):
    """The cutout fix must not disturb the (already safe) outer-boundary case.

    Here the copper is *inside* the circle, so the chords are nearer the
    copper than the true arc and the check was already conservative.
    """
    cx, cy = _CUTOUT_CENTER
    radius = 20.0
    edges = _extract_edge_segments(
        f'(kicad_pcb (gr_circle (center {cx} {cy}) (end {cx + radius} {cy}) (layer "Edge.Cuts")))'
    )
    n = circle_segment_count(radius)
    angle = _chord_midpoint_angle(n) if at_chord_midpoint else 0.0

    def violations_for(true_clearance):
        # Inward-pointing radial trace whose outer end is the nearest point.
        start_radius = radius - _TRACE_HALF_WIDTH - true_clearance
        return _edge_violations(
            edges, _radial_trace(_CUTOUT_CENTER, start_radius, angle, length=-2.0)
        )

    assert violations_for(0.25), "clearly-violating trace must still fail"
    assert not violations_for(0.35), "clearly-passing trace must still pass"
    assert not violations_for(_EDGE_CLEARANCE), "nominal clearance must still pass"


def test_straight_outlines_certify_zero_approximation_error():
    """Exact geometry must not pay any conservatism tax."""
    straight = _extract_edge_segments(
        '(kicad_pcb (gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts")))'
    )
    assert isinstance(straight, OutlineSegments)
    assert straight.max_error_mm == 0.0

    # Trace exactly at the nominal clearance from a straight edge: the
    # historical verdict (pass) must be bit-for-bit unchanged.
    trace = Segment(
        x1=10.0,
        y1=_EDGE_CLEARANCE + _TRACE_HALF_WIDTH,
        x2=20.0,
        y2=_EDGE_CLEARANCE + _TRACE_HALF_WIDTH,
        width=2 * _TRACE_HALF_WIDTH,
        layer=Layer.F_CU,
        net=1,
    )
    assert not _edge_violations(straight, trace)


def test_edge_keepout_widens_by_the_certified_outline_error():
    """``add_edge_keepout`` paints ``clearance + max_error_mm``.

    A plain ``list`` certifies nothing and keeps the historical radius, so
    boards without curved Edge.Cuts geometry are untouched.
    """
    from kicad_tools.router.grid import RoutingGrid

    edges = [((1.0, 1.0), (9.0, 1.0))]
    clearance = 0.1

    def blocked(segments):
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=DesignRules(grid_resolution=0.01),
            resolution_override=0.01,
        )
        return grid.add_edge_keepout(segments, clearance)

    plain = blocked(list(edges))
    certified = blocked(OutlineSegments(edges, max_error_mm=0.05))
    assert certified > plain, (
        "a certified tessellation error must widen the painted keepout so it "
        "still contains the true-outline keepout"
    )
    assert blocked(OutlineSegments(edges, max_error_mm=0.0)) == plain


# ---------------------------------------------------------------------------
# Issue #5367 follow-up: the segment ceiling is a resource limit, never a
# silent weakening of the documented error bound.
# ---------------------------------------------------------------------------


def test_circle_segment_count_honours_the_bound_right_up_to_the_cap():
    radius = 1200.0  # ~24.3k chords: under the 25k ceiling
    n = circle_segment_count(radius)
    assert _CIRCLE_MIN_SEGMENTS <= n <= _CIRCLE_MAX_SEGMENTS
    assert circle_sagitta(radius, n) <= _CIRCLE_TESSELLATION_MAX_ERROR_MM
    # One chord fewer would breach the bound: the count is not overshooting.
    assert circle_sagitta(radius, n - 1) > _CIRCLE_TESSELLATION_MAX_ERROR_MM


@pytest.mark.parametrize("radius", [1e4, 1e8])
def test_circle_segment_count_refuses_radii_beyond_the_cap(radius):
    """Previously these silently returned 20000 with a sagitta of up to 1.23 mm."""
    with pytest.raises(ValueError, match="more than"):
        circle_segment_count(radius)
    with pytest.raises(ValueError, match="more than"):
        circle_tessellation_points((0.0, 0.0), radius)
    root = parse_string(
        f'(kicad_pcb (gr_circle (center 0 0) (end {radius} 0) (layer "Edge.Cuts")))'
    )
    with pytest.raises(ValueError, match="more than"):
        board_outline_segments(root)


def test_circle_segment_count_survives_the_cancellation_case():
    """``1 - max_error / radius`` used to round to exactly 1.0 here.

    The old ``acos`` formulation then produced a zero half-angle and fell
    back to the ceiling, advertising a 1e-5 mm bound while delivering a
    sagitta of over a millimetre.  The ``asin`` form refuses instead.
    """
    with pytest.raises(ValueError, match="more than"):
        circle_segment_count(1e12, max_error=1e-9)


def test_circle_segment_count_rejects_bad_max_error():
    with pytest.raises(ValueError, match="max_error"):
        circle_segment_count(5.0, max_error=0.0)
    with pytest.raises(ValueError, match="max_error"):
        circle_segment_count(5.0, max_error=math.inf)


def test_circle_segment_count_floor_applies_when_the_bound_is_slack():
    # max_error covering the whole diameter: only the aesthetic floor governs.
    assert circle_segment_count(1.0, max_error=10.0) == _CIRCLE_MIN_SEGMENTS


@pytest.mark.parametrize("segments", [12, 100, 1571, 24336])
def test_circle_sagitta_matches_the_closed_form(segments):
    """``circle_sagitta`` agrees with ``r * (1 - cos(pi / n))``.

    The tolerance widens with ``n`` on purpose: it is the *naive* form that
    degrades (``cos`` approaches 1.0 and the subtraction cancels away the
    significant digits), which is precisely why ``circle_sagitta`` evaluates
    the half-angle identity instead.
    """
    radius = 5.0
    naive = radius * (1 - math.cos(math.pi / segments))
    # Cancellation costs the naive form roughly one digit per decade of n.
    tolerance = 1e-12 * segments
    assert circle_sagitta(radius, segments) == pytest.approx(naive, rel=tolerance, abs=1e-18)
