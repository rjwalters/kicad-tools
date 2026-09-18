"""Tests for the report-only RoutingPlan sidecar (Issue #5519, Epic #5510 Phase 1).

Not to be confused with ``boards/03-usb-joystick/routing_plan.py`` /
``routing-plan.json`` (an unrelated board-03 copper replay recipe) or
``tests/test_board03_routing_plan_alignment.py`` (which tests that recipe).
This file tests ``kicad_tools.router.routing_plan.RoutingPlan``.
"""

from __future__ import annotations

import json
import os

import pytest

from kicad_tools.router.global_router import CorridorAssignment, GlobalRoutingResult
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.region_graph import RegionGraph
from kicad_tools.router.routing_plan import (
    EdgePlanEntry,
    NetPlanEntry,
    OverflowReport,
    RegionInfo,
    RoutingPlan,
)
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.router.sparse import Corridor, Waypoint


def _make_two_region_graph(num_layers: int = 2) -> RegionGraph:
    """A 2x1 tile graph: regions 0 and 1, a single undirected edge (0, 1)."""
    return RegionGraph(
        board_width=10.0,
        board_height=5.0,
        num_cols=2,
        num_rows=1,
        trace_pitch=2.0,
        num_layers=num_layers,
    )


def _corridor_assignment(net: int, region_path: list[int], layer: int = 0) -> CorridorAssignment:
    waypoints = [
        Waypoint(x=float(i), y=0.0, layer=layer, waypoint_type="global") for i in region_path
    ]
    corridor = Corridor.from_waypoints(waypoints=waypoints, net=net, width=0.5)
    return CorridorAssignment(
        net=net,
        region_path=list(region_path),
        corridor=corridor,
        waypoint_coords=[(w.x, w.y) for w in waypoints],
        layer=layer,
    )


