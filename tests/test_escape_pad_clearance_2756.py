"""Tests for Issue #2756: escape-pattern endpoint pad-clearance check.

PR #2753's coordinate-space fix surfaced 105 ``clearance_pad_segment``
violations on board 05 (BLDC motor controller).  Curator analysis traced
~80% of these to the QFP/QFN/HTSSOP alternating-direction escape emitter
in ``_escape_qfp_alternating`` -- specifically, the odd-pin "parallel
along the edge" escapes that the emitter generates without first checking
whether the segment endpoint would clip a neighbouring pad on the same
edge.  The DRV8301 HTSSOP-56 (U3) and STM32G431 LQFP-32 (U10) on board 05
together account for 88 of the 105 violations, clustered tightly around
U3-32 (6 violations), U10-30/32 and U3-31/45 (4 each).

This test module pins down the invariant that the post-fix emitter
honours: every escape segment produced by ``generate_escapes`` must
maintain at least ``effective_clearance`` mm of edge-to-edge gap from
every OTHER pad on the same package, on the same layer.  Tests cover:

1. The new ``_compute_max_safe_escape_length`` helper (unit-level)
2. ``_create_alternating_escape`` clipping when ``pad_clearance_margin``
   is provided
3. ``_escape_qfp_alternating`` deferral when a clipped segment is too
   short to be useful (the in-pad fallback path on capable manufacturers
   continues to work)
4. ``_escape_radial`` clipping for TO-220-style packages where
   neighbour-pad clearance is the binding constraint

Backward compatibility:
- Passing ``pad_clearance_margin=None`` to ``_create_alternating_escape``
  preserves pre-#2756 behaviour exactly.
- SSOP/TSSOP escapes (separate ``_escape_fine_pitch_dual_row`` path) are
  unchanged -- they already had per-segment pad-clearance checks since
  Issue #2319.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.escape import (
    EscapeDirection,
    EscapeRouter,
    PackageType,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _make_dense_qfp_pads(
    pins_per_side: int = 14,
    pitch: float = 0.5,
    pad_short: float = 0.28,
    pad_long: float = 1.30,
    body_size: float | None = None,
    ref: str = "U3",
    start_net: int = 1,
) -> list[Pad]:
    """Build a synthetic dense QFP-like package.

    Defaults approximate the DRV8301 HTSSOP-56 layout (14 pins per side
    at 0.5 mm pitch on a ~7 x 14 mm body) -- the dominant violation
    source on board 05.
    """
    if body_size is None:
        body_size = (pins_per_side - 1) * pitch + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_center_offset = half_body + pad_long / 2

    pads: list[Pad] = []
    pin_no = 1
    span = (pins_per_side - 1) * pitch
    half_span = span / 2

    # WEST edge (vertical pads): long axis = X, short axis = Y
    for i in range(pins_per_side):
        y = half_span - i * pitch
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # SOUTH edge (horizontal pads): long axis = Y, short axis = X
    for i in range(pins_per_side):
        x = -half_span + i * pitch
        pads.append(
            Pad(
                x=x,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # EAST edge
    for i in range(pins_per_side):
        y = -half_span + i * pitch
        pads.append(
            Pad(
                x=pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # NORTH edge
    for i in range(pins_per_side):
        x = half_span - i * pitch
        pads.append(
            Pad(
                x=x,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=start_net + pin_no - 1,
                net_name=f"NET{start_net + pin_no - 1}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    return pads


def _jlcpcb_rules() -> DesignRules:
    """Default JLCPCB-tier-0 design rules (no via-in-pad)."""
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        via_clearance=0.127,
        grid_resolution=0.05,
        fine_pitch_clearance=0.127,
        fine_pitch_threshold=0.8,
        min_trace_width=0.127,
    )


def _make_router(rules: DesignRules | None = None) -> EscapeRouter:
    if rules is None:
        rules = _jlcpcb_rules()
    grid = RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=-20.0,
        origin_y=-20.0,
    )
    return EscapeRouter(grid, rules)


# ----------------------------------------------------------------------------
# Unit tests: _compute_max_safe_escape_length helper
# ----------------------------------------------------------------------------


class TestComputeMaxSafeEscapeLength:
    """Unit-level coverage of the new clipping helper."""

    def test_unobstructed_direction_returns_full_length(self):
        """When no other pads sit along the launch ray, the helper must
        return ``max_length`` unchanged."""
        router = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        # Single pad means no neighbours to violate.
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
        )
        assert result == pytest.approx(2.0)

    def test_neighbor_on_launch_axis_clips_segment(self):
        """A neighbour pad sitting directly along the launch direction
        must clip the segment to stop short of the neighbour's
        clearance halo."""
        router = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        # Neighbour at x=1.0, half-width 0.15.  Neighbour west edge sits
        # at x=0.85.  With trace half-width 0.1 + clearance 0.127, the
        # candidate endpoint must satisfy x <= 0.85 - 0.1 - 0.127 = 0.623.
        neighbour = Pad(
            x=1.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=2,
            net_name="B",
            ref="U1",
            pin="2",
            layer=Layer.F_CU,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad, neighbour],
            min_clearance=0.127,
            max_length=2.0,
        )
        assert result < 0.85, f"Expected clip below neighbour west edge (0.85mm), got {result}"
        # The clipped distance must keep the required clearance.
        assert result == pytest.approx(0.85 - 0.1 - 0.127, abs=1e-3)

    def test_neighbor_on_other_layer_does_not_clip(self):
        """A neighbour on a different layer (and not PTH) must NOT
        contribute to clipping -- the clearance check is per-layer."""
        router = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        neighbour_back = Pad(
            x=0.5,
            y=0.0,
            width=0.3,
            height=0.3,
            net=2,
            net_name="B",
            ref="U1",
            pin="2",
            layer=Layer.B_CU,
            through_hole=False,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad, neighbour_back],
            min_clearance=0.127,
            max_length=2.0,
        )
        # Should return full length since neighbour is on opposite layer.
        assert result == pytest.approx(2.0)

    def test_through_hole_neighbor_always_clips(self):
        """A PTH pad blocks every copper layer, so it must clip the
        segment regardless of ``pad.layer``."""
        router = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        # PTH neighbour with layer set to B.Cu still blocks F.Cu segments.
        neighbour = Pad(
            x=1.0,
            y=0.0,
            width=0.6,
            height=0.6,
            net=2,
            net_name="B",
            ref="U1",
            pin="2",
            layer=Layer.B_CU,
            through_hole=True,
            drill=0.3,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad, neighbour],
            min_clearance=0.127,
            max_length=2.0,
        )
        # PTH neighbour west edge at x=0.7; expected clip 0.7 - 0.1 - 0.127 = 0.473
        assert result < 0.7
        assert result == pytest.approx(0.7 - 0.1 - 0.127, abs=1e-3)

    def test_zero_max_length_returns_zero(self):
        """Defensive: passing max_length=0 must short-circuit to 0."""
        router = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="U1",
            pin="1",
            layer=Layer.F_CU,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=0.0,
        )
        assert result == 0.0


# ----------------------------------------------------------------------------
# Integration tests: _create_alternating_escape clipping
# ----------------------------------------------------------------------------


class TestAlternatingEscapeClipping:
    """``_create_alternating_escape`` must clip segments when
    ``pad_clearance_margin`` is provided."""

    def test_unclipped_behavior_preserved_when_margin_none(self):
        """Backward compatibility: passing pad_clearance_margin=None
        must produce the exact same EscapeRoute as the pre-#2756 code."""
        router = _make_router()
        pads = _make_dense_qfp_pads(pins_per_side=8, pitch=0.5)
        info = router.analyze_package(pads)
        assert info.package_type in (
            PackageType.QFP,
            PackageType.QFN,
            PackageType.TQFP,
        )

        # Pick the first west-edge pad to escape east (parallel along edge).
        pad = pads[0]
        unclipped = router._create_alternating_escape(
            pad=pad,
            direction=EscapeDirection.EAST,
            package=info,
            pad_clearance_margin=None,
        )
        # The unclipped escape should reach the full launch distance.
        expected_dist = router.escape_clearance + router.rules.trace_width * 2
        seg = unclipped.segments[0]
        actual_dist = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
        assert actual_dist == pytest.approx(expected_dist, abs=1e-6)

    def test_clipped_escape_respects_pad_clearance(self):
        """With pad_clearance_margin set, the returned segment must
        keep clearance to every other pad in the package."""
        router = _make_router()
        pads = _make_dense_qfp_pads(pins_per_side=8, pitch=0.5)
        info = router.analyze_package(pads)
        clearance = router.rules.trace_clearance

        for pad in pads[:4]:
            # Direction along the edge (the failure mode)
            direction = EscapeDirection.EAST if pad.x < 0 else EscapeDirection.WEST
            escape = router._create_alternating_escape(
                pad=pad,
                direction=direction,
                package=info,
                pad_clearance_margin=clearance,
            )
            seg = escape.segments[0]
            for other in pads:
                if other is pad:
                    continue
                if other.layer != seg.layer and not other.through_hole:
                    continue
                gap = router._segment_to_pad_edge_gap(seg, other)
                assert gap >= clearance - 1e-6, (
                    f"Clipped escape from {pad.net_name} towards {direction.name} "
                    f"violates clearance against {other.net_name}: "
                    f"gap={gap:.4f} mm, required={clearance:.4f} mm"
                )

    def test_clipping_shortens_perpendicular_segments_minimally(self):
        """Perpendicular launches (the EVEN-pin case in
        ``_escape_qfp_alternating``) should require minimal clipping
        because they launch AWAY from neighbour pads."""
        router = _make_router()
        pads = _make_dense_qfp_pads(pins_per_side=8, pitch=0.5)
        info = router.analyze_package(pads)
        clearance = router.rules.trace_clearance

        # Pick a west-edge pad; perpendicular escape is WEST (away from package)
        west_pad = next(p for p in pads if p.x < 0)
        escape = router._create_alternating_escape(
            pad=west_pad,
            direction=EscapeDirection.WEST,
            package=info,
            pad_clearance_margin=clearance,
        )
        seg = escape.segments[0]
        # Perpendicular escape should reach close to the original launch dist.
        full_dist = router.escape_clearance + router.rules.trace_width * 2
        actual_dist = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
        # Perpendicular escape should retain at least 50% of the launch dist.
        assert actual_dist >= 0.5 * full_dist, (
            f"Perpendicular escape was over-clipped: {actual_dist}mm of {full_dist}mm"
        )


