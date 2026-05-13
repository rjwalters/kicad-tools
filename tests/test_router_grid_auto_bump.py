"""Tests for pad-position-aware grid auto-bump (issue #2837).

This module validates the multi-resolution grid plan's ability to handle
off-grid pad clusters that the uniform-grid selector cannot resolve due
to memory caps.  Board 03 (USB joystick) was the motivating case:

- Board is 80x60mm, so 0.01mm uniform grid would need 48M cells (way over
  the 500k cap in ``auto_select_grid_resolution``).
- The memory cap forces 0.1mm uniform, which leaves 36/87 pads off-grid
  (J1 USB-C on half-grid, U1 TQFP on half-grid, Y1 crystal at sub-0.05mm
  offsets, several passives at 0.05mm half-grid).
- The pre-#2837 plan only added fine zones for J1 (USB-C) and U1 (TQFP)
  because Y1's 4.88mm pitch fell above the 0.8mm ``fine_pitch_threshold``
  AND the off-grid escalation threshold of 50% never fired (41.4% < 50%).

This test file verifies the structural fix:

1. Synthetic 80x60mm board with a TQFP-32 at half-grid offsets, an HC49-U
   crystal at 2.44mm half-pitch, and a few on-grid resistors -- the plan
   includes fine zones with appropriate (resolution, offset) pairs.
2. On-grid-only board -- no fine zones, no spurious bumps.
3. Telemetry is emitted when escalation fires.

Issue #2837 (subsumes #2387's half-completed work).
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.router.io import (
    FineZone,
    _compute_zone_resolution_and_offset,
    _count_off_grid_with_offset,
    _is_on_grid_with_offset,
    auto_select_grid_resolution,
    compute_multi_resolution_plan,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pad(
    x: float,
    y: float,
    ref: str,
    pin: str,
    net: int = 1,
    width: float = 0.3,
    height: float = 0.6,
    through_hole: bool = False,
) -> Pad:
    """Construct a Pad with the bare minimum for grid analysis."""
    return Pad(
        x=x,
        y=y,
        width=width,
        height=height,
        net=net,
        net_name=f"N{net}",
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
        through_hole=through_hole,
    )


def _tqfp32_pads(
    cx: float,
    cy: float,
    pitch: float = 0.8,
    ref: str = "U1",
) -> list[Pad]:
    """Generate 32 pads in a QFP-32 ring (8 pads per side).

    Pads are placed symmetrically around (cx, cy) so the corner pad
    offsets from cx/cy are at +/- 3.5*pitch = +/-2.8mm (for 0.8mm pitch).
    """
    pads: list[Pad] = []
    n_per_side = 8
    half = (n_per_side - 1) * pitch / 2.0
    # Body half-width / half-height: place pads ~3mm out from cx/cy along
    # the perpendicular axis.
    body_half = 3.0

    for i in range(n_per_side):
        # Left side: x = cx - body_half, y varies
        y = cy - half + i * pitch
        pads.append(_make_pad(cx - body_half, y, ref, f"{1 + i}"))
    for i in range(n_per_side):
        # Bottom side: y = cy + body_half, x varies
        x = cx - half + i * pitch
        pads.append(_make_pad(x, cy + body_half, ref, f"{9 + i}"))
    for i in range(n_per_side):
        # Right side: x = cx + body_half
        y = cy + half - i * pitch
        pads.append(_make_pad(cx + body_half, y, ref, f"{17 + i}"))
    for i in range(n_per_side):
        # Top side: y = cy - body_half
        x = cx + half - i * pitch
        pads.append(_make_pad(x, cy - body_half, ref, f"{25 + i}"))
    return pads


def _hc49_crystal_pads(
    cx: float,
    cy: float,
    pitch: float = 4.88,
    ref: str = "Y1",
) -> list[Pad]:
    """Generate two crystal pads at +/- pitch/2 from cx along x."""
    return [
        _make_pad(cx - pitch / 2.0, cy, ref, "1",
                  width=1.4, height=1.4, through_hole=True),
        _make_pad(cx + pitch / 2.0, cy, ref, "2",
                  width=1.4, height=1.4, through_hole=True),
    ]


def _on_grid_resistor_pads(
    cx: float,
    cy: float,
    pitch: float = 1.0,
    ref: str = "R1",
) -> list[Pad]:
    """Generate 0805-style resistor with two pads at +/- pitch/2."""
    return [
        _make_pad(cx - pitch / 2.0, cy, ref, "1", width=1.0, height=1.25),
        _make_pad(cx + pitch / 2.0, cy, ref, "2", width=1.0, height=1.25),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeZoneResolutionAndOffset:
    """Tests for the per-component (resolution, offset) solver."""

    def test_on_grid_component_picks_coarsest_finer_than_coarse(self):
        """A component whose pads are on a 0.05mm grid prefers 0.05mm."""
        # 4 pads at x = 10, 11, 12, 13; y = 5 (all multiples of 0.05)
        pads = [_make_pad(10.0 + i, 5.0, "R1", str(i + 1)) for i in range(4)]
        res, x_off, y_off = _compute_zone_resolution_and_offset(
            pads, coarse_resolution=0.1, min_fine_resolution=0.005,
        )
        # The coarsest fine grid finer than 0.1mm that aligns is 0.05mm.
        assert res == 0.05
        assert (x_off, y_off) == (0.0, 0.0)

    def test_half_grid_pads_use_offset(self):
        """Pads at half-grid use 0.05mm with no offset (50.025mm % 0.05 = 0.025)."""
        # USB-C-style pads at x = 137.250 (10x exact for 0.05mm: 2745.0)
        pads = [
            _make_pad(137.250, 100.0, "J1", "1"),
            _make_pad(137.750, 100.0, "J1", "2"),
            _make_pad(138.250, 100.0, "J1", "3"),
        ]
        res, x_off, y_off = _compute_zone_resolution_and_offset(
            pads, coarse_resolution=0.1, min_fine_resolution=0.005,
        )
        # 137.250 % 0.05 = 0 (250.5 = exact 250 in float terms within
        # threshold). 0.05 should be chosen at offset (0, 0).
        assert res == 0.05
        # All pads must be on the chosen (res, offset) grid:
        for p in pads:
            assert _is_on_grid_with_offset(p.x, res, x_off)
            assert _is_on_grid_with_offset(p.y, res, y_off)

    def test_crystal_at_sub_005mm_offsets(self):
        """HC49-U crystal at x=152.560, 157.440 (delta 4.88) needs finer than 0.05mm."""
        pads = _hc49_crystal_pads(155.0, 100.0)
        # Reset x positions to the literal trace from board 03:
        pads[0].x = 152.560
        pads[1].x = 157.440
        res, x_off, y_off = _compute_zone_resolution_and_offset(
            pads, coarse_resolution=0.1, min_fine_resolution=0.005,
        )
        # All pads must land on the chosen grid+offset.
        off_count = _count_off_grid_with_offset(pads, res, x_off, y_off)
        assert off_count == 0, (
            f"Expected 0 off-grid, got {off_count} at res={res}, offset=({x_off},{y_off})"
        )
        # Resolution should be strictly finer than the coarse grid.
        assert res < 0.1

    def test_floors_at_min_fine_resolution(self):
        """No candidate below ``min_fine_resolution`` is returned."""
        pads = [
            _make_pad(152.560, 100.0, "Y1", "1"),
            _make_pad(157.440, 100.0, "Y1", "2"),
        ]
        # With a generous floor (0.05mm), the solver may fall back to a
        # best-effort resolution rather than 0.005mm.  Verify the result
        # is at or above the floor.
        res, _, _ = _compute_zone_resolution_and_offset(
            pads, coarse_resolution=0.1, min_fine_resolution=0.05,
        )
        # If 0.05mm can't cover both pads, the solver still returns >= 0.05
        # (the lower-bound candidate filter).
        assert res >= 0.05

    def test_empty_component_returns_floor(self):
        """An empty pad list returns the min_fine_resolution and zero offsets."""
        res, x, y = _compute_zone_resolution_and_offset(
            [], coarse_resolution=0.1, min_fine_resolution=0.025,
        )
        assert (res, x, y) == (0.025, 0.0, 0.0)


class TestSyntheticOffGridBoard:
    """Synthetic 80x60mm board mirroring the board 03 problem.

    The fixture mixes pads with INCOMPATIBLE origin offsets so the global
    origin-offset optimiser in ``auto_select_grid_resolution`` cannot
    absorb both -- forcing the off-grid escalation branch to fire.

    - Many on-grid resistors anchor the global offset to (0, 0).
    - A TQFP-32 is placed at half-grid offsets (corner pad at 17.225,
      17.225), so it is off-grid against any (0, 0)-origin 0.1mm grid.
    - An HC49-U crystal has sub-0.05mm pad spacings (pitch 4.88mm with
      the centre placed at 50.0, giving pads at 47.560 and 52.440), so
      it is off-grid against ANY 0.05mm uniform grid regardless of offset.
    """

    @pytest.fixture
    def synthetic_pads(self) -> list[Pad]:
        pads: list[Pad] = []
        # TQFP-32 at half-grid offset.  Corner pad at (17.225, 17.225)
        # which is 0.05mm-aligned but NOT 0.1mm-aligned at any single
        # offset that also aligns the resistor pads.
        pads.extend(_tqfp32_pads(cx=20.025, cy=20.025, pitch=0.8, ref="U1"))
        # HC49-U crystal at sub-0.05mm half-pitch
        pads.extend(_hc49_crystal_pads(cx=50.0, cy=30.0, pitch=4.88, ref="Y1"))
        # Plenty of on-grid resistors (anchor the global offset to 0).
        for i in range(8):
            pads.extend(_on_grid_resistor_pads(
                cx=5.0 + i * 5.0, cy=50.0, pitch=1.0, ref=f"R{i + 1}",
            ))
            pads.extend(_on_grid_resistor_pads(
                cx=5.0 + i * 5.0, cy=55.0, pitch=1.0, ref=f"R{i + 10}",
            ))
        return pads

    def test_uniform_grid_leaves_offgrid_pads(self, synthetic_pads):
        """At 0.1mm memory-capped, the synthetic board has off-grid pads."""
        result = auto_select_grid_resolution(
            synthetic_pads,
            clearance=0.15,
            board_width=80.0,
            board_height=60.0,
        )
        # The selector picks the coarsest grid that minimises off-grid count.
        # U1 + Y1 should leave a meaningful off-grid residue at any 0.1mm
        # grid (32 + 2 = 34 pads off the 0.1mm grid).
        assert result.off_grid_pads > 0
        assert result.resolution >= 0.05

    def test_multi_resolution_plan_covers_offgrid_components(
        self, synthetic_pads,
    ):
        """The plan must include fine zones for U1 AND Y1 with valid (R, O)."""
        plan = compute_multi_resolution_plan(
            pads=synthetic_pads,
            clearance=0.15,
            board_width=80.0,
            board_height=60.0,
        )
        assert plan is not None
        assert plan.is_multi_resolution

        zones_by_ref = {z.ref: z for z in plan.fine_zones}
        # Both off-grid components must have fine zones.
        assert "U1" in zones_by_ref, (
            f"U1 missing from fine zones: {sorted(zones_by_ref.keys())}"
        )
        assert "Y1" in zones_by_ref, (
            f"Y1 missing from fine zones: {sorted(zones_by_ref.keys())}"
        )

        # Each zone's (resolution, offset) must place ALL of that
        # component's pads on-grid.
        for ref in ("U1", "Y1"):
            zone = zones_by_ref[ref]
            comp_pads = [p for p in synthetic_pads if p.ref == ref]
            off = _count_off_grid_with_offset(
                comp_pads, zone.resolution, zone.x_offset, zone.y_offset,
            )
            assert off == 0, (
                f"{ref} fine zone @ {zone.resolution}mm offset="
                f"({zone.x_offset},{zone.y_offset}) leaves {off}/{len(comp_pads)} "
                "off-grid"
            )

    def test_plan_within_cell_budget(self, synthetic_pads):
        """Total cell estimate stays under the 2M default budget."""
        plan = compute_multi_resolution_plan(
            pads=synthetic_pads,
            clearance=0.15,
            board_width=80.0,
            board_height=60.0,
            max_cells=2_000_000,
        )
        assert plan is not None
        assert plan.total_cell_estimate < 2_000_000


class TestOnGridBoardNoSpuriousBump:
    """Regression guard: an already-aligned board must not get fine zones."""

    def test_on_grid_resistors_only_produces_no_plan(self):
        """50x50mm board with on-grid 0805 resistors -> plan is None."""
        pads: list[Pad] = []
        for i in range(8):
            pads.extend(_on_grid_resistor_pads(
                cx=10.0 + i * 5.0, cy=10.0, pitch=1.0, ref=f"R{i + 1}",
            ))
            pads.extend(_on_grid_resistor_pads(
                cx=10.0 + i * 5.0, cy=20.0, pitch=1.0, ref=f"R{i + 10}",
            ))

        # Sanity: the selector finds a perfect grid.
        u = auto_select_grid_resolution(
            pads, clearance=0.15, board_width=50.0, board_height=50.0,
        )
        assert u.off_grid_pads == 0

        plan = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=50.0,
            board_height=50.0,
        )
        assert plan is None, (
            f"Expected None (uniform is optimal) but got plan with "
            f"{len(plan.fine_zones) if plan else 0} fine zones"
        )


class TestEscalationTelemetry:
    """Telemetry / logging contract."""

    def test_escalation_logs_off_grid_summary(self, synthetic_pads_factory, caplog):
        """When escalation fires, an INFO-level log entry is emitted."""
        # synthetic board with off-grid pads
        pads = synthetic_pads_factory()
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
            plan = compute_multi_resolution_plan(
                pads=pads,
                clearance=0.15,
                board_width=80.0,
                board_height=60.0,
            )
        assert plan is not None and plan.is_multi_resolution

        # Find the escalation log entry
        matching = [
            r for r in caplog.records
            if "Off-grid escalation" in r.getMessage()
        ]
        assert matching, (
            "Expected an 'Off-grid escalation' log entry but found none. "
            f"Records: {[r.getMessage() for r in caplog.records]}"
        )


@pytest.fixture
def synthetic_pads_factory():
    """Reusable factory for the synthetic 80x60mm board fixture."""

    def _factory() -> list[Pad]:
        pads: list[Pad] = []
        pads.extend(_tqfp32_pads(cx=20.025, cy=20.025, pitch=0.8, ref="U1"))
        pads.extend(_hc49_crystal_pads(cx=50.0, cy=30.0, pitch=4.88, ref="Y1"))
        for i in range(8):
            pads.extend(_on_grid_resistor_pads(
                cx=5.0 + i * 5.0, cy=50.0, pitch=1.0, ref=f"R{i + 1}",
            ))
            pads.extend(_on_grid_resistor_pads(
                cx=5.0 + i * 5.0, cy=55.0, pitch=1.0, ref=f"R{i + 10}",
            ))
        return pads

    return _factory


class TestFineZoneOffsetFields:
    """FineZone backwards-compatibility for the new offset fields."""

    def test_offsets_default_to_zero(self):
        """Old constructor signature (no offsets) still works."""
        zone = FineZone(
            ref="U1", x_min=0.0, y_min=0.0,
            x_max=10.0, y_max=10.0, resolution=0.05,
        )
        assert zone.x_offset == 0.0
        assert zone.y_offset == 0.0

    def test_offsets_accepted_explicitly(self):
        """New constructor with explicit offsets stores them."""
        zone = FineZone(
            ref="U1", x_min=0.0, y_min=0.0,
            x_max=10.0, y_max=10.0, resolution=0.05,
            x_offset=0.025, y_offset=0.025,
        )
        assert zone.x_offset == 0.025
        assert zone.y_offset == 0.025


class TestSubGridUsesZoneOffset:
    """SubGridRouter must anchor fine-grid candidates to the zone offset."""

    def test_fine_grid_candidates_align_to_offset(self):
        """When a zone carries (x_offset, y_offset), candidate points
        fall on ``x_offset + k * fine_resolution`` (and similarly for y).
        """
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.rules import DesignRules
        from kicad_tools.router.subgrid import SubGridRouter

        rules = DesignRules(
            grid_resolution=0.1, trace_width=0.15, trace_clearance=0.1,
        )
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules)

        # Place an off-grid pad at (10.025, 10.025)
        pad = _make_pad(10.025, 10.025, "U1", "1", net=1)
        grid.add_pad(pad)

        # Fine zone covering the pad with explicit offset (0.025, 0.025)
        zone = FineZone(
            ref="U1", x_min=9.0, y_min=9.0,
            x_max=11.0, y_max=11.0,
            resolution=0.05,
            x_offset=0.025, y_offset=0.025,
        )

        router = SubGridRouter(grid, rules, fine_zones=[zone])

        # Sanity: zone lookup works
        assert router._get_pad_fine_zone(pad) is zone
        assert router._get_pad_fine_resolution(pad) == 0.05

        # Walk an analysis: ensure the fine-grid candidate generation
        # actually uses the zone's offset.  We don't need the full router
        # execution -- we just need to verify the anchor logic produces
        # at least one candidate whose snap point falls on
        # ``x_offset + k * fine_resolution``.
        analysis = router.analyze_pads([pad])
        assert analysis.has_off_grid_pads
        sgp = analysis.off_grid_pads[0]

        candidates = router._generate_fine_grid_candidates(sgp, 0.05)
        assert candidates, "expected at least one fine-grid candidate"

        # Every candidate's (snap_x, snap_y) must satisfy the zone offset.
        threshold = 0.05 / 10  # _is_on_grid default threshold
        for _, _, _, snap_x, snap_y in candidates:
            sx_residue = (snap_x - zone.x_offset) % 0.05
            sx_residue = min(sx_residue, 0.05 - sx_residue)
            sy_residue = (snap_y - zone.y_offset) % 0.05
            sy_residue = min(sy_residue, 0.05 - sy_residue)
            assert sx_residue <= threshold, (
                f"snap_x={snap_x} not on offset grid (residue={sx_residue})"
            )
            assert sy_residue <= threshold, (
                f"snap_y={snap_y} not on offset grid (residue={sy_residue})"
            )
