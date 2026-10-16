"""Tests for the experimental min-cost-flow escape assignment (Issue #6352).

Covers the pure solver (``kicad_tools.router.escape_flow``) on hand-checkable
graphs -- capacity conflicts, infeasible assignments, disconnected pins,
stable equal-cost ties -- and the ``EscapeCorridorPlanner(assignment="flow")``
integration: it reuses ``EscapeCorridorPlan``/``reserve()``, falls back
cleanly to the greedy plan when no useful assignment exists, never reserves
anything during planning, and leaves the default ``"greedy"`` path untouched.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.congestion_estimator import CongestionEstimator, TileGrid
from kicad_tools.router.core import Autorouter
from kicad_tools.router.escape import PackageInfo, PackageType
from kicad_tools.router.escape_corridor import EscapeCorridorPlanner
from kicad_tools.router.escape_flow import (
    EscapeFlowCostModel,
    ExitSlot,
    assign_pins_to_exits,
    exit_point,
    min_cost_flow,
    pin_exit_cost_mm,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# =============================================================================
# Solver: min_cost_flow
# =============================================================================


def test_min_cost_flow_picks_cheaper_parallel_path() -> None:
    # 0 -> 1 -> 3 costs 5; 0 -> 2 -> 3 costs 2; each path capacity 1.
    arcs = [(0, 1, 1, 2), (1, 3, 1, 3), (0, 2, 1, 1), (2, 3, 1, 1)]
    one = min_cost_flow(4, arcs, 0, 3, max_flow=1)
    assert (one.flow, one.cost) == (1, 2)
    assert one.arc_flow == [0, 0, 1, 1]
    both = min_cost_flow(4, arcs, 0, 3)
    assert (both.flow, both.cost) == (2, 7)


def test_min_cost_flow_uses_reverse_arc_to_reach_optimum() -> None:
    # The first shortest path is s-a-b-t (cost 3).  The second augmentation
    # must cancel a->b through its residual arc (s-b-a-t, cost 3 - 1 + 3 = 5),
    # giving the optimum s-a-t + s-b-t (cost 4 + 4 = 8).
    s, a, b, t = 0, 1, 2, 3
    arcs = [(s, a, 1, 1), (s, b, 1, 3), (a, b, 1, 1), (a, t, 1, 3), (b, t, 1, 1)]
    res = min_cost_flow(4, arcs, s, t)
    assert res.flow == 2
    assert res.cost == 8
    # a->b ends up unused once the second augmentation reroutes through it.
    assert res.arc_flow[2] == 0


def test_min_cost_flow_rejects_negative_cost() -> None:
    with pytest.raises(ValueError):
        min_cost_flow(2, [(0, 1, 1, -1)], 0, 1)


def test_min_cost_flow_no_path_is_zero_flow() -> None:
    res = min_cost_flow(3, [(0, 1, 5, 1)], 0, 2)
    assert (res.flow, res.cost, res.augmentations) == (0, 0, 0)


# =============================================================================
# Assignment: capacity conflicts, infeasibility, ties
# =============================================================================


def test_capacity_conflict_resolved_jointly_not_greedily() -> None:
    """Two pins both prefer exit 0 (capacity 1).

    Pin 0: exit0 = 1, exit1 = 2.  Pin 1: exit0 = 2, exit1 = 10.
    Greedy-in-pin-order gives pin0->0, forcing pin1->1 (1 + 10 = 11); the
    joint optimum is pin0->1, pin1->0 (2 + 2 = 4).
    """
    exits = [ExitSlot("N", 0, capacity=1), ExitSlot("S", 0, capacity=1)]
    costs = {(0, 0): 1, (0, 1): 2, (1, 0): 2, (1, 1): 10}
    res = assign_pins_to_exits(2, exits, costs)
    assert res.feasible
    assert res.assignment == {0: 1, 1: 0}
    assert res.total_cost == 4
    assert res.exit_loads(2) == [1, 1]


def test_oversubscribed_exit_is_infeasible_not_an_error() -> None:
    """Three pins, one usable exit of capacity 2: one pin is left over."""
    exits = [ExitSlot("E", 1, capacity=2)]
    costs = {(p, 0): 5 for p in range(3)}
    res = assign_pins_to_exits(3, exits, costs)
    assert not res.feasible
    assert res.assigned_count == 2
    assert res.unassigned == [2]  # stable: the highest-index pin is dropped
    assert res.total_capacity == 2


def test_disconnected_pin_is_reported_unassigned() -> None:
    exits = [ExitSlot("N", 0, capacity=5), ExitSlot("S", 0, capacity=5)]
    costs = {(0, 0): 1, (0, 1): 1, (2, 1): 1}  # pin 1 has no legal exit
    res = assign_pins_to_exits(3, exits, costs)
    assert res.unassigned == [1]
    assert set(res.assignment) == {0, 2}


def test_zero_capacity_exit_is_never_used() -> None:
    exits = [ExitSlot("N", 0, capacity=0), ExitSlot("S", 0, capacity=1)]
    res = assign_pins_to_exits(1, exits, {(0, 0): 0, (0, 1): 99})
    assert res.assignment == {0: 1}


def test_equal_cost_ties_resolve_to_lowest_index_and_are_stable() -> None:
    exits = [ExitSlot("N", 0, capacity=1), ExitSlot("E", 0, capacity=1), ExitSlot("S", 0, 1)]
    costs = {(p, e): 7 for p in range(3) for e in range(3)}
    first = assign_pins_to_exits(3, exits, costs)
    assert first.assignment == {0: 0, 1: 1, 2: 2}
    # Repeated solves of identical inputs are identical (determinism).
    for _ in range(5):
        again = assign_pins_to_exits(3, exits, dict(reversed(list(costs.items()))))
        assert again.assignment == first.assignment
        assert again.total_cost == first.total_cost


def test_load_step_cost_spreads_ties_across_exits() -> None:
    """With a convex load cost, equal-cost exits share load instead of one
    exit filling to capacity first."""
    exits = [ExitSlot("E", 1, capacity=10, load_step_cost=1)] + [
        ExitSlot("E", 2, capacity=10, load_step_cost=1)
    ]
    costs = {(p, e): 100 for p in range(4) for e in range(2)}
    res = assign_pins_to_exits(4, exits, costs)
    assert res.exit_loads(2) == [2, 2]
    flat = assign_pins_to_exits(
        4, [ExitSlot("E", 1, capacity=10), ExitSlot("E", 2, capacity=10)], costs
    )
    assert flat.exit_loads(2) == [4, 0]


def test_no_pins_is_trivially_feasible() -> None:
    res = assign_pins_to_exits(0, [ExitSlot("N", 0, 1)], {})
    assert res.feasible and res.assignment == {}


# =============================================================================
# Cost model
# =============================================================================


def test_exit_point_projects_and_clamps() -> None:
    bbox = (0.0, 0.0, 4.0, 2.0)
    assert exit_point("N", 1.0, 1.0, bbox) == (1.0, 0.0)
    assert exit_point("S", 9.0, 1.0, bbox) == (4.0, 2.0)
    assert exit_point("W", 1.0, 1.5, bbox) == (0.0, 1.5)
    assert exit_point("E", 1.0, -3.0, bbox) == (4.0, 0.0)


def test_foreign_face_and_via_are_penalised() -> None:
    bbox = (0.0, 0.0, 4.0, 4.0)
    model = EscapeFlowCostModel()
    kw = {"pin_xy": (2.0, 0.0), "pin_face": "N", "dest_xy": (2.0, -5.0), "bbox": bbox}
    own = pin_exit_cost_mm(exit_face="N", needs_via=False, exit_congestion=0.0, model=model, **kw)
    via = pin_exit_cost_mm(exit_face="N", needs_via=True, exit_congestion=0.0, model=model, **kw)
    foreign = pin_exit_cost_mm(
        exit_face="S", needs_via=False, exit_congestion=0.0, model=model, **kw
    )
    hot = pin_exit_cost_mm(exit_face="N", needs_via=False, exit_congestion=1.0, model=model, **kw)
    assert own == pytest.approx(5.0)
    assert via == pytest.approx(own + model.via_cost_mm)
    assert hot == pytest.approx(own + model.congestion_cost_mm)
    # 4 mm under the body (x2) + 9 mm back out to the destination.
    assert foreign == pytest.approx(4.0 * model.under_body_factor + 9.0)


# =============================================================================
# Planner integration
# =============================================================================

BOARD = 30.0
CENTER = 15.0


@pytest.fixture
def rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.15,
        grid_resolution=0.1,
    )


@pytest.fixture
def grid_4layer(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        BOARD, BOARD, rules, origin_x=0, origin_y=0, layer_stack=LayerStack.four_layer_all_signal()
    )


@pytest.fixture
def grid_2layer(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0)


def _qfn(pins_per_side: int = 4, pitch: float = 0.5, dest_dist: float = 8.0):
    """A 4-sided QFN whose every pin's net leaves straight out its own face."""
    half = (pins_per_side - 1) * pitch / 2.0
    edge = half + 1.0
    pads: list[Pad] = []
    positions: dict[int, list[tuple[float, float]]] = {}
    nid = 1
    for side, (ox, oy) in {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}.items():
        for i in range(pins_per_side):
            along = -half + i * pitch
            if side in ("N", "S"):
                px, py = CENTER + along, CENTER + oy * edge
            else:
                px, py = CENTER + ox * edge, CENTER + along
            pads.append(
                Pad(
                    x=px,
                    y=py,
                    width=0.25,
                    height=0.25,
                    net=nid,
                    net_name=f"{side}{i}",
                    layer=Layer.F_CU,
                    ref="U1",
                    pin=str(nid),
                )
            )
            positions[nid] = [(px, py), (px + ox * dest_dist, py + oy * dest_dist)]
            nid += 1
    return pads, positions


