"""Integration: the BundlePlan HARD-lane path through the router (#4256, A3).

Drives ``Autorouter._apply_byte_lane_inner_priority`` on synthetic coupled
(diff-pair) byte-lane fixtures and pins the A3 wiring contract:

  * flag OFF (default): NO bundle-plan HARD lanes, NO stored plan —
    byte-identical to pre-#4256 main (the existing SOFT single-ended
    reservation counters are unchanged, asserted in
    ``test_byte_lane_corridor_reservation.py``);
  * flag ON, **planar** TMDS bundle: a FEASIBLE ``BundlePlan`` is stored and
    HARD per-member lanes are reserved (``bundle_plan_corridor_reservations``
    > 0, grid cells reserved);
  * flag ON, **reversed** TMDS bundle at inner budget 1: an explicit
    **INFEASIBLE** verdict is stored and NO HARD lanes are reserved (never a
    silent partial);
  * flag ON, **single-ended** DDR byte (no diff pairs): the TMDS-only-first
    scope guard skips the bundle-plan path entirely, so the single-ended
    #4079 board-07 behaviour is untouched.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting


def _make_tmds_router(
    *,
    reversed_secondary: bool,
    layer_stack: LayerStack | None = None,
    pitch: float = 0.8,
) -> tuple[Autorouter, list[int]]:
    """Build a mirrored TMDS coupled-group router (3 diff pairs = 6 nets)."""
    group_name = "TMDS"
    cls = NetClassRouting(
        name=group_name,
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group=group_name,
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=120.0, height=80.0, net_class_map=net_class_map, layer_stack=layer_stack
    )

    names = [
        "TMDS_D0_P",
        "TMDS_D0_N",
        "TMDS_D1_P",
        "TMDS_D1_N",
        "TMDS_D2_P",
        "TMDS_D2_N",
    ]
    n = len(names)
    centre_y = 40.0
    base_y = centre_y - (n - 1) * pitch / 2.0

    net_ids: list[int] = []
    for i, name in enumerate(names):
        net_id = i + 1
        net_ids.append(net_id)
        y_primary = base_y + i * pitch
        j = (n - 1 - i) if reversed_secondary else i
        y_secondary = base_y + j * pitch
        router.add_component(
            "U1",
            [{"number": str(1 + i), "x": 40.0, "y": y_primary, "net": net_id, "net_name": name}],
        )
        router.add_component(
            "U2",
            [{"number": str(1 + i), "x": 80.0, "y": y_secondary, "net": net_id, "net_name": name}],
        )
        net_class_map[name] = cls

    router.net_class_map = net_class_map
    return router, net_ids


def _make_ddr_router(
    *,
    layer_stack: LayerStack | None = None,
    pitch: float = 0.8,
) -> tuple[Autorouter, list[int]]:
    """Single-ended DDR byte (no diff pairs) — the scope-guard control."""
    group_name = "DDR_DATA_BYTE_0"
    cls = NetClassRouting(
        name=group_name,
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group=group_name,
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=120.0, height=80.0, net_class_map=net_class_map, layer_stack=layer_stack
    )

    n = 8
    centre_y = 40.0
    base_y = centre_y - (n - 1) * pitch / 2.0
    net_ids: list[int] = []
    for i in range(n):
        net_id = i + 1
        name = f"DQ{i}"
        net_ids.append(net_id)
        y = base_y + i * pitch
        router.add_component(
            "U1", [{"number": str(1 + i), "x": 40.0, "y": y, "net": net_id, "net_name": name}]
        )
        router.add_component(
            "U2", [{"number": str(1 + i), "x": 80.0, "y": y, "net": net_id, "net_name": name}]
        )
        net_class_map[name] = cls
    router.net_class_map = net_class_map
    return router, net_ids


class TestFlagOffNoBundlePlan:
    def test_flag_off_reserves_no_hard_lanes(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_tmds_router(reversed_secondary=False, layer_stack=stack)
        assert router.enable_bundle_river_planner is False
        router._apply_byte_lane_inner_priority(net_ids)
        assert router._escape.bundle_plan_corridor_reservations == 0
        assert router._last_bundle_plans == {}


class TestFlagOnPlanarFeasible:
    def test_planar_tmds_feasible_hard_lanes_reserved(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_tmds_router(reversed_secondary=False, layer_stack=stack)
        router.enable_bundle_river_planner = True
        router._apply_byte_lane_inner_priority(net_ids)

        plan = router._last_bundle_plans.get("TMDS")
        assert plan is not None
        assert plan.feasible is True
        assert len(plan.lanes) == 6
        # HARD per-member lanes were reserved on their own counter.
        assert router._escape.bundle_plan_corridor_reservations > 0
        assert router._escape.bundle_plan_corridor_reserved_cells > 0
        assert router.grid.reserved_cell_count() > 0


class TestFlagOnReversedInfeasible:
    def test_reversed_tmds_infeasible_no_hard_lanes(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_tmds_router(reversed_secondary=True, layer_stack=stack)
        router.enable_bundle_river_planner = True
        router._apply_byte_lane_inner_priority(net_ids)

        plan = router._last_bundle_plans.get("TMDS")
        assert plan is not None
        # A full reversal of 6 nets over an inner budget of 1 is a genuine
        # over-subscription: an explicit infeasible verdict, never a partial.
        assert plan.infeasible is True
        assert plan.reason
        assert plan.lanes == ()
        assert router._escape.bundle_plan_corridor_reservations == 0


class TestScopeGuardSingleEnded:
    def test_single_ended_ddr_byte_skips_bundle_plan(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_ddr_router(layer_stack=stack)
        router.enable_bundle_river_planner = True
        router._apply_byte_lane_inner_priority(net_ids)
        # No diff pairs -> no coupled group -> no HARD lanes, no stored plan.
        assert router._escape.bundle_plan_corridor_reservations == 0
        assert "DDR_DATA_BYTE_0" not in router._last_bundle_plans


class TestOrderUnchanged:
    def test_bundle_plan_is_pure_side_effect(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_tmds_router(reversed_secondary=False, layer_stack=stack)
        router.enable_bundle_river_planner = True
        out = router._apply_byte_lane_inner_priority(net_ids)
        # The allocator reserves lanes; it does not reorder nets.
        assert out == net_ids
