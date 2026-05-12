"""Tests for per-net progress output during rip-up-and-reroute iterations (Issue #1265).

These tests verify that flush_print is called with per-net progress messages
during both the targeted and full rip-up reroute loops in route_all_negotiated(),
and during two-phase rip-up reroute iterations (Issue #2318).

Issue #2795 adds tests for visibility into ``NegotiatedRouter.targeted_ripup``
itself when invoked from ``_attempt_blocked_component_ripup_negotiated``:
the rip-up of N siblings + the failed-net reroute is N+1 sequential A* calls
that previously emitted zero log lines mid-flight, causing chorus-test runs
to look hung for many minutes.
"""

import re
from unittest.mock import MagicMock, patch

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter
from kicad_tools.router.core import Autorouter


def _make_router_with_two_nets():
    """Create a minimal router with two nets that have two pads each."""
    router = Autorouter(width=50.0, height=40.0)
    # Net 1: two pads
    pads1 = [
        {
            "number": "1",
            "x": 5.0,
            "y": 20.0,
            "width": 0.5,
            "height": 0.5,
            "net": 1,
            "net_name": "VCC",
        },
        {
            "number": "2",
            "x": 15.0,
            "y": 20.0,
            "width": 0.5,
            "height": 0.5,
            "net": 1,
            "net_name": "VCC",
        },
    ]
    router.add_component("U1", pads1)

    # Net 2: two pads that cross net 1's path
    pads2 = [
        {
            "number": "1",
            "x": 10.0,
            "y": 15.0,
            "width": 0.5,
            "height": 0.5,
            "net": 2,
            "net_name": "GND",
        },
        {
            "number": "2",
            "x": 10.0,
            "y": 25.0,
            "width": 0.5,
            "height": 0.5,
            "net": 2,
            "net_name": "GND",
        },
    ]
    router.add_component("U2", pads2)
    return router


# find_overused_cells returns list[tuple[int, int, int, int]] => (x, y, layer, usage_count)
_FAKE_OVERUSED = [(5, 10, 0, 2)]