def _package(pads: list[Pad]) -> PackageInfo:
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref="U1",
        package_type=PackageType.QFN,
        center=((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=0.5,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


def _estimator(value: float = 1.0) -> CongestionEstimator:
    tgrid = TileGrid.from_board(0.0, 0.0, BOARD, BOARD, target_tiles=100)
    est = CongestionEstimator(grid=tgrid)
    est.demand = [[value] * tgrid.cols for _ in range(tgrid.rows)]
    return est


def test_planner_rejects_unknown_assignment(grid_4layer: RoutingGrid) -> None:
    with pytest.raises(ValueError):
        EscapeCorridorPlanner(grid_4layer, assignment="bogus")


def test_flow_plan_reuses_cluster_objects_and_reserve(grid_4layer: RoutingGrid) -> None:
    pads, positions = _qfn()
    planner = EscapeCorridorPlanner(
        grid_4layer, None, net_pad_positions=positions, assignment="flow"
    )
    plan = planner.plan(_package(pads))
    assert plan.assignment == "flow"
    assert plan.fallback_reason is None
    assert plan.flow_stats["pins"] == 16
    assert plan.flow_stats["unassigned"] == 0
    # Every pin's net escapes straight out its own face, so the joint
    # optimum never sends a pin under the body.
    assert plan.flow_stats["face_moved"] == 0
    assigned = {p.net for c in plan.clusters for p in c.pads}
    assert assigned == set(range(1, 17))
    assert all(c.layer_idx is not None and c.corridor_cells for c in plan.clusters)
    # Planning alone never reserves anything.
    assert not grid_4layer._reserved_for_nets
    total = planner.reserve(plan)
    assert total > 0
    assert plan.total_reserved_cells == total


def test_flow_plan_is_deterministic_under_input_permutation(grid_4layer: RoutingGrid) -> None:
    pads, positions = _qfn()
    planner = EscapeCorridorPlanner(
        grid_4layer, None, net_pad_positions=positions, assignment="flow"
    )

    def signature(p):
        return [
            (c.face, c.dest_octant, c.layer_idx, sorted(x.net for x in c.pads)) for c in p.clusters
        ]

    a = planner.plan(_package(pads))
    b = planner.plan(_package(list(reversed(pads))))
    assert signature(a) == signature(b)


def test_flow_spreads_layers_with_convex_load(grid_4layer: RoutingGrid) -> None:
    """On a 4-layer stack the convex load term spreads one face's pins over
    more than one layer instead of stacking them all on the cheapest one."""
    pads, positions = _qfn(pins_per_side=6)
    planner = EscapeCorridorPlanner(
        grid_4layer,
        None,
        net_pad_positions=positions,
        assignment="flow",
        flow_cost_model=EscapeFlowCostModel(via_cost_mm=0.0, load_step_mm=1.0),
    )
    plan = planner.plan(_package(pads))
    assert plan.assignment == "flow"
    assert len(plan.layers_used()) > 1


def test_capacity_conflict_moves_pins_to_another_face(rules: DesignRules) -> None:
    """Block the north face's channel on every layer: north pins must be
    sent to another face (or the plan must fall back) -- never assigned past
    capacity."""
    grid = RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0)
    pads, positions = _qfn()
    pkg = _package(pads)
    planner = EscapeCorridorPlanner(grid, None, net_pad_positions=positions, assignment="flow")
    offset = planner._flow_probe_offset(pkg)
    (x0, y0), (x1, _y1) = planner._face_probe_line("N", pkg.bounding_box, offset)
    gy = grid.world_to_grid(x0, y0)[1]
    gx0 = grid.world_to_grid(x0, y0)[0]
    gx1 = grid.world_to_grid(x1, y0)[0]
    for layer in range(grid.num_layers):
        for gx in range(gx0, gx1 + 1):
            grid.cell_at(layer, gy, gx).blocked = True
    for layer in grid.get_routable_indices():
        assert planner._exit_capacity("N", pkg.bounding_box, offset, layer, frozenset()) == 0
    plan = planner.plan(pkg)
    assert plan.assignment == "flow"
    assert all(c.face != "N" for c in plan.clusters)
    assert plan.flow_stats["face_moved"] == 4


def test_infeasible_flow_falls_back_to_greedy(rules: DesignRules) -> None:
    """Block every face on every layer: the flow is empty, so the planner
    returns the unchanged greedy plan and says why."""
    grid = RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0)
    for layer in range(grid.num_layers):
        for gy in range(grid.rows):
            for gx in range(grid.cols):
                grid.cell_at(layer, gy, gx).blocked = True
    pads, positions = _qfn()
    pkg = _package(pads)
    flow = EscapeCorridorPlanner(grid, None, net_pad_positions=positions, assignment="flow")
    greedy = EscapeCorridorPlanner(grid, None, net_pad_positions=positions)
    plan = flow.plan(pkg)
    assert plan.assignment == "greedy"
    assert plan.fallback_reason is not None
    assert plan.fallback_reason.startswith("empty")
    assert plan.flow_stats["total_capacity"] == 0
    ref = greedy.plan(pkg)
    assert [(c.key, c.layer_idx, c.corridor_cells) for c in plan.clusters] == [
        (c.key, c.layer_idx, c.corridor_cells) for c in ref.clusters
    ]