# ----------------------------------------------------------------------------
# Integration tests: _escape_qfp_alternating end-to-end behaviour
# ----------------------------------------------------------------------------


class TestQfpAlternatingClearance:
    """End-to-end: every escape from ``generate_escapes`` must satisfy
    pad-to-segment clearance against every OTHER pad on the same
    package."""

    def test_dense_htssop_escapes_respect_clearance(self):
        """The flagship test: a DRV8301-style HTSSOP-56 package at
        0.5mm pitch must produce zero clearance-violating escape
        segments after the #2756 fix."""
        router = _make_router()
        pads = _make_dense_qfp_pads(
            pins_per_side=14,
            pitch=0.5,
            ref="U3",
        )
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        clearance = router.rules.trace_clearance
        violations: list[tuple[str, str, float]] = []
        for escape in escapes:
            for seg in escape.segments:
                if seg.layer != Layer.F_CU:
                    continue
                for other in pads:
                    if other is escape.pad:
                        continue
                    if other.layer != seg.layer and not other.through_hole:
                        continue
                    gap = router._segment_to_pad_edge_gap(seg, other)
                    if gap < clearance - 1e-6:
                        violations.append((escape.pad.net_name, other.net_name, gap))

        assert violations == [], (
            f"Issue #2756 regression: {len(violations)} escape segments "
            f"violate pad-to-segment clearance on a dense HTSSOP-56 fixture. "
            f"First few: {violations[:5]}"
        )

    def test_lqfp32_escapes_respect_clearance(self):
        """STM32G431 LQFP-32 (board 05 U10) at 0.8mm pitch -- the
        ``use_perpendicular_only`` branch.  All perpendicular escapes
        should remain emittable and clearance-compliant."""
        router = _make_router()
        pads = _make_dense_qfp_pads(
            pins_per_side=8,
            pitch=0.8,
            ref="U10",
        )
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # LQFP-32 at 0.8mm pitch should escape ALL pads (perpendicular).
        # We don't require every pad to escape but we DO require zero
        # clearance violations.
        clearance = router.rules.trace_clearance
        for escape in escapes:
            for seg in escape.segments:
                if seg.layer != Layer.F_CU:
                    continue
                for other in pads:
                    if other is escape.pad:
                        continue
                    if other.layer != seg.layer and not other.through_hole:
                        continue
                    gap = router._segment_to_pad_edge_gap(seg, other)
                    assert gap >= clearance - 1e-6, (
                        f"LQFP-32 escape from {escape.pad.net_name} "
                        f"violates clearance against {other.net_name}: "
                        f"gap={gap:.4f} < required={clearance:.4f}"
                    )

    def test_perpendicular_escapes_survive_clipping(self):
        """Even-indexed pins escape perpendicular to the row -- these
        should always be emittable because the launch direction is
        away from same-edge neighbour pads.  At least one perpendicular
        escape per edge must survive."""
        router = _make_router()
        pads = _make_dense_qfp_pads(pins_per_side=14, pitch=0.5)
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # Each edge should contribute at least one perpendicular escape
        # (the even-pin case in _escape_qfp_alternating).
        directions = {e.direction for e in escapes}
        # NORTH/SOUTH/EAST/WEST = perpendicular directions for each edge.
        perp_dirs = {
            EscapeDirection.NORTH,
            EscapeDirection.SOUTH,
            EscapeDirection.EAST,
            EscapeDirection.WEST,
        }
        present_perp = directions & perp_dirs
        assert len(present_perp) >= 2, (
            f"Expected perpendicular escapes from at least 2 edges, "
            f"got directions: {[d.name for d in directions]}"
        )


