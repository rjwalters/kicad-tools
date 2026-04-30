"""Tests for connectivity-aware net counting in two-phase summary (#2352).

The two-phase routing summary should only count nets as 'routed' when all
pads in the net are connected, not just when any route segment exists.
Disconnected escape stubs should not inflate the 'nets routed' count.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Layer, Pad, Route, Segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pad(net: int, x: float, y: float, ref: str = "U1", pin: str = "1") -> Pad:
    return Pad(
        x=x, y=y, width=0.5, height=0.5,
        net=net, net_name=f"Net{net}",
        layer=Layer.F_CU, ref=ref, pin=pin,
    )


def _make_segment(net: int, x1: float, y1: float, x2: float, y2: float) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=0, net=net)


def _make_route(net: int, segments: list[Segment]) -> Route:
    return Route(net=net, net_name=f"Net{net}", segments=segments)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectivityAwareNetCount:
    """Verify that validate_net_connectivity correctly distinguishes
    fully-connected nets from those with only stub/partial routes,
    which is the mechanism used by the two-phase summary (#2352)."""

    def test_connected_net_is_reported_connected(self):
        """A net where a segment spans both pads is fully connected."""
        pad_a = _make_pad(1, 0.0, 0.0)
        pad_b = _make_pad(1, 10.0, 0.0, pin="2")
        route = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])

        result = validate_net_connectivity([route], {1: [pad_a, pad_b]})
        assert result[1]["connected"] is True
        assert result[1]["connected_pads"] == 2

    def test_stub_net_is_reported_disconnected(self):
        """A net where the segment only reaches one pad is not connected."""
        pad_a = _make_pad(1, 0.0, 0.0)
        pad_b = _make_pad(1, 10.0, 0.0, pin="2")
        # Stub goes away from pad_b (endpoint at 5, 5 -- far from pad_b at 10, 0)
        stub = _make_route(1, [_make_segment(1, 0.0, 0.0, 5.0, 5.0)])

        result = validate_net_connectivity([stub], {1: [pad_a, pad_b]})
        assert result[1]["connected"] is False
        assert result[1]["connected_pads"] == 1

    def test_counting_logic_matches_issue_expectation(self):
        """The connected-count formula from two_phase.py should exclude
        partially routed nets."""
        # Net 1: fully connected
        pad_1a = _make_pad(1, 0.0, 0.0, ref="U1", pin="1")
        pad_1b = _make_pad(1, 10.0, 0.0, ref="U1", pin="2")
        route_1 = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])

        # Net 2: stub only (disconnected fragment)
        pad_2a = _make_pad(2, 0.0, 5.0, ref="U2", pin="1")
        pad_2b = _make_pad(2, 10.0, 5.0, ref="U2", pin="2")
        stub_2 = _make_route(2, [_make_segment(2, 0.0, 5.0, 3.0, 7.0)])

        all_routes = [route_1, stub_2]
        net_pads = {1: [pad_1a, pad_1b], 2: [pad_2a, pad_2b]}
        connectivity = validate_net_connectivity(all_routes, net_pads)

        # This is the same formula used in the fixed two_phase.py
        nets_with_segments = len({r.net for r in all_routes})
        connected_nets = sum(
            1 for info in connectivity.values() if info["connected"]
        )

        assert nets_with_segments == 2, "Both nets have segments"
        assert connected_nets == 1, "Only one net is fully connected"

    def test_no_pads_fallback(self):
        """When net_pads is empty, validate_net_connectivity returns empty
        and the fallback (nets_with_segments) should be used."""
        route = _make_route(1, [_make_segment(1, 0.0, 0.0, 10.0, 0.0)])
        result = validate_net_connectivity([route], {})
        assert len(result) == 0
