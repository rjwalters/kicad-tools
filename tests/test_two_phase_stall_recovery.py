"""Tests for two-phase router stall-recovery integration (issues #2527, #2745).

When the two-phase router's detailed-routing initial pass leaves
``overflow == 0`` but unrouted (or partially routed) nets remain, the
iteration loop's ``overflow > 0`` guard previously short-circuited the
entire rip-up loop -- so the destination-component sibling rip-up that
PR #2523 added to the negotiated ``route_all`` path was never invoked
from the two-phase code path.

Issue #2745 (board 04 OSC_OUT stagnation) showed that the original
``overflow == 0`` gate also failed the inverse case: when **one** net is
fully blocked (zero placed segments) AND **another** net produced minor
overflow, the standard rip-up loop selects victims via
``find_nets_through_overused_cells`` which can only see nets with placed
segments — so the failed net never enters the rip-up rotation.  The
recovery gate has therefore been broadened: it now fires whenever
``stall_failed`` is non-empty, regardless of overflow.  The per-net
``stall_budget = 3`` prevents thrash.

These tests exercise the two-phase router's stall-recovery hook
(``attempt_blocked_component_ripup`` callable) and verify:

1. The hook is invoked when the initial pass stalls with overflow=0.
2. The hook is NOT invoked when the initial pass completes cleanly
   (no unrouted nets).
3. The hook IS invoked when overflow > 0 but unrouted/partial nets
   remain (issue #2745).
4. ``build_pads_by_net`` is called with the routing-order net list and
   the returned mapping is what the helper sees.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def autorouter_with_two_phase():
    """Build a minimal Autorouter that drives the two-phase router."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Pad

    ar = Autorouter(width=50, height=50)
    ar.net_class_map = {}

    def _mk_pad(x, y, net, net_name, ref, pin):
        return Pad(
            x=x,
            y=y,
            width=1.0,
            height=1.0,
            net=net,
            net_name=net_name,
            ref=ref,
            pin=pin,
        )

    # Two nets, both with two pads on shared component J1 / U1.
    ar.pads[("J1", "1")] = _mk_pad(5.0, 5.0, 1, "NET_A", "J1", "1")
    ar.pads[("U1", "1")] = _mk_pad(20.0, 5.0, 1, "NET_A", "U1", "1")
    ar.pads[("J1", "2")] = _mk_pad(5.0, 7.0, 2, "NET_B", "J1", "2")
    ar.pads[("U1", "2")] = _mk_pad(20.0, 7.0, 2, "NET_B", "U1", "2")

    ar.nets[1] = [("J1", "1"), ("U1", "1")]
    ar.nets[2] = [("J1", "2"), ("U1", "2")]
    ar.net_names = {1: "NET_A", 2: "NET_B"}

    return ar