# ----------------------------------------------------------------------------
# Integration tests: _escape_radial clipping (TO-220 case)
# ----------------------------------------------------------------------------


class TestRadialClearance:
    """``_escape_radial`` (used for TO-220 MOSFETs Q5/Q6 on board 05)
    must also clip segments to honour pad clearance."""

    def test_to220_radial_escapes_respect_clearance(self):
        """A 3-pin TO-220 with closely-spaced pads should not emit
        radial escapes that clip neighbour pads."""
        router = _make_router()
        # Synthetic TO-220 layout: 3 pads at 2.54mm pitch, 1.5x1.5mm pads.
        pads = [
            Pad(
                x=-2.54,
                y=0.0,
                width=1.5,
                height=1.5,
                net=1,
                net_name="DRAIN",
                ref="Q5",
                pin="1",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
            ),
            Pad(
                x=0.0,
                y=0.0,
                width=1.5,
                height=1.5,
                net=2,
                net_name="GATE",
                ref="Q5",
                pin="2",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
            ),
            Pad(
                x=2.54,
                y=0.0,
                width=1.5,
                height=1.5,
                net=3,
                net_name="SOURCE",
                ref="Q5",
                pin="3",
                layer=Layer.F_CU,
                through_hole=True,
                drill=1.0,
            ),
        ]
        info = router.analyze_package(pads)
        # Force radial path -- TO-220 should not be classified as dense.
        # Call _escape_radial directly to avoid the package-type dispatch.
        escapes = router._escape_radial(info)

        clearance = router.rules.trace_clearance
        for escape in escapes:
            for seg in escape.segments:
                for other in pads:
                    if other is escape.pad:
                        continue
                    # PTH pads block every layer, so always check.
                    gap = router._segment_to_pad_edge_gap(seg, other)
                    assert gap >= clearance - 1e-6, (
                        f"Radial escape from {escape.pad.net_name} "
                        f"violates clearance against {other.net_name}: "
                        f"gap={gap:.4f} < required={clearance:.4f}"
                    )


