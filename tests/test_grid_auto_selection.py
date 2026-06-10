"""Tests for automatic grid resolution selection."""

import logging
import warnings

import pytest

from kicad_tools.router.io import (
    GridAutoSelection,
    PadPosition,
    _compute_gcd_grid_candidates,
    _count_off_grid_with_offset,
    _find_optimal_origin_offset,
    _is_on_grid,
    _is_on_grid_with_offset,
    auto_select_grid_resolution,
    extract_board_dimensions,
    extract_pad_positions,
    recommend_grid_for_board_size,
)
from kicad_tools.router.primitives import Pad


class TestIsOnGrid:
    """Tests for _is_on_grid helper function."""

    def test_on_grid_exact(self):
        """Test value exactly on grid."""
        assert _is_on_grid(0.5, 0.25)
        assert _is_on_grid(1.0, 0.25)
        assert _is_on_grid(2.54, 0.127)

    def test_on_grid_within_threshold(self):
        """Test value within default threshold (resolution/10)."""
        # 0.25mm grid, threshold is 0.025mm
        assert _is_on_grid(0.51, 0.25)  # 0.01 from 0.5
        assert _is_on_grid(0.49, 0.25)  # 0.01 from 0.5

    def test_off_grid(self):
        """Test value clearly off grid."""
        assert not _is_on_grid(0.33, 0.25)  # 0.08 from 0.25
        assert not _is_on_grid(0.15, 0.25)  # 0.10 from 0.0 or 0.25

    def test_custom_threshold(self):
        """Test with custom threshold."""
        # Strict threshold
        assert not _is_on_grid(0.51, 0.25, threshold=0.005)
        # Relaxed threshold
        assert _is_on_grid(0.35, 0.25, threshold=0.15)

    def test_zero_value(self):
        """Test zero is always on grid."""
        assert _is_on_grid(0.0, 0.25)
        assert _is_on_grid(0.0, 0.1)


class TestPadPosition:
    """Tests for PadPosition dataclass."""

    def test_creation(self):
        """Test creating a pad position."""
        pos = PadPosition(x=2.54, y=5.08)
        assert pos.x == 2.54
        assert pos.y == 5.08


