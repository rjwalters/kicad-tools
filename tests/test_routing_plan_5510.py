"""Tests for the report-only RoutingPlan sidecar (Issue #5519, Epic #5510 Phase 1)
and for the plan stage that runs on every default route (Issue #5520, Phase 1b).

Not to be confused with ``boards/03-usb-joystick/routing_plan.py`` /
``routing-plan.json`` (an unrelated board-03 copper replay recipe) or
``tests/test_board03_routing_plan_alignment.py`` (which tests that recipe).
This file tests ``kicad_tools.router.routing_plan.RoutingPlan``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kicad_tools.router.core import Autorouter, KeepoutRuleArea
from kicad_tools.router.global_router import CorridorAssignment, GlobalRouter, GlobalRoutingResult
from kicad_tools.router.layers import LayerDefinition, LayerStack, LayerType
from kicad_tools.router.primitives import Layer, Route, Segment, Via
from kicad_tools.router.region_graph import RegionGraph
from kicad_tools.router.routing_plan import (
    EdgePlanEntry,
    NetPlanEntry,
    OverflowReport,
    RegionInfo,
    RoutingPlan,
    build_plan,
    collect_blockage_rects,
    select_plan_nets,
    signal_layer_indices,
)
from kicad_tools.router.rules import DesignRules, NetClassRouting
from kicad_tools.router.sparse import Corridor, Waypoint

REPO_ROOT = Path(__file__).resolve().parents[1]


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


# =============================================================================
# Plan stage on every default route (Issue #5520, Epic #5510 Phase 1b)
# =============================================================================


def _route_signature(routes) -> list:
    """Order-independent copper signature (segments + vias per net)."""
    out = []
    for route in routes:
        segs = tuple(
            (
                round(s.start[0], 6),
                round(s.start[1], 6),
                round(s.end[0], 6),
                round(s.end[1], 6),
                s.layer,
                round(s.width, 6),
            )
            for s in route.segments
        )
        vias = tuple((round(v.x, 6), round(v.y, 6)) for v in route.vias)
        out.append((route.net, segs, vias))
    return sorted(out)


def _plain_autorouter(
    with_single_pad_net: bool = False,
    layer_stack: LayerStack | None = None,
) -> Autorouter:
    """A small NON-dense board: no BGA/fine-pitch package, so ``kct route``
    would take the plain ``route_all_negotiated`` path, never the two-phase
    router that Issue #5519 hooked."""
    router = Autorouter(
        width=20.0,
        height=20.0,
        rules=DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.35,
            via_diameter=0.7,
            grid_resolution=0.1,
        ),
        **({"layer_stack": layer_stack} if layer_stack is not None else {}),
        force_python=True,
    )
    router.add_component(
        ref="R1",
        pads=[
            {"number": "1", "x": 3.0, "y": 10.0, "net": 1, "net_name": "N1"},
            {"number": "2", "x": 17.0, "y": 10.0, "net": 1, "net_name": "N1"},
        ],
    )
    router.add_component(
        ref="R2",
        pads=[
            {"number": "1", "x": 10.0, "y": 3.0, "net": 2, "net_name": "N2"},
            {"number": "2", "x": 10.0, "y": 17.0, "net": 2, "net_name": "N2"},
        ],
    )
    if with_single_pad_net:
        router.add_component(
            ref="TP1",
            pads=[{"number": "1", "x": 5.0, "y": 5.0, "net": 3, "net_name": "N3"}],
        )
    return router


class TestPlanStageOnNegotiatedPath:
    """``route_all_negotiated`` runs the plan stage (Issue #5520).

    Before this slice only boards reaching ``route_all_two_phase``
    (dense packages / ``--two-phase``) produced a ``RoutingPlan``; a plain
    negotiated route left ``Autorouter.routing_plan`` at ``None``.
    """

    def test_plain_negotiated_route_builds_a_plan(self):
        router = _plain_autorouter()
        router.route_all_negotiated(max_iterations=3, timeout=60.0)

        plan = router.routing_plan
        assert plan is not None
        assert {"N1", "N2"} == {n.name for n in plan.nets.values()}
        assert plan.overflow_report is not None
        # AC: the sidecar's total_overflow is the graph's own query, never a
        # hand-summed value.
        assert router.plan_region_graph is not None
        assert plan.overflow_report.total_overflow == router.plan_region_graph.get_total_overflow()
        assert plan.overflow_report.elapsed_s >= 0.0

    def test_no_routing_plan_switch_suppresses_plan_and_keeps_copper_identical(self):
        on = _plain_autorouter()
        on.emit_routing_plan = True
        routes_on = on.route_all_negotiated(max_iterations=3, timeout=60.0)

        off = _plain_autorouter()
        off.emit_routing_plan = False
        routes_off = off.route_all_negotiated(max_iterations=3, timeout=60.0)

        assert on.routing_plan is not None
        assert off.routing_plan is None
        # The plan stage is report-only: identical copper with and without it.
        assert _route_signature(routes_on) == _route_signature(routes_off)

    def test_plan_stage_is_silent(self, capsys):
        """The core hook must not print -- the text summary + sidecar stay
        the CLI's job, so the many unit tests that call
        ``route_all_negotiated`` directly keep their stdout assertions."""
        router = _plain_autorouter()
        router.plan_routing()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_plan_stage_never_touches_the_grid(self, monkeypatch):
        """Byte-identity rests on the stage not setting corridor preferences
        (or otherwise mutating the routing grid)."""
        router = _plain_autorouter()

        def _forbidden(*args, **kwargs):
            raise AssertionError("plan stage must not mutate the routing grid")

        monkeypatch.setattr(router.grid, "set_corridor_preference", _forbidden)
        monkeypatch.setattr(router.grid, "clear_all_corridor_preferences", _forbidden)
        assert router.plan_routing() is not None

    def test_existing_plan_is_not_rebuilt(self):
        """A board that already planned (two-phase, or an earlier negotiated
        call on the same Autorouter) must not run the pass twice."""
        router = _plain_autorouter()
        sentinel = RoutingPlan()
        router.routing_plan = sentinel
        router._run_routing_plan_stage()
        assert router.routing_plan is sentinel

    def test_emit_routing_plan_false_skips_the_stage_entirely(self):
        router = _plain_autorouter()
        router.emit_routing_plan = False
        router._run_routing_plan_stage()
        assert router.routing_plan is None

    def test_plan_stage_failure_is_not_fatal(self, monkeypatch, capsys):
        router = _plain_autorouter()

        def _boom():
            raise RuntimeError("synthetic plan failure")

        monkeypatch.setattr(router, "plan_routing", _boom)
        router._run_routing_plan_stage()  # must not raise
        assert router.routing_plan is None
        assert "routing-plan stage skipped" in capsys.readouterr().out

    def test_dense_and_non_dense_paths_select_the_same_nets(self):
        """``select_plan_nets`` is shared by ``TwoPhaseRouter.route_all`` and
        ``Autorouter.plan_routing``, so a board's plan does not depend on
        which path it took."""
        router = _plain_autorouter(with_single_pad_net=True)
        two_phase = router._create_two_phase_router()

        from_autorouter = select_plan_nets(router)
        from_two_phase = select_plan_nets(two_phase)

        assert from_autorouter.net_order == from_two_phase.net_order
        assert from_autorouter.single_pad_nets == from_two_phase.single_pad_nets
        assert from_autorouter.pour_nets == from_two_phase.pour_nets

    def test_filtered_nets_keep_their_status_in_the_plan(self):
        """The single-pad filter still reaches the plan through the shared
        selection helper (status ``single_pad``, not silently missing)."""
        router = _plain_autorouter(with_single_pad_net=True)
        plan = router.plan_routing()
        assert plan is not None
        statuses = {n.name: n.status for n in plan.nets.values()}
        assert statuses["N3"] == "single_pad"
        assert statuses["N1"] == "assigned"

    def test_select_plan_nets_reports_only_when_asked(self, capsys):
        router = _plain_autorouter(with_single_pad_net=True)

        select_plan_nets(router)
        assert capsys.readouterr().out == ""

        lines: list[str] = []
        select_plan_nets(router, report=lines.append)
        assert any("single-pad net(s)" in line for line in lines)

    def test_build_plan_emit_false_still_returns_the_graph(self):
        router = _plain_autorouter()
        selection = select_plan_nets(router)
        result = build_plan(router, net_order=selection.net_order, emit=False)
        assert result.plan is None
        assert result.region_graph is not None
        assert result.global_result is not None
        assert result.tile_mm > 0.0

    def test_board_without_routable_nets_plans_nothing(self):
        router = Autorouter(width=10.0, height=10.0, force_python=True)
        assert router.plan_routing() is None
        assert router.routing_plan is None


