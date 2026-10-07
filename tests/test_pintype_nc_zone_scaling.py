"""``_NoConnectZone.build`` is indexed, not O(R*W*J) (issue #6016).

Two guards:

* an equivalence check against the original brute-force flood fill from
  PR #6000 on random sheets, including points sitting exactly on and just
  past the 1e-3 mm tolerance (the hash-grid cell boundary cases);
* an operation-count bound on a 2000-wire / 300-junction sheet.  Counting
  ``_on_segment`` calls instead of wall time keeps the test robust on a
  loaded CI runner: the old code made tens of millions of calls here.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.operations import pintype
from kicad_tools.operations.pintype import (
    _POINT_TOLERANCE,
    _at_wire_end,
    _NoConnectZone,
    _on_segment,
    _segments_touch,
)


def _reference_reached(points, wires, junctions) -> list:
    """The PR #6000 flood fill, verbatim in behaviour."""
    reached = []
    pending = [w for w in wires if any(_at_wire_end(x, y, w) for x, y in points)]
    remaining = [w for w in wires if w not in pending]
    while pending:
        wire = pending.pop()
        reached.append(wire)
        nxt = []
        for other in remaining:
            if _segments_touch(wire, other) or any(
                _on_segment(jx, jy, wire) and _on_segment(jx, jy, other) for jx, jy in junctions
            ):
                pending.append(other)
            else:
                nxt.append(other)
        remaining = nxt
    return reached


def _reference_covers(points, reached, x, y) -> bool:
    tol = _POINT_TOLERANCE
    if any(abs(x - px) <= tol and abs(y - py) <= tol for px, py in points):
        return True
    return any(_at_wire_end(x, y, w) for w in reached)


# Offsets around the tolerance: exact, inside, on the boundary, just outside,
# and around the 4e-3 mm hash-cell size.
_JITTER = [0.0, 0.0004, -0.0004, 0.001, -0.001, 0.0011, -0.0011, 0.0039, 0.004, 0.0041]


def _pt(rnd: random.Random, grid: int) -> tuple[float, float]:
    return (
        rnd.randint(0, grid) * 1.27 + rnd.choice(_JITTER),
        rnd.randint(0, grid) * 1.27 + rnd.choice(_JITTER),
    )


def _random_sheet(seed: int):
    rnd = random.Random(seed)
    grid = rnd.choice([3, 5, 8])
    wires = []
    for _ in range(rnd.randint(1, 25)):
        x1, y1 = _pt(rnd, grid)
        kind = rnd.random()
        if kind < 0.4:  # horizontal
            x2, y2 = _pt(rnd, grid)[0], y1
        elif kind < 0.8:  # vertical
            x2, y2 = x1, _pt(rnd, grid)[1]
        elif kind < 0.9:  # diagonal
            x2, y2 = _pt(rnd, grid)
        else:  # zero length
            x2, y2 = x1, y1
        wires.append((x1, y1, x2, y2))
    if wires and rnd.random() < 0.3:
        wires.append(rnd.choice(wires))  # exact duplicate
    junctions = [_pt(rnd, grid) for _ in range(rnd.randint(0, 8))]
    # Junctions on wire interiors, so junction-joined T's actually occur.
    for x1, y1, x2, y2 in rnd.sample(wires, min(len(wires), 4)):
        t = rnd.random()
        junctions.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    points = [_pt(rnd, grid) for _ in range(rnd.randint(1, 4))]
    points += [(w[0], w[1]) for w in rnd.sample(wires, min(len(wires), 2))]
    probes = [_pt(rnd, grid) for _ in range(30)]
    probes += [(w[2], w[3]) for w in wires]
    return points, wires, junctions, probes


@pytest.mark.parametrize("seed", range(400))
def test_indexed_build_matches_brute_force(seed: int) -> None:
    points, wires, junctions, probes = _random_sheet(seed)
    zone = _NoConnectZone.build(points, wires, junctions)
    expected = _reference_reached(points, wires, junctions)
    assert sorted(zone.wires) == sorted(expected)
    for x, y in probes:
        assert zone.covers(x, y) is _reference_covers(points, expected, x, y), (x, y)


def test_non_finite_coordinates_match_brute_force() -> None:
    nan, inf = float("nan"), float("inf")
    wires = [
        (0.0, 0.0, 2.54, 0.0),
        (2.54, 0.0, nan, nan),
        (inf, 0.0, 2.54, 0.0),
        (1.27, 0.0, 1.27, 5),
    ]
    junctions = [(nan, 0.0), (inf, inf), (1.27, 0.0)]
    points = [(0.0, 0.0), (nan, nan)]
    zone = _NoConnectZone.build(points, wires, junctions)
    expected = _reference_reached(points, wires, junctions)
    assert len(zone.wires) == len(expected) == 4
    for x, y in [(2.54, 0.0), (1.27, 5.0), (nan, nan), (inf, 0.0), (9.0, 9.0)]:
        assert zone.covers(x, y) is _reference_covers(points, expected, x, y)


def _dense_sheet(flood: bool):
    """2000 wires / 300 junctions: one long chain, or scattered stubs."""
    rnd = random.Random(6016)
    wires, junctions = [], []
    if flood:
        x = y = 0.0
        for i in range(2000):
            nx, ny = (x + 2.54, y) if i % 2 == 0 else (x, y + 2.54)
            wires.append((x, y, nx, ny))
            x, y = nx, ny
        junctions = [wires[i][:2] for i in range(0, 2000, 7)][:300]
        points = [(0.0, 0.0)]
    else:
        for _ in range(2000):
            x, y = rnd.randint(0, 4000) * 0.635, rnd.randint(0, 4000) * 0.635
            length = rnd.randint(1, 20) * 1.27
            wires.append((x, y, x + length, y) if rnd.random() < 0.5 else (x, y, x, y + length))
        junctions = [wires[i][:2] for i in range(0, 900, 3)]
        points = []
        for k in range(30):
            x = 5000 + k * 5.08
            wires.append((x, 0.0, x, 2.54))
            points.append((x, 0.0))
    return points, wires, junctions


@pytest.mark.parametrize("flood", [True, False], ids=["flood", "stubs"])
def test_build_does_bounded_segment_work(monkeypatch, flood: bool) -> None:
    points, wires, junctions = _dense_sheet(flood)
    calls = 0
    real = pintype._on_segment

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pintype, "_on_segment", counting)
    zone = _NoConnectZone.build(points, wires, junctions)
    # Each junction is tested only against the few wires sharing its grid
    # cell; the brute-force flood fill made O(R * W * J) calls.
    assert calls <= 20 * len(junctions)
    if flood:
        assert len(zone.wires) == len(wires)
    else:
        assert len(zone.wires) >= 30
