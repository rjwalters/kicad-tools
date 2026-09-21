"""Group 11 -- the escape router / stitcher world-coordinate predicates.

``router/via_clearance.py`` is the one place in the tree where the clearance
arithmetic is *already* free-standing: four pure functions over plain tuples,
shared by the escape router, the stitcher, the diff-pair fan-out and the
post-route nudge.  All four are driven here:

* ``point_clear_of_copper`` (``:250``) -- the canonical via-placement
  predicate.  Answers a candidate **via** against foreign tracks, vias and
  pads (and, when the caller opts in, drills).
* ``segment_clears_foreign_via`` (``:528``) -- a candidate **segment**
  against a foreign via.
* ``via_clears_foreign_segment`` (``:610``) -- the symmetric direction.
* ``drill_hole_to_hole_clear`` (``:90``) -- drill edge-to-edge, the
  ``hole_to_hole_clearance`` pre-check.

Each pair is routed to whichever of the four production consults for that
shape, so the row is the *union* verdict of the module as a consumer, not one
arbitrary function's.

**Pad copper is measured through the rect-aware 5-tuple.**
``point_clear_of_copper`` accepts a foreign pad either as a disc-bound
``(x, y, radius, net)`` 4-tuple or as a rect-aware
``(x, y, width, height, net)`` 5-tuple (issue #2951).  The 5-tuple is the
newer, tighter form and the one the stitcher passes today, so it is what this
row measures -- and its *axis-aligned* rectangle is the interesting part: the
probe pads are rotated, and the 5-tuple carries no angle, so a rotated pad is
scored against an unrotated box of its local size.  That is the consumer's
real model, reproduced faithfully rather than corrected.

**Pair kinds: every kind with a via on one side, plus nothing else.**
``seg-seg`` has no entry point here (the module is about vias and the copper
around them); ``pad-seg`` has none either -- a candidate segment is only
compared against a foreign *via*, never a pad.  So the scope is ``seg-via``,
``via-via`` and ``pad-via``.

**Epic §1 also lists ``router/subgrid.py`` under this group and it is *not*
measured here.**  ``SubGridRouter._min_clearance_to_neighbors``
(``subgrid.py:550``) and ``_validate_segment_relaxed`` (``:1063``) are private
methods of a class whose construction requires a live sub-grid escape context
(a parent grid, a pad-cluster bounding box and a per-pad override map), none
of which a two-object conformance case can supply without inventing a
production configuration.  Reaching into them would measure a harness fiction.
They are recorded as a ``not measured`` sub-note on this row and belong to the
escape router's own Phase 3 PR, which has the context to build them.
"""

from __future__ import annotations

from tests.conformance.adapters import (
    KIND_CLEARANCE,
    KIND_HOLE_TO_HOLE,
    Verdict,
)
from tests.conformance.adapters._support import (
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    router_segment,
    router_via,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec, ViaSpec

__all__ = ["ViaClearanceAdapter"]

#: Every kind with a via on one side -- see the module docstring.
VIA_BEARING_KINDS = frozenset({PairKind.SEG_VIA, PairKind.VIA_VIA, PairKind.PAD_VIA})


class ViaClearanceAdapter:
    """Drives the four pure predicates in ``router/via_clearance.py``."""

    name = "via_clearance"
    group = 11
    pair_kinds = VIA_BEARING_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.router.via_clearance import (
            drill_hole_to_hole_clear,
            point_clear_of_copper,
            segment_clears_foreign_via,
            via_clears_foreign_segment,
        )

        nets = net_ids(case)
        rules = router_rules(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            existing, candidate = context.existing, context.candidate
            net_a, net_b = context.nets

            if isinstance(candidate, SegmentSpec):
                # seg-via, via-first: a candidate trace against a foreign via.
                assert isinstance(existing, ViaSpec)
                clear = segment_clears_foreign_via(
                    router_segment(candidate, nets),
                    router_via(existing, nets),
                    rules.trace_clearance,
                )
                if not clear:
                    found.add(
                        Verdict.pair(
                            KIND_CLEARANCE,
                            net_a,
                            net_b,
                            gap_mm=None,
                            required_mm=rules.trace_clearance,
                        )
                    )
                continue

            # Everything else is a candidate VIA.
            assert isinstance(candidate, ViaSpec)
            via = router_via(candidate, nets)

            if isinstance(existing, SegmentSpec):
                # Both directions of the symmetric segment/via pair are
                # production predicates and both are consulted here; the
                # module documents them as sharing one distance computation,
                # so a divergence would be a bug in this group, not a
                # disagreement with kicad-cli.
                seg = router_segment(existing, nets)
                clear = via_clears_foreign_segment(
                    via, seg, rules.trace_clearance
                ) and point_clear_of_copper(
                    via.x,
                    via.y,
                    via.diameter,
                    rules.via_clearance,
                    other_net_tracks=[_TrackView(seg)],
                )
            elif isinstance(existing, ViaSpec):
                other = router_via(existing, nets)
                clear = point_clear_of_copper(
                    via.x,
                    via.y,
                    via.diameter,
                    rules.via_clearance,
                    other_net_vias=[(other.x, other.y, other.diameter, other.net)],
                )
                if clear and not drill_hole_to_hole_clear(
                    via.x,
                    via.y,
                    via.drill,
                    [(other.x, other.y, other.drill)],
                    rules.min_hole_to_hole,
                ):
                    found.add(
                        Verdict.pair(
                            KIND_HOLE_TO_HOLE,
                            net_a,
                            net_b,
                            gap_mm=None,
                            required_mm=rules.min_hole_to_hole,
                        )
                    )
            else:
                assert isinstance(existing, PadSpec)
                pad = router_pad(existing, nets)
                clear = point_clear_of_copper(
                    via.x,
                    via.y,
                    via.diameter,
                    rules.via_clearance,
                    # Rect-aware 5-tuple: the form the stitcher passes today.
                    other_net_pads=[(pad.x, pad.y, pad.width, pad.height, pad.net)],
                )

            if not clear:
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the predicates answer yes/no
                        required_mm=rules.via_clearance,
                    )
                )
        return found


class _TrackView:
    """``TrackSegmentLike`` adapter over a router ``Segment``.

    ``point_clear_of_copper`` reads foreign tracks structurally, through
    ``start_x`` / ``start_y`` / ``end_x`` / ``end_y`` / ``width`` -- the
    stitcher's field names, not the router primitive's ``x1`` / ``y1``.  This
    is the rename, and nothing else.
    """

    __slots__ = ("end_x", "end_y", "start_x", "start_y", "width")

    def __init__(self, segment) -> None:
        self.start_x = segment.x1
        self.start_y = segment.y1
        self.end_x = segment.x2
        self.end_y = segment.y2
        self.width = segment.width
