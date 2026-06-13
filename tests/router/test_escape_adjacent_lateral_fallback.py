"""Issue #3430: auto-lateral-via fallback when an adjacent fine-pitch pin
blocks an in-pad rescue.

Background
----------

This is the RECOVERY half of the adjacent-pin via-in-pad story; #3429 is
the DETECTION half.

At fine pitch (LQFP-48 0.5 mm) two adjacent foreign-net pins can each be
flagged for an in-pad via rescue, but their 0.6 mm barrels cannot coexist
(required center spacing 0.3 + 0.3 + 0.127 = 0.727 mm > 0.5 mm pitch).
#3429 added ``_adjacent_in_pad_via_conflict`` so the second pin's
in-pad rescue (:meth:`EscapeRouter._try_in_pad_escape`) returns ``None``
instead of committing a pair that ``apply_escape_routes`` later silently
drops.

#3430 ensures the refused pin still escapes: the dispatcher
(``_escape_qfp_alternating``) falls through to
:meth:`EscapeRouter._try_lateral_via_escape`, which pushes the via OFF
the pad along the outward escape direction (more breathing room than an
in-pad via).  Critically, the lateral candidate must be validated against
the SIBLING escape vias placed earlier in the SAME pass -- otherwise the
fix would merely relocate the silent commit-time drop from the refused
in-pad via to its lateral replacement.

These tests exercise:

1. ``_lateral_via_sibling_conflict`` -- the barrel-spacing predicate used
   by the lateral helper (mirrors ``_adjacent_in_pad_via_conflict`` but
   also rejects against sibling LATERAL vias, not just in-pad ones).
2. ``_try_lateral_via_escape(existing_escapes=...)`` -- the lateral helper
   skips candidate positions that conflict with a sibling via and returns
   the first position that clears it.
"""

from __future__ import annotations

import pytest

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
    """A fine-pitch oblong pad whose long axis runs along X."""
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


def _sibling_escape(
    x: float,
    y: float,
    net: int = 99,
    diameter: float = 0.6,
    pin: str = "99",
    in_pad: bool = True,
) -> EscapeRoute:
    """A foreign-net escape that already placed a via at (x, y).

    ``in_pad`` controls whether the via models an in-pad rescue (#3429's
    first pin) or a lateral via (an earlier pin that itself took the
    #3430 fallback).
    """
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
            in_pad=in_pad,
        ),
        ring_index=0,
    )


# ---------------------------------------------------------------------------
# 1. Lateral barrel-spacing predicate (``_lateral_via_sibling_conflict``)
# ---------------------------------------------------------------------------