class TestNoRoutingPlanFlag:
    """``--no-routing-plan`` (Issue #5520) is the only new flag."""

    @staticmethod
    def _inner_parser():
        from kicad_tools.cli.route_cmd import _route_parser

        return _route_parser()

    @staticmethod
    def _outer_parser():
        from kicad_tools.cli.parser import create_parser

        return create_parser()

    def test_inner_parser_defaults_to_on(self):
        args = self._inner_parser().parse_args(["board.kicad_pcb"])
        assert args.routing_plan is True

    def test_inner_parser_flag_turns_it_off(self):
        args = self._inner_parser().parse_args(["board.kicad_pcb", "--no-routing-plan"])
        assert args.routing_plan is False

    def test_outer_parser_defaults_to_on(self):
        args = self._outer_parser().parse_args(["route", "board.kicad_pcb"])
        assert args.routing_plan is True

    def test_outer_parser_flag_turns_it_off(self):
        args = self._outer_parser().parse_args(["route", "board.kicad_pcb", "--no-routing-plan"])
        assert args.routing_plan is False

    def test_there_is_no_positive_opt_in_flag(self):
        with pytest.raises(SystemExit):
            self._inner_parser().parse_args(["board.kicad_pcb", "--routing-plan"])

    def test_apply_flag_helper_sets_the_router_switch(self):
        from argparse import Namespace

        from kicad_tools.cli.route_cmd import _apply_routing_plan_flag

        router = _plain_autorouter()
        _apply_routing_plan_flag(router, Namespace(routing_plan=False))
        assert router.emit_routing_plan is False
        _apply_routing_plan_flag(router, Namespace(routing_plan=True))
        assert router.emit_routing_plan is True
        # Absent flag (library callers / older Namespaces) keeps the default.
        _apply_routing_plan_flag(router, Namespace())
        assert router.emit_routing_plan is True

    def test_outer_command_forwards_the_flag_to_the_inner_cli(self, monkeypatch):
        """The outer ``kct route`` shim rebuilds argv from a whitelist, so a
        flag that is not forwarded is silently dropped."""
        from kicad_tools.cli import route_cmd

        captured: list[list[str]] = []

        def _fake_main(argv):
            captured.append(list(argv))
            return 0

        monkeypatch.setattr(route_cmd, "main", _fake_main)
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._outer_parser().parse_args(["route", "board.kicad_pcb", "--no-routing-plan"])
        run_route_command(args)
        assert "--no-routing-plan" in captured[0]

        captured.clear()
        args = self._outer_parser().parse_args(["route", "board.kicad_pcb"])
        run_route_command(args)
        assert "--no-routing-plan" not in captured[0]


# =============================================================================
# Honest plan capacity (Issue #5575, Epic #5510 Phase 1b -- #5520 PR-B)
#
# Three ways the pre-#5575 capacity model lied, and the fixtures that pin the
# fix:
#
#   1. One global pitch      -> per-class pitch, demand in base-pitch units.
#   2. Planes counted        -> signal_layer_indices excludes PLANE layers.
#   3. Blockage = pads only  -> keepout rule areas + preserved copper.
#
# Everything here is still REPORT-ONLY: no test below routes detailed copper,
# and the channel fixture asserts that explicitly.
# =============================================================================


#: A 2-layer stack whose back copper is a PLANE, i.e. exactly ONE signal layer.
#: Used by the channel fixture so capacity is ``(N-1)``, not ``layers x (N-1)``.
_ONE_SIGNAL_LAYER_STACK = LayerStack(
    name="1-Signal-1-Plane",
    description="F.Cu signal over a solid B.Cu GND plane",
    layers=[
        LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True),
        LayerDefinition("B.Cu", 1, LayerType.PLANE, plane_net="GND", is_outer=True),
    ],
)

#: Design rules shared by the #5575 fixtures: base pitch 0.2 + 0.2 = 0.4 mm,
#: so ``TILE_PITCH_FACTOR`` gives 4 mm tiles.
_PITCH_04_RULES = DesignRules(
    trace_width=0.2,
    trace_clearance=0.2,
    via_drill=0.35,
    via_diameter=0.7,
    grid_resolution=0.1,
)


def make_channel_board(
    n_nets: int = 6,
    channel_width_mm: float | None = None,
    *,
    board_mm: float = 40.0,
    boundary_x: float = 20.0,
) -> Autorouter:
    """N two-pin nets that must funnel through a channel fitting N-1.

    Geometry (all mm, base pitch 0.4 -> 4 mm tiles -> a 10x10 tile grid on
    the default 40 mm board):

    - ``n_nets`` two-pad nets running left (x=2) to right (x=38), all inside
      the SAME tile row, so every one of them must cross the vertical tile
      boundary at ``boundary_x``.
    - Two preserved-copper "walls" that **straddle** that boundary
      (``boundary_x +- 0.5`` once padded by ``width/2 + clearance``), leaving
      a gap of exactly ``channel_width_mm`` (default ``(N-1) * pitch``).
      Straddling is load-bearing: the blockage model is boundary-based, so a
      wall strictly inside a tile blocks nothing at all.
    - ONE signal layer (``_ONE_SIGNAL_LAYER_STACK``), otherwise the boundary's
      capacity would be ``layers x (N-1)`` and N nets would fit.

    The result: the channel edge has capacity ``N-1`` against a demand of
    ``N`` -> overflow 1, while every other crossing of that boundary is
    walled to capacity 0.
    """
    pitch = _PITCH_04_RULES.trace_width + _PITCH_04_RULES.trace_clearance
    if channel_width_mm is None:
        channel_width_mm = (n_nets - 1) * pitch

    router = Autorouter(
        width=board_mm,
        height=board_mm,
        rules=_PITCH_04_RULES,
        layer_stack=_ONE_SIGNAL_LAYER_STACK,
        force_python=True,
    )

    # Centre the gap inside one tile row (row 5 spans y = 20..24 on a 40 mm
    # board with 4 mm tiles), and put every net's pads inside that gap.
    gap_lo = 21.0
    gap_hi = gap_lo + channel_width_mm
    span = gap_hi - gap_lo
    for i in range(n_nets):
        net = i + 1
        y = gap_lo + span * (i + 0.5) / n_nets
        router.add_component(
            ref=f"L{net}",
            pads=[{"number": "1", "x": 2.0, "y": y, "net": net, "net_name": f"N{net}"}],
        )
        router.add_component(
            ref=f"R{net}",
            pads=[{"number": "1", "x": board_mm - 2.0, "y": y, "net": net, "net_name": f"N{net}"}],
        )

    # Walls as preserved copper.  Padding is width/2 + trace_clearance = 0.5,
    # so a 0.6 mm-wide segment on the boundary line spans boundary_x +- 0.5
    # (straddling) and its ends pull back by 0.5 from the gap edges.
    wall_width = 0.6
    pad = wall_width / 2 + _PITCH_04_RULES.trace_clearance
    router.existing_routes = [
        Route(
            net=0,
            net_name="WALL",
            segments=[
                Segment(
                    x1=boundary_x,
                    y1=-2.0,
                    x2=boundary_x,
                    y2=gap_lo - pad,
                    width=wall_width,
                    layer=Layer.F_CU,
                    net=0,
                ),
                Segment(
                    x1=boundary_x,
                    y1=gap_hi + pad,
                    x2=boundary_x,
                    y2=board_mm + 2.0,
                    width=wall_width,
                    layer=Layer.F_CU,
                    net=0,
                ),
            ],
        )
    ]
    return router


