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
from pathlib import Path

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.global_router import CorridorAssignment, GlobalRoutingResult
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.region_graph import RegionGraph
from kicad_tools.router.routing_plan import (
    EdgePlanEntry,
    NetPlanEntry,
    OverflowReport,
    RegionInfo,
    RoutingPlan,
    build_plan,
    select_plan_nets,
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


def _plain_autorouter(with_single_pad_net: bool = False) -> Autorouter:
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
# Do NOT "simplify" this to the one-line grep in
# ``scripts/ci/board_route_determinism_smoke.sh`` (``normalize_copper()``,
# ``grep -E '^[[:space:]]*\((segment|via|arc)'``).  This repo writes copper
# as MULTI-LINE s-expressions, so that grep keeps only the bare ``(segment``
# / ``(via`` header lines and discards every ``(start ...)`` / ``(end ...)``
# / ``(layer ...)`` child: on board 03 it reduces a 25k-line PCB to 1913
# lines that are exactly ``1872 segments + 41 vias``, i.e. it compares copper
# element COUNTS and is blind to geometry.  ``_copper_elements`` keeps the
# whole node.  (The smoke script's own blind spot is tracked in #5580; this
# test does not depend on it.)
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


def _route(pcb: Path, out: Path, *extra: str) -> None:
    env = dict(os.environ, PYTHONHASHSEED="0")
    subprocess.run(
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