def test_partially_infeasible_flow_falls_back(rules: DesignRules, monkeypatch) -> None:
    grid = RoutingGrid(BOARD, BOARD, rules, origin_x=0, origin_y=0)
    pads, positions = _qfn()
    planner = EscapeCorridorPlanner(grid, None, net_pad_positions=positions, assignment="flow")
    # One track per exit, 4 faces x 2 layers = 8 tracks for 16 pins, so half
    # the pins are unplaceable.
    monkeypatch.setattr(planner, "_exit_capacity", lambda *a, **k: 1)
    plan = planner.plan(_package(pads))
    assert plan.assignment == "greedy"
    assert plan.fallback_reason == "infeasible: 8 of 16 pins unassigned"
    assert plan.flow_stats["assigned"] == 8


def test_no_routable_pins_falls_back(grid_4layer: RoutingGrid) -> None:
    pads, _ = _qfn()
    for p in pads:
        p.net = 0
    plan = EscapeCorridorPlanner(grid_4layer, None, assignment="flow").plan(_package(pads))
    assert plan.assignment == "greedy"
    assert plan.fallback_reason == "no routable pins"
    assert plan.clusters == []


def test_congestion_reservation_gate_applies_to_flow_plans(grid_4layer: RoutingGrid) -> None:
    """The #4519 selectivity gate in reserve() is shared: a zero-demand
    estimator reserves nothing for the flow strategy either."""
    pads, positions = _qfn()
    planner = EscapeCorridorPlanner(
        grid_4layer, _estimator(0.0), net_pad_positions=positions, assignment="flow"
    )
    plan = planner.plan_and_reserve(_package(pads))
    assert plan.assignment == "flow"
    assert plan.total_reserved_cells == 0


