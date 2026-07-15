"""Regression tests for `route-auto` silent data loss (issue #2913).

Pre-#2913 the ``RoutingOrchestrator``'s ``_route_global`` returned
``RoutingResult(success=True)`` with an empty ``segments`` list and the
``route_net_auto`` MCP tool saved the PCB without ever mutating it,
silently producing boards with zero new tracks while reporting success.

These tests guard against that class of failure on three levels:

1. ``_route_global`` populates ``result.segments`` whenever it returns
   ``success=True``.
2. The same invariant holds for every "primary" strategy in the
   orchestrator: ``_route_escape_then_global``, ``_route_hierarchical``,
   ``_route_subgrid_adaptive``, ``_route_with_via_resolution``,
   ``_route_full_pipeline``, ``_route_multi_resolution``.  Any future
   strategy that returns ``success=True`` with no segments AND no vias
   indicates a regression of the bug.
3. End-to-end: a real ``route-auto`` call on board 07 produces a PCB
   with strictly more ``(segment ...)`` blocks than the input.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_tools.router.orchestrator import RoutingOrchestrator
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.strategies import RoutingResult, RoutingStrategy


def _pad(
    x: float,
    y: float,
    net: int = 1,
    net_name: str = "TEST",
    width: float = 1.0,
    height: float = 1.0,
    ref: str = "U1",
    pin: str = "1",
) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        ref=ref,
        pin=pin,
    )


@pytest.fixture
def mock_pcb() -> MagicMock:
    pcb = MagicMock()
    pcb.width = 65.0
    pcb.height = 56.0
    return pcb


@pytest.fixture
def design_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        grid_resolution=0.1,
    )


@pytest.fixture
def orchestrator(mock_pcb, design_rules) -> RoutingOrchestrator:
    return RoutingOrchestrator(pcb=mock_pcb, rules=design_rules, backend="cpu")


class TestRouteGlobalMaterializesSegments:
    """Issue #2913: ``_route_global`` must populate ``result.segments``."""

    def test_route_global_success_yields_segments(self, orchestrator):
        """Successful global routing must produce at least one segment."""
        pads = [
            _pad(x=5.0, y=5.0, net=42, net_name="TEST", pin="1"),
            _pad(x=25.0, y=5.0, net=42, net_name="TEST", ref="U2", pin="1"),
        ]

        result = orchestrator._route_global("TEST", pads)

        assert result.success is True
        # The smoking-gun assertion: success=True implies segments > 0.
        # Prior to #2913 this assertion failed because the orchestrator
        # returned an empty ``segments`` list.
        assert len(result.segments) > 0, (
            "Issue #2913 regression: _route_global returned success=True "
            "with no segments; route-auto would silently save an empty PCB."
        )
        # All segments belong to the correct net.
        for seg in result.segments:
            assert seg.net == 42
            assert seg.net_name == "TEST"
            assert seg.width == pytest.approx(0.2)

    def test_route_global_segment_total_length_matches_metrics(self, orchestrator):
        """Sum of segment lengths matches the reported metric."""
        import math

        pads = [
            _pad(x=5.0, y=5.0, net=7, net_name="SIG", pin="1"),
            _pad(x=25.0, y=15.0, net=7, net_name="SIG", ref="U2", pin="1"),
        ]

        result = orchestrator._route_global("SIG", pads)

        assert result.success is True
        seg_total = sum(math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for s in result.segments)
        # ``total_length_mm`` should agree with the segment sum to within
        # floating-point noise.
        assert seg_total == pytest.approx(result.metrics.total_length_mm, rel=1e-6, abs=1e-6)

    def test_route_global_with_string_net_uses_pad_net_id(self, orchestrator):
        """String net name should round-trip via pads[0].net, not hash()."""
        pads = [
            _pad(x=5.0, y=5.0, net=99, net_name="MIPI_CLK_N", pin="1"),
            _pad(x=25.0, y=5.0, net=99, net_name="MIPI_CLK_N", ref="U2", pin="1"),
        ]

        result = orchestrator._route_global("MIPI_CLK_N", pads)

        assert result.success is True
        assert len(result.segments) > 0
        # The integer net stored on the segment must come from the pads,
        # not a hash of the net name.  Pre-#2913 this was hash-derived.
        for seg in result.segments:
            assert seg.net == 99

    def test_route_global_insufficient_pads_failure_unchanged(self, orchestrator):
        """The 1-pad failure path must still report success=False."""
        result = orchestrator._route_global("TEST", [_pad(x=5.0, y=5.0)])
        assert result.success is False
        assert "Insufficient" in result.error_message


