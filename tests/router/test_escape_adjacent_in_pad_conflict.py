"""Issue #3429: adjacent-pin via-in-pad conflict detection.

Background
----------

The QFP-alternating dispatcher's per-pad in-pad rescue
(``EscapeRouter._try_in_pad_escape``) validates a candidate via only
against the footprint's PADS (``_via_clears_other_pads``).  Sibling
escapes' vias placed earlier in the SAME dispatch pass are invisible to
that check because the routing grid is not populated until commit time.
At fine pitch (LQFP-48 0.5 mm) two adjacent foreign-net pins can each be
flagged for an in-pad rescue, but their via barrels + annular rings
cannot coexist: the required center-to-center spacing is
``via_r_A + via_r_B + clearance`` (0.727 mm on jlcpcb-tier1 with 0.6 mm
OD vias + 0.127 mm clearance) -- already wider than the 0.5 mm pitch.

Before #3429 this conflict surfaced only at the ``apply_escape_routes``
commit-time cross-validation, where the losing escape was dropped with
no retry (a silent gap).  #3429 adds a cheap pairwise pre-check BEFORE
the second via is committed so the dispatcher refuses it (returns None)
and falls through to the lateral / surface escape path.

Distinct from #3470 (``_in_pad_stub_conflicts``), which guards the
inner-layer STUB copper: #3429 guards the VIA-BARREL spacing, which the
stub-flip retry cannot fix (flipping the stub does not move the barrel).

The guard is gated to the LEGACY violation / pin_boxed rescues (which
have a lateral / surface fallback when refused).  The #3428 pocket-escape
rescue is exempt -- its divergent target-aware stubs are designed to let
two adjacent in-pad vias coexist.
"""

from __future__ import annotations

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRoute,
    EscapeRouter,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Segment, Via
from kicad_tools.router.rules import DesignRules


def _make_rules(manufacturer: str | None = "jlcpcb-tier1") -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


def _make_pad(x: float, y: float, net: int, name: str, pin: str = "1") -> Pad:
    """A fine-pitch oblong pad large enough to host an in-pad via."""
    return Pad(
        x=x,
        y=y,
        width=1.5,  # long axis along X
        height=0.3,  # short axis along Y
        net=net,
        net_name=name,
        ref="U1",
        pin=pin,
        layer=Layer.F_CU,
    )


def _sibling_in_pad_escape(
    x: float,
    y: float,
    net: int = 99,
    diameter: float = 0.6,
    pin: str = "99",
) -> EscapeRoute:
    """A foreign-net escape that already placed an in-pad via at (x, y)."""
    pad = _make_pad(x, y, net, f"NET_{net}", pin=pin)
    return EscapeRoute(
        pad=pad,
        direction=EscapeDirection.WEST,
        escape_point=(x - 0.6, y),
        escape_layer=Layer.IN1_CU,
        via_pos=(x, y),
        segments=[
            Segment(
                x1=x,
                y1=y,
                x2=x - 0.6,
                y2=y,
                width=0.2,
                layer=Layer.IN1_CU,
                net=net,
                net_name=f"NET_{net}",
            ),
        ],
        via=Via(
            x=x,
            y=y,
            drill=diameter / 2,
            diameter=diameter,
            layers=(Layer.F_CU, Layer.IN1_CU),
            net=net,
            net_name=f"NET_{net}",
            in_pad=True,
        ),
        ring_index=0,
    )


# ---------------------------------------------------------------------------
# 1. Pairwise spacing predicate (``_adjacent_in_pad_via_conflict``)
# ---------------------------------------------------------------------------


class TestAdjacentInPadViaConflictPredicate:
    def test_sub_pitch_pair_conflicts(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        # Sibling 0.6 mm via 0.5 mm away: required spacing
        # 0.3 + 0.3 + 0.127 = 0.727 mm > 0.5 mm -> conflict.
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99)
        conflict = er._adjacent_in_pad_via_conflict(
            x=0.0,
            y=0.5,
            via_diameter=0.6,
            clearance=0.127,
            same_net=1,
            existing_escapes=[sibling],
        )
        assert conflict is sibling

    def test_coarse_pitch_pair_does_not_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        # 0.8 mm apart >= 0.727 mm required -> no conflict (the guard
        # must NOT fire for coarse-pitch packages).
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99)
        assert (
            er._adjacent_in_pad_via_conflict(
                x=0.0,
                y=0.8,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is None
        )

    def test_same_net_sibling_is_not_a_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=1)
        assert (
            er._adjacent_in_pad_via_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is None
        )

    def test_surface_escape_sibling_ignored(self):
        """A sibling escape with no in-pad via is not a barrel conflict."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        surface = EscapeRoute(
            pad=_make_pad(0.0, 0.0, net=99, name="NET_99"),
            direction=EscapeDirection.WEST,
            escape_point=(-1.0, 0.0),
            escape_layer=Layer.F_CU,
            via_pos=None,
            segments=[],
            via=None,
            ring_index=0,
        )
        assert (
            er._adjacent_in_pad_via_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[surface],
            )
            is None
        )

    def test_none_existing_escapes_is_no_op(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        assert (
            er._adjacent_in_pad_via_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=None,
            )
            is None
        )

    def test_micro_via_pair_at_half_mm_does_not_conflict(self):
        """0.3 mm OD micro-vias at 0.5 mm pitch: required spacing
        0.15 + 0.15 + 0.127 = 0.427 mm < 0.5 mm -> no conflict.  This is
        why the board-04 production recipe (``--micro-via-in-pad-fallback``)
        can fit two adjacent in-pad vias where the standard 0.6 mm pair
        cannot."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99, diameter=0.3)
        assert (
            er._adjacent_in_pad_via_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.3,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is None
        )


# ---------------------------------------------------------------------------
# 2. ``_try_in_pad_escape`` refuses the conflicting second rescue
# ---------------------------------------------------------------------------


class TestTryInPadEscapeRefusal:
    def _router(self) -> EscapeRouter:
        rules = _make_rules()
        return EscapeRouter(_make_grid(rules), rules)

    def test_second_rescue_refused_when_sibling_via_conflicts(self):
        er = self._router()
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        # A sibling in-pad via 0.5 mm away (sub-pitch conflict).
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99)
        pad = _make_pad(0.0, 0.5, net=1, name="VICTIM")
        before = er.adjacent_in_pad_via_conflicts_refused
        route = er._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.WEST,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=[sibling],
            enforce_adjacent_via_spacing=True,
        )
        assert route is None, "conflicting second in-pad via must be refused"
        assert er.adjacent_in_pad_via_conflicts_refused == before + 1

    def test_no_refusal_when_spacing_is_adequate(self):
        er = self._router()
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99)
        pad = _make_pad(0.0, 1.0, net=1, name="OK")  # 1.0 mm away, clears
        route = er._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.WEST,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=[sibling],
            enforce_adjacent_via_spacing=True,
        )
        assert route is not None
        assert route.via is not None and route.via.in_pad
        assert er.adjacent_in_pad_via_conflicts_refused == 0

    def test_guard_disabled_by_default_preserves_legacy(self):
        """``enforce_adjacent_via_spacing`` defaults to False -- the same
        conflicting geometry produces a via, exactly as before #3429."""
        er = self._router()
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        sibling = _sibling_in_pad_escape(0.0, 0.0, net=99)
        pad = _make_pad(0.0, 0.5, net=1, name="VICTIM")
        route = er._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.WEST,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=[sibling],
            # enforce_adjacent_via_spacing omitted -> default False
        )
        assert route is not None and route.via is not None
        assert er.adjacent_in_pad_via_conflicts_refused == 0
