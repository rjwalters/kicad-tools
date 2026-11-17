"""Staircase compression must not lay copper back over its own chain (Issue #8603).

On board 07's reviewed copper, ``kct optimize-traces`` raised
``redundant_copper_covered`` from 30 to 46.  Disabling only the staircase pass
dropped it to 16, and every new finding was a ``compress_staircase``
replacement whose leg ran back along a kept neighbour of the same chain:

* a serpentine rung (W, S, W between two N legs) alternates two directions,
  so it reads as a staircase, but any start-to-end shortcut of it lies on the
  N leg before or after it;
* a staircase that ends where the route turns back (a pad stub) got the
  diagonal-first shortcut, whose closing orthogonal leg folds onto the next
  segment, although the orthogonal-first shortcut fits.
"""

from __future__ import annotations

import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
from kicad_tools.router.optimizer.algorithms import compress_staircase
from kicad_tools.router.primitives import Segment

#: Same "lies on the centreline" tolerance as ``redundant_copper_covered``.
COVERED_TOL_MM = 0.001


def _seg(a: tuple[float, float], b: tuple[float, float]) -> Segment:
    return Segment(
        x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=0.2, layer=Layer.F_CU, net=1, net_name="N"
    )


def _path(*pts: tuple[float, float]) -> list[Segment]:
    return [_seg(a, b) for a, b in zip(pts, pts[1:], strict=False)]


def _point_seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    len2 = dx * dx + dy * dy
    if len2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _covered(segments: list[Segment]) -> list[tuple[Segment, Segment]]:
    """Pairs (inner, outer) the ``redundant_copper_covered`` rule would flag."""
    found = []
    for s in segments:
        length = math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        for o in segments:
            if o is s or math.hypot(o.x2 - o.x1, o.y2 - o.y1) <= length + COVERED_TOL_MM:
                continue
            ends = ((s.x1, s.y1), (s.x2, s.y2))
            if all(_point_seg_dist(p, (o.x1, o.y1), (o.x2, o.y2)) <= COVERED_TOL_MM for p in ends):
                found.append((s, o))
                break
    return found


def _endpoints(segments: list[Segment]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (segments[0].x1, segments[0].y1), (segments[-1].x2, segments[-1].y2)


def test_serpentine_rung_is_not_folded_onto_its_neighbours():
    # N leg, then a W-S-W rung, then the next N leg: one turn of a
    # length-tuning meander (board 07 DQ2, F.Cu, 1.44 mm pitch).
    chain = _path((0.0, 5.33), (0.0, 0.0), (-0.72, 0.0), (-0.72, 5.33), (-1.44, 5.33), (-1.44, 0.0))
    assert not _covered(chain)

    out = compress_staircase(chain, OptimizationConfig())

    assert not _covered(out), "staircase shortcut laid copper over the chain's own N leg"
    assert _endpoints(out) == _endpoints(chain)


def test_staircase_into_a_turnback_uses_the_orthogonal_first_shortcut():
    # Approach from the east, a S / SW staircase, then the route turns back
    # north from the staircase's last point (board 07 +5V, F.Cu).
    chain = _path(
        (1.0, 0.0),
        (0.0, 0.0),
        (0.0, 0.15),
        (-0.15, 0.30),
        (-0.15, 0.45),
        (-0.30, 0.60),
        (-0.30, -2.0),
    )
    assert not _covered(chain)

    out = compress_staircase(chain, OptimizationConfig())

    assert not _covered(out), "diagonal-first shortcut folded onto the turn-back leg"
    # The staircase is still compressed: 4 steps become 2 legs.
    assert len(out) == len(chain) - 2
    assert _endpoints(out) == _endpoints(chain)
    # Connected end to end.
    for a, b in zip(out, out[1:], strict=False):
        assert math.isclose(a.x2, b.x1, abs_tol=1e-9) and math.isclose(a.y2, b.y1, abs_tol=1e-9)


def _serpentine_apex() -> tuple[list[Segment], list[Segment]]:
    """Two chains of one net meeting at a serpentine apex 0.2 micron apart.

    Chain sorting (1e-4 mm) keeps them apart (board 07 DQ5, In2.Cu).  Chain
    *b* is a 90-degree-diagonal staircase whose diagonal-first shortcut runs
    straight back along chain *a*.
    """
    a = _path((2.2, 2.2), (0.0, 0.0))
    b = _path((0.0, 0.0002), (0.5, -0.4998), (2.7, 1.7002), (3.2, 1.2002))
    return a, b


def test_staircase_shortcut_does_not_run_back_over_another_chain():
    a, b = _serpentine_apex()
    assert not _covered(a + b)

    out = compress_staircase(b, OptimizationConfig(), other_copper=a)

    assert not _covered(a + out), "shortcut laid copper over the net's other chain"
    assert len(out) == 2  # orthogonal-first shortcut still compresses it
    assert _endpoints(out) == _endpoints(b)


def test_optimize_segments_keeps_the_other_chain_in_view():
    a, b = _serpentine_apex()
    config = OptimizationConfig(
        merge_collinear=False,
        eliminate_zigzags=False,
        convert_45_corners=False,
        pull_tight=False,
        shorten_detours=False,
        remove_jogs=False,
    )

    out = TraceOptimizer(config).optimize_segments(a + b)

    assert not _covered(out)
    assert len(out) == 3


def test_pass_rewrite_folding_back_onto_the_net_is_rejected():
    # Backstop for every pass: a rewrite whose new leg lies on the net's own
    # copper is discarded, a clean rewrite of the same chain is kept.
    chain = _path((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    folded = _path((0.0, 0.0), (1.0, 0.0), (0.5, 0.0), (1.0, 1.0))
    straight = _path((0.0, 0.0), (1.0, 1.0))
    config = OptimizationConfig(
        merge_collinear=False,
        compress_staircase=False,
        convert_45_corners=False,
        pull_tight=False,
        shorten_detours=False,
        remove_jogs=False,
    )

    opt = TraceOptimizer(config)
    opt.eliminate_zigzags = lambda segs: folded  # type: ignore[method-assign]
    assert opt.optimize_segments(list(chain)) == chain

    opt.eliminate_zigzags = lambda segs: straight  # type: ignore[method-assign]
    assert opt.optimize_segments(list(chain)) == straight


def test_plain_staircase_still_compresses_diagonal_first():
    # Regression guard: an ordinary staircase keeps the existing behaviour.
    pts = [(0.0, 0.0)]
    for i in range(12):
        x, y = pts[-1]
        pts.append((x + 0.05, y) if i % 2 == 0 else (x + 0.05, y + 0.05))
    chain = _path(*pts)

    out = compress_staircase(chain, OptimizationConfig())

    assert len(out) == 2
    first = out[0]
    assert math.isclose(abs(first.x2 - first.x1), abs(first.y2 - first.y1), abs_tol=1e-9)
    assert _endpoints(out) == _endpoints(chain)