def test_greedy_default_is_unchanged(grid_4layer: RoutingGrid) -> None:
    pads, positions = _qfn()
    default = EscapeCorridorPlanner(grid_4layer, _estimator(), net_pad_positions=positions)
    explicit = EscapeCorridorPlanner(
        grid_4layer, _estimator(), net_pad_positions=positions, assignment="greedy"
    )
    a = default.plan(_package(pads))
    b = explicit.plan(_package(pads))
    assert a.assignment == b.assignment == "greedy"
    assert a.fallback_reason is None and a.flow_stats == {}
    assert [(c.key, c.layer_idx, c.corridor_cells) for c in a.clusters] == [
        (c.key, c.layer_idx, c.corridor_cells) for c in b.clusters
    ]


# =============================================================================
# Autorouter / CLI selection
# =============================================================================


def test_autorouter_defaults_to_greedy() -> None:
    router = Autorouter(width=BOARD, height=BOARD)
    assert router.escape_corridor_assignment == "greedy"
    assert router._escape_flow_counters == {}


def test_cli_flag_selects_flow_and_implies_reservation() -> None:
    import argparse

    from kicad_tools.cli.route_cmd import _apply_escape_corridor_reservation

    router = Autorouter(width=BOARD, height=BOARD)
    _apply_escape_corridor_reservation(
        router,
        argparse.Namespace(escape_corridor_reservation=False, escape_corridor_assignment="greedy"),
    )
    assert router.enable_escape_corridor_reservation is False
    assert router.escape_corridor_assignment == "greedy"
    _apply_escape_corridor_reservation(
        router,
        argparse.Namespace(escape_corridor_reservation=False, escape_corridor_assignment="flow"),
    )
    assert router.enable_escape_corridor_reservation is True
    assert router.escape_corridor_assignment == "flow"


