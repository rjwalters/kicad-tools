"""Tests for Issue #2998: escape-commit segment-vs-foreign-via clearance.

The bug: ``EscapeRouter.apply_escape_routes`` committed escape segments
directly to the grid via ``grid.mark_route`` without validating them
against foreign-net vias from previously-committed routes / earlier
escapes in the same call.  The symmetric sibling of PR #2952 (Issue
#2947) -- which protects a NEW via from foreign segments/pads -- this
patch protects a NEW segment from a foreign-net VIA.

Concrete failure mode (board-04, PCB (143.8, 119.7) on B.Cu):
the SWDIO B.Cu escape segment clipped the previously-committed BOOT0
in-pad via by -0.075 mm (hard geometric intersection).  The validator-
time block in C++ (``Grid3D::validate_route`` block 1c) catches this
post-route but only after the bad geometry is on the board.

The fix adds a pre-commit gate in ``apply_escape_routes`` that drops
any escape whose segments fail the predicate::

    dist(via_center, seg_centerline)
      >= via.diameter/2 + seg.width/2 + trace_clearance

Layer-aware: only checks vias whose layer range spans the segment's
layer (so a B.Cu segment ignores In1.Cu-only vias on 4+ layer stacks,
and vice-versa).  Same-net vias are filtered out (mirrors the boundary
convention of ``point_clear_of_copper`` and PR #2952's setter).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.escape import EscapeRoute, EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


def _make_router() -> tuple[EscapeRouter, RoutingGrid, DesignRules]:
    rules = _make_rules()
    grid = RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.two_layer(),
    )
    return EscapeRouter(grid, rules), grid, rules


class TestSegmentClearsForeignVia:
    """Unit tests for the layer-aware predicate."""

    def test_passes_when_via_far_enough(self):
        """Distance > via_radius + seg_half_width + trace_clearance -> pass."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        via = Via(
            x=5.0,
            y=2.0,  # 2.0mm away from segment centerline
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=99,
            net_name="BOOT0",
        )
        # required = 0.3 + 0.1 + 0.15 = 0.55; actual = 2.0 -> pass
        assert EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)

    def test_rejects_when_via_within_clearance(self):
        """The board-04 case: B.Cu segment clipping a through-hole via."""
        seg = Segment(
            x1=140.0,
            y1=119.7,
            x2=150.0,
            y2=119.7,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        # Via centered on the segment centerline (worst case).
        via = Via(
            x=143.8,
            y=119.7,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=99,
            net_name="BOOT0",
        )
        # required = 0.3 + 0.1 + 0.15 = 0.55; actual = 0 -> reject
        assert not EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)

    def test_layer_filter_skips_non_overlapping_via(self):
        """A B.Cu segment ignores a via whose layer range stops at In1.Cu."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SIG",
        )
        # Blind via F.Cu -> In1.Cu (does NOT reach B.Cu).
        via = Via(
            x=5.0,
            y=0.0,  # right on the centerline
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.IN1_CU),
            net=99,
            net_name="OTHER",
        )
        # Despite geometric overlap, layer mismatch -> pass.
        assert EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)

    def test_layer_filter_admits_overlapping_via(self):
        """Same geometry, but via now spans B.Cu -> rejected."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SIG",
        )
        via = Via(
            x=5.0,
            y=0.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=99,
            net_name="OTHER",
        )
        assert not EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)

    def test_threshold_boundary_passes(self):
        """At exactly the required distance the predicate admits."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SIG",
        )
        # required = 0.3 + 0.1 + 0.15 = 0.55
        via = Via(
            x=5.0,
            y=0.55,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=99,
            net_name="OTHER",
        )
        assert EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)


class TestApplyEscapeRoutesGate:
    """Integration-level: ``apply_escape_routes`` drops the offending escape."""

    def test_clean_escape_committed(self):
        """A single escape with no foreign vias commits normally."""
        router, grid, rules = _make_router()
        seg = Segment(
            x1=5.0,
            y1=5.0,
            x2=10.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        pad = Pad(
            x=5.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        escape = EscapeRoute(
            pad=pad,
            direction=router._direction_to_vector,  # type: ignore[arg-type]
            escape_point=(10.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[seg],
            via=None,
            ring_index=0,
        )
        # Use a real EscapeDirection rather than the bound method.
        from kicad_tools.router.escape import EscapeDirection

        escape.direction = EscapeDirection.EAST

        routes = router.apply_escape_routes([escape])
        assert len(routes) == 1
        assert routes[0].segments == [seg]

    def test_marginal_clearance_does_not_drop_escape(self):
        """Marginal sub-clearance violations are NOT dropped.

        The board-04 NRST/OSC_OUT cluster has segments running at ~0.05
        mm clearance to foreign vias (below the manufacturer minimum
        but still positive); these are tolerated by the existing
        allowlist and must NOT be dropped here -- dropping them
        regresses 9/9 completion (NRST has no alternative escape on
        a 0.5 mm LQFP-48 west edge).  Only HARD intersection
        (negative clearance, copper overlap) triggers the drop.
        """
        router, grid, rules = _make_router()

        # Foreign via at (5, 5); through-hole, F.Cu -> B.Cu.
        grid.mark_route(
            Route(
                net=10,
                net_name="OSC_OUT",
                segments=[],
                vias=[
                    Via(
                        x=5.0,
                        y=5.0,
                        drill=0.3,
                        diameter=0.6,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=10,
                        net_name="OSC_OUT",
                        in_pad=True,
                    )
                ],
            )
        )

        # B.Cu segment 0.4 mm away from via center.
        # via_radius (0.3) + seg_half_width (0.1) = 0.4.
        # Distance = 0.4 -- exactly at the hard-intersection threshold,
        # ZERO margin above zero.  Predicate uses ``>= required - 1e-9``
        # so this PASSES the hard-intersection gate.  (At trace_clearance
        # 0.15 the STANDARD threshold would be 0.55 and this segment
        # would fail -- exactly the case we are NOT dropping.)
        marginal_seg = Segment(
            x1=0.0,
            y1=5.4,
            x2=10.0,
            y2=5.4,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="NRST",
        )
        marginal_pad = Pad(
            x=0.0,
            y=5.4,
            width=0.3,
            height=1.4,
            net=5,
            net_name="NRST",
            layer=Layer.F_CU,
            ref="U1",
            pin="7",
        )
        from kicad_tools.router.escape import EscapeDirection

        candidate = EscapeRoute(
            pad=marginal_pad,
            direction=EscapeDirection.EAST,
            escape_point=(10.0, 5.4),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[marginal_seg],
            via=None,
            ring_index=0,
        )

        committed = router.apply_escape_routes([candidate])
        # Marginal sub-clearance must NOT drop the escape.
        assert len(committed) == 1, (
            "Issue #2998: marginal sub-clearance escapes must be kept "
            "(board-04 NRST regression guard)"
        )

    def test_blocks_segment_through_foreign_via(self):
        """Pre-commit gate: an escape segment with HARD intersection is dropped.

        Reproduces the board-04 SWDIO/BOOT0 site at a synthetic 2-net fixture::

            BOOT0 (committed first): in-pad via at (5, 5) spanning F.Cu->B.Cu
            SWDIO (escape candidate): B.Cu segment from (4, 5) to (8, 5)

        The SWDIO segment centerline runs straight through the BOOT0 via
        center (distance 0 << via_radius + seg_half_width = 0.4); the
        predicate must reject and the escape must be dropped.
        """
        router, grid, rules = _make_router()

        # Pre-commit a foreign-net route holding the BOOT0 via.
        boot0_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=10,
            net_name="BOOT0",
            in_pad=True,
        )
        grid.mark_route(
            Route(
                net=10,
                net_name="BOOT0",
                segments=[],
                vias=[boot0_via],
            )
        )

        # Candidate SWDIO escape: B.Cu segment that clips BOOT0 via center.
        swdio_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        swdio_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        from kicad_tools.router.escape import EscapeDirection

        candidate = EscapeRoute(
            pad=swdio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[swdio_seg],
            via=None,
            ring_index=0,
        )

        # Pre-existing route count: just the BOOT0 holder.
        routes_before = len(grid.routes)

        committed = router.apply_escape_routes([candidate])
        # The escape must be dropped -- defer to main router.
        assert committed == [], (
            "Issue #2998: escape segment clipping foreign via must be dropped, not committed"
        )
        # Grid state must be unchanged (no new route appended).
        assert len(grid.routes) == routes_before

    def test_same_net_via_does_not_block_segment(self):
        """A via on the segment's own net is filtered (same-net is fine)."""
        router, grid, rules = _make_router()

        # Same-net via at the segment's path (legitimate own-net geometry).
        own_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=5,
            net_name="SWDIO",
        )
        grid.mark_route(
            Route(
                net=5,
                net_name="SWDIO",
                segments=[],
                vias=[own_via],
            )
        )

        # Same-net segment that geometrically overlaps the same-net via.
        own_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        own_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        from kicad_tools.router.escape import EscapeDirection

        candidate = EscapeRoute(
            pad=own_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[own_seg],
            via=None,
            ring_index=0,
        )

        committed = router.apply_escape_routes([candidate])
        # Same-net via must not block -- the escape commits.
        assert len(committed) == 1

    def test_layer_separated_via_does_not_block_segment(self):
        """A B.Cu segment ignores a foreign via whose range stops at In1.Cu."""
        rules = _make_rules()
        # Use a 4-layer all-signal stack so blind vias make sense and all
        # copper layers admit routing (In1/In2 are not planes here).
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            origin_x=0.0,
            origin_y=0.0,
            layer_stack=LayerStack.four_layer_all_signal(),
        )
        router = EscapeRouter(grid, rules)

        blind_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.IN1_CU),  # Does NOT reach B.Cu
            net=10,
            net_name="OTHER",
        )
        grid.mark_route(
            Route(
                net=10,
                net_name="OTHER",
                segments=[],
                vias=[blind_via],
            )
        )

        # B.Cu segment that would overlap blind_via geometrically but not
        # by layer.
        b_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        from kicad_tools.router.escape import EscapeDirection

        candidate = EscapeRoute(
            pad=pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[b_seg],
            via=None,
            ring_index=0,
        )

        committed = router.apply_escape_routes([candidate])
        # Layer mismatch -- escape must commit.
        assert len(committed) == 1

    def test_dropped_escape_removed_from_input_list(self):
        """In-place mutation contract: dropped escapes are removed from the
        caller's input list so the ``_escape_pad_overrides`` loop in
        ``Autorouter.generate_escape_routes`` (``core.py:10127``) does
        not see them.  Without this contract, stale overrides redirect
        the main router to virtual endpoints that have no escape
        segment -- producing connectivity gaps and completion
        regressions.  See ``apply_escape_routes`` docstring.
        """
        router, grid, rules = _make_router()

        # Foreign via blocking SWDIO's path.
        grid.mark_route(
            Route(
                net=10,
                net_name="BOOT0",
                segments=[],
                vias=[
                    Via(
                        x=5.0,
                        y=5.0,
                        drill=0.3,
                        diameter=0.6,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=10,
                        net_name="BOOT0",
                        in_pad=True,
                    )
                ],
            )
        )

        swdio_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        from kicad_tools.router.escape import EscapeDirection

        candidate = EscapeRoute(
            pad=swdio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[
                Segment(
                    x1=4.0,
                    y1=5.0,
                    x2=8.0,
                    y2=5.0,
                    width=0.2,
                    layer=Layer.B_CU,
                    net=5,
                    net_name="SWDIO",
                )
            ],
            via=None,
            ring_index=0,
        )

        # Clean second escape on a different net that does NOT clip.
        gpio_pad = Pad(
            x=10.0,
            y=15.0,
            width=0.3,
            height=1.4,
            net=7,
            net_name="GPIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="36",
        )
        clean = EscapeRoute(
            pad=gpio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(14.0, 15.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[
                Segment(
                    x1=10.0,
                    y1=15.0,
                    x2=14.0,
                    y2=15.0,
                    width=0.2,
                    layer=Layer.B_CU,
                    net=7,
                    net_name="GPIO",
                )
            ],
            via=None,
            ring_index=0,
        )

        escapes_list = [candidate, clean]
        router.apply_escape_routes(escapes_list)
        # The list must now contain ONLY the clean escape.
        assert escapes_list == [clean], (
            "Issue #2998: apply_escape_routes must remove dropped escapes "
            "from the input list so caller's override loop skips them"
        )

    def test_no_violations_leaves_input_list_intact(self):
        """When NO escape is rejected, the input list is not mutated.

        This is a no-regression guard: pre-#2998 callers may have
        relied on identity preservation; the only intentional mutation
        is the drop case.
        """
        router, grid, rules = _make_router()

        gpio_pad = Pad(
            x=10.0,
            y=15.0,
            width=0.3,
            height=1.4,
            net=7,
            net_name="GPIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="36",
        )
        from kicad_tools.router.escape import EscapeDirection

        clean = EscapeRoute(
            pad=gpio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(14.0, 15.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[
                Segment(
                    x1=10.0,
                    y1=15.0,
                    x2=14.0,
                    y2=15.0,
                    width=0.2,
                    layer=Layer.B_CU,
                    net=7,
                    net_name="GPIO",
                )
            ],
            via=None,
            ring_index=0,
        )
        escapes_list = [clean]
        before_id = id(escapes_list)
        router.apply_escape_routes(escapes_list)
        # List identity preserved AND content preserved.
        assert id(escapes_list) == before_id
        assert escapes_list == [clean]

    def test_in_flight_escape_blocks_later_escape(self):
        """Vias from an EARLIER commit in the same ``apply_escape_routes``
        call still gate LATER escapes.  This is the BOOT0-then-SWDIO
        timing the board-04 site exhibits when both nets escape via
        in-pad rescue on the same QFP."""
        router, grid, rules = _make_router()

        # BOOT0 escape: in-pad via at (5, 5) plus a tiny stub on B.Cu.
        boot0_pad = Pad(
            x=5.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=10,
            net_name="BOOT0",
            layer=Layer.F_CU,
            ref="U1",
            pin="44",
        )
        boot0_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=10,
            net_name="BOOT0",
            in_pad=True,
        )
        boot0_seg = Segment(
            x1=5.0,
            y1=5.0,
            x2=5.0,
            y2=6.0,  # Goes north, away from SWDIO path
            width=0.2,
            layer=Layer.B_CU,
            net=10,
            net_name="BOOT0",
        )
        from kicad_tools.router.escape import EscapeDirection

        boot0_esc = EscapeRoute(
            pad=boot0_pad,
            direction=EscapeDirection.NORTH,
            escape_point=(5.0, 6.0),
            escape_layer=Layer.B_CU,
            via_pos=(5.0, 5.0),
            segments=[boot0_seg],
            via=boot0_via,
            ring_index=0,
        )

        # SWDIO escape: B.Cu segment that runs through (5, 5) -- clips BOOT0 via.
        swdio_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        swdio_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        swdio_esc = EscapeRoute(
            pad=swdio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[swdio_seg],
            via=None,
            ring_index=0,
        )

        # Order matters: BOOT0 first.  SWDIO's clearance check must see
        # BOOT0's committed via.
        committed = router.apply_escape_routes([boot0_esc, swdio_esc])

        # BOOT0 commits, SWDIO is dropped.
        assert len(committed) == 1
        assert committed[0].net == 10  # BOOT0

    def test_swdio_then_boot0_ordering_blocked(self):
        """Issue #3013: SWDIO-first / BOOT0-second ordering -- the
        production order on board-04's U2 south edge -- must catch the
        same violation that BOOT0-first / SWDIO-second catches.

        PR #2999's single-pass loop interleaved via-commit and segment-
        validation per escape: when SWDIO is processed FIRST, BOOT0's
        in-pad via is not yet in ``self.grid.routes`` so SWDIO's gate
        sees an empty foreign-via universe and commits the segment.
        Then BOOT0 commits its via on top of the SWDIO segment.

        The two-pass commit (Pass A collects every planned via into a
        probe list before Pass B validates segments) closes this
        ordering hole.  This test fails WITHOUT the fix (SWDIO commits)
        and passes WITH the fix (SWDIO drops, BOOT0 commits).
        """
        router, grid, rules = _make_router()

        # SWDIO escape: B.Cu segment that runs through (5, 5).
        # Processed FIRST (production order on board-04's south edge).
        swdio_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        swdio_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        from kicad_tools.router.escape import EscapeDirection

        swdio_esc = EscapeRoute(
            pad=swdio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=None,
            segments=[swdio_seg],
            via=None,
            ring_index=0,
        )

        # BOOT0 escape: in-pad via at (5, 5) plus a tiny stub on B.Cu.
        # Processed SECOND -- the via has not committed when SWDIO is
        # gated in the OLD single-pass loop.
        boot0_pad = Pad(
            x=5.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=10,
            net_name="BOOT0",
            layer=Layer.F_CU,
            ref="U1",
            pin="44",
        )
        boot0_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=10,
            net_name="BOOT0",
            in_pad=True,
        )
        boot0_seg = Segment(
            x1=5.0,
            y1=5.0,
            x2=5.0,
            y2=6.0,  # Goes north, clear of SWDIO
            width=0.2,
            layer=Layer.B_CU,
            net=10,
            net_name="BOOT0",
        )
        boot0_esc = EscapeRoute(
            pad=boot0_pad,
            direction=EscapeDirection.NORTH,
            escape_point=(5.0, 6.0),
            escape_layer=Layer.B_CU,
            via_pos=(5.0, 5.0),
            segments=[boot0_seg],
            via=boot0_via,
            ring_index=0,
        )

        # Production order on board-04: SWDIO first.
        escapes = [swdio_esc, boot0_esc]
        committed = router.apply_escape_routes(escapes)

        # With the two-pass fix, BOOT0's via is visible during SWDIO's
        # segment gate (Pass A loaded it into the probe list), so the
        # SWDIO segment is rejected and BOOT0 commits.
        assert len(committed) == 1, (
            "Issue #3013: SWDIO-first ordering must drop SWDIO when its "
            "segment would clip BOOT0's planned via (two-pass commit)"
        )
        assert committed[0].net == 10, "Issue #3013: BOOT0 (net 10) must be the survivor"
        # In-place mutation contract preserved (PR #2999): the dropped
        # SWDIO escape is removed from the input list so the override
        # loop in ``Autorouter.generate_escape_routes`` skips it.
        assert escapes == [boot0_esc], "Issue #3013: dropped escape must be removed from input list"

    def test_two_pass_dropped_escape_via_not_committed(self):
        """Issue #3013 (rollback correctness): when Pass B rejects an
        escape, NO grid mutation has occurred for that escape.

        Pass A is probe-only -- it builds an in-memory list without
        touching ``self.grid.routes``.  Pass B mutates the grid ONLY
        for survivors via ``grid.mark_route``.  So a rejected escape
        leaves no orphan via, no orphan segment, no half-committed
        copper on the grid.

        This guards the cleanliness of the probe-list strategy: if a
        future refactor accidentally calls ``grid.mark_route`` during
        Pass A, this test would catch the leak.
        """
        router, grid, rules = _make_router()

        # Foreign via at (5, 5) -- via already on the grid before the call.
        pre_existing_via = Via(
            x=5.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=10,
            net_name="BOOT0",
            in_pad=True,
        )
        grid.mark_route(
            Route(
                net=10,
                net_name="BOOT0",
                segments=[],
                vias=[pre_existing_via],
            )
        )
        routes_before = len(grid.routes)
        assert routes_before == 1

        # SWDIO escape that WILL be rejected.  It also carries its own
        # via -- the test ensures that via does NOT survive on the grid.
        swdio_pad = Pad(
            x=4.0,
            y=5.0,
            width=0.3,
            height=1.4,
            net=5,
            net_name="SWDIO",
            layer=Layer.F_CU,
            ref="U1",
            pin="34",
        )
        swdio_via = Via(
            x=4.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=5,
            net_name="SWDIO",
            in_pad=True,
        )
        swdio_seg = Segment(
            x1=4.0,
            y1=5.0,
            x2=8.0,
            y2=5.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SWDIO",
        )
        from kicad_tools.router.escape import EscapeDirection

        swdio_esc = EscapeRoute(
            pad=swdio_pad,
            direction=EscapeDirection.EAST,
            escape_point=(8.0, 5.0),
            escape_layer=Layer.B_CU,
            via_pos=(4.0, 5.0),
            segments=[swdio_seg],
            via=swdio_via,
            ring_index=0,
        )

        committed = router.apply_escape_routes([swdio_esc])

        # The escape is rejected (segment clips pre-existing BOOT0 via).
        assert committed == [], "Issue #3013: SWDIO with segment clipping BOOT0 via must drop"
        # The grid state is UNCHANGED: no extra route, no orphan via.
        assert len(grid.routes) == routes_before, (
            "Issue #3013: rejected escape must NOT leave any route on the grid "
            "(Pass A is probe-only -- no grid mutation for dropped escapes)"
        )
        # Confirm specifically that the SWDIO via is not on any route on
        # the grid (defensive: catches a future Pass A leak even if some
        # other route accidentally counted).
        for route in grid.routes:
            for via in route.vias:
                assert not (via.net == 5 and via.x == 4.0 and via.y == 5.0), (
                    "Issue #3013: dropped SWDIO escape's via leaked onto the grid"
                )