class TestFromGlobalResult:
    """RoutingPlan.from_global_result on a hand-built RegionGraph."""

    def test_assigned_net_serializes_region_path_and_edge(self):
        graph = _make_two_region_graph(num_layers=2)
        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        # Mirror what GlobalRouter.route_net does on a real assignment:
        # bump the graph's own utilization counters.
        graph.update_utilization([0, 1], layer=0)
        result = GlobalRoutingResult(
            assignments={1: assignment},
            failed_nets=[],
            region_graph=graph,
            iterations=0,
            final_overflow=0,
        )

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=0.25,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        assert plan.schema_version == 1
        assert plan.source["tile_mm"] == 5.0
        assert plan.source["cols"] == 2
        assert plan.source["rows"] == 1
        assert plan.layers == {"signal": [0, 1], "plane": []}

        net_entry = plan.nets[1]
        assert net_entry.status == "assigned"
        assert net_entry.name == "N1"
        assert net_entry.net_class is None
        assert net_entry.pitch_mm == pytest.approx(0.4)
        assert net_entry.layer_set == [0]
        assert net_entry.region_path == [0, 1]

        assert len(plan.edges) == 1
        edge = plan.edges[0]
        assert (edge.a, edge.b) == (0, 1)
        assert edge.nets == [1]
        assert edge.demand == 1
        assert edge.capacity >= 1

        # Regions referenced by the assigned path are included with bounds.
        assert set(plan.regions.keys()) == {0, 1}
        assert isinstance(plan.regions[0], RegionInfo)

        assert plan.overflow_report is not None
        assert plan.overflow_report.total_overflow == graph.get_total_overflow()
        assert plan.overflow_report.overflowed_edges == len(graph.get_overflowed_edges())
        assert plan.overflow_report.feasible is True

    def test_net_class_map_resolves_pitch_and_class(self):
        graph = _make_two_region_graph(num_layers=1)
        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        result = GlobalRoutingResult(
            assignments={1: assignment}, region_graph=graph, iterations=1, final_overflow=0
        )
        net_class_map = {
            "DQ3": NetClassRouting(name="DDR", priority=2, trace_width=0.3, clearance=0.15)
        }

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "DQ3"},
            net_class_map=net_class_map,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=0.0,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        entry = plan.nets[1]
        assert entry.net_class == "DDR"
        assert entry.pitch_mm == pytest.approx(0.45)

    def test_overflow_matches_graph_queries_on_forced_overflow(self):
        """AC-2: total_overflow / overflowed_edges match RegionGraph exactly.

        Manually oversubscribes the canonical forward (0, 1) edge -- the
        same edge object ``get_total_overflow`` / ``get_overflowed_edges``
        count (see ``RoutingPlan.from_global_result``'s docstring on
        directed-edge selection) -- and confirms the serialized overflow
        report is derived from those exact queries, not a hand-summed
        value that could silently drift.
        """
        graph = _make_two_region_graph(num_layers=1)
        edge = graph._edge_lookup[(0, 1)]
        edge.utilization = edge.capacity + 3

        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        result = GlobalRoutingResult(
            assignments={1: assignment},
            failed_nets=[2],
            region_graph=graph,
            iterations=4,
            final_overflow=3,
        )

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1, 2],
            net_names={1: "N1", 2: "N2"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=1.0,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        assert plan.overflow_report.total_overflow == graph.get_total_overflow() > 0
        assert plan.overflow_report.overflowed_edges == len(graph.get_overflowed_edges()) == 1
        assert plan.overflow_report.failed_nets == [2]
        assert plan.overflow_report.feasible is False
        assert plan.nets[2].status == "failed"

        # The single reported edge carries the exact overflow figure.
        assert len(plan.edges) == 1
        assert plan.edges[0].overflow == graph.get_total_overflow()

    def test_edge_overflow_agrees_on_reverse_direction_traffic(self):
        """Issue #5544: descending-only traffic must not report overflow 0.

        A region pair ``(a, b)`` is backed by two directed ``RegionEdge``
        objects, and ``update_utilization()`` only bumps the one matching
        the path's traversal direction.  ``get_total_overflow()`` sums both
        (PR #5540), so an ``EdgePlanEntry`` that read only the ascending
        ``(a, b)`` edge reported ``overflow == 0`` for a pair whose traffic
        ran entirely in the descending ``b -> a`` direction -- disagreeing
        with the plan's own ``overflow_report``.  Built the same way as
        ``test_global_router.py::
        test_total_overflow_agrees_on_reverse_direction_traffic``.
        """
        graph = RegionGraph(
            board_width=10.0,
            board_height=5.0,
            num_cols=2,
            num_rows=1,
            trace_pitch=2.0,
            num_layers=1,
            base_capacity=1,
        )

        # Descending-ID traversal only: region 1 -> region 0.
        for _ in range(5):
            graph.update_utilization([1, 0], layer=0)

        # Precondition: all traffic (and all overflow) sits on the
        # descending edge; the ascending edge this code used to read is 0.
        assert graph._edge_lookup[(0, 1)].overflow == 0
        assert graph._edge_lookup[(1, 0)].overflow == 3
        assert graph.get_total_overflow() == 3

        assignment = _corridor_assignment(net=1, region_path=[1, 0], layer=0)
        result = GlobalRoutingResult(
            assignments={1: assignment},
            failed_nets=[],
            region_graph=graph,
            iterations=4,
            final_overflow=3,
        )

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=1.0,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        assert len(plan.edges) == 1
        edge = plan.edges[0]
        assert (edge.a, edge.b) == (0, 1)
        # Before the fix this was 0 while total_overflow was 3.
        assert edge.overflow == graph.get_total_overflow() == 3
        assert plan.overflow_report is not None
        assert edge.overflow == plan.overflow_report.total_overflow
        # Demand likewise counts traffic in both directions.
        assert edge.demand == 5

    def test_edge_demand_sums_both_directions_per_layer(self):
        """Issue #5544: per-layer demand also sums both traversal directions."""
        graph = _make_two_region_graph(num_layers=2)
        graph.update_utilization([0, 1], layer=0)  # ascending
        graph.update_utilization([1, 0], layer=0)  # descending
        graph.update_utilization([1, 0], layer=1)  # descending, other layer

        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        result = GlobalRoutingResult(
            assignments={1: assignment},
            failed_nets=[],
            region_graph=graph,
            iterations=1,
            final_overflow=0,
        )

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=0.5,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        assert len(plan.edges) == 1
        edge = plan.edges[0]
        assert edge.demand == 3
        assert edge.layers["0"]["demand"] == 2
        assert edge.layers["1"]["demand"] == 1
        # Capacity is symmetric across the pair, so it is NOT doubled.
        assert edge.capacity == graph._edge_lookup[(0, 1)].capacity
        assert edge.layers["0"]["capacity"] == graph._edge_lookup[(0, 1)].layer_capacity[0]

    def test_pour_skipped_and_single_pad_and_no_endpoints_status(self):
        graph = _make_two_region_graph(num_layers=1)
        result = GlobalRoutingResult(assignments={}, region_graph=graph, iterations=0)

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],  # candidate but never assigned/failed -> no_endpoints
            net_names={1: "N1", 2: "GND", 3: "TP1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=0.0,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
            pour_skipped=[2],
            single_pad=[3],
        )

        assert plan.nets[1].status == "no_endpoints"
        assert plan.nets[2].status == "pour_skipped"
        assert plan.nets[3].status == "single_pad"
        assert plan.edges == []

    def test_layer_set_is_single_element_list(self):
        graph = _make_two_region_graph(num_layers=2)
        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=1)
        result = GlobalRoutingResult(assignments={1: assignment}, region_graph=graph, iterations=0)

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=0.0,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )
        assert plan.nets[1].layer_set == [1]
        assert isinstance(plan.nets[1].layer_set, list)


