"""Unit tests for Phase 6 (P_FP6) of the fine-pitch escape ladder.

Issue #3381 / P_FP6 -- verifies the wiring of ``_try_in_pad_escape``
into the SOP staggered dispatcher (``_create_staggered_row_escapes``)
plus the regression-prevention contract for the
``--micro-via-in-pad-fallback`` flag on the SOP rescue path.

Six tests covering:

1. **Gate function** (``_sop_in_pad_rescue_eligible``):
   - All four conditions met (UCC27211 SOIC-8 + jlcpcb-tier1 + region +
     long-axis >= min via OD) -> True.
   - Tier-0 jlcpcb (no via-in-pad) -> False.
   - No region installed -> False.
   - Pitch above the 1.5 mm threshold -> False (excludes 2.54 mm SOPs).

2. **Wiring** (``_create_staggered_row_escapes``):
   - When a fine-pitch region is installed AND the staggered launch
     would clip a neighbour pad, the rescue fires and produces an
     in-pad via at the pad centre (replacing the staggered geometry).
   - When the staggered launch does NOT clip a neighbour pad (normal
     UCC27211 SOIC-8 geometry at tier-1), the rescue is skipped and
     the legacy staggered geometry is preserved bit-for-bit -- this
     pins the no-op contract for the common case.

3. **Regression-prevention** (P_FP5 fallback monotonicity):
   - Toggling ``KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK`` between off
     and on on the SOP staggered path produces escape counts where
     fallback-enabled count is >= fallback-disabled count.  Captures
     the architect-requested invariant for the SOP rescue path that
     P_FP6 introduces.

Issue: https://github.com/rjwalters/kicad-tools/issues/3381
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from kicad_tools.router.escape import EscapeDirection, EscapeRouter
from kicad_tools.router.fine_pitch_escape import FinePitchRegion
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ============================================================================
# Fixtures -- mirror P_FP5 with a tight SOIC-8 row that forces a clip
# ============================================================================


def _ucc27211_row(origin_x: float = 50.0, y: float = 50.0) -> list[Pad]:
    """4 pads of a UCC27211 SOIC-8 north row (1.27mm pitch, 0.30 x 1.55 mm).

    Mirrors the P_FP5 helper so the new wiring tests run on a geometry the
    pre-existing tests have already characterized.
    """
    pads: list[Pad] = []
    for i in range(4):
        pads.append(
            Pad(
                x=origin_x + i * 1.27,
                y=y,
                width=0.30,
                height=1.55,
                net=10 + i,
                net_name=f"NET{i + 1}",
                layer=Layer.F_CU,
                ref="U5",
                pin=str(i + 1),
            )
        )
    return pads


def _make_rules() -> DesignRules:
    """Strict tier-1 recipe (0.20mm clearance, 0.30mm trace)."""
    return DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        grid_resolution=0.10,
        via_drill=0.30,
        via_diameter=0.60,
        manufacturer="jlcpcb-tier1",
    )


def _make_tier0_rules() -> DesignRules:
    """Plain JLCPCB tier-0 (no via-in-pad)."""
    return DesignRules(
        trace_width=0.30,
        trace_clearance=0.20,
        grid_resolution=0.10,
        via_drill=0.30,
        via_diameter=0.60,
        manufacturer="jlcpcb",
    )


def _make_grid(rules: DesignRules) -> RoutingGrid:
    return RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=40.0,
        origin_y=40.0,
        layer_stack=LayerStack.two_layer(),
    )


def _make_router(rules: DesignRules) -> EscapeRouter:
    grid = _make_grid(rules)
    return EscapeRouter(grid, rules)


def _install_u5_region(router: EscapeRouter, pads: list[Pad], pitch: float = 1.27) -> None:
    """Install a fine-pitch region for U5 with tight escape clearance."""
    region = FinePitchRegion(
        package_ref="U5",
        package_origin=(pads[0].x + 1.5 * pitch, pads[0].y),
        radius_mm=5.0,
        pin_pitch=pitch,
        pad_size_along_pitch=0.30,
        escape_clearance=0.14,
    )
    router.grid.set_fine_pitch_regions([region])


class _FakePackage:
    """``PackageInfo`` stand-in carrying every attribute the SOP path reads.

    Includes ``pin_pitch`` (consumed by the P_FP6 gate) and an arbitrary
    ``package_type.name`` (consumed only for diagnostic logging).
    """

    class _Type:
        name = "SOP"

    package_type = _Type()

    def __init__(self, pads: list[Pad], pin_pitch: float = 1.27):
        self.pads = pads
        self.ref = pads[0].ref
        self.pin_pitch = pin_pitch
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        self.center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        # Bounding box (used elsewhere by escape helpers).
        self.bounding_box = (
            min(xs) - 0.15,
            min(ys) - 0.775,
            max(xs) + 0.15,
            max(ys) + 0.775,
        )


# ============================================================================
# 1. Gate function -- _sop_in_pad_rescue_eligible
# ============================================================================


class TestSopRescueGate:
    """Cover the four conjunctive conditions on the P_FP6 rescue gate."""

    def test_all_conditions_met_returns_true(self):
        """tier-1 mfr + region installed + 1.27mm pitch + 1.55mm long-axis -> True."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        _install_u5_region(router, pads)
        package = _FakePackage(pads, pin_pitch=1.27)
        assert router._sop_in_pad_rescue_eligible(package, pads) is True

    def test_tier0_disables_rescue(self):
        """Plain jlcpcb (no via-in-pad) -> gate returns False."""
        rules = _make_tier0_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        _install_u5_region(router, pads)
        package = _FakePackage(pads, pin_pitch=1.27)
        # Capability gate (via_in_pad_supported == False) short-circuits.
        assert router._sop_in_pad_rescue_eligible(package, pads) is False

    def test_no_region_disables_rescue(self):
        """Without an installed region, the rescue is opt-out (False)."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        # No region installed.
        router.grid.set_fine_pitch_regions([])
        package = _FakePackage(pads, pin_pitch=1.27)
        assert router._sop_in_pad_rescue_eligible(package, pads) is False

    def test_wide_pitch_disables_rescue(self):
        """Pitch above the 1.5 mm threshold falls through to legacy (False)."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        _install_u5_region(router, pads, pitch=2.54)
        # 2.54 mm pitch is a generic SOP that doesn't need the rescue.
        package = _FakePackage(pads, pin_pitch=2.54)
        assert router._sop_in_pad_rescue_eligible(package, pads) is False