class TestAutoSelectGridResolution:
    """Tests for auto_select_grid_resolution function."""

    def test_empty_pads(self):
        """Test with no pads."""
        result = auto_select_grid_resolution([], clearance=0.15)
        assert result.total_pads == 0
        assert result.off_grid_pads == 0

    def test_pads_on_standard_grid(self):
        """Test pads on 2.54mm (100mil) grid prefer coarser resolution."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=2.54),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 2.54mm is divisible by 0.127mm (5mil), so should prefer that or coarser
        assert result.off_grid_pads == 0 or result.resolution >= 0.127

    def test_pads_on_metric_grid(self):
        """Test pads on 1mm metric grid."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.0, y=0.0),
            PadPosition(x=2.0, y=1.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 1mm is divisible by 0.25, 0.1, 0.05
        assert result.off_grid_pads == 0

    def test_pads_on_fine_grid(self):
        """Test pads requiring fine grid (0.5mm pitch)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.5, y=0.0),
            PadPosition(x=1.0, y=0.5),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.off_grid_pads == 0
        assert result.resolution <= 0.25

    def test_mixed_grid_pads(self):
        """Test pads on mixed grid (some off-grid)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),  # 100mil
            PadPosition(x=0.33, y=0.0),  # Off-grid for most resolutions
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        # Should select resolution that minimizes off-grid count
        assert result.total_pads == 3
        assert result.candidates_tried  # Should have tried multiple candidates

    def test_drc_compliance(self):
        """Test that selected resolution respects DRC clearance.

        After issue #2387 the candidate filter is ``c <= clearance`` (not
        ``c <= clearance / 2``).  The negotiated router enforces
        edge-to-edge clearance directly, so coarser pad-aligned grids are
        preferred over half-clearance grids that misalign with pad pitch.
        """
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # Resolution must be <= clearance for DRC compliance (issue #2387)
        assert result.resolution <= 0.15

    def test_prefers_coarser_when_equal(self):
        """Test that coarser resolution is preferred when off-grid counts are equal."""
        # All pads on a grid that works for multiple resolutions
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.0, y=0.0),
            PadPosition(x=2.0, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.5)
        # Should prefer coarser resolution (0.5mm or 0.25mm over 0.1mm)
        assert result.resolution >= 0.25

    def test_with_pad_objects(self):
        """Test with full Pad objects instead of PadPosition."""
        pads = [
            Pad(x=0.0, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
            Pad(x=2.54, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.total_pads == 2

    def test_with_pad_dict(self):
        """Test with dict of pads (as returned by router)."""
        pads = {
            ("U1", "1"): Pad(x=0.0, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=2.54, y=0.0, width=1.0, height=1.0, net=1, net_name="NET1"),
        }
        result = auto_select_grid_resolution(pads, clearance=0.2)
        assert result.total_pads == 2

    def test_custom_candidates(self):
        """Test with custom candidate resolutions."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(
            pads,
            clearance=0.2,
            candidates=[0.2, 0.15, 0.1],
        )
        # Should only try specified candidates
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.5 not in resolutions_tried  # Default candidate not tried

    def test_tssop_pitch_alignment_with_default_candidates(self):
        """Test that default candidates include TSSOP-friendly 0.065mm."""
        # TSSOP pitch is 0.65mm, which divides evenly by 0.065mm
        # Use clearance=0.15 so that 0.065 <= 0.15/2 = 0.075 passes the filter
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),  # TSSOP pitch
            PadPosition(x=1.30, y=0.0),  # 2x TSSOP pitch
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # Should include 0.065mm in candidates tried
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.065 in resolutions_tried

    def test_selects_0065_for_tssop_pads(self):
        """Test that 0.065mm is selected for pure TSSOP placement."""
        # All pads on 0.65mm grid
        # Use clearance=0.15 so that 0.065 <= 0.075 passes the filter
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # 0.065mm should have zero off-grid pads (0.65 / 0.065 = 10 exact)
        # So should 0.05mm (0.65 / 0.05 = 13 exact)
        # Function prefers coarser when equal, so 0.065mm should be selected
        assert result.off_grid_pads == 0
        assert result.resolution in [0.065, 0.05]  # Either is valid

    def test_no_candidate_exceeds_clearance(self):
        """With clearance=0.15, no selected candidate should exceed 0.15.

        After issue #2387 the candidate filter is ``c <= clearance``
        instead of ``c <= clearance / 2`` so pad-aligned grids like 0.1mm
        survive at clearance=0.15mm.
        """
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=1.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.resolution <= 0.15
        # Also verify all *tried* candidates respect the (relaxed) threshold
        for res, _off in result.candidates_tried:
            assert res <= 0.15

    def test_tight_clearance_floor(self):
        """With very tight clearance (0.1mm), grid stays <= 0.1mm.

        After issue #2387 the filter is ``c <= clearance``.  With
        clearance=0.1mm, the 0.1mm candidate now survives the DRC filter
        and is preferred over 0.05mm by the coarser-when-equal rule.
        """
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.1)
        # All surviving candidates must be <= 0.1mm
        assert result.resolution <= 0.1
        # Coarser-when-equal preference should pick 0.1mm (0/1 off-grid)
        assert result.resolution == 0.1

    def test_board05_clearance_selects_fine_grid(self):
        """Board 05 scenario: clearance=0.2mm should select grid <= 0.2mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.27, y=0.0),
            PadPosition(x=2.54, y=1.27),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.2)
        # After issue #2387, candidate filter is c <= clearance (not /2).
        assert result.resolution <= 0.2

    def test_imperial_tht_pads_zero_off_grid_with_loose_clearance(self):
        """Imperial THT pads (2.54mm, 5.08mm) should have zero off-grid with loose clearance.

        When clearance allows 0.127mm grid (clearance >= 0.254mm), the auto-selector
        should pick 0.127mm which divides evenly into 2.54mm and 5.08mm.
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            PadPosition(x=0.0, y=2.54),
            PadPosition(x=2.54, y=2.54),
            PadPosition(x=5.08, y=2.54),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        assert result.off_grid_pads == 0, (
            f"Imperial THT pads should have zero off-grid pads, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_imperial_tht_pads_with_tight_clearance(self):
        """Imperial THT pads with tight clearance (0.127mm) should use 0.0508mm grid.

        When clearance is 0.127mm (JLCPCB), max_grid is 0.0635mm.
        The 0.0508mm (2 mil) candidate divides evenly into 2.54mm (50x).
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.127)
        # 0.0508mm divides evenly into 2.54mm and 5.08mm
        assert result.off_grid_pads == 0, (
            f"Imperial THT pads should have zero off-grid pads even with tight clearance, "
            f"got {result.off_grid_pads} with grid {result.resolution}mm"
        )

    def test_mixed_imperial_metric_pads(self):
        """Mixed imperial THT + metric SMD pads should minimise off-grid count."""
        pads = [
            # Imperial THT pads at 2.54mm pitch
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            # Metric SMD pads at 0.65mm pitch (TSSOP)
            PadPosition(x=10.0, y=0.0),
            PadPosition(x=10.65, y=0.0),
            PadPosition(x=11.30, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # No single grid aligns with both; auto-selector picks the one
        # that minimises off-grid count
        assert result.total_pads == 6
        assert result.off_grid_pads < result.total_pads, (
            "Should have fewer off-grid pads than total"
        )

    def test_0508mm_candidate_included(self):
        """Default candidates should include 0.0508mm (2 mil) for imperial compatibility."""
        pads = [PadPosition(x=0.0, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.127)
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.0508 in resolutions_tried, (
            f"0.0508mm should be in candidates, got {resolutions_tried}"
        )


class TestMemoryCapping:
    """Tests for memory budget capping in auto_select_grid_resolution."""

    def test_memory_capping_with_large_board(self):
        """Fine grids should be filtered when board is large."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        # 65x56mm board with max_cells=500_000
        # 0.005mm grid would be 65/0.005 * 56/0.005 = 13000 * 11200 = 145.6M cells
        # So fine candidates should be filtered out
        result = auto_select_grid_resolution(
            pads, clearance=0.3, board_width=65.0, board_height=56.0
        )
        assert result.memory_capped is True
        assert result.uncapped_resolution is not None
        # The selected resolution should produce cells <= 500K
        cells = (65.0 / result.resolution) * (56.0 / result.resolution)
        assert cells <= 500_000

    def test_no_capping_without_board_dimensions(self):
        """Without board dimensions, memory filter is not applied."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=2.54, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        assert result.memory_capped is False
        assert result.uncapped_resolution is None

    def test_no_capping_when_all_candidates_fit(self):
        """Small board should not trigger capping."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=2.54, y=0.0)]
        # 10x10mm board: even 0.05mm grid = 200*200 = 40K cells (well within budget)
        result = auto_select_grid_resolution(
            pads, clearance=0.3, board_width=10.0, board_height=10.0
        )
        assert result.memory_capped is False
        assert result.uncapped_resolution is None

    def test_capping_boundary_exact_max_cells(self):
        """Grid producing exactly max_cells should pass the filter."""
        pads = [PadPosition(x=0.0, y=0.0)]
        # For max_cells=500_000 and 0.1mm grid: need board area = 500_000 * 0.01 = 5000 mm^2
        # e.g. ~70.7 x 70.7mm board: 70.7/0.1 * 70.7/0.1 = 707*707 = ~500K
        # Use a board where 0.1mm is exactly at boundary
        result = auto_select_grid_resolution(
            pads, clearance=0.3, board_width=70.7, board_height=70.7, max_cells=500_000
        )
        # 0.1mm should still be in candidates (500K cells approximately)
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.1 in resolutions_tried

    def test_summary_shows_capping_info(self):
        """Summary should mention capping when memory_capped is True."""
        result = GridAutoSelection(
            resolution=0.1,
            off_grid_pads=1,
            total_pads=3,
            off_grid_percentage=33.3,
            candidates_tried=[(0.1, 1)],
            memory_capped=True,
            uncapped_resolution=0.005,
        )
        summary = result.summary()
        assert "capped" in summary.lower()
        assert "0.005" in summary

    def test_summary_no_capping_info_when_not_capped(self):
        """Summary should not mention capping when memory_capped is False."""
        result = GridAutoSelection(
            resolution=0.1,
            off_grid_pads=0,
            total_pads=3,
            off_grid_percentage=0.0,
            candidates_tried=[(0.1, 0)],
            memory_capped=False,
            uncapped_resolution=None,
        )
        summary = result.summary()
        assert "capped" not in summary.lower()


class TestClearanceAwareMemoryBump:
    """Tests for issue #3239: clearance-aware memory budget bump.

    When the memory cap forces a grid coarser than ``clearance / 2``, the
    auto-selector should attempt a one-shot budget bump (up to 4M cells)
    so the selected grid stays clearance-safe.  If the bump cannot achieve
    ``<= clearance/2``, an actionable warning naming the memory cap as the
    cause is emitted instead of the generic ``may cause clearance
    violations`` warning.
    """

    def _chorus_like_pads(self) -> list[PadPosition]:
        """Pad layout that exercises the chorus-test-revA selection path.

        A handful of metric pads on a grid coarse enough to align to 0.1mm,
        plus a few on 0.05mm half-grid offsets to keep off-grid analysis
        non-trivial.
        """
        return [
            PadPosition(x=2.0, y=2.0),
            PadPosition(x=4.0, y=2.0),
            PadPosition(x=6.0, y=2.0),
            PadPosition(x=2.0, y=4.0),
            PadPosition(x=4.05, y=4.0),  # 0.05mm half-grid offset
            PadPosition(x=6.0, y=4.05),
        ]

    def test_chorus_like_memory_cap_bumps_to_clearance_safe(self, caplog):
        """Chorus-like board: memory cap forces 0.127mm, bump unlocks
        a clearance-safe grid (<= 0.075mm).

        Acceptance criterion #1: at clearance=0.15mm on a 65x56mm board
        with default max_cells=500_000, the selector should bump and pick
        a grid <= 0.075mm without emitting "may cause clearance violations".
        """
        pads = self._chorus_like_pads()

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = auto_select_grid_resolution(
                    pads,
                    clearance=0.15,
                    board_width=65.0,
                    board_height=56.0,
                    max_cells=500_000,
                )

        # Acceptance criterion #1: selected grid <= clearance/2 = 0.075mm
        assert result.resolution <= 0.075, (
            f"Expected grid <= 0.075mm, got {result.resolution}mm. "
            f"Bump path did not engage or did not pick a safe grid."
        )
        assert result.clearance_compliant_at_clearance_over_2 is True

        # No "may cause clearance violations" warning text
        warning_texts = [str(w.message) for w in caught]
        for text in warning_texts:
            assert "may cause clearance violations" not in text, (
                f"Unexpected clearance-violation warning emitted: {text}"
            )

        # Acceptance criterion #2: bump is logged at INFO level
        bump_logs = [r for r in caplog.records if "bumped memory cap" in r.message.lower()]
        assert bump_logs, (
            f"Expected INFO log mentioning the memory cap bump. "
            f"Got records: {[r.message for r in caplog.records]}"
        )
        # Bump log should reference the new cell count
        assert any(
            str(result.memory_budget_used) in r.message
            or f"{result.memory_budget_used:,}" in r.message
            for r in bump_logs
        ), "Bump log should name the new max_cells value"

        # Effective budget should have been bumped above the default
        assert result.memory_budget_used > 500_000

    def test_loose_clearance_pad_alignment_preserved(self):
        """Issue #2387 regression guard: at clearance=0.30mm, the
        selector still prefers a coarser-but-pad-aligned grid (the
        original #2387 intent) when no memory cap is in play.

        The clearance-aware retry should ONLY engage when memory_capped
        is True -- otherwise the existing pad-alignment-first logic
        wins.  This test asserts that #2387's relaxation survives: at
        loose clearance with pad-aligned candidates available, the
        coarser grid is still picked (no over-aggressive tightening),
        and no memory-cap warning fires because memory_capped is False.
        """
        # Pads aligned to 0.25mm exactly (no fractional offsets)
        pads = [
            PadPosition(x=2.0, y=2.0),
            PadPosition(x=2.5, y=2.0),
            PadPosition(x=3.0, y=2.0),
        ]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = auto_select_grid_resolution(
                pads,
                clearance=0.30,
                board_width=20.0,
                board_height=20.0,
            )

        # The selector picks 0.25mm: pad-aligned and DRC-compliant
        # (<= clearance).  This is intentional #2387 behaviour even
        # though 0.25 > clearance/2 -- the router enforces edge-to-edge
        # clearance directly, so pad-alignment dominates when memory
        # is not binding.
        assert result.memory_capped is False
        # No memory-cap warning -- bump only engages when capped.
        warning_texts = [str(w.message) for w in caught]
        for text in warning_texts:
            assert "memory budget cap" not in text.lower(), (
                f"Unexpected memory-cap warning at loose clearance: {text}"
            )
        # Budget was NOT bumped (default 500_000 retained)
        assert result.memory_budget_used == 500_000

    def test_unsatisfiable_budget_emits_actionable_warning(self):
        """When even the bumped budget (4M cells) cannot satisfy
        clearance/2, the actionable warning naming the memory cap fires.

        Construct a very large board where 0.075mm grid would need
        > 4M cells: area > 4_000_000 * 0.075^2 = 22,500 mm^2, e.g.
        200mm x 200mm.
        """
        pads = [
            PadPosition(x=10.0, y=10.0),
            PadPosition(x=20.0, y=10.0),
        ]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = auto_select_grid_resolution(
                pads,
                clearance=0.15,
                board_width=200.0,
                board_height=200.0,
                max_cells=500_000,
            )

        # The selector should NOT silently pick a clearance-risky grid
        # without naming the cause.
        warning_texts = [str(w.message) for w in caught]
        actionable = [
            t for t in warning_texts if "memory budget cap" in t.lower() and "max_cells" in t
        ]
        assert actionable, (
            f"Expected an actionable warning naming the memory cap. Got warnings: {warning_texts}"
        )
        # The selected grid is still memory-capped and > clearance/2
        assert result.memory_capped is True
        assert result.resolution > 0.075
        assert result.clearance_compliant_at_clearance_over_2 is False

    def test_no_capping_no_bump_no_warning(self):
        """When memory is not the binding constraint, the existing
        pad-alignment-first logic runs unchanged and the bump path is
        a no-op (no INFO log, no warning)."""
        pads = [
            PadPosition(x=2.0, y=2.0),
            PadPosition(x=2.5, y=2.0),
        ]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = auto_select_grid_resolution(
                pads,
                clearance=0.15,
                board_width=10.0,
                board_height=10.0,
            )

        # Memory cap should not engage on a tiny board
        assert result.memory_capped is False
        # Budget unchanged
        assert result.memory_budget_used == 500_000
        # No memory-cap warning
        warning_texts = [str(w.message) for w in caught]
        for text in warning_texts:
            assert "memory budget cap" not in text.lower()

    def test_new_fields_set_for_normal_path(self):
        """The new GridAutoSelection fields are populated even when
        the bump path doesn't engage."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=2.54, y=0.0)]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # Default max_cells when no board dimensions -> still recorded
        assert result.memory_budget_used == 500_000
        # 0.3mm clearance / 2 = 0.15mm.  Default candidates include 0.1
        # which is <= 0.15.
        assert result.clearance_compliant_at_clearance_over_2 is True


class TestLatticeRescue:
    """Tests for issue #3441: lattice-aware auto-grid memory bump.

    The #3239 bump only adopts a bumped candidate set when it reaches
    ``clearance/2``, so a board-lattice-aligned grid that is merely
    ``<= clearance`` (0.1mm at clearance 0.15mm) could never be rescued
    -- the memory filter excluded it BEFORE the off-grid vote ever saw
    it (board 07: 0.127mm selected with 190/244 pads off-grid while
    0.1mm had only 53).  The lattice rescue retries the budget bump and
    adopts it when the unlocked candidate strictly reduces the off-grid
    count AND places a majority of pads on-grid (dominant lattice).
    """

    def _board07_like_pads(self) -> list[PadPosition]:
        """Synthetic board-07-like layout: dominant 0.1mm lattice plus a
        small genuinely-off-lattice BGA cluster at 1.27mm pitch with
        0.635mm offsets.
        """
        pads: list[PadPosition] = []
        # Dominant lattice: 40 pads on varied 0.1mm multiples (0.7/0.9mm
        # pitches), spread so no single 0.127mm origin offset can align a
        # majority of them.
        for i in range(8):
            for j in range(5):
                pads.append(PadPosition(x=10.0 + i * 0.7, y=20.0 + j * 0.9))
        # Minority off-lattice cluster: 6 pads at 1.27mm pitch with
        # 0.635mm offsets (0.635/0.1 = 6.35 -> off the 0.1mm lattice).
        for k in range(6):
            pads.append(PadPosition(x=60.635 + k * 1.27, y=50.635))
        return pads

    def test_lattice_rescue_selects_dominant_lattice_grid(self, caplog):
        """100x100mm board at clearance 0.15, max_cells 500k:

        - memory filter excludes everything finer than 0.127mm
          (0.127 needs 620k > 500k cells, so even the coarsest fails and
          the filter keeps [0.127] as the fallback);
        - the #3239 bump (2M cells) unlocks 0.1mm (1M cells) but not
          0.065mm (2.37M), and 0.1 > clearance/2 = 0.075 so the #3239
          adoption predicate rejects it;
        - the #3441 lattice rescue must adopt 0.1mm: it has strictly
          fewer off-grid pads and a majority of pads on-grid.
        """
        pads = self._board07_like_pads()
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = auto_select_grid_resolution(
                    pads,
                    clearance=0.15,
                    board_width=100.0,
                    board_height=100.0,
                    max_cells=500_000,
                    candidates=[0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508],
                )

        assert result.resolution == 0.1, (
            f"Expected lattice rescue to select 0.1mm, got "
            f"{result.resolution}mm (candidates tried: "
            f"{result.candidates_tried})"
        )
        assert result.lattice_rescued is True
        assert result.memory_budget_used == 2_000_000
        # Only the genuinely off-lattice BGA cluster remains off-grid.
        assert result.off_grid_pads == 6

        # INFO log names the rescue
        rescue_logs = [r for r in caplog.records if "lattice rescue" in r.message.lower()]
        assert rescue_logs, (
            f"Expected INFO log for the lattice rescue. Got: {[r.message for r in caplog.records]}"
        )

        # The warning is the honest lattice-rescue variant, not the
        # misleading "memory budget cap forces" one.
        warning_texts = [str(w.message) for w in caught]
        assert any("lattice rescue" in t.lower() for t in warning_texts), (
            f"Expected lattice-rescue warning, got: {warning_texts}"
        )
        for t in warning_texts:
            assert "memory budget cap forces" not in t, (
                f"Misleading 'forces' warning emitted after rescue: {t}"
            )

    def test_no_rescue_without_dominant_lattice(self):
        """When the unlocked candidate still leaves a majority of pads
        off-grid, the rescue must NOT burn a 4x memory bump; the original
        memory-cap warning fires instead."""
        # Pads on a 0.127mm lattice (imperial): the unlocked 0.1mm
        # candidate does not help them at all.
        pads = [PadPosition(x=10.0 + i * 1.27 + 0.0635, y=20.0) for i in range(20)]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = auto_select_grid_resolution(
                pads,
                clearance=0.15,
                board_width=100.0,
                board_height=100.0,
                max_cells=500_000,
                candidates=[0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508],
            )

        assert result.lattice_rescued is False
        assert result.memory_budget_used == 500_000
        warning_texts = [str(w.message) for w in caught]
        assert any("memory budget cap forces" in t for t in warning_texts), (
            f"Expected original memory-cap warning, got: {warning_texts}"
        )

    def test_clearance_safe_bump_takes_priority_over_rescue(self, caplog):
        """When the #3239 bump can reach a clearance/2 grid, it runs first
        and the lattice rescue stays out of the way."""
        pads = [
            PadPosition(x=2.0, y=2.0),
            PadPosition(x=4.0, y=2.0),
            PadPosition(x=4.05, y=4.0),
        ]
        with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
            result = auto_select_grid_resolution(
                pads,
                clearance=0.15,
                board_width=65.0,
                board_height=56.0,
                max_cells=500_000,
            )
        assert result.resolution <= 0.075
        assert result.lattice_rescued is False

    def test_no_rescue_when_memory_not_capped(self):
        """Tiny board: no memory cap, no rescue."""
        pads = [PadPosition(x=2.0, y=2.0), PadPosition(x=2.7, y=2.0)]
        result = auto_select_grid_resolution(
            pads,
            clearance=0.15,
            board_width=10.0,
            board_height=10.0,
        )
        assert result.memory_capped is False
        assert result.lattice_rescued is False
        assert result.memory_budget_used == 500_000

    def test_summary_mentions_lattice_rescue(self):
        """GridAutoSelection.summary() surfaces the rescue."""
        sel = GridAutoSelection(
            resolution=0.1,
            off_grid_pads=53,
            total_pads=244,
            off_grid_percentage=21.7,
            candidates_tried=[(0.127, 190), (0.1, 53)],
            memory_capped=True,
            memory_budget_used=2_000_000,
            lattice_rescued=True,
        )
        assert "lattice rescue" in sel.summary()

    def test_board07_real_pcb_selects_lattice_grid(self):
        """End-to-end on the committed board 07 PCB: auto-grid must now
        select 0.1mm (the board's dominant pad lattice) instead of the
        clearance-risky 0.127mm that put 190/244 pads off-grid."""
        import pathlib

        pcb = (
            pathlib.Path(__file__).parent.parent
            / "boards"
            / "07-matchgroup-test"
            / "output"
            / "matchgroup_test.kicad_pcb"
        )
        if not pcb.exists():
            pytest.skip("board 07 artifact not present")
        pads = extract_pad_positions(str(pcb))
        dims = extract_board_dimensions(str(pcb))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = auto_select_grid_resolution(
                pads,
                clearance=0.15,
                board_width=dims[0],
                board_height=dims[1],
            )
        assert result.resolution == 0.1
        assert result.lattice_rescued is True
        # The 53 off-grid pads are the genuinely off-lattice components
        # (U4 BGA-49 at 1.27mm pitch: 45 pads; J3 2.54mm THT header:
        # 8 pads) -- NOT a grid-origin bug (curator diagnosis confirmed).
        assert result.off_grid_pads == 53


class TestGridAutoSelectionSummary:
    """Tests for GridAutoSelection.summary() method."""

    def test_summary_format(self):
        """Test summary output format."""
        result = GridAutoSelection(
            resolution=0.127,
            off_grid_pads=2,
            total_pads=10,
            off_grid_percentage=20.0,
            candidates_tried=[(0.25, 5), (0.127, 2)],
        )
        summary = result.summary()
        assert "0.127mm" in summary
        assert "10" in summary  # Total pads
        assert "2" in summary  # Off-grid pads
        assert "20.0%" in summary
        assert "selected" in summary.lower()


class TestExtractPadPositions:
    """Tests for extract_pad_positions function."""

    @pytest.fixture
    def minimal_pcb(self, tmp_path):
        """Create a minimal PCB file for testing."""
        pcb_content = """(kicad_pcb (version 20230121) (generator "test")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "VCC")

  (footprint "Package_SO:SOIC-8" (layer "F.Cu")
    (at 100 100)
    (property "Reference" "U1")
    (pad "1" smd rect (at -2.54 -0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
    (pad "2" smd rect (at -2.54 0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
    (pad "3" smd rect (at 2.54 0.635) (size 0.6 0.9) (layers "F.Cu") (net 1 "VCC"))
  )
)"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_extract_from_file(self, minimal_pcb):
        """Test extracting pad positions from a PCB file."""
        positions = extract_pad_positions(minimal_pcb)
        assert len(positions) == 3
        # Verify positions are transformed correctly
        for pos in positions:
            assert pos.x > 90  # Near 100mm
            assert pos.y > 90  # Near 100mm

    def test_extract_from_text(self, minimal_pcb):
        """Test extracting pad positions from PCB text content."""
        pcb_text = minimal_pcb.read_text()
        positions = extract_pad_positions(pcb_text)
        assert len(positions) == 3

    def test_footprint_rotation(self, tmp_path):
        """Test that footprint rotation is applied correctly."""
        # Footprint rotated 90 degrees
        pcb_content = """(kicad_pcb (version 20230121) (generator "test")
  (layers (0 "F.Cu" signal))
  (net 0 "")

  (footprint "Test" (layer "F.Cu")
    (at 100 100 90)
    (property "Reference" "U1")
    (pad "1" smd rect (at 1 0) (size 0.5 0.5) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(pcb_content)

        positions = extract_pad_positions(pcb_file)
        assert len(positions) == 1
        # With 90 degree rotation, pad at (1, 0) relative becomes (0, 1) relative
        # Absolute: (100 + 0, 100 + 1) = (100, 101)
        pos = positions[0]
        assert abs(pos.x - 100.0) < 0.01
        assert abs(pos.y - 101.0) < 0.01


class TestRecommendGridForBoardSize:
    """Tests for recommend_grid_for_board_size function."""

    def test_small_board_gets_fine_grid(self):
        """Test that small boards get 0.05mm grid for best pitch alignment."""
        # 65x56mm board is small
        grid = recommend_grid_for_board_size(65, 56, clearance=0.15)
        assert grid == 0.05

    def test_medium_board_gets_balanced_grid(self):
        """Test that medium boards get 0.1mm grid."""
        # 120x80mm board is medium
        grid = recommend_grid_for_board_size(120, 80, clearance=0.15)
        assert grid == 0.1

    def test_large_board_gets_coarse_grid(self):
        """Test that large boards get 0.25mm grid for memory efficiency."""
        # 200x120mm board is large
        grid = recommend_grid_for_board_size(200, 120, clearance=0.3)
        assert grid == 0.25

    def test_grid_clamped_to_clearance(self):
        """Test that grid resolution never exceeds clearance."""
        # Large board with small clearance
        grid = recommend_grid_for_board_size(200, 120, clearance=0.127)
        assert grid == 0.127  # Clamped to clearance, not 0.25

    def test_tssop_pitch_alignment(self):
        """Test that small board grid aligns with TSSOP 0.65mm pitch."""
        grid = recommend_grid_for_board_size(50, 40, clearance=0.15)
        # 0.05mm grid divides evenly into 0.65mm: 0.65 / 0.05 = 13
        # Use round to avoid floating point precision issues
        assert round(0.65 / grid) == 0.65 / grid or abs(0.65 / grid - round(0.65 / grid)) < 0.01

    def test_qfp_pitch_alignment(self):
        """Test that recommended grids align with QFP 0.5mm pitch."""
        # Small board
        grid_small = recommend_grid_for_board_size(50, 40, clearance=0.15)
        # 0.5 / 0.05 = 10 exact
        divisions = 0.5 / grid_small
        assert abs(divisions - round(divisions)) < 0.01

        # Medium board
        grid_medium = recommend_grid_for_board_size(120, 80, clearance=0.15)
        # 0.5 / 0.1 = 5 exact
        divisions = 0.5 / grid_medium
        assert abs(divisions - round(divisions)) < 0.01

    def test_custom_thresholds(self):
        """Test with custom board size thresholds."""
        # Use smaller thresholds
        grid = recommend_grid_for_board_size(
            80,
            60,
            clearance=0.15,
            small_board_threshold=(50, 40),
            medium_board_threshold=(75, 55),
        )
        # 80x60 is now "large" with custom thresholds
        assert grid == 0.15  # 0.25 clamped to clearance

    def test_boundary_conditions(self):
        """Test boards at exact threshold boundaries."""
        # Exactly at small threshold
        grid = recommend_grid_for_board_size(100, 75, clearance=0.15)
        assert grid == 0.05  # Still small

        # Just over small threshold
        grid = recommend_grid_for_board_size(101, 75, clearance=0.15)
        assert grid == 0.1  # Now medium

        # Exactly at medium threshold
        grid = recommend_grid_for_board_size(150, 100, clearance=0.3)
        assert grid == 0.1  # Still medium

        # Just over medium threshold
        grid = recommend_grid_for_board_size(151, 100, clearance=0.3)
        assert grid == 0.25  # Now large


class TestComputeGcdGridCandidates:
    """Tests for _compute_gcd_grid_candidates helper."""

    def test_empty_pads(self):
        """No candidates from fewer than 2 pads."""
        assert _compute_gcd_grid_candidates([]) == []
        assert _compute_gcd_grid_candidates([PadPosition(x=0.0, y=0.0)]) == []

    def test_single_spacing(self):
        """Two pads 0.65mm apart produce GCD = 0.65mm."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=0.65, y=0.0)]
        result = _compute_gcd_grid_candidates(pads)
        # GCD should be 0.65mm; multiples 1.3mm and 3.25mm also returned
        assert 0.65 in result

    def test_tssop_pitch(self):
        """Multiple 0.65mm-spaced pads produce GCD = 0.65mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        assert 0.65 in result

    def test_mixed_065_254_gcd(self):
        """Mixed 0.65mm and 2.54mm spacings produce GCD = 0.01mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=10.0, y=0.0),
            PadPosition(x=12.54, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        # GCD of 650 and 2540 (in microns) is 10 microns = 0.01mm
        assert 0.01 in result

    def test_pure_imperial(self):
        """Pure 2.54mm pads produce GCD = 2.54mm."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = _compute_gcd_grid_candidates(pads)
        assert 2.54 in result

    def test_min_grid_filter(self):
        """Candidates below min_grid are filtered out."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.003, y=0.0),  # 3um spacing
        ]
        result = _compute_gcd_grid_candidates(pads, min_grid=0.005)
        # 0.003mm rounds to 0.005 after the 5um rounding, but delta may be 0
        # Either way, nothing below 0.005mm should appear
        for c in result:
            assert c >= 0.005


class TestGcdBasedGridSelection:
    """Tests for GCD-based candidate integration in auto_select_grid_resolution."""

    def test_ssop_065_pitch_zero_off_grid(self):
        """Board with 0.65mm-pitch SSOP pads achieves 0 off-grid.

        This is the core scenario from issue #1753: SSOP/TSSOP packages
        with 0.65mm pitch should be fully on-grid after GCD candidate
        injection.
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
            PadPosition(x=2.60, y=0.0),
            PadPosition(x=3.25, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.off_grid_pads == 0, (
            f"SSOP 0.65mm pads should have zero off-grid, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_mixed_065_and_254_minimises_off_grid(self):
        """Mixed 0.65mm SSOP + 2.54mm THT pads with GCD candidates.

        The GCD of spacings should produce a candidate that aligns with
        both pitches, or at least minimise off-grid count better than
        the fixed candidates alone.
        """
        pads = [
            # SSOP at 0.65mm pitch
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
            # THT at 2.54mm pitch
            PadPosition(x=10.0, y=5.0),
            PadPosition(x=12.54, y=5.0),
            PadPosition(x=15.08, y=5.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # With GCD candidates, off-grid count should be lower than without
        assert result.total_pads == 7
        # The GCD-derived candidate (e.g. 0.01mm) should achieve all on-grid
        # if it passes the memory filter
        assert result.off_grid_pads <= 3, (
            f"Mixed board should have at most 3 off-grid pads, got {result.off_grid_pads}"
        )

    def test_standard_pitch_regression(self):
        """Pure standard-pitch boards behave unchanged.

        For boards with only 0.5mm/1.27mm/2.54mm components, the GCD
        candidates should not change the selected grid (the fixed
        candidates already handle these pitches).
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=1.27, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # 0.127mm is the classic imperial grid; should still be selected
        assert result.off_grid_pads == 0
        assert result.resolution == 0.127

    def test_gcd_candidate_respects_memory_budget(self):
        """Fine GCD candidates are filtered out when they exceed memory budget."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
        ]
        # Board where 0.065mm grid would exceed budget but 0.65mm fits
        # 100x100mm board, max_cells=500k: 100*100/0.065^2 = 2.37M (too much)
        # but 100*100/0.65^2 = 23.7k (fits)
        result = auto_select_grid_resolution(
            pads,
            clearance=1.5,  # Very loose clearance so all candidates pass DRC
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
        )
        # The fine GCD candidates (0.065mm etc.) should be filtered out
        # by the memory budget; only coarser ones should survive.
        cells = (100.0 * 100.0) / (result.resolution**2)
        assert cells <= 500_000, (
            f"Selected grid {result.resolution}mm produces {cells:.0f} cells, "
            f"exceeds budget of 500000"
        )

    def test_gcd_candidates_in_summary(self):
        """GCD-derived candidates appear in the summary output."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=1.5)
        summary = result.summary()
        # The GCD of 0.65mm spacings is 0.65mm; it or its multiples should
        # appear as candidates in the summary
        assert "0.65mm" in summary or "1.3mm" in summary or "3.25mm" in summary, (
            f"GCD-derived candidates should appear in summary:\n{summary}"
        )

    def test_single_component_single_pad(self):
        """Board with only 1 pad produces no GCD candidates (no crash)."""
        pads = [PadPosition(x=5.0, y=5.0)]
        result = auto_select_grid_resolution(pads, clearance=0.3)
        # Should work fine, just use fixed candidates
        assert result.total_pads == 1

    def test_custom_candidates_skips_gcd(self):
        """When custom candidates are provided, GCD injection is skipped."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=1.5, candidates=[0.5, 0.25])
        resolutions_tried = [c[0] for c in result.candidates_tried]
        # Only the user-specified candidates should be tried
        assert 0.65 not in resolutions_tried


class TestExtractBoardDimensions:
    """Tests for extract_board_dimensions function."""

    def test_extract_from_pcb_text(self):
        """Extract dimensions from PCB text with gr_rect."""
        pcb_text = """(kicad_pcb (version 20230121) (generator "test")
  (layers (0 "F.Cu" signal))
  (gr_rect (start 115 75) (end 180 131) (layer "Edge.Cuts") (stroke (width 0.1)))
)"""
        dims = extract_board_dimensions(pcb_text)
        assert dims is not None
        width, height = dims
        assert abs(width - 65.0) < 0.01
        assert abs(height - 56.0) < 0.01

    def test_extract_from_file(self, tmp_path):
        """Extract dimensions from a PCB file."""
        pcb_content = """(kicad_pcb (version 20230121) (generator "test")
  (layers (0 "F.Cu" signal))
  (gr_rect (start 10 20) (end 60 70) (layer "Edge.Cuts") (stroke (width 0.1)))
)"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)
        dims = extract_board_dimensions(pcb_file)
        assert dims is not None
        assert abs(dims[0] - 50.0) < 0.01
        assert abs(dims[1] - 50.0) < 0.01

    def test_no_outline_returns_none(self):
        """Returns None when no gr_rect is found."""
        pcb_text = """(kicad_pcb (version 20230121) (generator "test")
  (layers (0 "F.Cu" signal))
)"""
        dims = extract_board_dimensions(pcb_text)
        assert dims is None


class TestIsOnGridWithOffset:
    """Tests for _is_on_grid_with_offset helper."""

    def test_on_grid_with_zero_offset(self):
        """Zero offset behaves the same as _is_on_grid."""
        assert _is_on_grid_with_offset(0.5, 0.25, 0.0)
        assert _is_on_grid_with_offset(1.0, 0.25, 0.0)

    def test_on_grid_with_nonzero_offset(self):
        """Value on shifted grid is detected correctly."""
        # Grid at offset 0.04, resolution 0.1 -> grid points at 0.04, 0.14, 0.24, ...
        assert _is_on_grid_with_offset(0.04, 0.1, 0.04)
        assert _is_on_grid_with_offset(0.14, 0.1, 0.04)
        assert _is_on_grid_with_offset(0.24, 0.1, 0.04)

    def test_off_grid_with_offset(self):
        """Value not on shifted grid is detected correctly."""
        # Grid at offset 0.04, resolution 0.1 -> 0.0 is off-grid
        assert not _is_on_grid_with_offset(0.0, 0.1, 0.04)


class TestCountOffGridWithOffset:
    """Tests for _count_off_grid_with_offset helper."""

    def test_all_on_grid_no_offset(self):
        """All pads on-grid with zero offset."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=0.5, y=0.5)]
        assert _count_off_grid_with_offset(pads, 0.25) == 0

    def test_offset_brings_pads_on_grid(self):
        """Offset shifts grid to align with pads."""
        pads = [PadPosition(x=0.04, y=0.0), PadPosition(x=0.14, y=0.0)]
        # Without offset, pads are off-grid at 0.1mm resolution
        assert _count_off_grid_with_offset(pads, 0.1, 0.0, 0.0) == 2
        # With offset 0.04, pads are on-grid
        assert _count_off_grid_with_offset(pads, 0.1, 0.04, 0.0) == 0


class TestFindOptimalOriginOffset:
    """Tests for _find_optimal_origin_offset helper."""

    def test_zero_offset_when_already_aligned(self):
        """Returns (0,0) when pads are already on-grid."""
        pads = [PadPosition(x=0.0, y=0.0), PadPosition(x=0.5, y=0.5)]
        offset = _find_optimal_origin_offset(pads, 0.25)
        assert offset == (0.0, 0.0)

    def test_finds_offset_for_shifted_pads(self):
        """Finds offset that aligns shifted pads."""
        pads = [
            PadPosition(x=0.04, y=0.0),
            PadPosition(x=0.14, y=0.0),
            PadPosition(x=0.24, y=0.0),
        ]
        offset = _find_optimal_origin_offset(pads, 0.1)
        # With offset, all pads should be on-grid
        off_grid = _count_off_grid_with_offset(pads, 0.1, offset[0], offset[1])
        assert off_grid == 0

    def test_empty_pad_list(self):
        """Returns (0,0) for empty list."""
        assert _find_optimal_origin_offset([], 0.1) == (0.0, 0.0)


class TestMixedPitchOriginOffset:
    """Tests for the mixed metric/imperial pad alignment fix (issue #2033).

    The core bug: auto_select_grid_resolution with a mix of 2.54mm-pitch
    (imperial THT) and 0.65mm-pitch (TSSOP) pads would produce 97% off-grid
    because no single zero-origin grid aligns with both pitches.
    """

    def test_mixed_254_065_under_20_pct_off_grid(self):
        """Mixed 2.54mm + 0.65mm pads produce < 20% off-grid.

        This is the primary acceptance criterion from issue #2033.
        """
        pads = [
            # Imperial THT headers at 2.54mm pitch
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            PadPosition(x=7.62, y=0.0),
            # TSSOP at 0.65mm pitch
            PadPosition(x=20.0, y=0.0),
            PadPosition(x=20.65, y=0.0),
            PadPosition(x=21.30, y=0.0),
            PadPosition(x=21.95, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.off_grid_percentage < 20.0, (
            f"Mixed 2.54mm + 0.65mm pads should have < 20% off-grid, "
            f"got {result.off_grid_percentage:.1f}% with grid {result.resolution}mm "
            f"and offset {result.origin_offset}"
        )

    def test_origin_offset_reported(self):
        """GridAutoSelection reports the chosen grid origin offset."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert isinstance(result.origin_offset, tuple)
        assert len(result.origin_offset) == 2

    def test_pure_imperial_zero_off_grid(self):
        """Pure 2.54mm pads still produce 0% off-grid (regression check)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=2.54, y=0.0),
            PadPosition(x=5.08, y=0.0),
            PadPosition(x=7.62, y=0.0),
            PadPosition(x=0.0, y=2.54),
            PadPosition(x=2.54, y=2.54),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.off_grid_pads == 0, (
            f"Pure imperial pads should have zero off-grid, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_pure_metric_065_zero_off_grid(self):
        """Pure 0.65mm pads still produce 0% off-grid (regression check)."""
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.65, y=0.0),
            PadPosition(x=1.30, y=0.0),
            PadPosition(x=1.95, y=0.0),
            PadPosition(x=2.60, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        assert result.off_grid_pads == 0, (
            f"Pure 0.65mm pads should have zero off-grid, got {result.off_grid_pads} "
            f"with grid {result.resolution}mm"
        )

    def test_summary_shows_offset(self):
        """Summary includes offset when it's non-zero."""
        result = GridAutoSelection(
            resolution=0.065,
            off_grid_pads=1,
            total_pads=8,
            off_grid_percentage=12.5,
            candidates_tried=[(0.065, 1)],
            origin_offset=(0.04, 0.0),
        )
        summary = result.summary()
        assert "origin offset" in summary.lower()
        assert "0.0400" in summary

    def test_summary_hides_offset_when_zero(self):
        """Summary omits offset when it's (0,0)."""
        result = GridAutoSelection(
            resolution=0.065,
            off_grid_pads=0,
            total_pads=4,
            off_grid_percentage=0.0,
            candidates_tried=[(0.065, 0)],
            origin_offset=(0.0, 0.0),
        )
        summary = result.summary()
        assert "origin offset" not in summary.lower()


class TestRoutingGridOriginOffset:
    """Tests for RoutingGrid with non-zero origin offset."""

    def test_world_to_grid_with_offset(self):
        """RoutingGrid with offset correctly maps world coords to grid indices."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules(grid_resolution=0.1)
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            origin_x=0.0,
            origin_y=0.0,
            grid_origin_offset=(0.04, 0.0),
        )
        # With offset 0.04, grid point 0 is at x=0.04
        # So x=0.04 should map to grid index 0
        gx, gy = grid.world_to_grid(0.04, 0.0)
        assert gx == 0
        # x=0.14 should map to grid index 1
        gx, gy = grid.world_to_grid(0.14, 0.0)
        assert gx == 1

    def test_grid_to_world_with_offset(self):
        """RoutingGrid with offset correctly maps grid indices to world coords."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules(grid_resolution=0.1)
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            origin_x=0.0,
            origin_y=0.0,
            grid_origin_offset=(0.04, 0.0),
        )
        # Grid index 0 should map to x=0.04
        wx, wy = grid.grid_to_world(0, 0)
        assert abs(wx - 0.04) < 0.001
        # Grid index 1 should map to x=0.14
        wx, wy = grid.grid_to_world(1, 0)
        assert abs(wx - 0.14) < 0.001

    def test_offset_from_rules(self):
        """RoutingGrid reads offset from DesignRules when not explicitly given."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules(
            grid_resolution=0.1,
            grid_origin_offset=(0.04, 0.02),
        )
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        assert grid.grid_origin_offset == (0.04, 0.02)
        # origin_x should be shifted
        assert abs(grid.origin_x - 0.04) < 0.001
        assert abs(grid.origin_y - 0.02) < 0.001


class TestIssue2387ClearanceFilterRelaxation:
    """Regression tests for issue #2387.

    The candidate filter previously was ``c <= clearance / 2``, which
    excluded the 0.1mm grid at clearance=0.15mm and forced the auto-selector
    onto a 0.065mm grid that placed 86% of pads off-grid on board 03
    (USB-joystick).  The fix relaxes the filter to ``c <= clearance`` so that
    pad-aligned grids beat misaligned half-clearance grids.
    """

    @staticmethod
    def _board03_tqfp32_pad_positions() -> list[PadPosition]:
        """TQFP-32 pad positions at the board-03 layout coordinates.

        U1 is centered at (130, 120) with 0.8mm pitch and 8 pads per side.
        First-pad offset from center is (4*0.8 - 0.4) = 3.2 - 0.4 = 2.8mm
        from the side edge ... we just generate 32 pads at exact 0.1mm-aligned
        positions matching the board-03 PCB.
        """
        positions: list[PadPosition] = []
        cx, cy = 130.0, 120.0
        # Bottom row pads (1..8): x = cx - 2.8 + i*0.8, y = cy + 2.8
        for i in range(8):
            positions.append(PadPosition(x=cx - 2.8 + i * 0.8, y=cy + 2.8))
        # Right column pads (9..16): x = cx + 2.8, y = cy + 2.8 - i*0.8
        for i in range(8):
            positions.append(PadPosition(x=cx + 2.8, y=cy + 2.8 - i * 0.8))
        # Top row pads (17..24): x = cx + 2.8 - i*0.8, y = cy - 2.8
        for i in range(8):
            positions.append(PadPosition(x=cx + 2.8 - i * 0.8, y=cy - 2.8))
        # Left column pads (25..32): x = cx - 2.8, y = cy - 2.8 + i*0.8
        for i in range(8):
            positions.append(PadPosition(x=cx - 2.8, y=cy - 2.8 + i * 0.8))
        return positions

    def test_tqfp32_at_clearance_015_picks_pad_aligned_grid(self):
        """TQFP-32 at 0.1mm-aligned coords with clearance=0.15 picks 0.1 or coarser.

        Before issue #2387 this returned 0.065mm with 100% off-grid count;
        after the fix the 0.1mm candidate (which divides 0.8mm evenly)
        survives the DRC filter and is preferred by the coarser-when-equal
        rule.
        """
        pads = self._board03_tqfp32_pad_positions()
        result = auto_select_grid_resolution(pads, clearance=0.15)
        # All 32 pads should be on-grid at the chosen resolution
        assert result.off_grid_pads == 0, (
            f"TQFP-32 at 0.1mm-aligned coords should have zero off-grid "
            f"pads with clearance=0.15mm; got {result.off_grid_pads} off "
            f"at grid {result.resolution}mm"
        )
        # Auto-selector should *not* pick 0.065mm or finer for this case
        assert result.resolution >= 0.1, (
            f"Expected pad-aligned grid >= 0.1mm, got {result.resolution}mm. "
            f"This is the regression that broke board 03."
        )

    def test_tqfp32_picks_at_least_01mm_with_loose_clearance(self):
        """Even at clearance=0.20, the 0.1mm pad-aligned grid wins."""
        pads = self._board03_tqfp32_pad_positions()
        result = auto_select_grid_resolution(pads, clearance=0.20)
        assert result.off_grid_pads == 0
        assert result.resolution >= 0.1

    def test_clearance_010_keeps_01mm_in_candidates(self):
        """At clearance=0.10mm, the 0.1mm candidate is now valid (was excluded)."""
        pads = self._board03_tqfp32_pad_positions()
        result = auto_select_grid_resolution(pads, clearance=0.10)
        # 0.1mm is on the boundary (c <= clearance) and divides 0.8mm evenly
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.1 in resolutions_tried, (
            f"0.1mm candidate must survive c <= clearance filter; got tried = {resolutions_tried}"
        )
        # And at this clearance, 0.1mm is the coarsest valid candidate
        assert result.resolution == 0.1

    def test_clearance_015_does_not_filter_out_01mm(self):
        """The 0.1mm grid is not filtered when clearance=0.15mm (issue #2387).

        This is the canonical regression: before the fix, 0.1 > 0.075
        excluded 0.1mm; afterwards 0.1 <= 0.15 keeps it.
        """
        pads = [
            PadPosition(x=0.0, y=0.0),
            PadPosition(x=0.8, y=0.0),
            PadPosition(x=1.6, y=0.0),
        ]
        result = auto_select_grid_resolution(pads, clearance=0.15)
        resolutions_tried = [c[0] for c in result.candidates_tried]
        assert 0.1 in resolutions_tried


class TestIssue2387MultiResPlanWithBoardDims:
    """Regression test for issue #2387: compute_multi_resolution_plan needs
    board dimensions to avoid memory-busting fine grids.
    """

    def test_inner_auto_select_respects_board_dimensions(self):
        """When board_width/board_height are passed, inner auto_select_grid
        is memory-capped instead of selecting the finest survivor.
        """
        from kicad_tools.router.io import compute_multi_resolution_plan
        from kicad_tools.router.primitives import Pad

        # 60x40mm board with TQFP-32 at 0.8mm pitch (one fine-pitch
        # component) plus a few capacitor pads off-grid at 0.05mm
        cx, cy = 30.0, 20.0
        pads: list[Pad] = []
        # TQFP-32 — 0.1mm-aligned positions
        for i in range(8):
            pads.append(
                Pad(
                    x=cx - 2.8 + i * 0.8,
                    y=cy + 2.8,
                    width=0.5,
                    height=1.5,
                    net=i + 1,
                    net_name=f"NET{i + 1}",
                    ref="U1",
                    pin=str(i + 1),
                )
            )

        # Without board dimensions, inner auto-select picks finest GCD
        # candidate (0.1 from 0.8 pitch) but no memory budget guard is
        # applied; with board dimensions, the same plan can use the cap.
        plan_with_dims = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=60.0,
            board_height=40.0,
        )
        plan_without_dims = compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
        )
        # Both should be None (no fine-pitch components below 0.8 strict;
        # 0.8 is the threshold) OR a multi-res plan; the important
        # contract is that passing board dims doesn't crash or change
        # the chosen coarse resolution toward something memory-busting.
        # Assert that when a plan is returned, the coarse resolution
        # divides the board cleanly within the memory budget.
        for plan in (plan_with_dims, plan_without_dims):
            if plan is None:
                continue
            cells = (60.0 / plan.coarse_resolution) * (40.0 / plan.coarse_resolution)
            # The plan with board dims must respect the inner auto-select
            # budget (default 500k cells); plan without dims may exceed it.
            if plan is plan_with_dims:
                # 0.1 -> 240k cells, 0.05 -> 960k (would exceed default 500k)
                # so the chosen coarse must be >= ~0.09mm to fit
                assert cells <= 500_000 * 4, (
                    f"With board dims, multi-res plan should not select a "
                    f"memory-busting grid; got {plan.coarse_resolution}mm "
                    f"-> {cells:.0f} cells"
                )
