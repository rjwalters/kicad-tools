"""Pin-geometry slack-budget estimator for length-matched routing.

Issue #4085 (Phase 1, epic #4049 Gap 2).

This module implements a *pure-geometry* estimator that predicts the
length skew a differential pair (or an N-member match group) will
accumulate purely from the placement of its boundary pins, **before**
any routing happens.  The estimate is a Manhattan-distance proxy: the
pathfinder produces 45-degree-quantised routes, but the pin-to-pin
Manhattan span is a cheap, monotone lower bound on the routed length,
and the *difference* between two legs' spans is a good first-order
predictor of the post-route skew that the serpentine tuner must later
close.

The estimate has exactly one consumer in Phase 1:
:meth:`EscapeRouter._reserve_pair_continuation_corridor` widens its
inner-layer continuation corridor by the estimated skew when it exceeds
the pair's net-class skew tolerance, so the serpentine tuner has
already-reserved slack cells to meander into instead of scavenging
whatever leftover space happens to be free.

Design constraints (Phase 1):

* **No grid dependency.**  The estimator operates on world-coordinate
  pin positions only.  It is unit-testable in complete isolation from
  :class:`~kicad_tools.router.grid.RoutingGrid`.
* **No routing state.**  It runs before routing; it cannot consult
  actual routed lengths (that is what
  :class:`~kicad_tools.router.length.LengthTracker` does *after* the
  fact).
* **Monotone and symmetric.**  ``estimate_pair_skew_budget(a, b)``
  equals ``estimate_pair_skew_budget(b, a)`` and is always ``>= 0``.

The LR-style live A* cost term that would consume the reserved slack
*during* the search is explicitly deferred to a follow-up issue -- this
module is only the pre-routing estimator half of that plan.
"""

from __future__ import annotations

Point = tuple[float, float]


def _manhattan_span(pins: list[Point]) -> float:
    """Return the Manhattan span of a leg's pin set.

    The span is ``(max_x - min_x) + (max_y - min_y)`` over the pins --
    the perimeter half of the pins' axis-aligned bounding box.  For the
    common two-pin (source, sink) leg this reduces to the Manhattan
    distance between the two pins, which is the natural lower bound on a
    45-degree-quantised route's length.  For legs with intermediate pins
    (branch/star nets) the bounding-box span is a stable, order-free
    proxy that does not depend on how the pins are permuted.

    Args:
        pins: World-coordinate ``(x, y)`` positions of the leg's pins.
            An empty or single-pin leg has a zero span.

    Returns:
        The Manhattan span in the same units as the input coordinates
        (millimetres for board geometry).  Always ``>= 0``.
    """
    if len(pins) < 2:
        return 0.0
    xs = [p[0] for p in pins]
    ys = [p[1] for p in pins]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def estimate_pair_skew_budget(
    leg_a_pins: list[Point],
    leg_b_pins: list[Point],
) -> float:
    """Estimate the length skew a diff pair will accumulate from geometry.

    Computes the Manhattan span of each leg's pins and returns the
    absolute difference.  This is a pre-routing proxy for the routed
    skew ``|L_p - L_n|`` that the serpentine tuner must later close: a
    pair whose two legs have very different pin-to-pin Manhattan spans
    is structurally destined to route to very different lengths.

    The estimate is intentionally conservative in only one direction --
    it is a *lower bound* on the achievable skew when both legs route
    monotonically, but real routes detour, so the true post-route skew
    is typically larger.  Consumers should treat the returned value as
    the *minimum* slack budget worth reserving, not an exact prediction.

    Args:
        leg_a_pins: World-coordinate pins for the first leg (e.g. the
            P-net's boundary pins).
        leg_b_pins: World-coordinate pins for the second leg (e.g. the
            N-net's boundary pins).

    Returns:
        Estimated skew in millimetres, ``>= 0``.  Symmetric in its
        arguments.  Returns ``0.0`` when either leg has fewer than two
        pins (no span to compare).
    """
    return abs(_manhattan_span(leg_a_pins) - _manhattan_span(leg_b_pins))


def estimate_group_skew_budget(legs_pins: list[list[Point]]) -> float:
    """Estimate the max skew across an N-member match group from geometry.

    Generalises :func:`estimate_pair_skew_budget` to N legs (e.g. a DDR
    data byte): returns the spread ``max(span) - min(span)`` of the
    per-leg Manhattan spans, matching the ``max(L) - min(L)`` skew
    definition used by
    :class:`~kicad_tools.router.match_group_length.MatchGroupTracker`.

    Args:
        legs_pins: A list of per-leg pin lists.  Legs with fewer than
            two pins contribute a zero span.

    Returns:
        Estimated group skew in millimetres, ``>= 0``.  Returns ``0.0``
        for an empty group or a single-member group.
    """
    if len(legs_pins) < 2:
        return 0.0
    spans = [_manhattan_span(pins) for pins in legs_pins]
    return max(spans) - min(spans)