def test_flag_hidden_from_help_and_parsed_by_both_parsers() -> None:
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["route", "x.kicad_pcb", "--escape-corridor-assignment", "flow"])
    assert args.escape_corridor_assignment == "flow"
    assert parser.parse_args(["route", "x.kicad_pcb"]).escape_corridor_assignment == "greedy"


# =============================================================================
# A/B harness: an inert treatment must not read as a null result
# =============================================================================


def _harness():
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/research/escape_flow_experiment.py"
    spec = importlib.util.spec_from_file_location("escape_flow_experiment", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves its module through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_harness_parses_corridor_and_flow_lines() -> None:
    h = _harness()
    lines = [
        "  Escape corridors (U2): 14 cluster(s), 617 cells reserved across layers [0] (Issue #4474)",
        "  Escape-flow (U2): planned pins=17 exits=8 capacity=160 face_moved=0 clusters=14",
        "  Escape-flow summary: packages=1 planned=1 infeasible=0 empty=0 error=0 (Issue #6352)",
    ]
    diag = h.corridor_diagnostics(lines)
    assert diag["corridor_cells"] == 617
    assert diag["flow_counters"]["planned"] == 1
    assert h.treatment_status("C", diag) == (True, "")
    assert h.treatment_status("B", diag) == (True, "")


def test_harness_flags_inert_treatment_arms() -> None:
    h = _harness()
    # No dense package: the router never reaches the planner.
    nothing = h.corridor_diagnostics([])
    assert h.treatment_status("A", nothing)[0] is True
    assert h.treatment_status("B", nothing)[0] is False
    assert h.treatment_status("C", nothing)[0] is False
    # Every package fell back to greedy: C measured greedy, not flow.
    fallback = h.corridor_diagnostics(
        [
            "  Escape corridors (U1): 3 cluster(s), 90 cells reserved across layers [1]",
            "  Escape-flow summary: packages=1 planned=0 infeasible=1 empty=0 error=0",
        ]
    )
    applied, note = h.treatment_status("C", fallback)
    assert applied is False and "no package got a flow plan" in note
    zero = h.corridor_diagnostics(["  Escape-flow summary: packages=0 (no dense packages)"])
    assert zero["flow_summaries"] == 1
    assert h.treatment_status("C", zero)[0] is False
