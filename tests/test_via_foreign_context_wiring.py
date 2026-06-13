"""Tests for Issue #2953: wire ``set_via_foreign_context`` into negotiated
and N-port routing paths.

PR #2952 (closes #2947) introduced the world-coord via clearance check in
``Router._check_via_placement_cached`` and wired the boards-wide foreign
context push at ``Autorouter.route_net()``.  However, three sibling entry
points bypass ``route_net()`` and therefore never push the context:

  - ``_route_net_negotiated``      (default negotiated strategy)
  - ``_route_net_with_mst_edges``  (N-port interleaved path)
  - ``_route_net_with_corridor``   (corridor-aware variant)

Issue #2953 patches each method with a single helper call
(``self._update_router_via_foreign_context(net)``) right after the
early-return guards.  This test file spies on the helper to assert each
entry point invokes it exactly once per call, with the correct
``current_net`` argument.
"""

from unittest.mock import patch

import pytest

from kicad_tools.router.core import Autorouter, MSTEdgeInfo


@pytest.fixture
def router_with_two_pads():
    """Build an Autorouter populated with a single two-pad net (net=1)."""
    router = Autorouter(width=50.0, height=40.0)
    pads = [
        {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
    ]
    router.add_component("R1", pads)
    return router


class TestForeignContextWiringNegotiated:
    """``_route_net_negotiated`` must invoke the foreign-context setter."""

    def test_negotiated_path_calls_helper_with_net(self, router_with_two_pads):
        router = router_with_two_pads
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            router._route_net_negotiated(1, present_cost_factor=0.5)
            assert spy.call_count == 1, (
                "Issue #2953: _route_net_negotiated must call "
                "_update_router_via_foreign_context exactly once per net"
            )
            spy.assert_called_with(1)

    def test_negotiated_skips_helper_for_unknown_net(self, router_with_two_pads):
        """Early-return path (unknown net) must NOT push context."""
        router = router_with_two_pads
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            result = router._route_net_negotiated(999, present_cost_factor=0.5)
            assert result == []
            assert spy.call_count == 0, (
                "Unknown-net early-return must short-circuit before the "
                "foreign-context push (avoids unnecessary work)."
            )


class TestForeignContextWiringMSTEdges:
    """``_route_net_with_mst_edges`` must invoke the foreign-context setter."""

    def test_mst_path_calls_helper_with_net(self, router_with_two_pads):
        router = router_with_two_pads
        # MST edges for our 2-pad net: a single edge 0 -> 1.
        edges = [
            MSTEdgeInfo(
                net_id=1,
                edge_index=0,
                source_idx=0,
                target_idx=1,
                distance=5.0,
                is_first=True,
            ),
        ]
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            router._route_net_with_mst_edges(1, edges)
            assert spy.call_count == 1, (
                "Issue #2953: _route_net_with_mst_edges must call "
                "_update_router_via_foreign_context exactly once per net"
            )
            spy.assert_called_with(1)

    def test_mst_skips_helper_for_unknown_net(self, router_with_two_pads):
        router = router_with_two_pads
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            result = router._route_net_with_mst_edges(999, [])
            assert result == []
            assert spy.call_count == 0


class TestForeignContextWiringCorridor:
    """``_route_net_with_corridor`` must invoke the foreign-context setter."""

    def test_corridor_path_calls_helper_with_net(self, router_with_two_pads):
        router = router_with_two_pads
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            router._route_net_with_corridor(1, present_cost_factor=0.5)
            assert spy.call_count == 1, (
                "Issue #2953: _route_net_with_corridor must call "
                "_update_router_via_foreign_context exactly once per net"
            )
            spy.assert_called_with(1)

    def test_corridor_skips_helper_for_unknown_net(self, router_with_two_pads):
        router = router_with_two_pads
        with patch.object(
            router,
            "_update_router_via_foreign_context",
            wraps=router._update_router_via_foreign_context,
        ) as spy:
            result = router._route_net_with_corridor(999, present_cost_factor=0.5)
            assert result == []
            assert spy.call_count == 0


class TestForeignContextSetterReceivesNetArg:
    """Belt-and-suspenders: spy on the Router-level setter to confirm
    the helper actually plumbs through to ``set_via_foreign_context``
    when the C++ stub is absent (i.e. the Python backend path)."""

    def test_setter_called_per_entry_point(self, router_with_two_pads):
        router = router_with_two_pads
        # All three entry points share the same underlying Router, so we
        # patch ``set_via_foreign_context`` directly.
        if not hasattr(router.router, "set_via_foreign_context"):
            pytest.skip("Router backend lacks set_via_foreign_context hook")

        with patch.object(
            router.router,
            "set_via_foreign_context",
            wraps=router.router.set_via_foreign_context,
        ) as setter_spy:
            router._route_net_negotiated(1, present_cost_factor=0.5)
            router._route_net_with_corridor(1, present_cost_factor=0.5)
            router._route_net_with_mst_edges(
                1,
                [
                    MSTEdgeInfo(
                        net_id=1,
                        edge_index=0,
                        source_idx=0,
                        target_idx=1,
                        distance=5.0,
                        is_first=True,
                    )
                ],
            )
            # Three calls, one per entry point.  Each must include the
            # current_net's foreign context (here: empty, since net=1 is
            # the only net on the board).
            assert setter_spy.call_count == 3, (
                "Each of the 3 entry points must fire the setter exactly "
                f"once; got {setter_spy.call_count}"
            )
            for call in setter_spy.call_args_list:
                # foreign_pads / foreign_tracks are passed as kwargs by
                # the helper; both should be empty lists when only net=1
                # exists on the board.
                kwargs = call.kwargs
                assert "foreign_pads" in kwargs
                assert "foreign_tracks" in kwargs
                # Same-net pads filtered out -> empty for our fixture.
                assert kwargs["foreign_pads"] == []
                assert kwargs["foreign_tracks"] == []
