"""Coupling exemptions retain the exhaustive geometric decision and thresholds."""

import math
import random
from types import SimpleNamespace

import pytest

from kicad_tools.schema.pcb import Segment
from kicad_tools.validate.rules import clearance as rule


def board(segments):
    return SimpleNamespace(
        copper_layers=[SimpleNamespace(name=n) for n in ("F.Cu", "B.Cu")],
        segments_on_layer=lambda layer: [s for s in segments if s.layer == layer],
    )


def segment(net, start, end, width=0.2, layer="F.Cu"):
    return Segment(start=start, end=end, width=width, layer=layer, net_number=net)


def exhaustive(pcb):
    total = 0.0
    for layer in pcb.copper_layers:
        items = pcb.segments_on_layer(layer.name)
        for a in (s for s in items if s.net_number == 1):
            for b in (s for s in items if s.net_number == 2):
                total += rule._segments_are_coupled(
                    a, b, rule._COUPLING_MAX_GAP_MM, rule._COUPLING_ANGLE_TOL_DEG
                )
                if total >= rule._COUPLING_MIN_PARALLEL_LEN_MM:
                    return True
    return False


@pytest.mark.parametrize("gap,expected", [(0.5 - 1e-10, True), (0.5, True), (0.5 + 1e-10, False)])
@pytest.mark.parametrize("reverse", [False, True])
def test_gap_boundary_keeps_wide_copper_and_reversed_segments(gap, expected, reverse):
    start, end = (0.0, 1.75 + gap), (2.0, 1.75 + gap)
    if reverse:
        start, end = end, start
    pcb = board([segment(1, (0.0, 0.0), (2.0, 0.0), 3.0), segment(2, start, end, 0.5)])
    assert exhaustive(pcb) is expected
    assert rule._pair_is_geometrically_coupled(pcb, 1, 2) is expected


@pytest.mark.parametrize("length", [1.0 - 1e-10, 1.0, 1.0 + 1e-10])
def test_cumulative_length_floor_and_layer_partition(length):
    pcb = board([segment(1, (0, 0), (length, 0)), segment(2, (0, 0.3), (length, 0.3))])
    assert rule._pair_is_geometrically_coupled(pcb, 1, 2) == exhaustive(pcb) == (length >= 1)
    pcb = board(
        [segment(1, (0, 0), (length, 0)), segment(2, (0, 0.3), (length, 0.3), layer="B.Cu")]
    )
    assert not rule._pair_is_geometrically_coupled(pcb, 1, 2)


def test_seeded_rotated_translated_and_crossing_geometry_matches_exhaustive():
    rng = random.Random(5240)
    for case in range(100):
        segments = []
        for net in (1, 2):
            for _ in range(12):
                x, y = rng.uniform(-20, 20), rng.uniform(-20, 20)
                angle, length = rng.uniform(-math.pi, math.pi), rng.uniform(0, 10)
                segments.append(
                    segment(
                        net,
                        (x, y),
                        (x + length * math.cos(angle), y + length * math.sin(angle)),
                        rng.uniform(0.05, 2),
                        rng.choice(["F.Cu", "B.Cu"]),
                    )
                )
        if case % 2:
            angle = rng.uniform(-math.pi, math.pi)
            ux, uy = math.cos(angle), math.sin(angle)
            start = (rng.uniform(-20, 20), rng.uniform(-20, 20))
            end = (start[0] + 2 * ux, start[1] + 2 * uy)
            segments += [
                segment(1, start, end),
                segment(
                    2,
                    (start[0] - 0.3 * uy, start[1] + 0.3 * ux),
                    (end[0] - 0.3 * uy, end[1] + 0.3 * ux),
                ),
            ]
        rng.shuffle(segments)
        pcb = board(segments)
        assert rule._pair_is_geometrically_coupled(pcb, 1, 2) == exhaustive(pcb)


def test_sparse_uncoupled_nets_do_not_require_quadratic_exact_checks(monkeypatch):
    pcb = board(
        [
            segment(net, (i * 10, net * 100), (i * 10 + 1, net * 100))
            for net in (1, 2)
            for i in range(100)
        ]
    )
    calls = 0
    original = rule._segments_are_coupled

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(rule, "_segments_are_coupled", counted)
    assert not rule._pair_is_geometrically_coupled(pcb, 1, 2)
    assert calls < 100
    calls = 0
    assert not exhaustive(pcb)
    assert calls == 10_000


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.2])
def test_invalid_width_preserves_existing_exact_behavior(value):
    pcb = board([segment(1, (0, 0), (2, 0), value), segment(2, (0, 0.3), (2, 0.3))])
    assert rule._pair_is_geometrically_coupled(pcb, 1, 2) == exhaustive(pcb)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_invalid_coordinate_retains_exhaustive_candidate_order(value, monkeypatch):
    segments = [
        segment(1, (0, 0), (2, 0)),
        segment(1, (value, 0), (2, 0)),
        segment(2, (100, 100), (102, 100)),
    ]
    calls = []

    def exact(a, b, *_):
        calls.append((a, b))
        return 0.0

    monkeypatch.setattr(rule, "_segments_are_coupled", exact)
    assert not rule._pair_is_geometrically_coupled(board(segments), 1, 2)
    assert calls == [(segments[0], segments[2]), (segments[1], segments[2])]


def test_finite_geometry_with_overflowing_bounds_retains_exact_check(monkeypatch):
    segments = [
        segment(1, (1.7e308, 0), (1.7e308, 1), width=1.7e308),
        segment(2, (0, 100), (2, 100)),
    ]
    calls = []

    def exact(a, b, *_):
        calls.append((a, b))
        return 1.0

    monkeypatch.setattr(rule, "_segments_are_coupled", exact)
    assert rule._pair_is_geometrically_coupled(board(segments), 1, 2)
    assert calls == [(segments[0], segments[1])]


def test_no_shapely_fallback_retains_coupling(monkeypatch):
    from kicad_tools.validate import spatial

    monkeypatch.setattr(spatial, "has_shapely", lambda: False)
    pcb = board([segment(1, (0, 0), (2, 0)), segment(2, (0, 0.3), (2, 0.3))])
    assert rule._pair_is_geometrically_coupled(pcb, 1, 2) == exhaustive(pcb)
