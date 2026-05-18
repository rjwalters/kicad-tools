"""Tests for the Issue #3028 Part A DRC-nudge foreign-via destination gate.

The pre-PR-#3028 ``_nudge_segment_with_chain`` had no collision-checker
and no foreign-net via validation -- its only guards were SAME-NET pad-
and via-anchor protections.  This meant a nudge translating a segment
by ``(deficit + margin)`` to repair ONE specific seg-vs-seg or seg-vs-
pad violation could land that segment in violation of a *different*
foreign-net via (the board-04 SWDIO/BOOT0 violation at PCB (143.8,
119.7) on B.Cu was the strong empirical suspect for this failure mode).

The fix wires the existing :func:`segment_clears_foreign_via` predicate
(PR #2999 / PR #3006 / PR #3019 / PR #3027) into the post-nudge step:
if the candidate position would clip ANY foreign-net via in the
router's routes, the nudge is REVERTED and the structured skip-reason
``foreign_via_blocked`` is recorded.

The predicate reuse keeps the 4-quadrant clearance matrix's geometry
consistent across the routing pipeline -- the same threshold the
negotiated loop uses to admit a candidate segment is also the threshold
the post-route nudge uses to REFUSE a candidate position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_tools.router.drc_nudge import (
    DRCNudgeResult,
    _nudge_segment_with_chain,
    _post_nudge_introduces_foreign_via_violation,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


# ---------------------------------------------------------------------------
# Lightweight stub for Autorouter (mirrors test_drc_nudge.py's _StubAutorouter)
# ---------------------------------------------------------------------------

@dataclass
class _StubAutorouter:
    """Minimal stand-in for Autorouter used by drc_nudge."""

    routes: list[Route] = field(default_factory=list)
    existing_routes: list[Route] = field(default_factory=list)
    rules: DesignRules = field(default_factory=DesignRules)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)


def _board04_rules() -> DesignRules:
    """DesignRules tuned to mirror board-04's clearance regime
    (matches the values used by ``tests/test_main_router_via_segment_clearance.py``)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


# ---------------------------------------------------------------------------
# Predicate: _post_nudge_introduces_foreign_via_violation
# ---------------------------------------------------------------------------

class TestPostNudgeForeignViaPredicate:
    """Unit tests for the predicate that powers the destination gate."""

    def test_clear_segment_returns_false(self):
        """A segment with no nearby foreign-net via should return False."""
        rules = _board04_rules()
        # Far-apart geometry: seg on B.Cu, via on B.Cu, 5mm apart.
        seg = Segment(
            x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.B_CU, net=10,
        )
        foreign_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20,
        )
        foreign_route = Route(net=20, net_name="BOOT0", vias=[foreign_via])
        router = _StubAutorouter(routes=[foreign_route], rules=rules)
        assert not _post_nudge_introduces_foreign_via_violation(seg, router)

    def test_clipping_segment_returns_true(self):
        """A segment that clips a foreign-net via returns True."""
        rules = _board04_rules()
        # Seg passes 0.1 mm from foreign via centre on B.Cu (negative
        # edge-to-edge clearance with via_radius=0.3, half_seg=0.1,
        # trace_clearance=0.15).
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=10,
        )
        foreign_via = Via(
            x=5.0, y=5.1, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20,
        )
        foreign_route = Route(net=20, net_name="BOOT0", vias=[foreign_via])
        router = _StubAutorouter(routes=[foreign_route], rules=rules)
        assert _post_nudge_introduces_foreign_via_violation(seg, router)

    def test_same_net_via_ignored(self):
        """A same-net via must not trigger the violation predicate.

        Mirrors the caller-side same-net filter convention used by the
        in-loop 4-quadrant clearance matrix.
        """
        rules = _board04_rules()
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=10,
        )
        same_net_via = Via(
            x=5.0, y=5.1, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=10,
        )
        same_route = Route(net=10, net_name="SWDIO", vias=[same_net_via])
        router = _StubAutorouter(routes=[same_route], rules=rules)
        # Even though geometry would violate, same-net is skipped.
        assert not _post_nudge_introduces_foreign_via_violation(seg, router)

    def test_other_layer_via_ignored(self):
        """A via whose layer span excludes the segment's layer is ignored."""
        rules = _board04_rules()
        # Segment on B.Cu, via spans F.Cu only (a hypothetical blind via).
        seg = Segment(
            x1=0.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=10,
        )
        f_only_via = Via(
            x=5.0, y=5.1, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.F_CU), net=20,
        )
        foreign_route = Route(net=20, net_name="BOOT0", vias=[f_only_via])
        router = _StubAutorouter(routes=[foreign_route], rules=rules)
        # Layer overlap check: via_lo=via_hi=F.Cu(0), seg.layer=B.Cu(31).
        # Predicate returns True (clears).  Our wrapper returns False.
        assert not _post_nudge_introduces_foreign_via_violation(seg, router)


