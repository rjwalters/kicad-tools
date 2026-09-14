"""Keep the CPU hot-path optimization identical to the original vector formula."""

import random

import pytest

from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.placement import PlacementOptimizer


def vector_force(point, start, end, charge, minimum):
    """Original force equation, expressed independently with vector operations."""
    edge = end - start
    length = edge.magnitude()
    if length < 1e-10:
        return Vector2D(0.0, 0.0)
    t = (point - start).dot(edge) / (length * length)
    t = max(0.0, min(1.0, t))
    displacement = point - (start + edge * t)
    distance = max(displacement.magnitude(), minimum)
    return displacement.normalized() * (charge * length / (distance * distance))


@pytest.mark.parametrize("minimum", [1e-8, 0.1, 10.0])
def test_cpu_force_matches_vector_equation_exactly(minimum):
    optimizer = PlacementOptimizer(
        Polygon.rectangle(50, 50, 50, 50), PlacementConfig(min_distance=minimum)
    )
    rng = random.Random(5240)
    cases = [
        (Vector2D(x, y), Vector2D(0, 0), Vector2D(1, 0), charge)
        for x in (-1.0, 0.0, 0.5, 1.0, 2.0)
        for y in (0.0, 1e-11, 1e-10, 1e-9, 0.01, 100.0)
        for charge in (-1.0, 0.0, 1.0)
    ]
    for index in range(2000):
        point, start, end = [
            Vector2D(rng.uniform(-100, 100), rng.uniform(-100, 100)) for _ in range(3)
        ]
        if index % 5 == 0:
            end = start
        if index % 7 == 0:
            point = start
        cases.append((point, start, end, rng.uniform(-3, 3)))
    for point, start, end, charge in cases:
        actual = optimizer.compute_edge_to_point_force(point, start, end, charge)
        expected = vector_force(point, start, end, charge, minimum)
        assert (actual.x, actual.y) == (expected.x, expected.y)
