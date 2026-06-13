"""Tests for Issue #3470: conflict-aware in-pad escape stub direction.

Background
----------

On board 05's DRV8301 (U3, 0.5 mm pitch) the in-pad escape rescues for
pin 31 (ISENSE_B-) and pin 33 (ISENSE_A-) emitted inner-layer stubs
pointing TOWARD each other across the intervening pin 32 column:

    pin 31 stub: (118.25, 153.75) -> (118.80, 153.75)  w=0.127  In1.Cu
    pin 33 stub: (119.25, 153.75) -> (118.70, 153.75)  w=0.500  In1.Cu

The stubs physically overlapped (DRC ``clearance_segment_segment``
actual = -0.3135 mm -- the single blocking violation on the committed
board-05 snapshot, stable across seeds 7/42/123) and each net's escape
endpoint (virtual pad) landed inside the other net's stub copper,
making both nets deterministically unroutable.

The fix threads the escapes generated so far into
``_try_in_pad_escape`` (``existing_escapes``).  The proposed stub is
validated against every FOREIGN-net escape stub/via; on conflict the
stub direction is retried (opposite first, then perpendiculars) and the
first conflict-free direction wins.
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


def _make_rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer="jlcpcb-tier1",  # via-in-pad capable tier
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


def _foreign_escape_with_inner_stub(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    net: int = 99,
    width: float = 0.2,
) -> EscapeRoute:
    """Build a pre-existing foreign-net escape with an In1.Cu stub."""
    pad = _make_pad(x1, y1, net, f"NET_{net}", pin="99")
    return EscapeRoute(
        pad=pad,
        direction=EscapeDirection.EAST,
        escape_point=(x2, y2),
        escape_layer=Layer.IN1_CU,
        via_pos=(x1, y1),
        segments=[
            Segment(
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                width=width,
                layer=Layer.IN1_CU,
                net=net,
                net_name=f"NET_{net}",
            ),
        ],
        via=Via(
            x=x1,
            y=y1,
            drill=0.15,
            diameter=0.3,
            layers=(Layer.F_CU, Layer.IN1_CU),
            net=net,
            net_name=f"NET_{net}",
            in_pad=True,
        ),
        ring_index=0,
    )


class TestInPadStubConflictDetection:
    """Unit tests for ``_in_pad_stub_conflicts``."""

    def test_overlapping_foreign_stub_detected(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        # Foreign stub runs WEST->EAST through the probe area.
        foreign = _foreign_escape_with_inner_stub(-1.0, 0.0, -0.2, 0.0)
        assert (
            er._in_pad_stub_conflicts(
                0.0,
                0.0,
                -0.6,
                0.0,  # proposed stub points WEST into the foreign stub
                0.2,
                Layer.IN1_CU,
                net=1,
                clearance=0.15,
                existing_escapes=[foreign],
            )
            is True
        )

    def test_same_net_stub_is_not_a_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        same_net = _foreign_escape_with_inner_stub(-1.0, 0.0, -0.2, 0.0, net=1)
        assert (
            er._in_pad_stub_conflicts(
                0.0,
                0.0,
                -0.6,
                0.0,
                0.2,
                Layer.IN1_CU,
                net=1,
                clearance=0.15,
                existing_escapes=[same_net],
            )
            is False
        )

    def test_different_layer_stub_is_not_a_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        foreign = _foreign_escape_with_inner_stub(-1.0, 0.0, -0.2, 0.0)
        # Same XY corridor but probing on F.Cu -- segments don't collide.
        # (The foreign In1 via barrel is >0.4mm clear of the probe stub.)
        assert (
            er._in_pad_stub_conflicts(
                0.0,
                0.0,
                -0.6,
                0.0,
                0.2,
                Layer.F_CU,
                net=1,
                clearance=0.15,
                existing_escapes=[foreign],
            )
            is False
        )

    def test_clear_geometry_is_not_a_conflict(self):
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        foreign = _foreign_escape_with_inner_stub(-5.0, 5.0, -4.0, 5.0)
        assert (
            er._in_pad_stub_conflicts(
                0.0,
                0.0,
                -0.6,
                0.0,
                0.2,
                Layer.IN1_CU,
                net=1,
                clearance=0.15,
                existing_escapes=[foreign],
            )
            is False
        )


class TestInPadStubDirectionRetry:
    """``_try_in_pad_escape`` flips the stub away from conflicting copper."""

    def _gen(self, er: EscapeRouter, pad: Pad, existing) -> EscapeRoute | None:
        return er._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.WEST,
            effective_clearance=0.15,
            escape_width=0.2,
            package=None,
            existing_escapes=existing,
        )

    def test_stub_flips_away_from_conflicting_foreign_stub(self):
        """The board-05 pin31/pin33 shape: a WEST-pointing stub would
        overlap the foreign EAST-pointing stub; the fix flips it EAST."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        pad = _make_pad(0.0, 0.0, net=1, name="ISENSE_A-")
        # Foreign stub occupies the corridor WEST of the pad on the inner
        # escape layer (mirrors ISENSE_B-'s pin-31 stub: it ends ~0.45 mm
        # from this pad's via center, so a WEST stub overlaps it while an
        # EAST stub clears it).
        foreign = _foreign_escape_with_inner_stub(-1.2, 0.0, -0.45, 0.0)

        route = self._gen(er, pad, [foreign])
        assert route is not None, "in-pad escape should still be generated"
        ex, ey = route.escape_point
        assert ex > pad.x, (
            f"Stub should flip EAST away from the foreign WEST corridor; "
            f"escape_point=({ex}, {ey}), direction={route.direction}"
        )
        # And the emitted stub must not overlap the foreign copper.
        assert not er._in_pad_stub_conflicts(
            route.segments[0].x1,
            route.segments[0].y1,
            route.segments[0].x2,
            route.segments[0].y2,
            route.segments[0].width,
            route.escape_layer,
            1,
            0.15,
            [foreign],
        )

    def test_no_existing_escapes_keeps_legacy_direction(self):
        """``existing_escapes=None`` preserves the legacy WEST stub
        byte-for-byte (no behaviour change for existing call sites)."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        pad = _make_pad(0.0, 0.0, net=1, name="ISENSE_A-")
        route = er._try_in_pad_escape(
            pad=pad,
            direction=EscapeDirection.WEST,
            effective_clearance=0.15,
            escape_width=0.2,
            package=None,
        )
        assert route is not None
        ex, _ey = route.escape_point
        assert ex < pad.x, "Legacy path must keep the WEST stub"

    def test_clear_primary_direction_is_kept(self):
        """When the primary direction has no conflict, it is kept even
        with existing_escapes supplied."""
        rules = _make_rules()
        er = EscapeRouter(_make_grid(rules), rules)
        if not er.via_in_pad_supported:
            import pytest

            pytest.skip("manufacturer profile does not enable via-in-pad")
        pad = _make_pad(0.0, 0.0, net=1, name="ISENSE_A-")
        far_foreign = _foreign_escape_with_inner_stub(-8.0, 6.0, -7.0, 6.0)
        route = self._gen(er, pad, [far_foreign])
        assert route is not None
        ex, _ey = route.escape_point
        assert ex < pad.x, "Unconflicted primary (WEST) direction must be kept"
