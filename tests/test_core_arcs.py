"""Direct tests for the shared start/mid/end arc geometry (issue #5882).

``kicad_tools.core.arcs`` is the one implementation of the circumcircle +
swept-direction + axis-extreme math that both ``core.board_outline`` (outline
bounds, refuses a degenerate arc) and ``router.fixed_copper`` (conservative
pad-copper bounds, degrades to the authored points) call. Those two consumers
are covered end to end by ``test_routing_outline_bounds.py`` and
``test_fixed_custom_pad_copper.py``; these tests pin the shared primitive
itself, including the part neither consumer can observe directly: that the
degeneracy *policy* is the caller's, because the helper only ever reports
``None``.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.arcs import ArcSweep, arc_sweep


def _on(center: tuple[float, float], radius: float, degrees: float) -> tuple[float, float]:
    angle = math.radians(degrees)
    return (center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))


def test_circumcircle_is_recovered_from_three_points_on_it():
    center, radius = (108.0, -105.0), 4.25
    sweep = arc_sweep(*(_on(center, radius, d) for d in (10.0, 70.0, 130.0)))
    assert sweep is not None
    assert sweep.center == pytest.approx(center, abs=1e-9)
    assert sweep.radius == pytest.approx(radius, abs=1e-9)


@pytest.mark.parametrize(
    "start_deg, sweep_deg",
    [(0.0, 90.0), (0.0, -90.0), (0.0, 270.0), (0.0, -270.0), (37.0, 179.0), (37.0, -179.0)],
)
def test_mid_selects_the_direction_and_magnitude_of_the_sweep(start_deg, sweep_deg):
    """``mid`` is the only thing distinguishing an arc from its complement."""
    center, radius = (-3.0, 11.0), 2.0
    points = [_on(center, radius, start_deg + sweep_deg * t) for t in (0.0, 0.5, 1.0)]
    sweep = arc_sweep(*points)
    assert sweep is not None
    assert sweep.counter_clockwise == (sweep_deg > 0)
    assert sweep.sweep == pytest.approx(math.radians(abs(sweep_deg)), abs=1e-9)
    assert sweep.start_angle == pytest.approx(math.radians(start_deg), abs=1e-9)


@pytest.mark.parametrize(
    "start_deg, sweep_deg, expected",
    [
        # A quarter from +x to +y reaches exactly those two axis extremes.
        (0.0, 90.0, [(1.0, 0.0), (0.0, 1.0)]),
        # Its complement travels the other way round and reaches all four.
        (0.0, -270.0, [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)]),
        # A sweep strictly inside one quadrant reaches no extreme at all.
        (10.0, 70.0, []),
        # Ending exactly on an extreme still reaches it (bounding geometry).
        (-90.0, 90.0, [(0.0, -1.0), (1.0, 0.0)]),
    ],
)
def test_axis_extremes_are_only_the_ones_the_sweep_reaches(start_deg, sweep_deg, expected):
    center, radius = (0.0, 0.0), 1.0
    points = [_on(center, radius, start_deg + sweep_deg * t) for t in (0.0, 0.5, 1.0)]
    sweep = arc_sweep(*points)
    assert sweep is not None
    extremes = sweep.axis_extreme_points()
    assert len(extremes) == len(expected)
    for got, want in zip(sorted(extremes), sorted(expected), strict=True):
        assert got == pytest.approx(want, abs=1e-9)


def test_axis_extremes_bound_a_sampled_arc_they_cannot_be_read_off_its_points():
    """The bound contains the real arc, which the authored points do not."""
    center, radius = (108.0, 105.0), 1.0
    points = [_on(center, radius, 0.0 + 270.0 * t) for t in (0.0, 0.5, 1.0)]
    sweep = arc_sweep(*points)
    assert sweep is not None
    bound = [*points, *sweep.axis_extreme_points()]
    xs = [p[0] for p in bound]
    ys = [p[1] for p in bound]
    for i in range(721):
        px, py = _on(center, radius, 270.0 * i / 720)
        assert min(xs) - 1e-12 <= px <= max(xs) + 1e-12
        assert min(ys) - 1e-12 <= py <= max(ys) + 1e-12
    # The authored points alone would have left the +y and -x bulges exposed.
    hull_xs = [p[0] for p in points]
    assert min(hull_xs) > min(xs)


@pytest.mark.parametrize(
    "points",
    [
        [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],  # collinear, evenly spaced
        [(0.0, 0.0), (3.0, 3.0), (1.0, 1.0)],  # collinear, mid outside the span
        [(5.0, 5.0), (5.0, 5.0), (5.0, 5.0)],  # fully coincident
        [(0.0, 0.0), (1.0, 1.0), (0.0, 0.0)],  # closed: no triangle to circle
    ],
)
def test_degenerate_points_report_no_circle_instead_of_choosing_a_policy(points):
    """The helper never raises and never guesses -- the caller decides."""
    assert arc_sweep(*points) is None


def test_non_finite_coordinates_report_no_circle():
    assert arc_sweep((0.0, 0.0), (float("nan"), 1.0), (2.0, 0.0)) is None
    assert arc_sweep((0.0, 0.0), (1.0, 1.0), (float("inf"), 0.0)) is None


def test_reaches_treats_a_touched_angle_as_reached():
    sweep = ArcSweep(
        center=(0.0, 0.0),
        radius=1.0,
        start_angle=0.0,
        sweep=math.pi / 2.0,
        counter_clockwise=True,
    )
    assert sweep.reaches(0.0)
    assert sweep.reaches(math.pi / 2.0)
    assert not sweep.reaches(math.pi)
    # Tolerance is slack on the sweep, not a redefinition of the interval.
    assert not sweep.reaches(math.pi / 2.0 + 1e-6)
    assert sweep.reaches(math.pi, tolerance=math.pi)


def test_point_at_stays_on_the_circle():
    sweep = arc_sweep((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0))
    assert sweep is not None
    for degrees in (0.0, 45.0, 123.5, -200.0):
        px, py = sweep.point_at(math.radians(degrees))
        assert math.hypot(px - sweep.center[0], py - sweep.center[1]) == pytest.approx(
            sweep.radius, abs=1e-12
        )