class TestRoundTrip:
    """to_dict / from_dict round-trip through JSON."""

    def test_round_trip_via_json(self):
        graph = _make_two_region_graph(num_layers=2)
        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        result = GlobalRoutingResult(assignments={1: assignment}, region_graph=graph, iterations=2)

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            tile_mm=5.0,
            elapsed_s=0.5,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        raw = json.dumps(plan.to_dict())
        restored = RoutingPlan.from_dict(json.loads(raw))

        assert restored.to_dict() == plan.to_dict()
        assert isinstance(restored.nets[1], NetPlanEntry)
        assert isinstance(restored.edges[0], EdgePlanEntry)
        assert isinstance(restored.regions[0], RegionInfo)
        assert isinstance(restored.overflow_report, OverflowReport)

    def test_net_status_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            NetPlanEntry(
                name="N1",
                net_class=None,
                pitch_mm=0.4,
                layer_set=[],
                region_path=[],
                status="bogus",
            )


class TestSummaryLine:
    def test_summary_line_format(self):
        graph = _make_two_region_graph(num_layers=1)
        assignment = _corridor_assignment(net=1, region_path=[0, 1], layer=0)
        result = GlobalRoutingResult(assignments={1: assignment}, region_graph=graph, iterations=0)

        plan = RoutingPlan.from_global_result(
            result,
            graph,
            net_order=[1],
            net_names={1: "N1"},
            net_class_map=None,
            layer_stack=LayerStack.two_layer(),
            tile_mm=5.0,
            elapsed_s=1.25,
            default_trace_width=0.2,
            default_trace_clearance=0.2,
        )

        line = plan.summary_line()
        assert line == "Routing plan: 1 nets, 1 edges, overflow 0 on 0 edges (1.2s)"


class TestWriteSidecar:
    def test_write_sidecar_success(self, tmp_path):
        plan = RoutingPlan()
        target = tmp_path / "board.routing_plan.json"
        assert plan.write_sidecar(target) is True
        assert target.exists()
        assert json.loads(target.read_text())["schema_version"] == 1

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
    def test_write_sidecar_blocked_dir_warns_not_raises(self, tmp_path, capsys):
        blocked_dir = tmp_path / "blocked"
        blocked_dir.mkdir()
        blocked_dir.chmod(0o500)
        target = blocked_dir / "board.routing_plan.json"
        try:
            plan = RoutingPlan()
            result = plan.write_sidecar(target)
            assert result is False
            captured = capsys.readouterr()
            assert "Warning: could not write routing-plan sidecar" in captured.out
        finally:
            blocked_dir.chmod(0o700)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
    def test_write_sidecar_blocked_dir_quiet_suppresses_warning(self, tmp_path, capsys):
        blocked_dir = tmp_path / "blocked_quiet"
        blocked_dir.mkdir()
        blocked_dir.chmod(0o500)
        target = blocked_dir / "board.routing_plan.json"
        try:
            plan = RoutingPlan()
            result = plan.write_sidecar(target, quiet=True)
            assert result is False
            captured = capsys.readouterr()
            assert captured.out == ""
        finally:
            blocked_dir.chmod(0o700)
