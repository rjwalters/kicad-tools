"""A4 (#4257): the BundlePlan HARD-lane allocator is driven by ALL THREE
negotiated entry points, not just a direct ``_apply_byte_lane_inner_priority``
call.

A3 (#4256) placed the discrete-allocator seam inside
``Autorouter._apply_byte_lane_inner_priority`` and pinned it with a test that
calls that helper directly (``test_bundle_plan_integration.py``).  A4's
integration contract is that the three negotiated routing entry points the
curator enumerated —

  * ``route_all``                (core.py:6292),
  * ``route_all_negotiated``     (core.py:7539),
  * ``TwoPhaseRouter`` built by ``_create_two_phase_router`` (core.py:12421),

— each actually reach that gated block, so the HARD-lane reservation is
honoured identically on every negotiated route.  All three share the SAME
bound ``self._apply_byte_lane_inner_priority`` on the SAME ``Autorouter``
instance and the SAME ``self.grid``, so a plan reserved on one is reserved on
the grid the route actually uses.

Each entry point is asserted twice:

  * flag OFF (default): NO stored plan, NO HARD lanes — byte-identical to
    pre-#4256 main (the flag-off invariant the board-07 gate depends on);
  * flag ON, planar TMDS bundle: a FEASIBLE ``BundlePlan`` is stored under the
    group name and HARD per-member lanes are reserved on the shared grid.

The planar-TMDS geometry mirrors the load-bearing board-07 finding (A3): the
committed placement carries the bundle co-oriented, so the allocator returns 6
trivial in-order OUTER lanes (0 via-hops).  This is why closing the board-07
TMDS opens is NOT guaranteed by the allocator — see the board-07 README
"Routing Plateau" section for the measured seed-42 verdict.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting

GROUP_NAME = "TMDS"


def _make_planar_tmds_router() -> tuple[Autorouter, list[int]]:
    """Mirrored, co-oriented TMDS coupled group (3 diff pairs = 6 nets).

    Both facing columns (U1, U2) carry the members in the SAME x-order, so the
    allocator's crossing set is empty and the plan is FEASIBLE with 6 in-order
    outer lanes — the board-07 planar topology.
    """
    cls = NetClassRouting(
        name=GROUP_NAME,
        priority=1,
        trace_width=0.15,
        clearance=0.10,
        length_critical=True,
        length_match_group=GROUP_NAME,
        length_match_reference=None,
        length_match_tolerance_mm=0.1,
    )
    net_class_map: dict[str, NetClassRouting] = {}
    router = Autorouter(
        width=120.0,
        height=80.0,
        net_class_map=net_class_map,
        layer_stack=LayerStack.four_layer_all_signal(),
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
    pitch = 0.8
    base_y = 40.0 - (n - 1) * pitch / 2.0
    net_ids: list[int] = []
    for i, name in enumerate(names):
        net_id = i + 1
        net_ids.append(net_id)
        y = base_y + i * pitch
        router.add_component(
            "U1",
            [{"number": str(1 + i), "x": 40.0, "y": y, "net": net_id, "net_name": name}],
        )
        router.add_component(
            "U2",
            [{"number": str(1 + i), "x": 80.0, "y": y, "net": net_id, "net_name": name}],
        )
        net_class_map[name] = cls

    router.net_class_map = net_class_map
    return router, net_ids


def _assert_flag_off_no_reservation(router: Autorouter) -> None:
    assert router.enable_bundle_river_planner is False
    assert router._last_bundle_plans == {}
    assert router._escape.bundle_plan_corridor_reservations == 0


def _assert_flag_on_feasible_reserved(router: Autorouter) -> None:
    plan = router._last_bundle_plans.get(GROUP_NAME)
    assert plan is not None, "allocator did not fire on this entry point"
    assert plan.feasible is True
    assert plan.infeasible is False
    assert len(plan.lanes) == 6
    assert all(lane.layer == "outer" and not lane.via_hop for lane in plan.lanes)
    # HARD per-member lanes reserved on the SHARED grid the route uses.
    assert router._escape.bundle_plan_corridor_reservations > 0
    assert router._escape.bundle_plan_corridor_reserved_cells > 0
    assert router.grid.reserved_cell_count() > 0


# ---------------------------------------------------------------------------
# Entry point 1: route_all  (core.py:6292 -> :6472 helper call)
# ---------------------------------------------------------------------------
class TestRouteAllDrivesAllocator:
    def test_route_all_flag_off_no_hard_lanes(self) -> None:
        router, net_ids = _make_planar_tmds_router()
        router.route_all(net_order=list(net_ids), suppress_no_timeout_warning=True)
        _assert_flag_off_no_reservation(router)

    def test_route_all_flag_on_reserves_hard_lanes(self) -> None:
        router, net_ids = _make_planar_tmds_router()
        router.enable_bundle_river_planner = True
        router.route_all(net_order=list(net_ids), suppress_no_timeout_warning=True)
        _assert_flag_on_feasible_reserved(router)


# ---------------------------------------------------------------------------
# Entry point 2: route_all_negotiated  (core.py:7539 -> :7986 helper call)
# ---------------------------------------------------------------------------
class TestRouteAllNegotiatedDrivesAllocator:
    def test_negotiated_flag_off_no_hard_lanes(self) -> None:
        router, _ = _make_planar_tmds_router()
        router.route_all_negotiated(max_iterations=1, timeout=30.0)
        _assert_flag_off_no_reservation(router)

    def test_negotiated_flag_on_reserves_hard_lanes(self) -> None:
        router, _ = _make_planar_tmds_router()
        router.enable_bundle_river_planner = True
        router.route_all_negotiated(max_iterations=1, timeout=30.0)
        _assert_flag_on_feasible_reserved(router)


# ---------------------------------------------------------------------------
# Entry point 3: TwoPhaseRouter via _create_two_phase_router
# (core.py:12421 -> two_phase.py:312 callback into the shared helper)
# ---------------------------------------------------------------------------
class TestTwoPhaseRouterDrivesAllocator:
    def test_two_phase_flag_off_no_hard_lanes(self) -> None:
        router, net_ids = _make_planar_tmds_router()
        two_phase = router._create_two_phase_router()
        two_phase.route_all(list(net_ids))
        _assert_flag_off_no_reservation(router)

    def test_two_phase_flag_on_reserves_hard_lanes(self) -> None:
        router, net_ids = _make_planar_tmds_router()
        router.enable_bundle_river_planner = True
        two_phase = router._create_two_phase_router()
        two_phase.route_all(list(net_ids))
        _assert_flag_on_feasible_reserved(router)