# ---------------------------------------------------------------------------
# Gate: _nudge_segment_with_chain
# ---------------------------------------------------------------------------

class TestNudgeForeignViaGate:
    """Integration tests for the destination gate in _nudge_segment_with_chain."""

    def test_nudge_blocked_by_foreign_via(self):
        """A nudge whose destination would clip a foreign-net via must be REFUSED.

        Scenario: SWDIO segment on B.Cu at y=5.5 nudged downward by 0.4mm
        toward y=5.1 would land within (radius + half_seg + clearance) of
        a BOOT0 foreign-net via at y=5.0.  The pre-PR-#3028 nudge would
        commit the move silently; the new gate REVERTS and reports
        ``foreign_via_blocked``.
        """
        rules = _board04_rules()
        # SWDIO segment on B.Cu, candidate nudge direction = -y.
        swdio_seg = Segment(
            x1=0.0, y1=5.5, x2=10.0, y2=5.5,
            width=0.2, layer=Layer.B_CU, net=10,
        )
        swdio_route = Route(net=10, net_name="SWDIO", segments=[swdio_seg])
        # BOOT0 via 0.5 mm below the nudge target -- well within the
        # clearance threshold (required = 0.3 + 0.1 + 0.15 = 0.55 mm).
        boot0_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20, net_name="BOOT0",
        )
        boot0_route = Route(net=20, net_name="BOOT0", vias=[boot0_via])
        router = _StubAutorouter(
            routes=[swdio_route, boot0_route], rules=rules,
        )
        result = DRCNudgeResult()

        # Attempt to nudge -0.4 in y.  Without the gate this would land
        # swdio_seg at y=5.1 (0.1 mm from BOOT0 via centre).
        success = _nudge_segment_with_chain(
            swdio_seg, 0.0, -1.0, 0.4, router, result=result,
        )

        assert success is False
        assert result.skipped.get("foreign_via_blocked", 0) == 1
        # Segment must be UNCHANGED.
        assert math.isclose(swdio_seg.y1, 5.5)
        assert math.isclose(swdio_seg.y2, 5.5)

    def test_nudge_proceeds_when_no_foreign_via_in_path(self):
        """A nudge whose destination is clear of foreign-net vias must PROCEED."""
        rules = _board04_rules()
        swdio_seg = Segment(
            x1=0.0, y1=5.5, x2=10.0, y2=5.5,
            width=0.2, layer=Layer.B_CU, net=10,
        )
        swdio_route = Route(net=10, net_name="SWDIO", segments=[swdio_seg])
        # BOOT0 via FAR from the nudge target (5mm away).
        boot0_via = Via(
            x=50.0, y=50.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20, net_name="BOOT0",
        )
        boot0_route = Route(net=20, net_name="BOOT0", vias=[boot0_via])
        router = _StubAutorouter(
            routes=[swdio_route, boot0_route], rules=rules,
        )
        result = DRCNudgeResult()

        success = _nudge_segment_with_chain(
            swdio_seg, 0.0, -1.0, 0.4, router, result=result,
        )

        assert success is True
        assert "foreign_via_blocked" not in result.skipped
        # Segment moved to y=5.1.
        assert math.isclose(swdio_seg.y1, 5.1)
        assert math.isclose(swdio_seg.y2, 5.1)

    def test_nudge_proceeds_when_same_net_via_in_path(self):
        """A same-net via must NOT block the nudge."""
        rules = _board04_rules()
        swdio_seg = Segment(
            x1=0.0, y1=5.5, x2=10.0, y2=5.5,
            width=0.2, layer=Layer.B_CU, net=10,
        )
        # Same-net via close to the nudge target -- this is a chain
        # situation, NOT a foreign clearance violation.
        same_net_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=10, net_name="SWDIO",
        )
        swdio_route = Route(
            net=10, net_name="SWDIO",
            segments=[swdio_seg],
            vias=[same_net_via],
        )
        router = _StubAutorouter(routes=[swdio_route], rules=rules)
        result = DRCNudgeResult()

        # The chain-aware nudge will refuse if the segment is via-anchored
        # to seg.x1/seg.y1 or seg.x2/seg.y2 (the existing via-anchor guard).
        # The same-net via above is at (5, 5), not on either endpoint, so
        # the via-anchor guard does not fire and the nudge can proceed.
        # The new foreign-via gate must ALSO let this through.
        success = _nudge_segment_with_chain(
            swdio_seg, 0.0, -1.0, 0.4, router, result=result,
        )

        assert success is True
        assert "foreign_via_blocked" not in result.skipped
        assert math.isclose(swdio_seg.y1, 5.1)

    def test_revert_preserves_chain(self):
        """A blocked nudge must revert the segment AND leave the chain intact.

        Three-segment chain: seg_a, seg_b, seg_c connected end-to-end on
        B.Cu.  Nudging seg_b (the middle one) into a foreign via must
        revert seg_b to its original position and leave seg_a/seg_c
        untouched (since the chain snap had not yet run).
        """
        rules = _board04_rules()
        seg_a = Segment(
            x1=0.0, y1=5.5, x2=2.0, y2=5.5, width=0.2, layer=Layer.B_CU, net=10,
        )
        seg_b = Segment(
            x1=2.0, y1=5.5, x2=8.0, y2=5.5, width=0.2, layer=Layer.B_CU, net=10,
        )
        seg_c = Segment(
            x1=8.0, y1=5.5, x2=10.0, y2=5.5, width=0.2, layer=Layer.B_CU, net=10,
        )
        swdio_route = Route(
            net=10, net_name="SWDIO", segments=[seg_a, seg_b, seg_c],
        )
        boot0_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20, net_name="BOOT0",
        )
        boot0_route = Route(net=20, net_name="BOOT0", vias=[boot0_via])
        router = _StubAutorouter(
            routes=[swdio_route, boot0_route], rules=rules,
        )
        result = DRCNudgeResult()

        success = _nudge_segment_with_chain(
            seg_b, 0.0, -1.0, 0.4, router, result=result,
        )

        assert success is False
        # All three segments must be UNCHANGED.
        assert math.isclose(seg_a.x1, 0.0) and math.isclose(seg_a.x2, 2.0)
        assert math.isclose(seg_a.y1, 5.5) and math.isclose(seg_a.y2, 5.5)
        assert math.isclose(seg_b.x1, 2.0) and math.isclose(seg_b.x2, 8.0)
        assert math.isclose(seg_b.y1, 5.5) and math.isclose(seg_b.y2, 5.5)
        assert math.isclose(seg_c.x1, 8.0) and math.isclose(seg_c.x2, 10.0)
        assert math.isclose(seg_c.y1, 5.5) and math.isclose(seg_c.y2, 5.5)

    def test_no_result_argument_still_works(self):
        """The gate must not crash when ``result`` is None (legacy callers).

        Some seg-vs-seg / seg-vs-via / seg-vs-pad call sites call the
        chain-aware nudge without a ``result``.  The gate must still
        revert and return False but record nothing.
        """
        rules = _board04_rules()
        swdio_seg = Segment(
            x1=0.0, y1=5.5, x2=10.0, y2=5.5,
            width=0.2, layer=Layer.B_CU, net=10,
        )
        swdio_route = Route(net=10, net_name="SWDIO", segments=[swdio_seg])
        boot0_via = Via(
            x=5.0, y=5.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20, net_name="BOOT0",
        )
        boot0_route = Route(net=20, net_name="BOOT0", vias=[boot0_via])
        router = _StubAutorouter(
            routes=[swdio_route, boot0_route], rules=rules,
        )

        success = _nudge_segment_with_chain(
            swdio_seg, 0.0, -1.0, 0.4, router, result=None,
        )
        assert success is False
        assert math.isclose(swdio_seg.y1, 5.5)

    def test_board04_swdio_boot0_regression(self):
        """Explicit regression for board-04 PCB (143.8, 119.7) violation.

        Reproduces the geometry that the issue tracker calls out as the
        empirical closer: a SWDIO B.Cu segment at y=120.4 (0.7 mm clear
        of BOOT0's via at y=119.7) being nudged into y=120.0246 (-0.0754
        mm clearance to the BOOT0 via).  The pre-PR-#3028 nudge would
        commit silently; the new gate must REFUSE.
        """
        rules = _board04_rules()
        # SWDIO segment at y=120.4 on B.Cu.
        swdio_seg = Segment(
            x1=140.0, y1=120.4, x2=145.0, y2=120.4,
            width=0.2, layer=Layer.B_CU, net=10,
        )
        swdio_route = Route(net=10, net_name="SWDIO", segments=[swdio_seg])
        # BOOT0 via at y=119.7 on F.Cu/B.Cu (spans both layers).
        boot0_via = Via(
            x=143.8, y=119.7, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=20, net_name="BOOT0",
        )
        boot0_route = Route(net=20, net_name="BOOT0", vias=[boot0_via])
        router = _StubAutorouter(
            routes=[swdio_route, boot0_route], rules=rules,
        )
        result = DRCNudgeResult()

        # Attempt to nudge SWDIO by -0.3754mm in y (matches the
        # geometry of the historical violation: y=120.4 -> y=120.0246).
        # Without the gate this would land the segment at the
        # exact -0.0754mm clearance position the routed PCB shows.
        success = _nudge_segment_with_chain(
            swdio_seg, 0.0, -1.0, 0.3754, router, result=result,
        )

        assert success is False
        assert result.skipped.get("foreign_via_blocked", 0) == 1
        # Segment must remain at y=120.4 (pre-nudge position).
        assert math.isclose(swdio_seg.y1, 120.4)
        assert math.isclose(swdio_seg.y2, 120.4)