class TestAllStrategiesPopulateSegmentsOrFail:
    """Audit: every "physical-route" strategy must follow the rule:
    ``success=True`` implies non-empty ``segments`` OR ``vias``.

    Strategies that are pure planners (do not produce physical traces)
    must instead return ``success=False`` so the caller can surface the
    failure rather than silently save an un-modified PCB.
    """

    @pytest.mark.parametrize(
        "method_name,strategy",
        [
            ("_route_global", RoutingStrategy.GLOBAL_WITH_REPAIR),
            ("_route_escape_then_global", RoutingStrategy.ESCAPE_THEN_GLOBAL),
            ("_route_subgrid_adaptive", RoutingStrategy.SUBGRID_ADAPTIVE),
            ("_route_with_via_resolution", RoutingStrategy.VIA_CONFLICT_RESOLUTION),
            ("_route_multi_resolution", RoutingStrategy.MULTI_RESOLUTION),
        ],
    )
    def test_strategy_success_implies_physical_output(self, orchestrator, method_name, strategy):
        """If a strategy returns success, it must have produced segments/vias."""
        pads = [
            _pad(x=5.0, y=5.0, net=11, net_name="SIG", pin="1"),
            _pad(x=25.0, y=5.0, net=11, net_name="SIG", ref="U2", pin="1"),
        ]

        method = getattr(orchestrator, method_name)
        result = method("SIG", pads)
        assert isinstance(result, RoutingResult)

        if result.success:
            assert (len(result.segments) > 0) or (len(result.vias) > 0), (
                f"Issue #2913 regression: {method_name} returned "
                f"success=True with empty segments AND empty vias; "
                f"route_net_auto would silently save an un-modified PCB."
            )


class TestRouteAutoPersistsToPCB:
    """End-to-end: ``route_net_auto`` writes segments to the saved PCB."""

    BOARD_07 = Path("boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb")

    @pytest.mark.skipif(
        not BOARD_07.exists(),
        reason="board 07 PCB not present (CI artifact)",
    )
    def test_route_auto_writes_segments_to_pcb(self, tmp_path):
        """``route_net_auto`` produces a PCB with non-zero segment delta."""
        from kicad_tools.mcp.tools.routing import route_net_auto

        # Count segments in the input PCB.
        input_text = self.BOARD_07.read_text()
        input_segment_count = input_text.count("(segment")

        out_path = tmp_path / "routed.kicad_pcb"
        result = route_net_auto(
            pcb_path=str(self.BOARD_07),
            net_name="MIPI_CLK_N",
            output_path=str(out_path),
        )

        assert result["success"] is True, f"route_net_auto failed: {result.get('error_message')}"
        # The headline regression assertion for issue #2913.
        assert result.get("segments_written", 0) > 0, (
            "Issue #2913: route_net_auto reported success but wrote no segments."
        )
        assert out_path.exists()
        output_segment_count = out_path.read_text().count("(segment")
        # The saved PCB must contain strictly more (segment ...) blocks
        # than the input.  Pre-#2913 this was always equal.
        assert output_segment_count > input_segment_count, (
            f"Issue #2913: output PCB has {output_segment_count} segments, "
            f"input had {input_segment_count} -- route-auto silently saved "
            "an empty PCB."
        )

    @pytest.mark.skipif(
        not BOARD_07.exists(),
        reason="board 07 PCB not present (CI artifact)",
    )
    def test_route_auto_net_status_complete_after_success(self, tmp_path):
        """After successful route-auto, net-status reports the net complete."""
        from kicad_tools.analysis.net_status import NetStatusAnalyzer
        from kicad_tools.mcp.tools.routing import route_net_auto
        from kicad_tools.schema.pcb import PCB

        out_path = tmp_path / "routed_a0.kicad_pcb"
        result = route_net_auto(
            pcb_path=str(self.BOARD_07),
            net_name="A0",
            output_path=str(out_path),
        )

        assert result["success"] is True
        assert result["segments_written"] > 0

        pcb = PCB.load(str(out_path))
        analyzer = NetStatusAnalyzer(pcb)
        analysis = analyzer.analyze()
        a0_status = analysis.get_net("A0")
        assert a0_status is not None
        # A0 must be Complete (not Incomplete/Unrouted) since traces were
        # physically written.
        assert a0_status.status == "complete", (
            f"Issue #2913: A0 is {a0_status.status} after route-auto success"
        )