class TestSegmentClearsForeignViaLayerEdgeCases:
    """Layer-range edge cases for buried / blind / micro vias."""

    def test_via_layer_range_inclusive(self):
        """A buried via spanning In1.Cu..In3.Cu blocks an In2.Cu segment."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.IN2_CU,
            net=5,
            net_name="SIG",
        )
        buried = Via(
            x=5.0,
            y=0.0,  # on centerline
            drill=0.3,
            diameter=0.6,
            layers=(Layer.IN1_CU, Layer.IN3_CU),
            net=99,
            net_name="OTHER",
        )
        assert not EscapeRouter._segment_clears_foreign_via(seg, buried, trace_clearance=0.15)

    def test_via_layer_range_reversed_tuple_normalised(self):
        """Predicate must work regardless of (lo, hi) vs (hi, lo) tuple order."""
        seg = Segment(
            x1=0.0,
            y1=0.0,
            x2=10.0,
            y2=0.0,
            width=0.2,
            layer=Layer.B_CU,
            net=5,
            net_name="SIG",
        )
        # layers = (B_CU, F_CU) -- reversed.
        via = Via(
            x=5.0,
            y=0.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.B_CU, Layer.F_CU),
            net=99,
            net_name="OTHER",
        )
        assert not EscapeRouter._segment_clears_foreign_via(seg, via, trace_clearance=0.15)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