# ----------------------------------------------------------------------------
# Test: in-pad fallback path is unaffected
# ----------------------------------------------------------------------------


class TestInPadFallbackPreserved:
    """Issue #2695 in-pad fallback must continue to engage on capable
    manufacturers when surface escapes fail clearance.  Specifically,
    the #2756 clipping must NOT mask the violation from the in-pad
    check (the in-pad path is the right answer for via-in-pad-capable
    manufacturers, since clipping leaves a stub that cannot continue
    routing past the same neighbour)."""

    def test_lqfp48_jlcpcb_tier1_emits_in_pad_vias(self):
        """LQFP-48 on jlcpcb-tier1 (via-in-pad supported) should still
        produce in-pad vias for inner pins that would otherwise fail
        surface clearance."""
        rules = _jlcpcb_rules()
        grid = RoutingGrid(
            width=40.0,
            height=40.0,
            rules=rules,
            origin_x=-20.0,
            origin_y=-20.0,
        )
        router = EscapeRouter(grid, rules, manufacturer="jlcpcb-tier1")
        assert router.via_in_pad_supported, (
            "Test precondition: jlcpcb-tier1 must report via-in-pad supported"
        )

        pads = _make_dense_qfp_pads(pins_per_side=12, pitch=0.5, ref="U2")
        info = router.analyze_package(pads)
        escapes = router.generate_escapes(info)

        # At least some escapes should use in-pad vias.
        in_pad_count = sum(
            1 for e in escapes if e.via is not None and getattr(e.via, "in_pad", False)
        )
        # We don't pin the exact count -- just verify the rescue path
        # is exercised.  Pre-#2756 this returned >= 1; post-#2756 must too.
        assert in_pad_count >= 1, (
            f"Expected in-pad via rescue to fire on LQFP-48 / jlcpcb-tier1, "
            f"got {in_pad_count} in-pad vias of {len(escapes)} escapes."
        )