class TestTwoPhaseStallRecoveryWiring:
    """Tests that ``_create_two_phase_router`` plumbs the recovery hooks."""

    def test_two_phase_router_receives_helper_callable(self, autorouter_with_two_phase):
        """Issue #2527: ``_create_two_phase_router`` must thread
        ``_attempt_blocked_component_ripup_negotiated`` into the
        TwoPhaseRouter so the stall path can engage it."""
        ar = autorouter_with_two_phase
        tp_router = ar._create_two_phase_router()

        assert tp_router._attempt_blocked_component_ripup is not None
        # Bound method identity: the callable should resolve to the
        # negotiated rip-up helper, not the route_all variant.
        assert (
            tp_router._attempt_blocked_component_ripup.__func__
            is type(ar)._attempt_blocked_component_ripup_negotiated
        )

    def test_two_phase_router_receives_pads_builder(self, autorouter_with_two_phase):
        """Issue #2527: ``build_pads_by_net`` must be threaded so the
        stall path constructs escape-pad-aware pad lists."""
        ar = autorouter_with_two_phase
        tp_router = ar._create_two_phase_router()

        assert tp_router._build_pads_by_net is not None
        mapping = tp_router._build_pads_by_net([1, 2])
        assert set(mapping.keys()) == {1, 2}
        assert len(mapping[1]) == 2
        assert len(mapping[2]) == 2

    def test_two_phase_router_receives_partial_routes_helper(self, autorouter_with_two_phase):
        """Issue #2527: ``get_partially_routed_nets`` must be threaded
        so the stall path can include partial-routed nets in its set."""
        ar = autorouter_with_two_phase
        tp_router = ar._create_two_phase_router()

        assert tp_router._get_partially_routed_nets is not None

    def test_pads_builder_honours_escape_pad_overrides(self, autorouter_with_two_phase):
        """Issue #2527: When dense-package escape routing has substituted
        a virtual escape-endpoint pad for an original pad
        (``_escape_pad_overrides``), the pads-by-net builder must return
        the virtual pad so the helper's A* targets the escape endpoint
        instead of the original pad center -- otherwise the rip-up would
        try to reroute to a coordinate the escape route has already
        committed to a different layer/position."""
        from kicad_tools.router.primitives import Pad

        ar = autorouter_with_two_phase
        # Stand in a virtual escape pad for J1.1
        virtual_pad = Pad(
            x=5.0,
            y=5.5,  # offset from original (5.0, 5.0)
            width=1.0,
            height=1.0,
            net=1,
            net_name="NET_A",
            ref="J1",
            pin="1",
        )
        ar._escape_pad_overrides[("J1", "1")] = virtual_pad

        tp_router = ar._create_two_phase_router()
        mapping = tp_router._build_pads_by_net([1, 2])
        # Net 1 must include the virtual pad, not the original.
        assert virtual_pad in mapping[1]
        # Net 2 should be untouched.
        assert virtual_pad not in mapping[2]


