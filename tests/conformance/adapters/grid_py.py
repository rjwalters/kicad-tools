"""Group 12 -- the Python commit gate, unmodified.

``RoutingGrid``'s three commit-time validators are what stand between a route
A* has found and the copper that gets written::

    validate_segment_clearance(seg, exclude_net)      -> (ok, min_actual, loc)
    validate_via_clearance(via, exclude_net)          -> (ok, min_actual, loc)
    validate_via_to_via_clearance(via, exclude_net)   -> (ok, min_actual, loc)

This adapter registers the pair's *counterpart* as existing copper -- pads via
``add_pad``, segments and vias via ``mark_route`` -- and then asks the gate
about the candidate, exactly as ``Autorouter`` does after a path is found.
Nothing is patched, relaxed or re-implemented.

Two properties of this consumer are visible in its row and are worth naming,
because they are findings rather than adapter artefacts:

**It applies two different requirements depending on which object is the
candidate.**  A segment candidate is compared against ``rules.trace_clearance``;
a via candidate against ``rules.via_clearance``.  On a board where those differ
(0.15 vs 0.20 by default -- see ``fixtures.py``) the same pair of objects gets
two different answers depending on insertion order.  That is #5398, and it is
why :func:`tests.conformance.adapters._support.pair_contexts` pins one order
(via-first) and says so.

**It has no via-vs-pad predicate at all.**  ``validate_via_clearance`` walks
``self.routes``' *segments*; ``validate_via_to_via_clearance`` walks their
*vias*; neither consults ``self._pads``.  The C++ twin does check via-vs-pad
(``grid.cpp`` ``violation_type = 8``).  A ``pad-via`` pair that kicad-cli flags
therefore shows up here as an under-reject -- correctly: in production that
pair is caught by the pad halo in the *occupancy* model (group 1), not by this
gate, so the gate alone would commit it.  Pair kinds are not narrowed to hide
this; it is one of the disagreements the table exists to record.
"""

from __future__ import annotations

import math

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    PairContext,
    net_ids,
    router_grid,
    router_pad,
    router_segment,
    router_via,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec

__all__ = ["GridPyAdapter"]


class GridPyAdapter:
    """Drives ``RoutingGrid``'s commit-time clearance validators."""

    name = "grid_py"
    group = 12
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        nets = net_ids(case)
        found: set[Verdict] = set()
        for context in _contexts(case):
            rejected, actual, required = self._judge(case, context, nets)
            if rejected:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=actual,
                        required_mm=required,
                    )
                )
        return found

    def _judge(
        self,
        case: CopperCase,
        context: PairContext,
        nets: dict[str, int],
    ) -> tuple[bool, float | None, float]:
        """``(rejected, measured_gap_mm, required_mm)`` for one pair."""
        grid = router_grid(case)
        existing = context.existing
        if isinstance(existing, PadSpec):
            grid.add_pad(router_pad(existing, nets))
        else:
            grid.mark_route(single_object_route(existing, nets))

        candidate = context.candidate
        exclude_net = nets[candidate.net]

        if isinstance(candidate, SegmentSpec):
            ok, actual, _ = grid.validate_segment_clearance(
                router_segment(candidate, nets), exclude_net
            )
            return (not ok, _finite(actual), grid.rules.trace_clearance)

        via = router_via(candidate, nets)
        ok_seg, actual_seg, _ = grid.validate_via_clearance(via, exclude_net)
        ok_via, actual_via, _ = grid.validate_via_to_via_clearance(via, exclude_net)
        actual = min(actual_seg, actual_via)
        return (not (ok_seg and ok_via), _finite(actual), grid.rules.via_clearance)


def _contexts(case: CopperCase) -> list[PairContext]:
    from tests.conformance.adapters._support import pair_contexts

    return [c for c in pair_contexts(case) if c.kind in GridPyAdapter.pair_kinds]


def _finite(value: float) -> float | None:
    """``inf`` means "nothing to measure against", which is not a gap."""
    return None if not math.isfinite(value) else value
