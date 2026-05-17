"""Tests for the byte-lane inner-corner corridor reservation (Issue #2983).

Background
----------

PR #2969 (closes #2962) landed a scaffolding-only detection hook,
:meth:`Autorouter._apply_byte_lane_inner_priority`, that detected
mirrored byte-lane match groups (e.g. board 07's DDR data byte on a
mirrored QFN-48 pair) but did NOT act on the detection.  PR #2969's
R1/R2/R3 rounds proved net-ordering alone could not break the
geometric DQ5/DQ4 0.44mm via-clearance collision.

Issue #2983 adds the **corridor reservation strategy** as the
layered-escape fix: for each inner-corner net (sorted positions 1
and N-2 of the co-located row), pre-reserve a lateral corridor on
an inner signal layer (or B.Cu on plane-stack-up 4-layer boards)
BEFORE any corner-net through-hole vias are placed.  The mechanic
is identical to the diff-pair continuation corridor from PR #2911
(:meth:`EscapeRouter._reserve_pair_continuation_corridor`) — the
new helper :meth:`EscapeRouter.reserve_inner_corner_lane_corridor`
generalises it to single-ended pads.

Tests in this module pin the contract:

1. ``byte_lane_corridor_reservations`` counter equals 2 per
   detected mirrored byte-lane group (one per inner-corner pad).
2. ``byte_lane_corridor_reserved_cells`` is non-zero when a valid
   inner routable layer exists in the stack-up.
3. The reservation runs as part of
   :meth:`_apply_byte_lane_inner_priority` (so it lands BEFORE
   any corner-net via marking in the escape pre-pass).
4. The ordering output is still identity (PR #2969 contract
   preserved — corridor reservation does not perturb net order).
5. The 2-layer guard is honoured (no reservation on 2-layer
   stack-ups; that case has no via-blocking contention to
   resolve and would actively starve partner-net escapes).
6. Small groups, no-group boards, and non-mirrored topologies
   skip the reservation cleanly.

The synthetic fixture uses the same mirrored QFN-48 geometry as
``test_byte_lane_priority.py`` so the regression fingerprint
matches PR #2969's scaffolding contract — only the new counter
assertions are added on top.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# Helpers
# =============================================================================


def _make_byte_lane_router(
    *,
    group_name: str = "DDR_DATA_BYTE_0",
    group_size: int = 10,
    pitch: float = 0.8,
    priority: int = 1,
    layer_stack: LayerStack | None = None,
) -> tuple[Autorouter, list[int], list[str]]:
    """Build a synthetic router with a mirrored byte-lane match group.

    Mirrors the test_byte_lane_priority.py fixture but exposes a
    ``layer_stack`` parameter so each test can pin the contract on
    the specific stack-up topology it cares about
    (4-layer plane vs 4-layer signal vs 2-layer).

    Args:
        group_name: ``length_match_group`` name (also class name).
        group_size: Number of nets in the byte-lane.
        pitch: Vertical spacing between pads on each component.
        priority: Net-class priority.
        layer_stack: Stack-up to pass to the Autorouter constructor.
            ``None`` defaults the constructor's internal 2-layer
            fallback.

    Returns:
        Tuple of (router, net_ids_in_creation_order, net_names).
    """
    cls = NetClassRouting(
        name=group_name,
        priority=priority,
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
    net_names: list[str] = []
    for i in range(group_size):
        net_id = i + 1
        net_name = f"DQ{i}"
        net_ids.append(net_id)
        net_names.append(net_name)
        y = base_y + i * pitch

        # Pad on U1 (left component, pads face east)
        router.add_component(
            "U1",
            [
                {
                    "number": str(25 + i),
                    "x": 40.0,
                    "y": y,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        # Mirrored pad on U2 (right component, pads face west)
        router.add_component(
            "U2",
            [
                {
                    "number": str(1 + i),
                    "x": 80.0,
                    "y": y,
                    "net": net_id,
                    "net_name": net_name,
                }
            ],
        )
        net_class_map[net_name] = cls

    router.net_class_map = net_class_map
    return router, net_ids, net_names


# =============================================================================
# Tests: Corridor reservation contract on 4-layer plane stack-up (board 07)
# =============================================================================


class TestCorridorReservationOnPlaneStackup:
    """Board 07's 4-layer SIG-GND-PWR-SIG stack-up (In1/In2 are PLANES).

    ``_select_inner_escape_layer`` falls back to B.Cu (no inner
    signal layers available).  The single-ended corridor helper
    SHOULD still reserve cells on B.Cu — this is safe because the
    reservation is per-net and partner-net through-hole vias will
    detour around it on B.Cu, while the inner-corner pad's own via
    matches the reservation owner set and lands freely.
    """

    def test_reservation_count_equals_two_per_byte_lane(self) -> None:
        """Mirrored 10-net byte-lane => 2 inner-corner pads => 2 calls."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )

        # Force escape router init so counters are accessible.
        escape = router._escape
        assert escape.byte_lane_corridor_reservations == 0
        assert escape.byte_lane_corridor_reserved_cells == 0

        # Drive the detection + reservation pass.
        out = router._apply_byte_lane_inner_priority(net_ids)

        # Identity contract preserved.
        assert out == net_ids
        # Two inner-corner pads (positions 1 and N-2) on the primary
        # component (U1 or U2 — both host all 10 group members; the
        # tie-break picks one deterministically).
        assert escape.byte_lane_corridor_reservations == 2
        assert escape.byte_lane_corridor_reserved_cells > 0

    def test_nine_net_byte_lane_two_reservations(self) -> None:
        """9-net byte-lane (DDR-byte minus DQS pair) — still 2 inner corners."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=9, layer_stack=stack
        )

        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
        assert router._escape.byte_lane_corridor_reservations == 2
        assert router._escape.byte_lane_corridor_reserved_cells > 0


# =============================================================================
# Tests: Corridor reservation contract on 4-layer all-signal stack-up
# =============================================================================


class TestCorridorReservationOnSignalStackup:
    """4-layer all-signal stack-up (inner layers ARE signal).

    ``_select_inner_escape_layer`` picks In1.Cu (first inner SIGNAL
    layer).  The corridor reservation lands there.
    """

    def test_reserves_on_inner_signal_layer(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )

        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
        assert router._escape.byte_lane_corridor_reservations == 2
        assert router._escape.byte_lane_corridor_reserved_cells > 0


# =============================================================================
# Tests: Reservation runs BEFORE corner-net via marking
# =============================================================================


class TestReservationSequencingBeforeViaMarking:
    """The reservation MUST land before any via marking.

    ``_apply_byte_lane_inner_priority`` is invoked from
    ``route_all`` / ``route_all_negotiated`` / ``TwoPhaseRouter``
    AFTER ``_interleave_match_groups`` and BEFORE the subgrid
    escape pre-pass (which is where corner-net through-hole vias
    are first placed via ``_run_subgrid_prepass``).  Calling the
    helper directly should bump the counters immediately —
    verifying the sequence without driving a full route.
    """

    def test_counters_bump_within_apply_helper(self) -> None:
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )

        # Before helper: no reservations.
        assert router._escape.byte_lane_corridor_reservations == 0
        # After helper: 2 reservations (one per inner-corner pad).
        router._apply_byte_lane_inner_priority(net_ids)
        assert router._escape.byte_lane_corridor_reservations == 2

        # The cells are recorded on the grid's reservation map.
        # ``RoutingGrid.reserved_cell_count()`` returns the total
        # number of currently reserved cells across all layers.
        assert router.grid.reserved_cell_count() > 0


# =============================================================================
# Tests: 2-layer guard — no reservation on 2-layer stack-ups
# =============================================================================


class TestTwoLayerStackupGuard:
    """The single-ended helper must NOT reserve cells on a 2-layer board.

    Mirrors the diff-pair primitive's 2-layer guard (#2677): on a
    2-layer board there are exactly two routable layers (F.Cu and
    B.Cu) and partner-net vias MUST be free to land on both.
    Reserving cells on B.Cu would starve partner-net escapes.
    """

    def test_no_reservation_on_two_layer_stack(self) -> None:
        stack = LayerStack.two_layer()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )

        router._apply_byte_lane_inner_priority(net_ids)
        # No reservations on 2-layer stack-up.
        assert router._escape.byte_lane_corridor_reservations == 0
        assert router._escape.byte_lane_corridor_reserved_cells == 0

    def test_no_reservation_when_no_stack_provided(self) -> None:
        """Autorouter default (layer_stack=None) => internal 2-layer fallback.

        Same guard applies: the synthetic 2-layer fallback path
        produces no reservations, matching the test_byte_lane_priority
        existing assertions where no stack is supplied.
        """
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=None
        )

        router._apply_byte_lane_inner_priority(net_ids)
        assert router._escape.byte_lane_corridor_reservations == 0


# =============================================================================
# Tests: Identity preservation (PR #2969 ordering contract)
# =============================================================================


class TestOrderingStillIdentity:
    """Corridor reservation must NOT change net ordering.

    PR #2969's R1/R2/R3 trace proved net-ordering changes alone are
    insufficient AND, in R1, actively regressed DRC.  The Issue #2983
    fix is corridor-reservation-only: the helper's return value must
    remain identical to the input order so the downstream priority
    semantics (PR #2914 starvation fairness, PR #2482 connector
    sibling promotion, complexity-tier ordering, etc.) are
    preserved verbatim.
    """

    def test_identity_on_4_layer_signal_stack(self) -> None:
        stack = LayerStack.four_layer_all_signal()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
        assert set(out) == set(net_ids)
        assert len(out) == len(net_ids)

    def test_identity_on_plane_stack(self) -> None:
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=10, layer_stack=stack
        )
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids


# =============================================================================
# Tests: Small / non-mirrored / no-group inputs short-circuit cleanly
# =============================================================================


class TestNoReservationOnUnsupportedInputs:
    """The helper must skip the reservation for inputs that don't
    look like a mirrored byte-lane.

    Mirrors the test_byte_lane_priority small-group / no-group
    coverage: in all cases the counters remain at zero AND the
    ordering is identity.
    """

    def test_four_member_group_no_reservation(self) -> None:
        """Below MIN_BYTE_LANE_SIZE=5 => no reservation."""
        stack = LayerStack.four_layer_all_signal()
        router, net_ids, _ = _make_byte_lane_router(
            group_size=4, layer_stack=stack
        )
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
        assert router._escape.byte_lane_corridor_reservations == 0

    def test_no_match_group_no_reservation(self) -> None:
        """Plain unrelated nets => no group detection => no reservation."""
        stack = LayerStack.four_layer_all_signal()
        router = Autorouter(width=80.0, height=80.0, layer_stack=stack)
        net_ids = [1, 2, 3, 4, 5, 6]
        for nid in net_ids:
            nm = f"NET{nid}"
            router.add_component(
                f"R{nid}_A",
                [{"number": "1", "x": float(nid), "y": 5.0, "net": nid, "net_name": nm}],
            )
            router.add_component(
                f"R{nid}_B",
                [{"number": "1", "x": float(nid) + 1.0, "y": 5.0, "net": nid, "net_name": nm}],
            )
        out = router._apply_byte_lane_inner_priority(net_ids)
        assert out == net_ids
        assert router._escape.byte_lane_corridor_reservations == 0