class TestTwoPhaseStallRecoveryInvocation:
    """Verify the stall path in ``_detailed_negotiated`` invokes the
    rip-up helper exactly when expected."""

    def _make_two_phase_router_with_mock_helper(self, ar):
        """Wrap ``_create_two_phase_router`` and replace the helper with
        a MagicMock so we can assert call counts / args."""
        tp_router = ar._create_two_phase_router()
        tp_router._attempt_blocked_component_ripup = MagicMock(return_value=False)
        return tp_router

    def test_helper_invoked_when_initial_pass_stalls(self, autorouter_with_two_phase):
        """Issue #2527: When ``_detailed_negotiated`` finishes the
        initial pass with overflow=0 but >=1 net unrouted, the
        BLOCKED_BY_COMPONENT helper must be invoked (one call per
        failed net)."""
        ar = autorouter_with_two_phase
        tp_router = self._make_two_phase_router_with_mock_helper(ar)

        # Force the per-net router to fail every net so the initial
        # pass leaves both nets unrouted with overflow=0.
        tp_router._route_net_with_corridor = MagicMock(return_value=[])

        # Patch the grid overflow to 0 so we hit the stall branch.
        tp_router.grid.get_total_overflow = MagicMock(return_value=0)

        # Skip the iteration loop's history/early-stop machinery by
        # mocking the negotiated router's internals.
        from unittest.mock import patch as _patch

        with _patch("kicad_tools.router.algorithms.NegotiatedRouter") as _NegMock:
            _NegMock.return_value.find_nets_through_overused_cells = MagicMock(return_value=[])
            _NegMock.return_value.rip_up_nets = MagicMock()

            tp_router._detailed_negotiated(
                net_order=[1, 2],
                corridor_penalty=0.0,
                corridors={},
                progress_callback=None,
                timeout=None,
                start_time=0.0,
                per_net_timeout=None,
                initial_routes=None,
                max_iterations=1,
                patience=2,
            )

        # Two unrouted nets -> two helper invocations.
        assert tp_router._attempt_blocked_component_ripup.call_count == 2
        # Each call must include max_ripups_per_net >= 3 (issue #2527
        # comment requires connector-class rip-up budget).
        for call in tp_router._attempt_blocked_component_ripup.call_args_list:
            assert call.kwargs["max_ripups_per_net"] >= 3
            assert call.kwargs["pads_by_net"] is not None
            assert call.kwargs["ripup_history"] is not None

    def test_helper_not_invoked_when_initial_pass_succeeds(self, autorouter_with_two_phase):
        """Issue #2527: A clean initial pass (every net routed end-to-end)
        must not engage the stall recovery.  The recovery path filters by
        ``_get_partially_routed_nets``, which checks pad-connectivity, so
        we provide segments that actually join each net's two pads."""
        ar = autorouter_with_two_phase
        tp_router = self._make_two_phase_router_with_mock_helper(ar)

        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment

        def _fake_route(net, present_factor, per_net_timeout=None):
            # Build a single segment whose endpoints coincide with the
            # net's two pads so validate_net_connectivity reports the
            # net as fully connected.
            pad_keys = ar.nets[net]
            p_a = ar.pads[pad_keys[0]]
            p_b = ar.pads[pad_keys[1]]
            seg = Segment(
                x1=p_a.x,
                y1=p_a.y,
                x2=p_b.x,
                y2=p_b.y,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
                net_name=ar.net_names[net],
            )
            return [
                Route(
                    net=net,
                    net_name=ar.net_names[net],
                    segments=[seg],
                    vias=[],
                )
            ]

        tp_router._route_net_with_corridor = _fake_route
        tp_router.grid.get_total_overflow = MagicMock(return_value=0)

        from unittest.mock import patch as _patch

        with _patch("kicad_tools.router.algorithms.NegotiatedRouter") as _NegMock:
            _NegMock.return_value.find_nets_through_overused_cells = MagicMock(return_value=[])
            _NegMock.return_value.rip_up_nets = MagicMock()

            tp_router._detailed_negotiated(
                net_order=[1, 2],
                corridor_penalty=0.0,
                corridors={},
                progress_callback=None,
                timeout=None,
                start_time=0.0,
                per_net_timeout=None,
                initial_routes=None,
                max_iterations=1,
                patience=2,
            )

        # All nets routed -> stall recovery must NOT fire.
        assert tp_router._attempt_blocked_component_ripup.call_count == 0

    def test_helper_invoked_when_overflow_positive_and_nets_unrouted(
        self, autorouter_with_two_phase
    ):
        """Issue #2745: When the initial pass leaves at least one net
        fully unrouted (or partially routed) AND another net produces
        minor overflow, the standard iteration loop's
        ``find_nets_through_overused_cells`` scheduler cannot see the
        zero-segment failed net.  The BLOCKED_BY_COMPONENT recovery
        must therefore fire even when ``overflow > 0`` -- otherwise the
        failed net is invisible to every subsequent rip-up rotation and
        the only "recovery" is wasted layer escalation.

        This is the exact failure signature of board 04 OSC_OUT:
        OSC_OUT had zero placed segments and OSC_IN's escape produced
        ``overflow = 1`` near U2, so the old gate (``overflow == 0``)
        skipped recovery and the iteration loop never re-evaluated
        OSC_OUT.
        """
        ar = autorouter_with_two_phase
        tp_router = self._make_two_phase_router_with_mock_helper(ar)

        # Force the per-net router to fail every net so both nets are
        # left unrouted (zero placed segments).
        tp_router._route_net_with_corridor = MagicMock(return_value=[])

        # Simulate the OSC_IN overflow=1 scenario: another (hypothetically
        # placed) sibling produced minor overflow, but the failed net
        # has no placed segments and so cannot enter the standard
        # iteration loop's rip-up cohort.
        tp_router.grid.get_total_overflow = MagicMock(return_value=1)

        from unittest.mock import patch as _patch

        with _patch("kicad_tools.router.algorithms.NegotiatedRouter") as _NegMock:
            _NegMock.return_value.find_nets_through_overused_cells = MagicMock(return_value=[])
            _NegMock.return_value.rip_up_nets = MagicMock()

            tp_router._detailed_negotiated(
                net_order=[1, 2],
                corridor_penalty=0.0,
                corridors={},
                progress_callback=None,
                timeout=None,
                start_time=0.0,
                per_net_timeout=None,
                initial_routes=None,
                max_iterations=1,
                patience=2,
            )

        # Both nets unrouted -> recovery must fire for each, even with
        # overflow > 0 (this is the issue #2745 fix).
        assert tp_router._attempt_blocked_component_ripup.call_count == 2
        # Per-net rip-up budget must still be at least 3 to prevent
        # thrash on charlieplex-style boards.
        for call in tp_router._attempt_blocked_component_ripup.call_args_list:
            assert call.kwargs["max_ripups_per_net"] >= 3


