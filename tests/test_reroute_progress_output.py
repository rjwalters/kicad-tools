"""Tests for per-net progress output during rip-up-and-reroute iterations (Issue #1265).

These tests verify that flush_print is called with per-net progress messages
during both the targeted and full rip-up reroute loops in route_all_negotiated().
"""

import re
from unittest.mock import patch

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
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