class TestFullRipupRerouteProgressOutput:
    """Tests for per-net progress output in the full rip-up sequential reroute loop."""

    def test_flush_print_called_in_sequential_reroute_loop(self):
        """Verify flush_print emits per-net 'Re-routing net' messages in the full rip-up loop.

        Mocks overflow detection and find_nets_through_overused_cells to force
        the full rip-up sequential path, then asserts per-net progress lines appear.
        """
        router = _make_router_with_two_nets()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            # First call (after initial pass): overflow triggers rip-up
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        # Force find_nets_through_overused_cells to return both nets
        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        with (
            patch(
                "kicad_tools.router.core.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(router.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(router.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            router.route_all_negotiated(
                max_iterations=2,
                use_targeted_ripup=False,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        rip_up_msgs = [msg for msg in flush_print_calls if "Ripping up" in msg]

        # The rip-up loop should have been entered
        assert len(rip_up_msgs) > 0, (
            f"Expected 'Ripping up' message but found none. "
            f"All flush_print calls: {flush_print_calls}"
        )

        # Per-net progress lines should be present
        assert len(reroute_msgs) == 2, (
            f"Expected 2 'Re-routing net' messages (one per net) but found {len(reroute_msgs)}. "
            f"All flush_print calls: {flush_print_calls}"
        )

        # Verify the messages reference the net names
        all_reroute_text = " ".join(reroute_msgs)
        assert "VCC" in all_reroute_text or "GND" in all_reroute_text, (
            f"Expected net names in re-routing messages: {reroute_msgs}"
        )


class TestTargetedRipupRerouteProgressOutput:
    """Tests for per-net progress output in the targeted rip-up reroute loop."""

    def test_flush_print_called_in_targeted_reroute_loop(self):
        """Verify flush_print emits per-net 'Re-routing net' messages in the targeted rip-up loop.

        Mocks overflow detection and find_nets_through_overused_cells to force
        the targeted rip-up path, then asserts per-net progress lines appear.
        """
        router = _make_router_with_two_nets()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        with (
            patch(
                "kicad_tools.router.core.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(router.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(router.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            router.route_all_negotiated(
                max_iterations=2,
                use_targeted_ripup=True,
            )

        targeted_msgs = [msg for msg in flush_print_calls if "targeted rip-up" in msg.lower()]
        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]

        # The targeted rip-up path should have been entered
        assert len(targeted_msgs) > 0, (
            f"Expected 'targeted rip-up' message but found none. "
            f"All flush_print calls: {flush_print_calls}"
        )

        # Per-net progress lines should be present (one per net in nets_to_reroute)
        assert len(reroute_msgs) == 2, (
            f"Expected 2 'Re-routing net' messages (one per net) but found {len(reroute_msgs)}. "
            f"All flush_print calls: {flush_print_calls}"
        )


class TestRerouteProgressFormat:
    """Tests for the format of re-routing progress messages."""

    def test_reroute_message_contains_counter_and_net_name(self):
        """Verify re-routing messages include N/M counter and net name."""
        router = _make_router_with_two_nets()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        with (
            patch(
                "kicad_tools.router.core.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(router.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(router.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            router.route_all_negotiated(
                max_iterations=2,
                use_targeted_ripup=False,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        assert len(reroute_msgs) > 0, "No re-routing messages found"

        for msg in reroute_msgs:
            # Format: "    Re-routing net N/M: <net_name>... (<time>)"
            assert "/" in msg, f"Expected 'N/M' counter in message: {msg}"
            assert "..." in msg, f"Expected '...' suffix in message: {msg}"
            assert any(name in msg for name in ["VCC", "GND", "Net_"]), (
                f"Expected net name in message: {msg}"
            )

    def test_reroute_message_contains_elapsed_time(self):
        """Verify re-routing messages include elapsed time in (N.Ns) format."""
        router = _make_router_with_two_nets()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        with (
            patch(
                "kicad_tools.router.core.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(router.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(router.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            router.route_all_negotiated(
                max_iterations=2,
                use_targeted_ripup=False,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        assert len(reroute_msgs) > 0, "No re-routing messages found"

        for msg in reroute_msgs:
            assert re.search(r"\(\d+\.\d+s\)", msg), (
                f"Expected elapsed time in format '(N.Ns)' in message: {msg}"
            )

    def test_reroute_counter_is_1_indexed(self):
        """Verify the counter starts at 1, not 0 (user-facing convention)."""
        router = _make_router_with_two_nets()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        with (
            patch(
                "kicad_tools.router.core.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(router.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(router.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            router.route_all_negotiated(
                max_iterations=2,
                use_targeted_ripup=False,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        assert len(reroute_msgs) >= 2, (
            f"Expected at least 2 re-routing messages, got {len(reroute_msgs)}"
        )

        # First message should start with "1/2", second with "2/2"
        assert "1/2" in reroute_msgs[0], (
            f"Expected first message to contain '1/2': {reroute_msgs[0]}"
        )
        assert "2/2" in reroute_msgs[1], (
            f"Expected second message to contain '2/2': {reroute_msgs[1]}"
        )


# ---------------------------------------------------------------------------
# Two-Phase Router rip-up reroute progress tests (Issue #2318)
# ---------------------------------------------------------------------------


def _make_two_phase_router():
    """Create a minimal TwoPhaseRouter with two nets for testing reroute progress."""
    autorouter = Autorouter(width=50.0, height=40.0)
    # Net 1: two pads
    pads1 = [
        {"number": "1", "x": 5.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "VCC"},
        {"number": "2", "x": 15.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "VCC"},
    ]
    autorouter.add_component("U1", pads1)
    # Net 2: two pads
    pads2 = [
        {"number": "1", "x": 10.0, "y": 15.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "GND"},
        {"number": "2", "x": 10.0, "y": 25.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "GND"},
    ]
    autorouter.add_component("U2", pads2)

    # Build a TwoPhaseRouter using the autorouter's internals
    def noop_route_net(net):
        return []

    def noop_route_net_with_corridor(net, present_factor, per_net_timeout=None):
        return []

    def noop_mark_route(route):
        pass

    tp = TwoPhaseRouter(
        grid=autorouter.grid,
        router=autorouter.router,
        rules=autorouter.rules,
        net_class_map=None,
        nets=autorouter.nets,
        net_names=autorouter.net_names,
        pads=autorouter.pads,
        routes=list(autorouter.routes),
        routing_failures=[],
        get_net_priority=lambda n: n,
        route_net=noop_route_net,
        route_net_with_corridor=noop_route_net_with_corridor,
        mark_route=noop_mark_route,
    )
    return tp


class TestTwoPhaseRerouteProgressOutput:
    """Tests for per-net progress output during two-phase rip-up reroute iterations."""

    def test_flush_print_called_per_net_in_reroute_loop(self):
        """Verify flush_print emits per-net 'Re-routing net' messages in _detailed_negotiated."""
        tp = _make_two_phase_router()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            # First call (after initial pass): overflow triggers rip-up
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        net_order = [n for n in sorted(tp.nets.keys()) if n != 0]

        with (
            patch(
                "kicad_tools.router.algorithms.two_phase.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(tp.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(tp.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            tp._detailed_negotiated(
                net_order=net_order,
                max_iterations=2,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        rip_up_msgs = [msg for msg in flush_print_calls if "ripping up" in msg]

        # The rip-up loop should have been entered
        assert len(rip_up_msgs) > 0, (
            f"Expected 'ripping up' message but found none. "
            f"All flush_print calls: {flush_print_calls}"
        )

        # Per-net progress lines should be present (one per net)
        assert len(reroute_msgs) == 2, (
            f"Expected 2 'Re-routing net' messages (one per net) but found {len(reroute_msgs)}. "
            f"All flush_print calls: {flush_print_calls}"
        )

    def test_reroute_message_format_includes_counter_and_name(self):
        """Verify two-phase reroute messages include N/M counter, net name, and elapsed time."""
        tp = _make_two_phase_router()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            if overflow_call_count[0] == 1:
                return 2
            return 0

        overused_call_count = [0]

        def mock_overused():
            overused_call_count[0] += 1
            if overused_call_count[0] == 1:
                return list(_FAKE_OVERUSED)
            return []

        def mock_find_nets(self_neg, net_routes, overused_cells):
            if overused_cells:
                return [1, 2]
            return []

        net_order = [n for n in sorted(tp.nets.keys()) if n != 0]

        with (
            patch(
                "kicad_tools.router.algorithms.two_phase.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(tp.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(tp.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            tp._detailed_negotiated(
                net_order=net_order,
                max_iterations=2,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]
        assert len(reroute_msgs) >= 2, (
            f"Expected at least 2 'Re-routing net' messages, got {len(reroute_msgs)}"
        )

        for msg in reroute_msgs:
            # Should contain N/M counter
            assert "/" in msg, f"Expected 'N/M' counter in message: {msg}"
            # Should contain net name
            assert any(name in msg for name in ["VCC", "GND"]), (
                f"Expected net name in message: {msg}"
            )
            # Should contain elapsed time in (N.Ns) format
            assert re.search(r"\(\d+\.\d+s\)", msg), (
                f"Expected elapsed time in format '(N.Ns)' in message: {msg}"
            )

        # Verify 1-indexed counter
        assert "1/2" in reroute_msgs[0], (
            f"Expected first message to contain '1/2': {reroute_msgs[0]}"
        )
        assert "2/2" in reroute_msgs[1], (
            f"Expected second message to contain '2/2': {reroute_msgs[1]}"
        )

    def test_no_reroute_output_when_nets_to_reroute_is_empty(self):
        """Verify no per-net reroute output when nets_to_reroute is empty (converged)."""
        tp = _make_two_phase_router()

        flush_print_calls = []

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        overflow_call_count = [0]

        def mock_overflow():
            overflow_call_count[0] += 1
            # Overflow triggers iteration, but no nets found
            if overflow_call_count[0] == 1:
                return 1
            return 0

        def mock_overused():
            return list(_FAKE_OVERUSED)

        def mock_find_nets(self_neg, net_routes, overused_cells):
            # No nets through overused cells => empty reroute set
            return []

        net_order = [n for n in sorted(tp.nets.keys()) if n != 0]

        with (
            patch(
                "kicad_tools.router.algorithms.two_phase.flush_print",
                side_effect=tracking_flush_print,
            ),
            patch.object(tp.grid, "get_total_overflow", side_effect=mock_overflow),
            patch.object(tp.grid, "find_overused_cells", side_effect=mock_overused),
            patch.object(
                NegotiatedRouter,
                "find_nets_through_overused_cells",
                mock_find_nets,
            ),
        ):
            tp._detailed_negotiated(
                net_order=net_order,
                max_iterations=2,
            )

        reroute_msgs = [msg for msg in flush_print_calls if "Re-routing net" in msg]

        # No per-net reroute messages should appear since nets_to_reroute was empty
        assert len(reroute_msgs) == 0, (
            f"Expected no 'Re-routing net' messages when nets_to_reroute is empty, "
            f"but found {len(reroute_msgs)}: {reroute_msgs}"
        )


# ---------------------------------------------------------------------------
# Issue #2795: targeted_ripup progress-callback visibility tests
# ---------------------------------------------------------------------------


def _make_router_with_three_nets():
    """Three-net variant of ``_make_router_with_two_nets`` for rip-up tests.

    Constructs an :class:`Autorouter` with three two-pin nets so we can
    exercise the ``targeted_ripup(failed_net, blocking_nets={B, C})`` shape
    that produces 1 + 2 = 3 sequential ``route_net_negotiated`` calls.
    """
    router = Autorouter(width=60.0, height=40.0)
    # Net 1: failed net (A)
    pads_a = [
        {"number": "1", "x": 5.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "A"},
        {"number": "2", "x": 25.0, "y": 20.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "A"},
    ]
    router.add_component("U1", pads_a)
    # Net 2: sibling (B) - crosses A's path
    pads_b = [
        {"number": "1", "x": 12.0, "y": 15.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "B"},
        {"number": "2", "x": 12.0, "y": 25.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "B"},
    ]
    router.add_component("U2", pads_b)
    # Net 3: sibling (C)
    pads_c = [
        {"number": "1", "x": 18.0, "y": 15.0, "width": 0.5, "height": 0.5, "net": 3, "net_name": "C"},
        {"number": "2", "x": 18.0, "y": 25.0, "width": 0.5, "height": 0.5, "net": 3, "net_name": "C"},
    ]
    router.add_component("U3", pads_c)
    return router


def _build_pads_by_net(router):
    """Group router pads by net id (mirroring the runtime structure).

    ``router.pads`` is a dict mapping pad-id to Pad; ``router.nets`` maps
    net-id to list of pad-ids.  This mirrors the lookup used by
    ``route_all_negotiated`` when it constructs ``pads_by_net``.
    """
    pads_by_net: dict[int, list] = {}
    for net_id, pad_ids in router.nets.items():
        if net_id == 0:
            continue
        pads_by_net[net_id] = [router.pads[p] for p in pad_ids]
    return pads_by_net


class TestTargetedRipupProgressCallback:
    """Issue #2795: progress_callback fires N+1 times per targeted_ripup call.

    These tests focus on the *inner* loop (``NegotiatedRouter.targeted_ripup``).
    They stub ``route_net_negotiated`` so no actual A* is run; we only verify
    the callback contract and the resulting flush_print output when the
    callback is the one supplied by ``_attempt_blocked_component_ripup_negotiated``.
    """

    def test_progress_callback_invoked_per_step(self):
        """Callback fires once for failed net + once for each sibling (=N+1)."""
        router = _make_router_with_three_nets()
        # Build a NegotiatedRouter mirroring the one constructed inside route_all.
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        # Pre-populate net_routes with stub Route objects so rip_up_nets has
        # something to clear; the exact content doesn't matter because we
        # mock route_net_negotiated.
        net_routes: dict[int, list] = {2: [], 3: []}
        routes_list: list = []

        callback = MagicMock()

        # Stub route_net_negotiated to return [] so we don't run A*.  The
        # progress callback fires BEFORE each call, so we still see N+1
        # invocations even though no routes are produced.
        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", return_value=[]),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=routes_list,
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                progress_callback=callback,
                net_names={1: "A", 2: "B", 3: "C"},
            )

        # 1 failed-net + 2 siblings = 3 callback invocations.
        assert callback.call_count == 3, (
            f"Expected 3 progress_callback invocations (1 failed + 2 siblings), "
            f"got {callback.call_count}"
        )

        # Inspect each call's payload.
        phases_seen = []
        for call_args in callback.call_args_list:
            label, info = call_args.args
            assert label == "ripup_phase", f"Unexpected label {label}"
            assert "phase" in info
            assert "net_name" in info
            assert "index" in info
            assert "total" in info
            assert "elapsed" in info
            assert info["total"] == 3
            phases_seen.append(info["phase"])

        # First payload is the failed-net retry; remaining are siblings.
        assert phases_seen[0] == "failed_net"
        assert phases_seen[1] == "sibling"
        assert phases_seen[2] == "sibling"

        # Index is 1-indexed against total.
        indices = [c.args[1]["index"] for c in callback.call_args_list]
        assert indices == [1, 2, 3], f"Expected indices [1,2,3], got {indices}"

        # Net names propagate from the net_names map.
        names = [c.args[1]["net_name"] for c in callback.call_args_list]
        assert names[0] == "A"
        assert set(names[1:]) == {"B", "C"}

    def test_progress_callback_optional_no_regression(self):
        """When progress_callback=None (default), no exception and no calls."""
        router = _make_router_with_three_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [], 3: []}

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", return_value=[]),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            # Should not raise; existing call sites pass no callback.
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
            )

    def test_progress_callback_exception_does_not_break_ripup(self):
        """A buggy callback raising must not abort the rip-up."""
        router = _make_router_with_three_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [], 3: []}

        def bad_callback(phase, info):
            raise RuntimeError("boom")

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", return_value=[]),
            patch.object(NegotiatedRouter, "rip_up_nets"),
        ):
            # Should not raise despite callback throwing each call.
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                progress_callback=bad_callback,
                net_names={1: "A", 2: "B", 3: "C"},
            )


class TestAttemptBlockedComponentRipupNegotiatedLogging:
    """Issue #2795: ``_attempt_blocked_component_ripup_negotiated`` emits
    per-sibling flush_print lines so users can tell progress from a hang.
    """

    def test_per_sibling_flush_print_lines_with_index_and_elapsed(self):
        """Each rip-up step emits ``rip-up [i/N] for <failed>:`` with elapsed time."""
        router = _make_router_with_three_nets()
        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        pads_by_net = _build_pads_by_net(router)
        net_routes: dict[int, list] = {2: [], 3: []}

        flush_print_calls: list[str] = []

        # Simulate the call-site shape used by
        # ``_attempt_blocked_component_ripup_negotiated``: the same closure,
        # the same flush_print module path.
        failed_name = router.net_names.get(1, "Net_1")

        def _progress(phase_label, info):
            from kicad_tools.cli.progress import flush_print as _fp

            phase = info["phase"]
            if phase == "failed_net":
                action = f"routing failed net {failed_name}"
            else:
                action = f"routing sibling {info['net_name']}"
            _fp(
                f"    rip-up [{info['index']}/{info['total']}] for {failed_name}: "
                f"{action} (elapsed {info['elapsed']:.1f}s)"
            )

        def tracking_flush_print(msg):
            flush_print_calls.append(msg)

        with (
            patch.object(NegotiatedRouter, "route_net_negotiated", return_value=[]),
            patch.object(NegotiatedRouter, "rip_up_nets"),
            patch("kicad_tools.cli.progress.flush_print", side_effect=tracking_flush_print),
        ):
            neg_router.targeted_ripup(
                failed_net=1,
                blocking_nets={2, 3},
                net_routes=net_routes,
                routes_list=[],
                pads_by_net=pads_by_net,
                present_cost_factor=1.0,
                mark_route_callback=lambda r: None,
                ripup_history={},
                max_ripups_per_net=3,
                progress_callback=_progress,
                net_names=router.net_names,
            )

        ripup_msgs = [m for m in flush_print_calls if "rip-up [" in m]
        assert len(ripup_msgs) == 3, (
            f"Expected 3 rip-up progress lines (1 failed + 2 siblings), "
            f"got {len(ripup_msgs)}: {flush_print_calls}"
        )
        # Each message follows the [i/N] for FAILED: action (elapsed Xs) shape.
        for msg in ripup_msgs:
            assert re.search(r"rip-up \[\d+/3\] for", msg), (
                f"Expected '[i/3] for' counter in: {msg}"
            )
            assert re.search(r"elapsed \d+\.\d+s", msg), (
                f"Expected 'elapsed N.Ns' in: {msg}"
            )

        # Counters increase 1..3.
        counters = []
        for msg in ripup_msgs:
            m = re.search(r"\[(\d+)/3\]", msg)
            assert m is not None
            counters.append(int(m.group(1)))
        assert counters == [1, 2, 3], f"Expected ordered [1,2,3], got {counters}"