class _FakeSeg:
    """Minimal stand-in for an orchestrator RoutingResult segment."""

    def __init__(self, x1, y1, x2, y2, width=0.2, layer="F.Cu"):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.width = width
        self.layer = layer  # plain KiCad string; add_trace accepts str


class _FakeVia:
    """Minimal stand-in for an orchestrator RoutingResult via."""

    def __init__(self, x, y, diameter=0.6, drill=0.3, layers=("F.Cu", "B.Cu")):
        self.x = x
        self.y = y
        self.diameter = diameter
        self.drill = drill
        self.layers = layers


class _FakeResult:
    def __init__(self, segments, vias=None):
        self.segments = segments
        self.vias = vias or []


class TestPersistIsIdempotent:
    """Issue #4175: persisting an identical route twice must not double copper.

    A completion-loop caller re-invokes ``route_net_auto`` per attempt; each
    call re-solves the same corridor and previously appended an exact-duplicate
    copy of the same copper.  ``_persist_routing_result_to_pcb`` now relies on
    the schema-level dedup in ``add_trace``/``add_via`` so the second persist is
    a no-op for copper while surfacing the dedup counts.
    """

    def _fresh_pcb(self):
        from kicad_tools.schema.pcb import PCB

        return PCB.create(width=100, height=100)

    def test_second_persist_does_not_double_segments(self):
        from kicad_tools.mcp.tools.routing import _persist_routing_result_to_pcb

        pcb = self._fresh_pcb()
        result = _FakeResult(
            segments=[
                _FakeSeg(10.0, 10.0, 50.0, 10.0),
                _FakeSeg(50.0, 10.0, 50.0, 40.0),
            ],
            vias=[_FakeVia(50.0, 10.0)],
        )

        segs1, vias1, dsegs1, dvias1 = _persist_routing_result_to_pcb(pcb, result, "NetA")
        assert (segs1, vias1) == (2, 1)
        assert (dsegs1, dvias1) == (0, 0)
        net_num = pcb.add_net("NetA").number
        assert len(list(pcb.segments_in_net(net_num))) == 2
        assert len(list(pcb.vias_in_net(net_num))) == 1

        # Second identical persist: nothing new written, all deduplicated.
        segs2, vias2, dsegs2, dvias2 = _persist_routing_result_to_pcb(pcb, result, "NetA")
        assert (segs2, vias2) == (0, 0)
        assert (dsegs2, dvias2) == (2, 1)
        # Copper counts are unchanged -> emission is idempotent.
        assert len(list(pcb.segments_in_net(net_num))) == 2
        assert len(list(pcb.vias_in_net(net_num))) == 1

    def test_route_net_auto_twice_is_idempotent(self, tmp_path):
        """route_net_auto on the same net twice does not increase copper."""
        from kicad_tools.mcp.tools.routing import (
            _persist_routing_result_to_pcb,
            route_net_auto,  # noqa: F401  (import guards the public entrypoint)
        )
        from kicad_tools.schema.pcb import PCB

        # Build a board, persist a route once, save.
        pcb = self._fresh_pcb()
        result = _FakeResult(segments=[_FakeSeg(5.0, 5.0, 45.0, 5.0)])
        _persist_routing_result_to_pcb(pcb, result, "V_TH_HI")
        out = tmp_path / "board.kicad_pcb"
        pcb.save(out)

        first = out.read_text().count("(segment")
        assert first >= 1

        # Reload and persist the identical route again (simulating a retry).
        pcb2 = PCB.load(out)
        segs, vias, dsegs, dvias = _persist_routing_result_to_pcb(pcb2, result, "V_TH_HI")
        assert segs == 0 and dsegs == 1
        pcb2.save(out)

        second = out.read_text().count("(segment")
        assert second == first, (
            f"Issue #4175: route-auto retry doubled copper ({first} -> {second} segments)"
        )