def _boundary_edge(graph: RegionGraph, row: int, col: int):
    """The ascending directed edge crossing the vertical boundary right of *col*."""
    a = graph._region_grid[row][col]
    b = graph._region_grid[row][col + 1]
    return graph._edge_lookup[(a, b)]


class TestPlaneLayerExclusion:
    """AC-1: PLANE layers advertise no routing capacity."""

    def test_signal_layer_indices_excludes_planes(self):
        """``LayerStack.signal_layers`` is ``is_routable``, which includes
        PLANE (#5014) -- the plan must not use it for capacity."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert [layer.index for layer in stack.signal_layers] == [0, 1, 2, 3]
        assert signal_layer_indices(stack) == [0, 3]
        # Stacks without planes are unaffected.
        assert signal_layer_indices(LayerStack.two_layer()) == [0, 1]
        assert signal_layer_indices(LayerStack.four_layer_all_signal()) == [0, 1, 2, 3]

    def test_four_layer_sig_gnd_pwr_sig_plan_has_no_plane_capacity(self):
        """AC-1: sidecar ``layers.signal == [0, 3]``; layers 1 and 2 zero."""
        router = _plain_autorouter(layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig())
        assert router.grid.num_layers == 4

        plan = router.plan_routing()
        assert plan is not None
        assert plan.layers["signal"] == [0, 3]
        assert plan.layers["plane"] == [1, 2]

        graph = router.plan_region_graph
        assert graph.signal_layer_indices == [0, 3]
        edge = next(iter(next(iter(graph.edges.values()))))
        assert sorted(edge.layer_capacity) == [0, 3]
        # The planes carry NO capacity -- not a reduced one, none.
        assert edge.layer_capacity.get(1, 0) == 0
        assert edge.layer_capacity.get(2, 0) == 0
        assert edge.remaining_capacity_on_layer(1) == 0
        assert edge.remaining_capacity_on_layer(2) == 0
        # Capacity is 2 signal layers' worth, not 4.
        assert edge.capacity == edge.layer_capacity[0] + edge.layer_capacity[3]

        # ... and no net was planned onto a plane layer.
        assigned = [n for n in plan.nets.values() if n.status == "assigned"]
        assert assigned
        for net in assigned:
            assert set(net.layer_set) <= {0, 3}

    def test_round_robin_indexes_into_the_signal_layer_list(self):
        """The mandatory one-line consequence: without this, every other net
        lands on a zero-capacity plane index and overflows instantly."""
        graph = RegionGraph(
            board_width=20.0,
            board_height=20.0,
            num_cols=4,
            num_rows=4,
            trace_pitch=0.4,
            num_layers=4,
            signal_layer_indices=[0, 3],
        )
        pads = {}
        nets = {}
        for net in (1, 2, 3, 4):
            y = 2.0 + 4.0 * net
            pads[(f"R{net}", "1")] = _channel_pad(net, 2.0, y, f"R{net}", "1")
            pads[(f"R{net}", "2")] = _channel_pad(net, 18.0, y, f"R{net}", "2")
            nets[net] = [(f"R{net}", "1"), (f"R{net}", "2")]

        router = GlobalRouter(region_graph=graph, corridor_width=0.5, negotiated=False)
        result = router.route_all(nets=nets, pad_dict=pads)

        layers_used = {a.layer for a in result.assignments.values()}
        assert layers_used == {0, 3}
        assert 1 not in layers_used and 2 not in layers_used
        # Round-robin still alternates -- only the index set changed.
        assert [result.assignments[n].layer for n in (1, 2, 3, 4)] == [0, 3, 0, 3]

    def test_legacy_graph_without_signal_indices_is_unchanged(self):
        """AC-6: omitting the new kwargs reproduces the pre-#5575 graph."""
        graph = RegionGraph(
            board_width=20.0,
            board_height=20.0,
            num_cols=4,
            num_rows=4,
            trace_pitch=0.4,
            num_layers=2,
        )
        assert graph.signal_layer_indices is None
        assert graph.num_signal_layers == 2
        edge = graph.edges[0][0]
        assert sorted(edge.layer_capacity) == [0, 1]
        assert edge.capacity == edge.layer_capacity[0] + edge.layer_capacity[1]
        assert graph.demand_weight(1) == 1.0


def _channel_pad(net: int, x: float, y: float, ref: str, pin: str):
    from kicad_tools.router.primitives import Pad

    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=f"N{net}",
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
    )


