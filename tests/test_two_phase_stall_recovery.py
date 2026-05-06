"""Tests for two-phase router stall-recovery integration (issue #2527).

When the two-phase router's detailed-routing initial pass leaves
``overflow == 0`` but unrouted (or partially routed) nets remain, the
iteration loop's ``overflow > 0`` guard previously short-circuited the
entire rip-up loop -- so the destination-component sibling rip-up that
PR #2523 added to the negotiated ``route_all`` path was never invoked
from the two-phase code path.

These tests exercise the two-phase router's stall-recovery hook
(``attempt_blocked_component_ripup`` callable) and verify:

1. The hook is invoked when the initial pass stalls with overflow=0.
2. The hook is NOT invoked when the initial pass completes cleanly
   (no unrouted nets).
3. The hook is NOT invoked when overflow > 0 (the existing iteration
   loop handles that case).
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

    def test_helper_not_invoked_when_overflow_positive(self, autorouter_with_two_phase):
        """Issue #2527: When overflow > 0 the existing iteration loop
        owns rip-up.  The new stall path is gated on overflow == 0 to
        avoid double-charging budget for nets the iteration loop is
        already going to displace."""
        ar = autorouter_with_two_phase
        tp_router = self._make_two_phase_router_with_mock_helper(ar)

        tp_router._route_net_with_corridor = MagicMock(return_value=[])
        # Non-zero overflow -> stall path must skip; iteration loop
        # owns recovery.
        tp_router.grid.get_total_overflow = MagicMock(return_value=5)

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

        assert tp_router._attempt_blocked_component_ripup.call_count == 0
