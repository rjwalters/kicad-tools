"""Integration tests for the scoped bundle river planner (Issue #4053).

These drive ``Autorouter._apply_byte_lane_inner_priority`` on synthetic
mirrored byte-lane fixtures and pin the flag contract:

  * flag OFF (default): NO via-hop corridors reserved beyond the existing
    #2983 inner-corner reservations — byte-identical to pre-#4053 main
    (protects the reservation-count assertions in
    ``test_byte_lane_corridor_reservation.py``);
  * flag ON, **reversed** fixture: additional via-hop corridors reserved
    for the crossing nets (reservation count exceeds the #2983 baseline
    of 2);
  * flag ON, **planar (co-oriented)** fixture: NO extra via-hop corridors
    (over-triggering guard) — the reservation count stays at the #2983
    baseline.

The reversed fixture mirrors board 07's DDR byte: U1's row carries the
nets top-to-bottom in order, U2's facing row carries them in reverse.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting


def _make_bus_router(
    *,
    reversed_secondary: bool,
    group_size: int = 10,
    pitch: float = 0.8,
    layer_stack: LayerStack | None = None,
) -> tuple[Autorouter, list[int]]:
    """Build a mirrored byte-lane router; secondary row optionally reversed.

    Primary component U1 carries DQ0..DQ(n-1) top-to-bottom.  U2 carries
    the same nets either co-oriented (``reversed_secondary=False``,
    planar) or in reverse row order (``reversed_secondary=True``, a full
    bus reversal like board 07's DDR byte).
    """
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
        width=120.0,
        height=80.0,
        net_class_map=net_class_map,
        layer_stack=layer_stack,
    )

    centre_y = 40.0
    base_y = centre_y - (group_size - 1) * pitch / 2.0

    net_ids: list[int] = []
    for i in range(group_size):
        net_id = i + 1
        net_name = f"DQ{i}"
        net_ids.append(net_id)
        y_primary = base_y + i * pitch
        # Secondary row: reversed order places DQ_i at the mirrored y.
        j = (group_size - 1 - i) if reversed_secondary else i
        y_secondary = base_y + j * pitch

        router.add_component(
            "U1",
            [
                {
                    "number": str(25 + i),
                    "x": 40.0,
                    "y": y_primary,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        router.add_component(
            "U2",
            [
                {
                    "number": str(1 + i),
                    "x": 80.0,
                    "y": y_secondary,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        net_class_map[net_name] = cls

    router.net_class_map = net_class_map
    return router, net_ids


class TestFlagOffByteIdentical:
    """Flag OFF => only the #2983 inner-corner reservations (count 2)."""

    def test_reversed_fixture_flag_off_baseline_reservations(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_bus_router(reversed_secondary=True, layer_stack=stack)
        # Default: enable_bundle_river_planner is False.
        assert router.enable_bundle_river_planner is False
        router._apply_byte_lane_inner_priority(net_ids)
        # Exactly the two inner-corner reservations from #2983 — no via
        # hops added when the planner flag is off.
        assert router._escape.byte_lane_corridor_reservations == 2

    def test_planar_fixture_flag_off_baseline_reservations(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_bus_router(reversed_secondary=False, layer_stack=stack)
        router._apply_byte_lane_inner_priority(net_ids)
        assert router._escape.byte_lane_corridor_reservations == 2


class TestFlagOnReversedReservesViaHops:
    """Flag ON on a reversal => extra via-hop corridors beyond the 2 baseline."""

    def test_reversed_fixture_reserves_via_hops(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_bus_router(reversed_secondary=True, layer_stack=stack)
        router.enable_bundle_river_planner = True
        router._apply_byte_lane_inner_priority(net_ids)
        # 2 inner-corner reservations + N via-hop corridors (one per losing
        # net of the reversal).  A full reversal loses on multiple nets, so
        # the count must exceed the #2983 baseline of 2.
        assert router._escape.byte_lane_corridor_reservations > 2
        assert router._escape.byte_lane_corridor_reserved_cells > 0


class TestFlagOnPlanarNoViaHops:
    """Flag ON on a co-oriented bundle => NO extra via hops (guard)."""

    def test_planar_fixture_no_via_hops(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_bus_router(reversed_secondary=False, layer_stack=stack)
        router.enable_bundle_river_planner = True
        router._apply_byte_lane_inner_priority(net_ids)
        # Planar bundle has an empty inversion set: the planner reserves no
        # via hops, so the count stays at the #2983 baseline of 2.
        assert router._escape.byte_lane_corridor_reservations == 2


class TestPermutationInvariantHeld:
    """The planner is a pure side-effect: net_order stays a permutation."""

    def test_order_unchanged_when_reorder_flag_off(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids = _make_bus_router(reversed_secondary=True, layer_stack=stack)
        router.enable_bundle_river_planner = True
        # enable_byte_lane_reorder stays False, so the ORDER is identity
        # even though via-hop corridors are reserved.
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