class TestTwoPhaseStallReliefRescue:
    """Issue #3471: the stall path escalates rip-up fast-fails to the
    #3438 relief rescue.

    Board 05's ISENSE cluster fails the BLOCKED_BY_COMPONENT rip-up at
    0.0s even with all 24 destination-component siblings displaced: the
    actual blockage is NON-RIPPABLE foreign escape copper in the U3
    sense band, which sibling rip-up by construction cannot clear.  The
    relief rescue (strictly transactional, foreign copper passable at a
    penalty) is the correct escalation for exactly the nets the rip-up
    returns False for.
    """

    def _make_tp_router(self, ar, ripup_result: bool, relief_result: bool = False):
        tp_router = ar._create_two_phase_router()
        tp_router._attempt_blocked_component_ripup = MagicMock(return_value=ripup_result)
        tp_router._relief_rescue = MagicMock(return_value=relief_result)
        # Force every net to fail the initial pass.
        tp_router._route_net_with_corridor = MagicMock(return_value=[])
        tp_router.grid.get_total_overflow = MagicMock(return_value=0)
        return tp_router

    def _run_detailed(self, tp_router):
        from unittest.mock import patch as _patch

        with _patch("kicad_tools.router.algorithms.NegotiatedRouter") as _NegMock:
            _NegMock.return_value.find_nets_through_overused_cells = MagicMock(return_value=[])
            _NegMock.return_value.rip_up_nets = MagicMock()
            tp_router._detailed_negotiated(
                net_order=[1, 2],
                corridor_penalty=0.0,
                corridors={},
                progress_callback=None,
                timeout=None,
                start_time=0.0,
                per_net_timeout=None,
                initial_routes=None,
                max_iterations=1,
                patience=2,
            )

    def test_create_two_phase_router_threads_relief_rescue(self, autorouter_with_two_phase):
        """``_create_two_phase_router`` must thread ``_relief_rescue``."""
        ar = autorouter_with_two_phase
        tp_router = ar._create_two_phase_router()
        assert tp_router._relief_rescue is not None
        assert tp_router._relief_rescue.__func__ is type(ar)._relief_rescue

    def test_relief_invoked_for_ripup_fast_fails(self, autorouter_with_two_phase):
        """Nets the rip-up could not rescue must each get one relief
        rescue attempt."""
        ar = autorouter_with_two_phase
        tp_router = self._make_tp_router(ar, ripup_result=False)
        self._run_detailed(tp_router)
        # Both nets unrescued by the rip-up -> two relief attempts.
        assert tp_router._relief_rescue.call_count == 2
        attempted_nets = {c.args[0] for c in tp_router._relief_rescue.call_args_list}
        assert attempted_nets == {1, 2}

    def test_relief_not_invoked_when_ripup_rescues(self, autorouter_with_two_phase):
        """A net the rip-up successfully rescued must NOT be escalated."""
        ar = autorouter_with_two_phase
        tp_router = self._make_tp_router(ar, ripup_result=True)
        self._run_detailed(tp_router)
        assert tp_router._relief_rescue.call_count == 0

    def test_relief_honours_disable_env(self, autorouter_with_two_phase, monkeypatch):
        """``KCT_DISABLE_RELIEF=1`` must keep the escalation off (the
        #3438 emergency escape hatch covers this call site too)."""
        monkeypatch.setenv("KCT_DISABLE_RELIEF", "1")
        ar = autorouter_with_two_phase
        tp_router = self._make_tp_router(ar, ripup_result=False)
        self._run_detailed(tp_router)
        assert tp_router._relief_rescue.call_count == 0

    def test_relief_none_hook_preserves_legacy(self, autorouter_with_two_phase):
        """A TwoPhaseRouter constructed without the hook (unit-test
        construction path) must not raise in the stall path."""
        ar = autorouter_with_two_phase
        tp_router = self._make_tp_router(ar, ripup_result=False)
        tp_router._relief_rescue = None
        self._run_detailed(tp_router)  # must not raise