class TestPerClassPitch:
    """AC-2: demand is measured in base-pitch units, not net count."""

    def test_demand_weight_is_class_pitch_over_base_pitch(self):
        # Base pitch 0.2 + 0.2 = 0.4.  A trace_width=2.6, clearance=0.2 class
        # is 2.8 mm of pitch -> 2.8 / 0.4 = 7.0 base-pitch units.
        pitches = {1: 2.8, 2: 2.8, 3: 0.4}
        graph = RegionGraph(
            board_width=12.0,
            board_height=6.0,
            num_cols=2,
            num_rows=1,
            trace_pitch=0.4,
            num_layers=1,
            pitch_for_net=lambda net: pitches[net],
        )
        assert graph.demand_weight(1) == pytest.approx(7.0)
        assert graph.demand_weight(3) == pytest.approx(1.0)

    def test_two_wide_nets_and_one_signal_demand_fifteen_not_three(self):
        """The exact arithmetic from the acceptance criterion.

        Class parameters, stated explicitly:
          - base pitch        = 0.2 (width) + 0.2 (clearance) = 0.4 mm
          - POWER class pitch = 2.6 (width) + 0.2 (clearance) = 2.8 mm
          - SIGNAL class      = the base pitch, 0.4 mm
        so a POWER net weighs 2.8 / 0.4 = 7.0 and the edge demand for two
        POWER nets plus one SIGNAL net is 2 x 7 + 1 = 15 -- NOT the net
        count, 3.
        """
        classes = {
            1: NetClassRouting(name="POWER", priority=1, trace_width=2.6, clearance=0.2),
            2: NetClassRouting(name="POWER", priority=1, trace_width=2.6, clearance=0.2),
            3: NetClassRouting(name="SIGNAL", priority=5, trace_width=0.2, clearance=0.2),
        }
        graph = RegionGraph(
            board_width=12.0,
            board_height=6.0,  # the 6 mm edge from the AC
            num_cols=2,
            num_rows=1,
            trace_pitch=0.4,
            num_layers=1,
            pitch_for_net=lambda net: classes[net].trace_width + classes[net].clearance,
        )
        edge = graph._edge_lookup[(0, 1)]
        assert edge.capacity == int(6.0 / 0.4) == 15

        for net in (1, 2, 3):
            graph.update_utilization([0, 1], layer=0, weight=graph.demand_weight(net))

        assert edge.utilization == pytest.approx(15.0)
        assert edge.utilization != 3
        # Exactly at capacity -- the float sum must not round up into a
        # spurious overflow.
        assert edge.overflow == 0
        assert graph.get_total_overflow() == 0

        # One more minimum-width net is the 16th track and DOES overflow.
        graph.update_utilization([0, 1], layer=0, weight=1.0)
        assert edge.overflow == 1

    def test_ripup_releases_the_same_weight_it_placed(self):
        """A wide net that is ripped up must not leak demand on the edge."""
        graph = RegionGraph(
            board_width=12.0,
            board_height=6.0,
            num_cols=2,
            num_rows=1,
            trace_pitch=0.4,
            num_layers=1,
            pitch_for_net=lambda net: 2.8,
        )
        router = GlobalRouter(region_graph=graph, corridor_width=0.5, negotiated=False)
        assignment = router.route_net(net=1, pad_positions=[(1.0, 3.0), (11.0, 3.0)])
        assert assignment is not None
        edge = graph._edge_lookup[(0, 1)]
        assert edge.utilization == pytest.approx(7.0)

        graph.release_utilization(
            assignment.region_path, layer=assignment.layer, weight=graph.demand_weight(1)
        )
        assert edge.utilization == pytest.approx(0.0)

    def test_build_plan_resolves_the_class_pitch_by_net_name(self):
        router = _plain_autorouter()
        router.net_class_map = {
            "N1": NetClassRouting(name="POWER", priority=1, trace_width=2.6, clearance=0.2)
        }
        plan = router.plan_routing()
        assert plan is not None
        graph = router.plan_region_graph
        n1 = next(n for n, entry in plan.nets.items() if entry.name == "N1")
        n2 = next(n for n, entry in plan.nets.items() if entry.name == "N2")
        assert graph.demand_weight(n1) == pytest.approx(2.8 / 0.4)
        assert graph.demand_weight(n2) == pytest.approx(1.0)
        # The per-net pitch the sidecar already reported now actually drives
        # the capacity model.
        assert plan.nets[n1].pitch_mm == pytest.approx(2.8)


class TestBlockageBeyondPads:
    """AC-3: keepouts and preserved copper reduce tile-boundary capacity."""

    @staticmethod
    def _graph() -> RegionGraph:
        # 2x1 tiles of 5 mm x 5 mm; the (0, 1) boundary is the vertical
        # segment x = 5, y in [0, 5].
        return RegionGraph(
            board_width=10.0,
            board_height=5.0,
            num_cols=2,
            num_rows=1,
            trace_pitch=0.5,
            num_layers=1,
        )

    def test_keepout_straddling_a_boundary_lowers_capacity_by_overlap_over_pitch(self):
        graph = self._graph()
        edge = graph._edge_lookup[(0, 1)]
        before = edge.capacity
        assert before == int(5.0 / 0.5) == 10

        # A 2 mm-tall keepout straddling x = 5 (4.5 .. 5.5).
        graph.register_blockage_rects([(4.5, 1.0, 5.5, 3.0)])

        assert edge.layer_blockage[0] == pytest.approx(2.0)
        assert edge.capacity == before - int(2.0 / 0.5) == 6
        # Symmetric across the pair.
        assert graph._edge_lookup[(1, 0)].capacity == 6

    def test_keepout_covering_the_whole_boundary_zeroes_capacity(self):
        graph = self._graph()
        graph.register_blockage_rects([(4.5, -1.0, 5.5, 6.0)])
        assert graph._edge_lookup[(0, 1)].capacity == 0

    def test_a_rect_strictly_inside_a_tile_blocks_nothing(self):
        """The model is boundary-based -- this is why the channel fixture's
        walls must straddle a column boundary, not sit inside a tile."""
        graph = self._graph()
        before = graph._edge_lookup[(0, 1)].capacity
        graph.register_blockage_rects([(1.0, 1.0, 3.0, 3.0)])
        assert graph._edge_lookup[(0, 1)].capacity == before
        assert graph._edge_lookup[(0, 1)].layer_blockage == {}

    def test_blockage_is_confined_to_the_named_layers(self):
        graph = RegionGraph(
            board_width=10.0,
            board_height=5.0,
            num_cols=2,
            num_rows=1,
            trace_pitch=0.5,
            num_layers=2,
        )
        edge = graph._edge_lookup[(0, 1)]
        graph.register_blockage_rects([(4.5, 1.0, 5.5, 3.0)], layers=[1])
        assert edge.layer_capacity[0] == 10
        assert edge.layer_capacity[1] == 6
        assert edge.capacity == 16

    def test_preserved_copper_is_collected_as_padded_rects(self):
        router = _plain_autorouter()
        router.existing_routes = [
            Route(
                net=9,
                net_name="FIXED",
                segments=[
                    Segment(x1=5.0, y1=4.0, x2=5.0, y2=12.0, width=0.4, layer=Layer.F_CU, net=9)
                ],
                vias=[
                    Via(
                        x=8.0,
                        y=8.0,
                        drill=0.35,
                        diameter=0.7,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=9,
                    )
                ],
            )
        ]
        rects = collect_blockage_rects(router)
        # width/2 + trace_clearance = 0.2 + 0.2 = 0.4 padding on the segment.
        assert ((4.6, 3.6, 5.4, 12.4), [0]) in rects
        # A via is a square of diameter + clearance = 0.9, on every layer.
        via_rect = next(r for r, layers in rects if layers is None)
        assert via_rect == pytest.approx((7.55, 7.55, 8.45, 8.45))

    def test_keepout_rule_areas_are_collected_as_bounding_boxes(self):
        router = _plain_autorouter()
        router._keepout_rule_area_polygons = lambda: [
            KeepoutRuleArea(
                polygon=((3.0, 4.0), (9.0, 4.0), (6.0, 11.0)),
                layers=frozenset({0}),
                blocks_tracks=True,
                blocks_vias=True,
                name="KO1",
            )
        ]
        assert collect_blockage_rects(router) == [((3.0, 4.0, 9.0, 11.0), [0])]

    def test_only_the_two_documented_sources_are_read(self):
        """Copper pours are deliberately NOT a blockage source: zones are
        filled AFTER routing and flow around traces, so counting them would
        make the plan stricter than the router it describes and report
        overflow the detailed router never experiences."""
        router = _plain_autorouter()
        assert collect_blockage_rects(router) == []
        # Only ``_keepout_rule_area_polygons`` and ``existing_routes`` are
        # consulted -- removing both silences the collector entirely.
        router._keepout_rule_area_polygons = lambda: []
        router.existing_routes = []
        assert collect_blockage_rects(router) == []

    def test_a_router_exposing_neither_hook_contributes_nothing(self):
        assert collect_blockage_rects(object()) == []


