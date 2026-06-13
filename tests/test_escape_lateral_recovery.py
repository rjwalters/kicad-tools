"""Tests for the lateral via-escape recovery (Issue #3063, sub-B of #3048).

When ``--strict-in-pad-clearance`` causes ``_try_in_pad_escape`` to
defer (return ``None`` because the dead-centre via would clip a
foreign neighbour and the long-axis nudge cannot rescue), the
dispatcher now retries with ``_try_lateral_via_escape``, probing
off-pad via candidates along the natural escape direction within
~0.5mm.

This module pins three behaviours:

1. **Success case**: a pad whose in-pad rescue would defer in strict
   mode has an open lane along the escape direction within 0.5mm;
   the lateral helper finds the first passing candidate and emits an
   ``EscapeRoute`` with an L-stub + via + inner-layer escape.

2. **Negative case**: a pad surrounded by foreign-net obstacles on
   all approach lanes within the search budget; the lateral helper
   returns ``None`` gracefully.

3. **Regression case**: with strict mode OFF, the legacy
   "commit-anyway" branch of ``_try_in_pad_escape`` still fires and
   the lateral helper is never invoked (no behaviour change).

The fixtures intentionally use a simpler geometry than
``tests/fixtures/strict_in_pad_min.py`` -- there the violation is on
the SHORT axis perpendicular to the long-axis nudge, so the in-pad
helper falls through to the strict deferral.  We layer additional
foreign pads (or lack thereof) along the SOUTH escape direction to
gate the lateral rescue success/failure.
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.router.escape import EscapeDirection, EscapeRouter, PackageInfo, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from tests.fixtures.strict_in_pad_min import (
    CLEARANCE,
    PAD_LONG,
    PAD_SHORT,
    PITCH,
    make_rules,
)

# ----------------------------------------------------------------------------
# Helpers
#
# These tests use a translated copy of the strict-in-pad violating-pair
# fixture: the primary pad sits at (5.0, 5.0) world rather than (0.0, 0.0).
# ``_can_place_via`` enforces world-coordinate bounds in [0, grid.width],
# so a primary at the origin would have its SOUTH (-Y) candidates rejected
# on bounds rather than on neighbour-clearance.  We translate up by half
# the grid extent so the search budget stays comfortably inside bounds.
# ----------------------------------------------------------------------------


# Place the primary pad at the middle of the 10x10 grid (origin 0,0 ->
# extents [0, 10]) so SOUTH (-Y) candidates have plenty of in-bounds
# room.  The grid here uses a non-negative origin to avoid the
# _can_place_via bounds quirk that rejects negative world coords.
PRIMARY_X: float = 5.0
PRIMARY_Y: float = 5.0


def _make_grid_origin_zero(rules) -> RoutingGrid:
    """RoutingGrid sized 10x10 mm with origin at (0, 0) -- world coords
    map directly onto the grid bounds ``_can_place_via`` checks.
    """
    return RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
    )


def _make_translated_violating_pair() -> tuple[Pad, Pad]:
    """Like ``make_violating_pair`` but translated so primary is at
    (PRIMARY_X, PRIMARY_Y) world.
    """
    primary = Pad(
        x=PRIMARY_X,
        y=PRIMARY_Y,
        width=PAD_LONG,
        height=PAD_SHORT,
        net=1,
        net_name="NET1",
        ref="U1",
        pin="1",
        layer=Layer.F_CU,
    )
    neighbour = Pad(
        x=PRIMARY_X,
        y=PRIMARY_Y + PITCH,
        width=PAD_LONG,
        height=PAD_SHORT,
        net=2,
        net_name="NET2",
        ref="U1",
        pin="2",
        layer=Layer.F_CU,
    )
    return primary, neighbour


def _make_translated_package() -> PackageInfo:
    primary, neighbour = _make_translated_violating_pair()
    pads = [primary, neighbour]
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref="U1",
        package_type=PackageType.QFP,
        center=(sum(xs) / len(xs), sum(ys) / len(ys)),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=PITCH,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


def _build_router(strict: bool = True) -> EscapeRouter:
    """Build an EscapeRouter on the translated violating-pair geometry."""
    rules = make_rules(manufacturer="jlcpcb-tier1")
    grid = _make_grid_origin_zero(rules)
    router = EscapeRouter(grid, rules)
    router.strict_in_pad_clearance = strict
    return router


def _package_with_extra_obstacle(blocker_dy: float) -> PackageInfo:
    """Violating pair + a third foreign pad south of the primary.

    ``blocker_dy`` is the Y-offset of the blocker pad RELATIVE to the
    primary (negative = SOUTH).  Choose a small ``blocker_dy`` (e.g.
    -0.2 mm) to block every lateral candidate within ~0.5 mm; choose
    a larger magnitude (e.g. -2.0) to leave the SOUTH lane open.
    """
    primary, neighbour = _make_translated_violating_pair()
    blocker = Pad(
        x=PRIMARY_X,
        y=PRIMARY_Y + blocker_dy,
        width=1.5,  # long axis along X
        height=0.30,  # short axis along Y
        net=99,
        net_name="BLOCK",
        ref="U1",
        pin="99",
        layer=Layer.F_CU,
    )
    pads = [primary, neighbour, blocker]
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref="U1",
        package_type=PackageType.QFP,
        center=(sum(xs) / len(xs), sum(ys) / len(ys)),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=PITCH,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


@pytest.fixture(autouse=True)
def _clear_env_strict(monkeypatch):
    """Clear KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE before each test so the
    constructor-time strict resolution starts from a known state.
    """
    monkeypatch.delenv("KICAD_TOOLS_STRICT_IN_PAD_CLEARANCE", raising=False)
    yield


# ----------------------------------------------------------------------------
# Success case: lateral lane available
# ----------------------------------------------------------------------------


class TestLateralRecoverySucceeds:
    """Lateral helper finds an off-pad via within the search budget."""

    def test_lateral_recovery_returns_route_when_lane_is_open(self, caplog):
        """With the violating pair as the package context, the lateral
        helper should find a passing candidate along the SOUTH
        direction (-Y) since the neighbour is to the NORTH (+Y).
        """
        router = _build_router(strict=True)
        # Primary at (PRIMARY_X, PRIMARY_Y) world; neighbour at primary + PITCH along Y.
        package = _make_translated_package()
        primary = package.pads[0]

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.escape"):
            route = router._try_lateral_via_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
            )

        assert route is not None, (
            "Lateral helper must find an off-pad via candidate when the "
            "escape direction (SOUTH = -Y) has an open lane.  Got None."
        )
        # Via must be off-pad: the whole point is the lateral offset.
        # Primary is at (0, 0); SOUTH means strictly negative Y.
        assert route.via is not None, "Lateral route must have a via"
        assert route.via.y < primary.y - 1e-6, (
            f"Lateral via must be SOUTH of the pad center; got via.y="
            f"{route.via.y} >= primary.y={primary.y}"
        )
        # Via should not be inside the pad copper (purpose of lateral)
        assert route.via.in_pad is False, "Lateral via must have in_pad=False (it sits off the pad)"
        # Route must have BOTH segments: surface stub + inner escape.
        assert len(route.segments) == 2, (
            f"Lateral route should have 2 segments (stub + inner); got {len(route.segments)}"
        )
        # First segment is the surface stub from pad to via.
        stub = route.segments[0]
        assert stub.layer == primary.layer, "Surface stub must stay on the pad's layer"
        assert abs(stub.x1 - primary.x) < 1e-6 and abs(stub.y1 - primary.y) < 1e-6, (
            "Surface stub must start at the pad center"
        )
        assert abs(stub.x2 - route.via.x) < 1e-6 and abs(stub.y2 - route.via.y) < 1e-6, (
            "Surface stub must end at the via location"
        )
        # Inner segment continues outward past the via.
        inner = route.segments[1]
        assert inner.layer != primary.layer, (
            "Inner escape segment must be on a different layer than the pad"
        )
        # Diagnostic INFO log line should be present.
        info_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "Lateral via-escape rescue" in r.message
        ]
        assert info_records, "Expected the 'Lateral via-escape rescue' INFO log line; saw: " + str(
            [r.message for r in caplog.records]
        )

    def test_lateral_recovery_picks_smallest_valid_offset(self):
        """The search starts at step=0.05mm and walks outward; the
        returned via should be at the SMALLEST offset that passes,
        not an arbitrary one further away.

        Geometry note: the neighbour is at primary.y + PITCH (0.50 mm),
        with pad height 0.30 mm (so its south edge is at
        primary.y + 0.35 mm).  A SOUTH-direction via at offset 0.05 mm
        sits at via_y = primary.y - 0.05 mm; its rect-distance to the
        neighbour pad is then 0.40 mm, which is below the
        ``via_radius (0.30) + clearance (0.15) = 0.45`` mm required gap.
        Offset 0.10 mm yields rect-distance 0.45 mm exactly, the first
        offset that passes.  This pinning catches regressions in the
        step size, search order, or clearance arithmetic.
        """
        router = _build_router(strict=True)
        package = _make_translated_package()
        primary = package.pads[0]

        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is not None
        assert route.via is not None
        observed_offset = primary.y - route.via.y
        assert observed_offset == pytest.approx(0.10, abs=1e-9), (
            f"Expected smallest-valid offset of 0.10 mm (see geometry note "
            f"in docstring); got {observed_offset:.4f} mm"
        )


# ----------------------------------------------------------------------------
# Surface-stub channel-clearance regression case (Issue #3073)
# ----------------------------------------------------------------------------


class TestLateralRecoveryNecksStubWidthForChannelClearance:
    """Issue #3073 regression: the lateral helper's surface stub from
    the pad center to the off-pad via must fit through the channel
    between same-row neighbour pads.  At LQFP-48 0.5mm pitch with a
    full-width (0.5mm) net trace, the stub overshoots the ~0.2mm
    inter-pad copper gap and produces pad-segment DRC errors at the
    manufacturer's 0.127mm minimum.

    The helper must validate the surface stub against neighbour pads
    BEFORE committing the candidate via, and neck the stub down to the
    manufacturer-minimum trace width when the dispatcher-supplied
    width would violate.  If even the necked width fails, the
    candidate is rejected and the next offset is tried.
    """

    @pytest.mark.parametrize("strict", [True, False])
    def test_lateral_stub_necks_to_min_trace_when_full_width_collides(
        self,
        strict: bool,
    ):
        """When the dispatcher passes a wide ``escape_width`` (0.5mm
        net trace) that does NOT fit the channel between same-row
        neighbour pads, the helper must neck the stub down to the
        manufacturer-minimum trace width and emit a valid route.

        Issue #3080: parameterised over ``strict_in_pad_clearance``
        because the gate at the three dispatcher invocation sites has
        been removed.  The helper itself is identical between modes;
        we exercise both to pin the contract that the necking is
        available on the default (non-strict) path -- this is what
        board 04 stitching depends on.

        Geometry: with the standard violating pair at PITCH=0.50mm
        and PAD_SHORT=0.30mm, a SOUTH stub from the primary at width
        0.5mm has half-width 0.25mm.  Its closest approach to the
        NORTH neighbour pad's south edge sits at edge-to-edge gap
        ``rect_dist - stub_half_w = 0.35 - 0.25 = 0.10mm``, which is
        below the 0.15mm effective clearance and would DRC-fail.  At
        the jlcpcb-tier1 min_trace of 0.127mm (half-width 0.0635mm),
        the gap rises to ``0.35 - 0.0635 = 0.2865mm`` -- well above
        clearance -- so the helper should neck down rather than
        rejecting the candidate.
        """
        router = _build_router(strict=strict)
        package = _make_translated_package()
        primary = package.pads[0]

        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.5,  # too wide for 0.5mm-pitch channel
            package=package,
        )
        assert route is not None, (
            "Helper must still return a route by necking the stub "
            f"down to manufacturer-minimum width (strict={strict})."
        )
        stub = route.segments[0]
        # The jlcpcb-tier1 manufacturer profile has min_trace=0.127mm.
        # The helper should neck the stub down to that value (not the
        # 0.5mm dispatcher width that would violate channel clearance).
        assert stub.width == pytest.approx(0.127, abs=1e-6), (
            f"Stub width must be necked to manufacturer min_trace "
            f"(0.127mm for jlcpcb-tier1); got {stub.width}mm "
            f"(strict={strict})"
        )
        # The inner-layer segment can stay at the dispatcher width
        # (inner layers have no fine-pitch pad congestion).
        inner = route.segments[1]
        assert inner.width == pytest.approx(0.5, abs=1e-6), (
            f"Inner segment should keep the dispatcher width "
            f"(no SMT pads on the inner layer); got {inner.width}mm "
            f"(strict={strict})"
        )

    def test_lateral_stub_keeps_dispatcher_width_when_channel_is_clear(self):
        """When the dispatcher-supplied ``escape_width`` already fits
        the channel (e.g. caller passed a fine-pitch min_trace value),
        the helper must NOT down-neck it -- preserving the dispatcher's
        intent for callers that already accounted for the channel.
        """
        router = _build_router(strict=True)
        package = _make_translated_package()
        primary = package.pads[0]

        # 0.2mm-wide stub fits the channel (gap = 0.35 - 0.10 = 0.25mm
        # > 0.15mm clearance).  Helper should keep this width.
        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is not None
        stub = route.segments[0]
        assert stub.width == pytest.approx(0.2, abs=1e-6), (
            f"Dispatcher width that already fits the channel must be preserved; got {stub.width}mm"
        )


# ----------------------------------------------------------------------------
# Negative case: no lane open within the search budget
# ----------------------------------------------------------------------------


class TestLateralRecoveryFailsGracefully:
    """When no lateral candidate fits, the helper returns None."""

    def test_blocked_south_lane_returns_none(self):
        """A foreign pad placed immediately south of the primary blocks
        every lateral candidate within the 0.5 mm budget.  The helper
        must return None rather than committing a violating via.
        """
        router = _build_router(strict=True)
        # Blocker at primary.y - 0.20 mm (i.e. immediately south).
        # With a 0.30 mm-tall pad it extends from primary.y - 0.35 to
        # primary.y - 0.05.  Combined with via_radius (0.30) and
        # clearance (0.15), every SOUTH offset in [0.05, 0.50] mm
        # sits within ``via_radius + clearance`` of the blocker's
        # bounding rectangle and is rejected.
        package = _package_with_extra_obstacle(blocker_dy=-0.20)
        primary = package.pads[0]

        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is None, (
            "With the SOUTH lane fully blocked, the lateral helper must "
            "return None (deferring to the main router) instead of "
            "committing a violating via."
        )

    def test_via_in_pad_unsupported_manufacturer_returns_none(self):
        """When the manufacturer doesn't support via-in-pad processing,
        the lateral helper short-circuits to None -- it can't ship a
        lateral via on a fine-pitch pad without filled-and-plated
        processing either.  This mirrors ``_try_in_pad_escape``.
        """
        rules = make_rules(manufacturer="jlcpcb")  # no via-in-pad support
        grid = _make_grid_origin_zero(rules)
        router = EscapeRouter(grid, rules)
        assert router.via_in_pad_supported is False
        router.strict_in_pad_clearance = True

        package = _make_translated_package()
        primary = package.pads[0]

        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.SOUTH,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is None, (
            "Without via-in-pad support the lateral helper must defer "
            "(same outcome as the in-pad helper); got a route."
        )

    def test_via_down_direction_returns_none(self):
        """The VIA_DOWN sentinel direction has no axis to walk along
        (its unit vector is (0, 0)).  The helper must reject it
        rather than spinning on zero-offset candidates.
        """
        router = _build_router(strict=True)
        package = _make_translated_package()
        primary = package.pads[0]

        route = router._try_lateral_via_escape(
            pad=primary,
            direction=EscapeDirection.VIA_DOWN,
            effective_clearance=CLEARANCE,
            escape_width=0.2,
            package=package,
        )
        assert route is None


# ----------------------------------------------------------------------------
# Regression case: in-pad commit-anyway still fires when strict mode is off
# ----------------------------------------------------------------------------


class TestLateralRecoveryWithStrictDisabled:
    """With ``strict_in_pad_clearance=False`` (default), the in-pad
    helper itself still commits a violating route via its legacy
    "commit-anyway" branch -- the lateral helper is only reached when
    that branch returns None (which it doesn't in this fixture's
    geometry).  We pin the in-pad helper's behaviour here.

    Issue #3080: the dispatcher-level gate that previously suppressed
    the lateral helper in non-strict mode has been REMOVED.  The
    lateral helper still functions identically in both modes when
    reached; see the parameterized success-case tests at the top of
    this module.
    """

    def test_inpad_commit_anyway_still_fires_when_strict_disabled(self, caplog):
        """In legacy mode, ``_try_in_pad_escape`` commits the violating
        via with a WARNING log -- no INFO line about lateral rescue
        should appear because the in-pad helper returns a non-None route
        and the dispatcher's lateral fallback is therefore never reached.
        """
        router = _build_router(strict=False)
        assert router.strict_in_pad_clearance is False

        package = _make_translated_package()
        primary = package.pads[0]

        with caplog.at_level(logging.DEBUG, logger="kicad_tools.router.escape"):
            route = router._try_in_pad_escape(
                pad=primary,
                direction=EscapeDirection.SOUTH,
                effective_clearance=CLEARANCE,
                escape_width=0.2,
                package=package,
                # Dispatcher contract: passes the attribute through.
                skip_on_clearance_violation=router.strict_in_pad_clearance,
            )

        assert route is not None, "Legacy (strict=False) path must commit the violating via"
        # We did not invoke the lateral helper directly here -- we called
        # ``_try_in_pad_escape`` in isolation, so it can't have logged.
        lateral_logs = [r for r in caplog.records if "Lateral via-escape rescue" in r.message]
        assert not lateral_logs, (
            "Direct _try_in_pad_escape call must not log the lateral "
            "rescue line; got: " + str([r.message for r in lateral_logs])
        )


# ----------------------------------------------------------------------------
# End-to-end via the QFP dispatcher
# ----------------------------------------------------------------------------


def _make_qfp_west_edge_package(n_pads: int = 6) -> PackageInfo:
    """Build a fine-pitch QFP-shaped fixture with ``n_pads`` pads on
    the WEST edge.

    The minimum-viable fixture for the QFP dispatcher: pads laid out
    along a single edge (west) so ``_escape_qfp_alternating`` actually
    classifies them as a row.  Pads are at 0.50 mm pitch (LQFP-48
    geometry) with oblong fingers (0.3 x 1.5 mm), and the package
    bounding box spans both the row span AND a wider east extent so
    ``edge_margin`` is non-zero (a pad whose y is within
    ``edge_margin`` of ``min_y`` gets classified as a west pad).

    All pads use different nets so the in-pad rescue would see foreign
    neighbours; with the QFP dispatcher's clearance check, the deferral
    path fires for inner pins.
    """
    pads = []
    # West edge at x = 1.0; pads along +Y at 0.5 mm pitch
    for i in range(n_pads):
        pads.append(
            Pad(
                x=1.0,
                y=2.0 + i * PITCH,
                width=1.5,  # long axis (pointing east, into IC body)
                height=0.30,  # short axis (along row)
                net=i + 1,
                net_name=f"NET{i + 1}",
                ref="U1",
                pin=str(i + 1),
                layer=Layer.F_CU,
            )
        )
    # Add a couple of east-edge pads so the bounding box has a real
    # width -- otherwise edge_margin = min(0, height)*0.2 = 0 and no
    # pad gets classified to any edge.
    pads.append(
        Pad(
            x=6.0,
            y=2.0,
            width=1.5,
            height=0.30,
            net=100,
            net_name="EAST1",
            ref="U1",
            pin="100",
            layer=Layer.F_CU,
        )
    )
    pads.append(
        Pad(
            x=6.0,
            y=2.0 + (n_pads - 1) * PITCH,
            width=1.5,
            height=0.30,
            net=101,
            net_name="EAST2",
            ref="U1",
            pin="101",
            layer=Layer.F_CU,
        )
    )
    xs = [p.x for p in pads]
    ys = [p.y for p in pads]
    return PackageInfo(
        ref="U1",
        package_type=PackageType.QFP,
        center=((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2),
        pads=pads,
        pin_count=len(pads),
        pin_pitch=PITCH,
        bounding_box=(min(xs), min(ys), max(xs), max(ys)),
        is_dense=True,
    )


class TestCanPlaceViaBoundsAreOriginAware:
    """Pin the origin-aware bounds fix on ``_can_place_via`` (Issue
    #3063).  Prior to the fix, the bounds check was
    ``0 <= x <= grid.width``, which was implicitly correct only for
    grids constructed with ``origin_x=origin_y=0``.  Boards whose
    routing grid sits at non-zero origin (e.g. board-04 STM32 PCB at
    world coordinates around (95, 90)) had every world-coord candidate
    rejected on bounds, which made the lateral re-attempt's
    ``_can_place_via`` probe a no-op.
    """

    def test_can_place_via_accepts_in_bounds_world_coord_on_offset_grid(self):
        """A grid with non-zero origin must accept a candidate whose
        world coordinates fall within ``[origin, origin + extent]``.
        """
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from tests.fixtures.strict_in_pad_min import make_rules

        rules = make_rules(manufacturer="jlcpcb-tier1")
        # Grid sized 10x10 with origin at (100, 100): valid world
        # coords are [100, 110] x [100, 110].
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            origin_x=100.0,
            origin_y=100.0,
            layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
        )
        router = EscapeRouter(grid, rules)
        # A point inside the grid in world coords (105, 105) -- the
        # pre-#3063 bounds check rejected this because 105 > grid.width
        # (=10).  The origin-aware check must accept it.
        ok = router._can_place_via(x=105.0, y=105.0, net=None)
        assert ok is True, (
            "Origin-aware bounds must accept a world-coord candidate "
            "inside [origin, origin+extent]; pre-#3063 form rejected "
            "this incorrectly."
        )

    def test_can_place_via_rejects_out_of_bounds_world_coord(self):
        """A candidate outside the grid extent in world coords must
        still be rejected (the origin-aware check tightens, not loosens).
        """
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack
        from tests.fixtures.strict_in_pad_min import make_rules

        rules = make_rules(manufacturer="jlcpcb-tier1")
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            origin_x=100.0,
            origin_y=100.0,
            layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
        )
        router = EscapeRouter(grid, rules)
        # Outside the [100, 110] extent.
        assert router._can_place_via(x=99.0, y=105.0, net=None) is False
        assert router._can_place_via(x=111.0, y=105.0, net=None) is False
        assert router._can_place_via(x=105.0, y=99.0, net=None) is False
        assert router._can_place_via(x=105.0, y=111.0, net=None) is False


class TestQfpDispatcherWiring:
    """The QFP/SSOP/even-pin dispatcher sites must invoke the lateral
    helper after a strict in-pad deferral.  We exercise the wiring by
    monkey-patching the in-pad helper to force-defer and asserting the
    lateral helper is called.
    """

    def test_dispatcher_invokes_lateral_after_strict_defer(self):
        """Force ``_try_in_pad_escape`` to always return None (simulating
        the strict-mode defer).  When the dispatcher hits the deferral
        in strict mode, it must invoke ``_try_lateral_via_escape``.
        """
        router = _build_router(strict=True)
        # Force the in-pad helper to defer unconditionally.  This
        # simulates the strict-mode condition where every dead-centre
        # via clips a neighbour and the long-axis nudge cannot rescue.
        router._try_in_pad_escape = lambda **kwargs: None  # type: ignore[method-assign]

        # Spy on the lateral helper to confirm it gets called.
        calls: list[dict] = []
        original_lateral = router._try_lateral_via_escape

        def spy_lateral(**kwargs):
            calls.append(kwargs)
            return original_lateral(**kwargs)

        router._try_lateral_via_escape = spy_lateral  # type: ignore[method-assign]

        package = _make_qfp_west_edge_package(n_pads=6)
        # Inject the pads into the grid so the dispatcher's clearance
        # checks see real pad copper.
        for pad in package.pads:
            router.grid.add_pad(pad)

        # Run the QFP dispatcher; with strict mode + forced in-pad
        # deferral, any clearance violation should funnel through the
        # lateral helper.
        try:
            router._escape_qfp_alternating(package)
        except Exception:  # noqa: BLE001
            # Minimal fixtures may trip downstream assertions; the
            # spy is what matters here.
            pass

        assert calls, (
            "Strict-mode dispatcher must invoke _try_lateral_via_escape "
            "after _try_in_pad_escape returns None; the spy recorded "
            "zero calls.  This indicates the dispatcher-site wiring is "
            "broken at the QFP-alternating site (escape.py ~2247)."
        )

    def test_dispatcher_invokes_lateral_in_non_strict_mode_after_inpad_defers(self):
        """Issue #3080: the lateral helper must be invoked in BOTH
        strict and non-strict mode when the in-pad helper returns None.

        Before #3080, the dispatcher gated the lateral fallback on
        ``self.strict_in_pad_clearance`` so non-strict callers (e.g.
        board 04, whose ``generate_design.py:route_pcb`` does NOT enable
        strict mode) could not benefit from PR #3079's surface-stub
        necking.  Board 04's U2.8 GND stitch window required the necked
        stub; the gate removal closes that gap.

        We force ``_try_in_pad_escape`` to defer (returning None) and
        assert the dispatcher reaches the lateral helper even though
        ``strict_in_pad_clearance`` is False.  This is strictly
        additive: when the in-pad helper succeeds (returns a non-None
        route), the `continue` short-circuits before this branch and
        non-strict callers whose in-pad rescue succeeds today are
        unaffected.
        """
        router = _build_router(strict=False)
        assert router.strict_in_pad_clearance is False
        # Force the in-pad helper to defer unconditionally so the
        # dispatcher's lateral fallback is the only remaining action.
        router._try_in_pad_escape = lambda **kwargs: None  # type: ignore[method-assign]

        calls: list[dict] = []
        original_lateral = router._try_lateral_via_escape

        def spy_lateral(**kwargs):
            calls.append(kwargs)
            return original_lateral(**kwargs)

        router._try_lateral_via_escape = spy_lateral  # type: ignore[method-assign]

        package = _make_qfp_west_edge_package(n_pads=6)
        for pad in package.pads:
            router.grid.add_pad(pad)

        try:
            router._escape_qfp_alternating(package)
        except Exception:  # noqa: BLE001
            pass

        assert calls, (
            "Issue #3080: non-strict mode MUST invoke the lateral helper "
            "after the in-pad helper defers.  The previous strict-only "
            "gate has been removed so PR #3079's surface-stub necking "
            "reaches default callers (board 04 stitching depends on it). "
            "Got zero calls."
        )