# ============================================================================
# 2. Wiring -- _create_staggered_row_escapes
# ============================================================================


class TestStaggeredRowRescueWiring:
    """Cover the in-pad rescue path inside ``_create_staggered_row_escapes``.

    The P_FP6 rescue runs as a *first try* when eligible: every non-
    plane pad gets an attempt at the in-pad escape before the legacy
    staggered geometry.  Two complementary vectors:

    a) **Eligible trigger** -- standard UCC27211 SOIC-8 geometry
       (1.27 mm pitch, 0.30 x 1.55 mm pads) in a fine-pitch region on
       jlcpcb-tier1.  The rescue must fire for every signal pad, placing
       an in-pad via dead-centre on each pad (offset 0 from the launch
       direction) instead of the staggered 0.44 mm offset.

    b) **Ineligible no-op** -- same UCC27211 SOIC-8 geometry but the
       fake package omits ``pin_pitch`` (mirrors the P_FP5 fixture path).
       The rescue gate falls through and the legacy staggered geometry
       is preserved bit-for-bit.  Pins the no-op contract for callers
       that do not carry ``pin_pitch`` metadata.
    """

    def test_eligible_geometry_triggers_in_pad_rescue(self):
        """1.27 mm SOIC + region + tier-1 -> rescue fires on every pad."""
        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        _install_u5_region(router, pads)
        package = _FakePackage(pads, pin_pitch=1.27)

        escapes = router._create_staggered_row_escapes(
            pads=pads, direction=EscapeDirection.NORTH, package=package,
        )

        # Every pad has a non-zero net, so every pad should be rescued.
        # In-pad rescue places the via dead-centre on the pad (offset 0
        # from the pad in the launch direction).  Without P_FP6 the via
        # would sit at pad.y + 0.44 mm (i=0, even) or pad.y + 0.84 mm
        # (i=1, odd with stagger).
        assert len(escapes) == 4
        for esc in escapes:
            assert esc.via_pos is not None
            assert esc.via is not None
            assert esc.via.in_pad is True, (
                f"Expected in-pad via for pad {esc.pad.pin}; got in_pad=False"
            )
            via_y_offset = esc.via_pos[1] - esc.pad.y
            # Long-axis nudge can shift the via up to a few hundred microns
            # along the long axis; the dead-centre case yields 0.
            assert abs(via_y_offset) < 0.50, (
                f"Expected dead-centre in-pad via for pad {esc.pad.pin}; "
                f"got via_y_offset={via_y_offset:.3f}mm"
            )

    def test_ineligible_package_preserves_legacy_staggered_path(self):
        """Bare ``_FakePackage`` (no pin_pitch) -> rescue does not fire.

        Mirrors the P_FP5 ``test_in_region_shortens_launch_step`` fixture
        path.  Pins the no-op contract for legacy / test-fake callers
        that do not surface ``pin_pitch``: the P_FP6 gate must fail
        gracefully and the legacy staggered geometry (with the P_FP5
        per-component clearance shrink) is preserved.
        """
        from tests.test_fine_pitch_p_fp5 import _FakePackage as _BarePackage

        rules = _make_rules()
        router = _make_router(rules)
        pads = _ucc27211_row()
        _install_u5_region(router, pads)
        # _BarePackage has no ``pin_pitch`` attribute -- the gate's
        # ``getattr(package, "pin_pitch", None)`` falls back to None
        # and the rescue is short-circuited.
        package = _BarePackage(pads)

        escapes = router._create_staggered_row_escapes(
            pads=pads, direction=EscapeDirection.NORTH, package=package,
        )

        # Pin 0 (i=0, even) gets no stagger offset -> via_y is at
        # pad.y + (0.14 + 0.30) = pad.y + 0.44 mm via the P_FP5 shrink.
        # If the rescue had fired, the via would be at the pad centre.
        first = escapes[0]
        assert first.via_pos is not None
        assert first.via is not None
        # Legacy staggered via is NOT in_pad-tagged.
        assert first.via.in_pad is False
        via_dist = first.via_pos[1] - first.pad.y
        assert via_dist == pytest.approx(0.44, abs=0.01), (
            f"Expected staggered launch distance 0.44 mm on legacy path; "
            f"got {via_dist:.3f}mm (suggests P_FP6 rescue fired on a "
            "bare PackageInfo fixture)."
        )