class TestKeepoutRuleAreaParseRefactor:
    """``_keepout_rule_area_polygons`` is the shared parse (Issue #5575)."""

    #: Board rect (100, 100)..(130, 116) with one track-blocking rule area and
    #: one pour-void-only area.  Deliberately NOT at sheet origin so the
    #: board-relative -> sheet-absolute shift is exercised (#4603).
    _BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect (start 100 100) (end 130 116)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (zone
    (net 0) (net_name "") (name "KO_TRACKS") (layers "F.Cu")
    (uuid "cccccccc-0000-0000-0000-000000000001")
    (hatch edge 0.5)
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed))
    (polygon (pts (xy 110 104) (xy 116 104) (xy 116 112) (xy 110 112)))
  )
  (zone
    (net 0) (net_name "") (name "POUR_VOID") (layers "F.Cu")
    (uuid "cccccccc-0000-0000-0000-000000000002")
    (hatch edge 0.5)
    (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed))
    (polygon (pts (xy 102 102) (xy 106 102) (xy 106 106) (xy 102 106)))
  )
)
"""

    def _router_on_board(self, tmp_path) -> Autorouter:
        pcb = tmp_path / "keepout.kicad_pcb"
        pcb.write_text(self._BOARD)
        router = _plain_autorouter()
        router._pairwise_attach_zone_pcb_path = str(pcb)
        return router

    def test_only_track_or_via_blocking_areas_are_returned(self, tmp_path):
        areas = self._router_on_board(tmp_path)._keepout_rule_area_polygons()
        assert [a.name for a in areas] == ["KO_TRACKS"]
        assert areas[0].blocks_tracks is True
        assert areas[0].layers == frozenset({0})

    def test_polygons_are_shifted_to_sheet_absolute_coordinates(self, tmp_path):
        """``PCB.load`` exposes zone polygons board-relative (#4416/#4603)."""
        area = self._router_on_board(tmp_path)._keepout_rule_area_polygons()[0]
        min_x, min_y, max_x, max_y = area.bbox
        assert (max_x - min_x, max_y - min_y) == pytest.approx((6.0, 8.0))
        # The shift landed the area inside the board rect, not near the origin.
        assert min_x >= 100.0 and min_y >= 100.0

    def test_result_is_cached(self, tmp_path):
        router = self._router_on_board(tmp_path)
        first = router._keepout_rule_area_polygons()
        assert router._keepout_rule_area_polygons() is first

    def test_no_recorded_board_yields_no_areas(self):
        assert _plain_autorouter()._keepout_rule_area_polygons() == []

    def test_lattice_projection_still_sees_the_same_area(self, tmp_path):
        """The lattice engine consumes the refactored parse unchanged."""
        mask = self._router_on_board(tmp_path)._lattice_keepout_projection()
        assert mask is not None
        assert [a.name for a in mask.areas] == ["KO_TRACKS"]


class TestChannelFixture:
    """AC-4: N nets through an (N-1)-wide channel overflow the channel edge."""

    def test_channel_edge_overflows_without_running_detailed_routing(self):
        n_nets = 6
        router = make_channel_board(n_nets)

        started = time.perf_counter()
        plan = router.plan_routing()
        elapsed_s = time.perf_counter() - started

        assert plan is not None
        assert elapsed_s < 5.0

        graph = router.plan_region_graph
        # The gap sits in tile row 5, between columns 4 and 5.
        channel = _boundary_edge(graph, row=5, col=4)
        assert channel.capacity == n_nets - 1
        assert channel.utilization == pytest.approx(float(n_nets))
        assert channel.overflow >= 1

        # Every other crossing of that boundary is walled shut.
        for row in range(graph.num_rows):
            if row == 5:
                continue
            assert _boundary_edge(graph, row=row, col=4).capacity == 0

        # AC-7: the sidecar's total is the graph's own query, unchanged.
        assert plan.overflow_report.total_overflow == graph.get_total_overflow() >= 1
        assert plan.overflow_report.feasible is False
        assert plan.overflow_report.elapsed_s < 5.0

        # REPORT-ONLY: no detailed routing ran.
        assert router.routes == []

    def test_no_detailed_router_is_constructed(self, monkeypatch):
        """Spy form of the same criterion, so a future refactor that starts
        routing copper inside the plan stage fails loudly."""
        from kicad_tools.router import core as core_module

        def _forbidden(*args, **kwargs):
            raise AssertionError("the plan stage must not run detailed routing")

        monkeypatch.setattr(core_module.NegotiatedRouter, "__init__", _forbidden)
        assert make_channel_board(6).plan_routing() is not None

    def test_widening_the_channel_removes_the_overflow(self):
        """Control: the overflow comes from the channel's WIDTH, not from the
        fixture merely having walls in it."""
        n_nets = 6
        pitch = 0.4
        router = make_channel_board(n_nets, channel_width_mm=(n_nets + 2) * pitch)
        plan = router.plan_routing()
        assert plan is not None
        channel = _boundary_edge(router.plan_region_graph, row=5, col=4)
        assert channel.capacity >= n_nets
        assert channel.overflow == 0

    def test_walls_are_what_creates_the_overflow(self):
        """Second control: the same nets with no walls overflow nothing."""
        router = make_channel_board(6)
        router.existing_routes = []
        plan = router.plan_routing()
        assert plan is not None
        assert plan.overflow_report.total_overflow == 0
        assert _boundary_edge(router.plan_region_graph, row=5, col=4).capacity == int(4.0 / 0.4)


class TestRudyNegativeControl:
    """AC-5: the plan's demand is geometry, not bounding-box density.

    ``CongestionEstimator`` (RUDY, #4871) has no obstacle input at all: its
    demand is mm of HPWL per tile, distributed over each net's bounding box.
    On the channel fixture it therefore cannot see the walls -- the estimate
    is bit-for-bit identical with and without them, while the plan's own
    capacity model reports overflow only WITH them.  That contrast is what
    makes the plan a geometric instrument rather than a density heuristic.

    Note: ``CongestionEstimator.from_board`` does not exist, and RUDY's
    demand is not a normalised utilisation, so a "no tile above 1.0"
    threshold would be unit-mismatched.  This control compares two
    estimates instead, which needs no units at all.
    """

    @staticmethod
    def _estimate(router: Autorouter):
        from kicad_tools.router.congestion_estimator import CongestionEstimator

        return CongestionEstimator.from_nets(
            nets=router.nets,
            pads=router.pads,
            board_origin_x=router.grid.origin_x,
            board_origin_y=router.grid.origin_y,
            board_width=router.grid.width,
            board_height=router.grid.height,
        )

    def test_rudy_cannot_see_the_channel_walls(self):
        walled = make_channel_board(6)
        open_board = make_channel_board(6)
        open_board.existing_routes = []

        assert (
            self._estimate(walled).get_demand_grid() == self._estimate(open_board).get_demand_grid()
        )

        # ... while the plan's capacity model tells the two apart.
        assert walled.plan_routing().overflow_report.total_overflow >= 1
        assert open_board.plan_routing().overflow_report.total_overflow == 0

    def test_rudy_exposes_no_overflow_notion(self):
        estimator = self._estimate(make_channel_board(6))
        assert not hasattr(estimator, "get_total_overflow")
        assert not hasattr(estimator, "get_overflowed_edges")
        assert not hasattr(estimator, "overflow")
        # It reports demand only -- no capacity to compare it against.
        assert not hasattr(estimator, "capacity")


# --- Full-board evidence (slow) ----------------------------------------------
#
# Routes board 00 (non-dense -> negotiated path), board 00 again with
# ``--no-auto-layers`` (the fixed-layer ``do_routing()`` closure, which an
# explicit ``--layers N`` also falls into -- ``route_cmd.py`` silently clears
# ``--auto-layers`` there) and board 03 (dense -> two-phase path) twice each,
# then asserts the routed COPPER is unchanged by the plan stage.
#
# ``@pytest.mark.slow`` keeps these out of the per-PR Test job (which runs
# ``-m "not slow"`` under a 60 s per-test timeout) and into the nightly Slow
# Tests workflow, which is where the Epic #5510 Phase 1b acceptance criterion
# "CI asserts boards 00 and 03" is enforced.  Measured 2026-09-19 on the
# fleet host: board 00 ~13 s, board 03 ~500 s for its two routes.
#
# DETERMINISM PROTOCOL (load-bearing -- do not drop these flags).  A bare
# ``kct route`` is NOT reproducible run-to-run: its iteration budget is
# wall-clock-based, so an A* that is near a budget boundary lands different
# copper on an otherwise identical run.  Measured 2026-09-19 on board 01:
# three unflagged runs produced three distinct copper sets *with the plan
# stage disabled*, i.e. the nondeterminism is pre-existing and has nothing to
# do with this phase.  ``--seed 42 --deterministic-budget`` plus a pinned
# ``PYTHONHASHSEED`` is the repo's existing remedy (#3538 / #3799); under it
# the fleet boards are reproducible AND identical with vs without
# ``--no-routing-plan``.  Without the flags this test would be a flake
# generator that tells you nothing about the plan stage.
#
# THE INSTRUMENT IS THE COPPER SET, NOT THE FILE (Issue #5578).  Comparing
# whole ``.kicad_pcb`` text with UUIDs substituted is NOT a valid measurement
# of "did the plan stage change copper", and getting this wrong is what
# produced the retracted #5578 report.  Two things in the file vary
# run-to-run on their own, with the plan stage OFF in both runs:
#
#   1. Element EMISSION ORDER.  Board 06, 2026-09-19, two stage-off runs:
#      2365 diff lines whole-file, yet the multiset of normalised lines was
#      byte-identical -- the files were pure permutations of each other.
#   2. Zone POUR FILL island decomposition.  Board 03's In2.Cu plane filled
#      as 5 islands on three runs and 20 on a fourth; the fourth happened to
#      be a stage-ON run, which is exactly how the plan stage gets blamed for
#      it.  Re-running stage-ON (``on2``) reproduced 5 islands with an
#      identical copper set, so the fragmentation is pre-existing
#      pour-fill variance, not a plan-stage effect.  Pours are filled after
#      routing and flow around finished traces; they are not routed copper.
#
# ``_copper_elements`` therefore compares the sorted multiset of whole
# ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` NODES with their ``uuid``
# tokens normalised -- geometry, width, layer and net included, emission
# order and pour fills excluded.  Under that instrument every fleet board
# measured on 2026-09-19 (00, 01, 02, 03, 04, 06) is copper-identical with
# vs without ``--no-routing-plan``, so the assertion below is a hard
# assertion, not a skip.  ``test_deterministic_flags_make_the_route_
# reproducible`` is the paired control: it routes with the stage OFF twice,
# so a future failure distinguishes "the plan stage changed copper" from
# "the protocol stopped working".
#
# Do NOT "simplify" this to a one-line ``grep -E
# '^[[:space:]]*\((segment|via|arc)'`` over the raw file.  This repo writes
# copper as MULTI-LINE s-expressions, so that grep keeps only the bare
# ``(segment`` / ``(via`` header lines and discards every ``(start ...)`` /
# ``(end ...)`` / ``(layer ...)`` child: on board 03 it reduces a routed PCB
# to 1793 lines holding just 3 distinct values (2026-09-19 at 0c261d41), i.e.
# it compares copper element COUNTS and is blind to geometry.
# ``_copper_elements`` keeps the whole node.  That grep WAS
# ``scripts/ci/board_route_determinism_smoke.sh``'s ``normalize_copper()``
# until #5580 replaced it with ``scripts/ci/normalize_copper.py`` (a
# paren-balanced whole-node normalizer); this test predates that fix and
# never depended on the smoke script.
#
# The element count is asserted non-empty: a normalisation regression that
# filtered everything out would otherwise compare two empty lists and pass.

_UUID_RE = re.compile(r'\(uuid "[^"]*"\)')
_TSTAMP_RE = re.compile(r"\(tstamp [^)]*\)")
_COPPER_OPEN_RE = re.compile(r"^\s*\((segment|via|arc)\b")

#: Flags that make ``kct route`` reproducible (see the note above).
_DETERMINISTIC_FLAGS = ("--seed", "42", "--deterministic-budget")


def _copper_elements(path: Path) -> list[str]:
    """Sorted multiset of a PCB's routed-copper s-expressions.

    Each ``(segment ...)`` / ``(via ...)`` / ``(arc ...)`` node is collected
    whole (paren-balanced, so multi-line nodes survive), whitespace-collapsed
    and ``uuid`` / ``tstamp``-normalised, then the list is sorted -- so the
    comparison is over the SET of copper geometry and is insensitive to
    emission order.  Zone pour fills and all non-copper nodes are excluded;
    see the module note above for why.
    """
    elements: list[str] = []
    current: list[str] | None = None
    depth = 0
    for raw in path.read_text().splitlines():
        if current is None:
            if not _COPPER_OPEN_RE.match(raw):
                continue
            current = []
            depth = 0
        current.append(raw.strip())
        depth += raw.count("(") - raw.count(")")
        if depth <= 0:
            node = " ".join(current)
            node = _UUID_RE.sub('(uuid "X")', node)
            elements.append(_TSTAMP_RE.sub("(tstamp X)", node))
            current = None
    return sorted(elements)


def _normalized_copper(path: Path) -> str:
    return "\n".join(_copper_elements(path))


def _route(
    pcb: Path, out: Path, *extra: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONHASHSEED="0")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(pcb),
            "-o",
            str(out),
            *_DETERMINISTIC_FLAGS,
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_deterministic_flags_make_the_route_reproducible(tmp_path):
    """Control for the copper-identity test below.

    Routes board 00 twice with the plan stage OFF both times.  If this
    fails, ``test_board_copper_unchanged_by_plan_stage`` is measuring the
    router's own run-to-run variance rather than anything the plan stage
    did -- fix the determinism protocol, not the plan stage.
    """
    pcb = REPO_ROOT / "boards/00-simple-led/output/simple_led.kicad_pcb"
    a = tmp_path / "a.kicad_pcb"
    b = tmp_path / "b.kicad_pcb"
    _route(pcb, a, "--no-routing-plan")
    _route(pcb, b, "--no-routing-plan")

    assert a.exists() and b.exists()
    assert _copper_elements(a), "no copper parsed -- the instrument is broken"
    assert _normalized_copper(a) == _normalized_copper(b)


@pytest.mark.slow
@pytest.mark.timeout(1800)
@pytest.mark.parametrize(
    ("pcb_rel", "extra"),
    [
        pytest.param(
            "boards/00-simple-led/output/simple_led.kicad_pcb",
            (),
            id="board00-escalation",
        ),
        pytest.param(
            "boards/00-simple-led/output/simple_led.kicad_pcb",
            ("--no-auto-layers",),
            id="board00-fixed-layers",
        ),
        pytest.param(
            "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
            (),
            id="board03-two-phase",
        ),
    ],
)
def test_board_copper_unchanged_by_plan_stage(tmp_path, pcb_rel, extra):
    pcb = REPO_ROOT / pcb_rel
    on = tmp_path / "on.kicad_pcb"
    off = tmp_path / "off.kicad_pcb"
    _route(pcb, on, *extra)
    _route(pcb, off, "--no-routing-plan", *extra)

    assert on.exists() and off.exists()

    # --- Flag behaviour ---
    # Default route writes the sidecar; --no-routing-plan suppresses it.
    assert (tmp_path / "on.routing_plan.json").exists()
    assert not (tmp_path / "off.routing_plan.json").exists()

    plan = json.loads((tmp_path / "on.routing_plan.json").read_text())
    assert plan["schema_version"] == 1
    # AC: plan stage wall clock < 5 s on the fleet boards.
    assert plan["overflow_report"]["elapsed_s"] < 5.0

    # --- Copper identity (the Phase 1 "report-only" contract) ---
    copper_on = _copper_elements(on)
    assert copper_on, f"no copper parsed from {on} -- the instrument is broken"
    assert copper_on == _copper_elements(off), (
        f"{pcb_rel} routed a different copper SET with the plan stage on "
        "than with --no-routing-plan -- the report-only plan stage changed "
        "routed copper, which Epic #5510 Phase 1 forbids.  If "
        "test_deterministic_flags_make_the_route_reproducible is also red, "
        "fix the determinism protocol first; the comparison is meaningless "
        "while a stage-off route cannot reproduce itself."
    )


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_cache_hit_still_emits_routing_plan_sidecar(tmp_path):
    """Regression test for Issue #5651.

    A routing-cache HIT short-circuits ``Autorouter.route_all_negotiated``
    entirely, and ``_run_routing_plan_stage`` (Issue #5520) was hooked
    INSIDE that method -- so before the fix, a cache hit produced a route
    with no ``<stem>.routing_plan.json`` sidecar (and no "Routing-plan
    sidecar:" log line) even though the identical uncached run produced
    both.  This is exactly the scenario that made
    ``test_board_copper_unchanged_by_plan_stage`` order-dependent: whichever
    param ran first warmed the cache for a later param routing the same
    board.

    Uses an isolated ``XDG_CACHE_HOME`` (Issue #5651 fix note: the routing
    cache is a persistent SQLite db under the user's real cache directory
    by default) so this test's cache state can neither leak into, nor be
    polluted by, any other test or a developer's real cache.
    """
    pcb = REPO_ROOT / "boards/00-simple-led/output/simple_led.kicad_pcb"
    cache_home = tmp_path / "xdg-cache"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = a_dir / "on.kicad_pcb"
    b = b_dir / "on.kicad_pcb"

    extra_env = {"XDG_CACHE_HOME": str(cache_home)}

    # Issue #5651 root cause: the routing cache is only consulted on the
    # NON-escalation single-shot path (``--no-auto-layers``) --
    # ``route_with_layer_escalation`` (the ``--auto-layers`` default) never
    # touches the ``RoutingCache`` at all, since one cached layer-count
    # result must never silently satisfy a different layer-count attempt.
    # ``--no-auto-layers`` is required here to exercise the cache-hit code
    # path this test targets; it is also what the issue's own repro used.
    first = _route(pcb, a, "--no-auto-layers", extra_env=extra_env)
    assert "Cache MISS" in first.stdout, (
        f"expected the first route into an isolated cache dir to MISS; stdout:\n{first.stdout}"
    )
    assert (a_dir / "on.routing_plan.json").exists(), (
        f"cache-MISS run did not emit the routing-plan sidecar; stdout:\n{first.stdout}"
    )

    second = _route(pcb, b, "--no-auto-layers", extra_env=extra_env)
    assert "Cache HIT" in second.stdout, (
        "expected the second identical route (same pcb, flags and cache dir) "
        f"to HIT the cache warmed by the first; stdout:\n{second.stdout}"
    )
    assert (b_dir / "on.routing_plan.json").exists(), (
        "cache-HIT run must ALSO emit the routing-plan sidecar (Issue #5651) "
        f"-- stdout:\n{second.stdout}"
    )

    # The plan stage now runs on the cache-HIT path too (before the cached
    # copper is restored to the grid) -- confirm it is still the report-only
    # no-op Epic #5510 Phase 1 requires: the HIT run's copper must match the
    # MISS run's exactly.
    assert _copper_elements(a), f"no copper parsed from {a} -- the instrument is broken"
    assert _copper_elements(a) == _copper_elements(b), (
        "cache-HIT copper differs from the cache-MISS copper it was restored "
        "from -- running the routing-plan stage before grid restore must not "
        "perturb the restored copper"
    )


# =============================================================================
# Overflow report text + computed relief (Issue #5521, Epic #5510 Phase 1c)
# =============================================================================


def _overflowed_plan() -> RoutingPlan:
    """A hand-built plan with one over-subscribed edge.

    Built by hand (not by routing anything) so the report assertions below
    are about the FORMATTER, not about whatever a fixture board happens to
    congest this week.
    """
    return RoutingPlan(
        source={"pcb": "ddr.kicad_pcb", "tile_mm": 4.0},
        layers={"signal": [0, 3], "plane": [1, 2]},
        regions={
            17: RegionInfo(row=3, col=5, min_x=20.0, min_y=12.0, max_x=24.0, max_y=16.0),
            18: RegionInfo(row=3, col=6, min_x=24.0, min_y=12.0, max_x=28.0, max_y=16.0),
        },
        nets={
            n: NetPlanEntry(
                name=f"/DQ{n}",
                net_class="DDR",
                pitch_mm=0.35,
                layer_set=[0],
                region_path=[17, 18],
                status="assigned",
            )
            for n in range(3)
        },
        edges=[
            EdgePlanEntry(
                a=17,
                b=18,
                capacity=9,
                demand=14.0,
                overflow=5,
                blockage_mm=1.2,
                nets=[0, 1, 2],
                layers={
                    "0": {"capacity": 5, "demand": 12.0},
                    "3": {"capacity": 4, "demand": 2.0},
                },
                refs_a=["U1", "C4"],
                refs_b=["U3"],
            )
        ],
        overflow_report=OverflowReport(
            iterations=15,
            total_overflow=5,
            overflowed_edges=1,
            failed_nets=[],
            feasible=False,
            elapsed_s=0.4,
        ),
        relief=[
            {
                "edge": [17, 18],
                "kind": "move_component",
                "ref": "U3",
                "dx": 2.0,
                "dy": 0.0,
                "expected_overflow": 0,
            },
            {
                "edge": [17, 18],
                "kind": "add_signal_layer",
                "layer_index": 1,
                "expected_overflow": 1,
            },
            {"edge": [17, 18], "kind": "swap_pins", "deferred": "#5511"},
        ],
        relief_meta={
            "candidates_evaluated": 9,
            "elapsed_s": 1.25,
            "truncated": False,
            "baseline_overflow": 5,
            "budget_s": 5.0,
        },
    )


class TestFormatOverflowReport:
    """AC-1: the report names tile pair, layer, demand, capacity, nets, refs."""

    def test_report_names_every_required_field(self):
        text = _overflowed_plan().format_overflow_report()

        # Tile pair and both regions' bounds.
        assert "tiles 17-18" in text
        assert "(20.000, 12.000)-(24.000, 16.000)" in text
        assert "(24.000, 12.000)-(28.000, 16.000)" in text
        # The overflowed LAYER only (layer 3 has spare capacity).
        assert "layer 0" in text
        # Demand / capacity / overflow.
        assert "demand 14.0 tracks" in text
        assert "capacity 9" in text
        assert "overflow 5" in text
        # The nets crossing it.
        assert "/DQ0 /DQ1 /DQ2" in text
        # The nearest component ref on each side (rank 1, not the whole list).
        assert "corridor U1 -> U3" in text

    def test_report_lists_each_relief_candidate(self):
        text = _overflowed_plan().format_overflow_report()
        assert "move U3 +2.0/+0.0 mm -> total overflow 0" in text
        assert "add signal layer 1 (currently a plane) -> total overflow 1" in text
        assert "swap_pins deferred (#5511)" in text
        assert "9 candidate(s)" in text

    def test_region_without_a_ref_falls_back_to_its_centre(self):
        plan = _overflowed_plan()
        plan.edges[0].refs_a = []
        text = plan.format_overflow_report()
        assert "tile 17 @ (22.000, 14.000) -> U3" in text

    def test_feasible_plan_reports_no_overflowed_edges(self):
        plan = _overflowed_plan()
        plan.edges[0].overflow = 0
        assert plan.overflow_report is not None
        plan.overflow_report.total_overflow = 0
        plan.overflow_report.overflowed_edges = 0
        plan.overflow_report.feasible = True
        text = plan.format_overflow_report()
        assert "no overflowed edges" in text
        assert "-- feasible" in text

    def test_report_survives_a_sidecar_round_trip(self):
        """The #5521 consumer reads a sidecar, not a live router -- so the
        report must be reproducible from ``from_dict`` alone."""
        plan = _overflowed_plan()
        restored = RoutingPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert restored.format_overflow_report() == plan.format_overflow_report()

    def test_truncated_search_says_so(self):
        plan = _overflowed_plan()
        plan.relief_meta["truncated"] = True
        assert "budget exhausted, truncated" in plan.format_overflow_report()


def make_blocker_board(blocker_xy: tuple[float, float] = (18.0, 23.0)) -> Autorouter:
    """Three nets through a channel a single component's pads over-subscribe.

    Built on :func:`make_channel_board` with a gap wide enough for FOUR
    tracks (``capacity 4`` vs ``demand 3`` -> feasible on its own), then a
    single-pad blocker component dropped inside the left-hand boundary tile.
    Its 1.2 mm pad contributes 1.2 mm of region blockage, which the edge
    model averages over the pair (0.6 mm) and subtracts from the boundary,
    taking capacity to 2 against a demand of 3 -> overflow 1.

    Moving the blocker +2 mm in Y lifts it out of BOTH boundary tiles, so
    the capacity comes back and the board is feasible again -- which is
    exactly the ``move_component`` relief candidate the search must find
    (and must find by MEASURING, not by guessing).
    """
    router = make_channel_board(3, channel_width_mm=1.6)
    router.add_component(
        ref="BLK",
        pads=[
            {
                "number": "1",
                "x": blocker_xy[0],
                "y": blocker_xy[1],
                "net": 99,
                "net_name": "NBLK",
                "width": 1.2,
                "height": 1.2,
            }
        ],
    )
    return router


def _pad_positions(router: Autorouter) -> dict:
    return {key: (pad.x, pad.y) for key, pad in router.pads.items()}


class TestComputedRelief:
    """AC-1/AC-4: relief is MEASURED per candidate, and placement is restored."""

    def test_move_candidate_empties_the_edge(self):
        router = make_blocker_board()
        plan = router.plan_routing()

        assert plan is not None
        assert plan.overflow_report.total_overflow == 1
        moves = [entry for entry in plan.relief if entry["kind"] == "move_component"]
        assert moves, "the +2 mm step that removes the blockage was not found"
        best = moves[0]
        assert best["ref"] == "BLK"
        assert best["expected_overflow"] == 0
        assert (best["dx"], best["dy"]) in {(0.0, 2.0), (0.0, -2.0), (2.0, 0.0), (-2.0, 0.0)}
        # Every kept candidate must actually improve on the baseline.
        assert all(m["expected_overflow"] < 1 for m in moves)

    def test_relief_restores_the_placement_exactly(self):
        """The search re-plans against a shifted COPY of the pads, so the
        router's own placement is not merely restored -- it is never
        touched.  Byte-identical copper depends on this."""
        router = make_blocker_board()
        before = _pad_positions(router)
        router.plan_routing()
        assert _pad_positions(router) == before

    def test_relief_names_the_adjacent_ref(self):
        router = make_blocker_board()
        plan = router.plan_routing()
        edge = next(e for e in plan.edges if e.overflow > 0)
        assert "BLK" in (edge.refs_a + edge.refs_b)
        assert "BLK" in plan.format_overflow_report()

    def test_swap_pins_is_a_deferred_stub_only(self):
        plan = make_blocker_board().plan_routing()
        stubs = [entry for entry in plan.relief if entry["kind"] == "swap_pins"]
        assert stubs == [{"edge": [54, 55], "kind": "swap_pins", "deferred": "#5511"}]
        # No evaluation, no expected_overflow, no #5511 implementation.
        assert "expected_overflow" not in stubs[0]

    def test_add_signal_layer_candidate_only_when_planes_exist(self):
        """The channel fixture's B.Cu is a PLANE, so promoting it is a
        legitimate capacity lever; an all-signal stack has none to offer."""
        with_plane = make_blocker_board().plan_routing()
        assert [e for e in with_plane.relief if e["kind"] == "add_signal_layer"]

        no_plane = make_channel_board(6)
        no_plane.grid.layer_stack = LayerStack.two_layer()
        plan = no_plane.plan_routing()
        assert plan.layers["plane"] == []
        assert [e for e in plan.relief if e["kind"] == "add_signal_layer"] == []

    def test_relief_meta_records_the_bound(self):
        plan = make_blocker_board().plan_routing()
        meta = plan.relief_meta
        assert meta["baseline_overflow"] == 1
        assert meta["budget_s"] == 5.0
        assert meta["truncated"] is False
        # 4 unit steps for the single adjacent ref + 1 plane candidate.
        assert 0 < meta["candidates_evaluated"] <= 3 * 3 * 4 + 2
        assert meta["elapsed_s"] < 5.0

    def test_exhausted_budget_truncates_rather_than_overrunning(self):
        from kicad_tools.router.routing_plan_relief import compute_relief

        router = make_blocker_board()
        result = build_plan(
            router,
            net_order=select_plan_nets(router).net_order,
            relief=False,
        )
        plan = result.plan
        assert plan.relief == [] and plan.relief_meta == {}

        compute_relief(
            plan,
            result.region_graph,
            router,
            net_order=select_plan_nets(router).net_order,
            budget_s=0.0,
        )
        assert plan.relief_meta["truncated"] is True
        assert plan.relief_meta["candidates_evaluated"] == 0
        # The stub is free (no re-plan), so it is still reported.
        assert [e["kind"] for e in plan.relief] == ["swap_pins"]

    def test_a_feasible_board_pays_nothing(self):
        """AC: boards with no overflow never enter the search."""
        router = _plain_autorouter()
        plan = router.plan_routing()
        assert plan.overflow_report.total_overflow == 0
        assert plan.relief == []
        assert plan.relief_meta == {}