class TestLateralViaSiblingConflictPredicate:
    def test_sub_pitch_in_pad_sibling_conflicts(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_escape(0.0, 0.0, net=99, in_pad=True)
        # 0.5 mm away: required 0.727 mm > 0.5 mm -> conflict.
        assert (
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is True
        )

    def test_lateral_sibling_also_conflicts(self):
        """A sibling LATERAL via (in_pad=False) must ALSO be respected --
        this is the difference from ``_adjacent_in_pad_via_conflict``,
        which only checks in-pad siblings."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_escape(0.0, 0.0, net=99, in_pad=False)
        assert (
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is True
        )

    def test_coarse_spacing_does_not_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_escape(0.0, 0.0, net=99, in_pad=True)
        # 0.8 mm away >= 0.727 mm required -> clears.
        assert (
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.8,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is False
        )

    def test_same_net_sibling_ignored(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        sibling = _sibling_escape(0.0, 0.0, net=1, in_pad=True)
        assert (
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[sibling],
            )
            is False
        )

    def test_none_existing_escapes_is_no_op(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        assert (
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=None,
            )
            is False
        )

    def test_surface_only_sibling_ignored(self):
        """A sibling escape with no via (pure surface escape) is not a
        barrel conflict."""
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
            er._lateral_via_sibling_conflict(
                x=0.0,
                y=0.5,
                via_diameter=0.6,
                clearance=0.127,
                same_net=1,
                existing_escapes=[surface],
            )
            is False
        )


# ---------------------------------------------------------------------------
# 2. ``_try_lateral_via_escape`` honours the sibling context
# ---------------------------------------------------------------------------


class TestTryLateralViaEscapeSiblingAware:
    def _router(self) -> EscapeRouter:
        rules = _make_rules()
        return EscapeRouter(_make_grid(rules), rules)

    def test_lateral_via_offset_past_sibling(self):
        """The victim pin escapes via an OFF-pad lateral via, placed far
        enough out that it clears the sibling in-pad via's barrel halo."""
        er = self._router()
        if not er.via_in_pad_supported:
            pytest.skip("manufacturer profile does not enable via-in-pad")
        # Sibling in-pad via at the pad center (0, 0); victim pad sits
        # 0.5 mm north.  Escape outward (NORTH, +y) along the victim's
        # short axis so the lateral via walks away from the sibling.
        sibling = _sibling_escape(0.0, 0.0, net=99, in_pad=True)
        victim = _make_pad(0.0, 0.5, net=1, name="VICTIM", pin="2")
        route = er._try_lateral_via_escape(
            pad=victim,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=[sibling],
        )
        assert route is not None, "victim must escape via a lateral via"
        assert route.via is not None
        assert route.via.in_pad is False, "fallback via is OFF the pad"
        # The accepted via must clear the sibling barrel: center spacing
        # >= via_r + via_r + clearance = 0.727 mm.
        import math

        spacing = math.hypot(route.via.x - sibling.via.x, route.via.y - sibling.via.y)
        assert spacing >= 0.727 - 1e-6, (
            f"lateral via at spacing {spacing:.3f}mm still conflicts with sibling barrel"
        )

    def test_legacy_call_without_siblings_unchanged(self):
        """With ``existing_escapes=None`` (legacy callers) the lateral
        helper behaves exactly as before -- no sibling rejection."""
        er = self._router()
        if not er.via_in_pad_supported:
            pytest.skip("manufacturer profile does not enable via-in-pad")
        victim = _make_pad(0.0, 0.5, net=1, name="VICTIM", pin="2")
        # No package, no siblings: the via lands at the first valid offset
        # along NORTH (the legacy behaviour the helper had pre-#3430).
        route = er._try_lateral_via_escape(
            pad=victim,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=None,
        )
        assert route is not None
        assert route.via is not None and route.via.in_pad is False

    def test_sibling_aware_pushes_via_further_than_legacy(self):
        """The sibling-aware call must place its via no closer to the
        sibling than the legacy (sibling-blind) call would -- i.e. the
        guard only ever pushes the via OUTWARD, never inward."""
        er = self._router()
        if not er.via_in_pad_supported:
            pytest.skip("manufacturer profile does not enable via-in-pad")
        import math

        sibling = _sibling_escape(0.0, 0.0, net=99, in_pad=True)
        victim = _make_pad(0.0, 0.5, net=1, name="VICTIM", pin="2")

        legacy = er._try_lateral_via_escape(
            pad=victim,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=None,
        )
        aware = er._try_lateral_via_escape(
            pad=victim,
            direction=EscapeDirection.NORTH,
            effective_clearance=0.127,
            escape_width=0.2,
            package=None,
            existing_escapes=[sibling],
        )
        assert legacy is not None and legacy.via is not None
        assert aware is not None and aware.via is not None
        legacy_spacing = math.hypot(legacy.via.x - sibling.via.x, legacy.via.y - sibling.via.y)
        aware_spacing = math.hypot(aware.via.x - sibling.via.x, aware.via.y - sibling.via.y)
        # The sibling-aware via must be at least as far from the sibling
        # as the legacy via, and must satisfy the barrel-spacing floor.
        assert aware_spacing >= legacy_spacing - 1e-6
        assert aware_spacing >= 0.727 - 1e-6


# ---------------------------------------------------------------------------
# 3. Forced-lateral observability counter
# ---------------------------------------------------------------------------


class TestForcedLateralCounterExists:
    def test_counter_initialises_to_zero(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        assert er.forced_lateral_via_fallbacks == 0
