"""Group 1 -- Python grid occupancy, the model A* actually searches on.

Every other adapter here answers with a *distance*.  This one answers with a
*cell set*, which is the whole reason #5410 happened: a via's keep-out is
marked as a Chebyshev square of

    ``radius = int((diameter/2 + via_clearance + trace_width/2) / resolution) + 1``

cells on **every** layer (``grid.py`` ``_mark_via``), and a segment's as a
band of ``int((width/2 + trace_clearance) / resolution) + 1 + 1`` cells around
its raster line (``grid.py`` ``mark_route`` / ``_mark_segment``).  A square
circumscribes the disc it stands for, and each ``int(...) + 1`` rounds
outwards, so the marked region reaches measurably further than the geometry
requires -- far enough to swallow a via that is legal on both copper and drill
counts.

**The rejection rule this adapter measures, stated exactly.**  Register the
pair's counterpart as existing copper (``add_pad`` for a pad, ``mark_route``
for a segment or via).  Mark the *candidate* on a second, otherwise identical
and empty grid, and take the set of cells that marking blocks -- so the cell
set comes from the consumer's own marking code rather than from a formula
re-derived here.  The candidate is **rejected** iff any of those cells is
``is_blocked_for_net(gx, gy, layer, candidate_net)`` on the first grid.  That
is precisely the predicate A* consults before it will step onto a cell, so a
rejection here means the search will not find a path through this pair even
when the copper is legal.

Because the answer is a cell set and not a distance, every verdict this
adapter emits has ``gap_mm = None``.  That is honest: there is no measured
millimetre value to report, and the table's over/under-reject columns compare
*whether* a pair was flagged, never by how much.
"""

from __future__ import annotations

import numpy as np

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    ALL_PAIR_KINDS,
    net_ids,
    pair_contexts,
    router_grid,
    router_pad,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, SegmentSpec

__all__ = ["OccupancyAdapter"]


class OccupancyAdapter:
    """Rejects a candidate whose own marked cells collide with foreign copper."""

    name = "occupancy"
    group = 1
    pair_kinds = ALL_PAIR_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        nets = net_ids(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue

            board = router_grid(case)
            existing = context.existing
            if isinstance(existing, PadSpec):
                board.add_pad(router_pad(existing, nets))
            else:
                board.mark_route(single_object_route(existing, nets))

            candidate = context.candidate
            candidate_net = nets[candidate.net]

            probe = router_grid(case)
            probe.mark_route(single_object_route(candidate, nets))
            cells = np.argwhere(probe._blocked)

            if _collides(board, cells, candidate_net):
                net_a, net_b = context.nets
                required = (
                    board.rules.trace_clearance
                    if isinstance(candidate, SegmentSpec)
                    else board.rules.via_clearance
                )
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # a cell set has no millimetre answer
                        required_mm=required,
                    )
                )
        return found


def _collides(board, cells: np.ndarray, net: int) -> bool:
    """True when any cell the candidate would claim is foreign-blocked."""
    for layer, gy, gx in cells:
        if board.is_blocked_for_net(int(gx), int(gy), int(layer), net):
            return True
    return False
