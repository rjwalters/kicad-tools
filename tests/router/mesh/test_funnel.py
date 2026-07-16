"""Funnel string-pull unit tests (issue #4268).

Ports the two synthetic corridors the P0.5 spike used to validate the Simple
Stupid Funnel before trusting it on the board:

* a straight channel must collapse to two points (start, goal), and
* an L-channel must pull taut around the inner reflex corner.
"""

from __future__ import annotations

import math

from kicad_tools.router.mesh.funnel import string_pull
from kicad_tools.router.mesh.geometry import dist


def _length(path: list[tuple[float, float]]) -> float:
    return sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def test_straight_channel_collapses_to_two_points() -> None:
    """A straight open channel yields exactly [start, goal]."""
    portals: list[tuple[tuple[float, float], tuple[float, float]]] = [
        ((0.0, 0.0), (0.0, 0.0)),  # start
    ]
    for x in range(1, 10):
        portals.append(((float(x), 1.0), (float(x), -1.0)))  # left top, right bottom
    portals.append(((10.0, 0.0), (10.0, 0.0)))  # goal

    path = string_pull(portals)

    assert len(path) == 2
    assert path[0] == (0.0, 0.0)
    assert path[-1] == (10.0, 0.0)
    assert math.isclose(_length(path), 10.0, abs_tol=1e-9)


def test_l_channel_pulls_taut_around_inner_corner() -> None:
    """An L-shaped corridor pulls taut around the inner reflex corner (4, 2)."""
    portals = [
        ((1.0, 1.0), (1.0, 1.0)),  # start inside horizontal leg
        ((2.0, 2.0), (2.0, 0.0)),  # travel +x: left=top, right=bottom
        ((4.0, 2.0), (4.0, 0.0)),  # inner corner is the top vertex here
        ((4.0, 2.0), (6.0, 2.0)),  # travel +y: left=x4 (inner corner), right=x6
        ((4.0, 4.0), (6.0, 4.0)),
        ((4.0, 6.0), (6.0, 6.0)),
        ((5.0, 7.0), (5.0, 7.0)),  # goal inside vertical leg
    ]

    path = string_pull(portals)

    assert path[0] == (1.0, 1.0)
    assert path[-1] == (5.0, 7.0)
    # The taut path bends exactly once, at the inner reflex corner.
    assert len(path) == 3
    assert path[1] == (4.0, 2.0)
    # And it is shorter than routing through any portal midpoint sequence.
    straight = dist((1.0, 1.0), (4.0, 2.0)) + dist((4.0, 2.0), (5.0, 7.0))
    assert math.isclose(_length(path), straight, abs_tol=1e-9)