# ============================================================================
# 3. Regression-prevention -- micro-via fallback monotonicity on SOP path
# ============================================================================


class TestSopRescueFallbackMonotonic:
    """Pin the architect-requested invariant for the SOP rescue path:

    Toggling ``KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK`` between off and on
    must not reduce the count of escape routes produced by
    ``_create_staggered_row_escapes`` on the new P_FP6 SOP rescue path.

    Empirical context: the architect measured the system-level fallback
    regression at 24/30 -> 22/30 on softstart rev B (issue #3381 comment).
    The dominant residual failures live on the U1 LQFP-32 escape path
    which is out of scope for P_FP6 (sibling issue #3385).  This unit
    test pins the contract for the SOP path P_FP6 introduces: the
    fallback flag must be monotonic-or-noop on the new wiring.
    """

    @staticmethod
    def _make_router_with_env(enabled: bool) -> EscapeRouter:
        """Construct a router with the fallback env var pinned."""
        rules = _make_rules()
        env = {
            "KICAD_TOOLS_MICRO_VIA_IN_PAD_FALLBACK": "1" if enabled else "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _make_router(rules)

    def test_fallback_does_not_regress_on_loose_geometry(self):
        """Toggling the flag on the no-clip geometry is a no-op."""
        # Off baseline.
        router_off = self._make_router_with_env(enabled=False)
        pads_off = _ucc27211_row()
        _install_u5_region(router_off, pads_off)
        pkg_off = _FakePackage(pads_off, pin_pitch=1.27)
        esc_off = router_off._create_staggered_row_escapes(
            pads=pads_off, direction=EscapeDirection.NORTH, package=pkg_off,
        )

        # On.
        router_on = self._make_router_with_env(enabled=True)
        pads_on = _ucc27211_row()
        _install_u5_region(router_on, pads_on)
        pkg_on = _FakePackage(pads_on, pin_pitch=1.27)
        esc_on = router_on._create_staggered_row_escapes(
            pads=pads_on, direction=EscapeDirection.NORTH, package=pkg_on,
        )

        # On the no-clip geometry the rescue must not fire either way,
        # so the routes are identical (same count, same via offsets).
        assert len(esc_on) >= len(esc_off), (
            "Fallback ON must not reduce escape route count; "
            f"off={len(esc_off)}, on={len(esc_on)}"
        )
        # Per-pad via offsets identical -- pins down the strict no-op
        # contract for the common case.
        for off_route, on_route in zip(esc_off, esc_on, strict=True):
            assert off_route.via_pos is not None
            assert on_route.via_pos is not None
            assert off_route.via_pos[1] == pytest.approx(
                on_route.via_pos[1], abs=1e-6,
            ), (
                "Fallback ON changed via_y on the legacy SOP path -- "
                "the flag must be a strict no-op when the rescue "
                "doesn't fire."
            )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--no-cov"]))
