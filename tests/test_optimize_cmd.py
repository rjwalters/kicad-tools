"""Tests for the optimize-traces CLI command, focusing on --drc-aware mode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.optimize_cmd import main

# ---------------------------------------------------------------------------
# Fixtures: Minimal KiCad PCB files for testing
# ---------------------------------------------------------------------------

# A minimal valid KiCad PCB with segments on net 1 (NET1) that can be
# optimized (two collinear segments that merge into one).
SIMPLE_PCB = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(generator_version "8.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(net 0 "")
\t(net 1 "NET1")
\t(net 2 "GND")
\t(gr_rect (start 90 30) (end 160 70)
\t\t(stroke (width 0.1) (type default))
\t\t(fill none)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge-rect")
\t)
\t(footprint "R_0603"
\t\t(layer "F.Cu")
\t\t(uuid "fp-r1")
\t\t(at 100 50)
\t\t(property "Reference" "R1" (at 0 -1.5) (layer "F.SilkS") (uuid "ref1"))
\t\t(property "Value" "1k" (at 0 1.5) (layer "F.Fab") (uuid "val1"))
\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
\t\t(pad "2" smd roundrect (at 0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET1"))
\t)
\t(footprint "R_0603"
\t\t(layer "F.Cu")
\t\t(uuid "fp-r2")
\t\t(at 130 50)
\t\t(property "Reference" "R2" (at 0 -1.5) (layer "F.SilkS") (uuid "ref2"))
\t\t(property "Value" "1k" (at 0 1.5) (layer "F.Fab") (uuid "val2"))
\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET1"))
\t\t(pad "2" smd roundrect (at 0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
\t)
\t(segment (start 100.8 50) (end 115 50) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
\t(segment (start 115 50) (end 129.2 50) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-2"))
)
"""


@pytest.fixture
def simple_pcb_path(tmp_path: Path) -> Path:
    """Write SIMPLE_PCB to a temp file and return the path."""
    pcb_file = tmp_path / "simple.kicad_pcb"
    pcb_file.write_text(SIMPLE_PCB)
    return pcb_file


# ---------------------------------------------------------------------------
# Tests: --drc-aware flag validation
# ---------------------------------------------------------------------------


class TestDrcAwareArgValidation:
    """Test CLI argument validation for --drc-aware mode."""

    def test_drc_aware_without_mfr_exits_with_error(self, simple_pcb_path: Path):
        """--drc-aware without --mfr should exit with error and helpful message."""
        result = main([str(simple_pcb_path), "--drc-aware"])
        assert result == 1

    def test_drc_aware_with_invalid_mfr_exits_with_error(self, simple_pcb_path: Path):
        """--drc-aware with invalid --mfr should exit with error."""
        result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "nosuchmanufacturer"])
        assert result == 1

    def test_drc_aware_with_valid_mfr_succeeds(self, simple_pcb_path: Path, tmp_path: Path):
        """--drc-aware with valid --mfr should not error on args."""
        output = tmp_path / "out.kicad_pcb"
        result = main(
            [
                str(simple_pcb_path),
                "--drc-aware",
                "--allow-default-rules",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output),
                "--quiet",
            ]
        )
        assert result == 0

    def test_mfr_without_drc_aware_is_ignored(self, simple_pcb_path: Path, tmp_path: Path):
        """--mfr without --drc-aware should still optimize normally."""
        output = tmp_path / "out.kicad_pcb"
        result = main(
            [
                str(simple_pcb_path),
                "--mfr",
                "jlcpcb",
                "-o",
                str(output),
                "--quiet",
            ]
        )
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: DRC-aware optimization behavior
# ---------------------------------------------------------------------------


class TestDrcAwareOptimization:
    """Test DRC-aware optimization behavior using mocks."""

    def test_drc_aware_dry_run_shows_stats(self, simple_pcb_path: Path, capsys):
        """--dry-run --drc-aware should show DRC stats without writing output."""
        result = main(
            [
                str(simple_pcb_path),
                "--drc-aware",
                "--allow-default-rules",
                "--mfr",
                "jlcpcb",
                "--dry-run",
            ]
        )
        assert result == 0
        captured = capsys.readouterr()
        assert "DRC errors:" in captured.out
        assert "Dry run" in captured.out

    def test_drc_aware_preserves_no_error_board(self, simple_pcb_path: Path, tmp_path: Path):
        """A board with no DRC issues should still get optimized normally."""
        output = tmp_path / "optimized.kicad_pcb"
        result = main(
            [
                str(simple_pcb_path),
                "--drc-aware",
                "--allow-default-rules",
                "--mfr",
                "jlcpcb",
                "-o",
                str(output),
                "--quiet",
            ]
        )
        assert result == 0
        assert output.exists()

    def test_drc_aware_with_net_filter(self, simple_pcb_path: Path, tmp_path: Path):
        """--net FILTER --drc-aware should only DRC-check the filtered net."""
        output = tmp_path / "optimized.kicad_pcb"
        result = main(
            [
                str(simple_pcb_path),
                "--drc-aware",
                "--allow-default-rules",
                "--mfr",
                "jlcpcb",
                "--net",
                "NET1",
                "-o",
                str(output),
                "--quiet",
            ]
        )
        assert result == 0
        assert output.exists()


# ---------------------------------------------------------------------------
# Tests: Rollback behavior via mock DRC
# ---------------------------------------------------------------------------


class TestDrcRollback:
    """Test per-net rollback when DRC violations increase."""

    def test_rollback_when_optimization_increases_errors(self, tmp_path: Path):
        """When optimization increases DRC errors, nets should be rolled back."""
        from kicad_tools.router.optimizer.config import OptimizationConfig
        from kicad_tools.router.optimizer.pcb import optimize_pcb

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(SIMPLE_PCB)
        output_file = tmp_path / "out.kicad_pcb"

        call_count = 0

        # Issue #3138: optimizer now uses _run_drc_error_count_by_category
        # (returns dict keyed by rule id with ``__total__``).  Mock that.
        def mock_drc_error_count_by_category(
            pcb_text,
            manufacturer,
            layers,
            copper_oz,
            min_clearance_mm=None,
            project_path=None,
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Baseline: 0 errors
                return {"__total__": 0}
            elif call_count == 2:
                # After full optimization: 3 errors (bad!)
                return {"__total__": 3, "clearance_segment_segment": 3}
            else:
                # Reverting the net: 0 errors (good!)
                return {"__total__": 0}

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count_by_category",
            side_effect=mock_drc_error_count_by_category,
        ):
            stats = optimize_pcb(
                pcb_path=str(pcb_file),
                output_path=str(output_file),
                optimize_fn=lambda segs: segs,  # identity -- segments unchanged
                config=config,
            )

        # Since optimize_fn is identity, no segments actually changed,
        # so the rollback loop won't find any net to revert (optimized == original).
        # This verifies the rollback logic runs without error.
        assert stats.drc_errors_before == 0

    def test_rollback_reverts_bad_nets(self, tmp_path: Path):
        """Nets that cause DRC regressions should be rolled back."""
        from kicad_tools.router.optimizer.config import OptimizationConfig
        from kicad_tools.router.optimizer.pcb import optimize_pcb
        from kicad_tools.router.primitives import Segment

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(SIMPLE_PCB)
        output_file = tmp_path / "out.kicad_pcb"

        def bad_optimize(segments: list[Segment]) -> list[Segment]:
            """Optimization that changes segments (triggers rollback check)."""
            if not segments:
                return segments
            # Shift all segments slightly to simulate an optimization
            result = []
            for seg in segments:
                result.append(
                    Segment(
                        x1=seg.x1,
                        y1=seg.y1 + 0.001,  # tiny shift
                        x2=seg.x2,
                        y2=seg.y2 + 0.001,
                        width=seg.width,
                        layer=seg.layer,
                        net=seg.net,
                        net_name=seg.net_name,
                    )
                )
            return result

        call_count = 0

        # Issue #3138: optimizer now uses _run_drc_error_count_by_category.
        def mock_drc(
            pcb_text, manufacturer, layers, copper_oz, min_clearance_mm=None, project_path=None
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"__total__": 2, "clearance_segment_segment": 2}  # baseline: 2 errors
            elif call_count == 2:
                return {
                    "__total__": 5,
                    "clearance_segment_segment": 5,
                }  # after optimization: 5 errors
            else:
                return {
                    "__total__": 2,
                    "clearance_segment_segment": 2,
                }  # after reverting: back to 2

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count_by_category",
            side_effect=mock_drc,
        ):
            stats = optimize_pcb(
                pcb_path=str(pcb_file),
                output_path=str(output_file),
                optimize_fn=bad_optimize,
                config=config,
            )

        assert stats.drc_errors_before == 2
        assert stats.nets_rolled_back >= 1
        # After rollback, errors should be back to baseline
        assert stats.drc_errors_after <= stats.drc_errors_before

    def test_no_rollback_when_optimization_is_safe(self, tmp_path: Path):
        """When optimization does not increase errors, no rollback occurs."""
        from kicad_tools.router.optimizer.config import OptimizationConfig
        from kicad_tools.router.optimizer.pcb import optimize_pcb
        from kicad_tools.router.primitives import Segment

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(SIMPLE_PCB)
        output_file = tmp_path / "out.kicad_pcb"

        def safe_optimize(segments: list[Segment]) -> list[Segment]:
            """Optimization that merges collinear segments (safe)."""
            if len(segments) <= 1:
                return segments
            # Merge into single segment (same as collinear merge)
            first = segments[0]
            last = segments[-1]
            return [
                Segment(
                    x1=first.x1,
                    y1=first.y1,
                    x2=last.x2,
                    y2=last.y2,
                    width=first.width,
                    layer=first.layer,
                    net=first.net,
                    net_name=first.net_name,
                )
            ]

        # Issue #3138: optimizer now uses _run_drc_error_count_by_category.
        def mock_drc(
            pcb_text, manufacturer, layers, copper_oz, min_clearance_mm=None, project_path=None
        ):
            # Always return 0 errors -- optimization is safe
            return {"__total__": 0}

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count_by_category",
            side_effect=mock_drc,
        ):
            stats = optimize_pcb(
                pcb_path=str(pcb_file),
                output_path=str(output_file),
                optimize_fn=safe_optimize,
                config=config,
            )

        assert stats.nets_rolled_back == 0
        assert stats.drc_errors_before == 0
        assert stats.drc_errors_after == 0
        # Should have merged 2 segments into 1
        assert stats.segments_after < stats.segments_before


class TestCategoryAwareRollback:
    """Issue #3138 Approach B: category-aware DRC rollback gate.

    The previous gate at ``router/optimizer/pcb.py:407`` was
    ``if full_errors > baseline_errors:`` -- a pure total-count check.
    If the optimizer swapped a ``clearance_segment_segment`` violation
    for a ``clearance_pad_segment`` one at constant total, the gate
    silently passed and the optimized PCB ended up with a route literally
    crossing a pad.  These tests cover the new pad-violation-category
    guard.
    """

    def test_rollback_fires_on_pad_violation_swap_flat_total(self, tmp_path: Path):
        """Total flat, but pad-violations grew: rollback must fire."""
        from kicad_tools.router.optimizer.config import OptimizationConfig
        from kicad_tools.router.optimizer.pcb import optimize_pcb
        from kicad_tools.router.primitives import Segment

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(SIMPLE_PCB)
        output_file = tmp_path / "out.kicad_pcb"

        def shift_optimize(segments: list[Segment]) -> list[Segment]:
            if not segments:
                return segments
            return [
                Segment(
                    x1=s.x1,
                    y1=s.y1 + 0.001,
                    x2=s.x2,
                    y2=s.y2 + 0.001,
                    width=s.width,
                    layer=s.layer,
                    net=s.net,
                    net_name=s.net_name,
                )
                for s in segments
            ]

        call_count = 0

        def mock_drc(
            pcb_text, manufacturer, layers, copper_oz, min_clearance_mm=None, project_path=None
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Baseline: 2 segment-segment violations, 0 pad violations
                return {
                    "__total__": 2,
                    "clearance_segment_segment": 2,
                }
            elif call_count == 2:
                # After optimization: same total 2, but now they are pad
                # violations (route crossing pads -- the show-stopper).
                return {
                    "__total__": 2,
                    "clearance_pad_segment": 2,
                }
            else:
                # After per-net rollback: pad violations cleared, back to baseline.
                return {
                    "__total__": 2,
                    "clearance_segment_segment": 2,
                }

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count_by_category",
            side_effect=mock_drc,
        ):
            stats = optimize_pcb(
                pcb_path=str(pcb_file),
                output_path=str(output_file),
                optimize_fn=shift_optimize,
                config=config,
            )

        # Approach B success: even with a FLAT total, the rollback
        # loop fires because pad-violations grew.
        assert stats.nets_rolled_back >= 1, (
            "Issue #3138 Approach B: rollback must fire on category swaps "
            "that introduce pad-clearance violations even when total is flat."
        )

    def test_no_rollback_when_only_segment_segment_grows(self, tmp_path: Path):
        """Non-pad category growth with flat total should NOT fire the new gate.

        The old behavior remains: if the total grows, rollback fires.  But
        if the total is flat AND the new growth is in a non-pad category,
        rollback does NOT fire (the old gate's behaviour is preserved).
        """
        from kicad_tools.router.optimizer.config import OptimizationConfig
        from kicad_tools.router.optimizer.pcb import optimize_pcb
        from kicad_tools.router.primitives import Segment

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(SIMPLE_PCB)
        output_file = tmp_path / "out.kicad_pcb"

        def shift_optimize(segments: list[Segment]) -> list[Segment]:
            if not segments:
                return segments
            return [
                Segment(
                    x1=s.x1,
                    y1=s.y1 + 0.001,
                    x2=s.x2,
                    y2=s.y2 + 0.001,
                    width=s.width,
                    layer=s.layer,
                    net=s.net,
                    net_name=s.net_name,
                )
                for s in segments
            ]

        call_count = 0

        def mock_drc(
            pcb_text, manufacturer, layers, copper_oz, min_clearance_mm=None, project_path=None
        ):
            nonlocal call_count
            call_count += 1
            # Always return the same categories: same total, only
            # non-pad violation categories change.  No pad-violation
            # growth means no rollback should fire.
            if call_count == 1:
                return {
                    "__total__": 2,
                    "clearance_segment_segment": 1,
                    "clearance_segment_via": 1,
                }
            else:
                return {
                    "__total__": 2,
                    "clearance_segment_segment": 2,
                    "clearance_segment_via": 0,
                }

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count_by_category",
            side_effect=mock_drc,
        ):
            stats = optimize_pcb(
                pcb_path=str(pcb_file),
                output_path=str(output_file),
                optimize_fn=shift_optimize,
                config=config,
            )

        # Non-pad category swap at flat total: rollback should NOT fire.
        assert stats.nets_rolled_back == 0, (
            "Issue #3138 Approach B: gate must remain conservative for "
            "non-pad category swaps to avoid undoing useful optimizations."
        )

    def test_pad_violations_grew_helper(self):
        """Direct unit test for the _pad_violations_grew helper."""
        from kicad_tools.router.optimizer.pcb import _pad_violations_grew

        # No change -- never grows
        assert not _pad_violations_grew({}, {})
        assert not _pad_violations_grew(
            {"clearance_pad_segment": 2},
            {"clearance_pad_segment": 2},
        )

        # Pad-segment growth
        assert _pad_violations_grew(
            {"clearance_pad_segment": 0},
            {"clearance_pad_segment": 1},
        )

        # Pad-via growth
        assert _pad_violations_grew(
            {},
            {"clearance_pad_via": 1},
        )

        # Non-pad category growth does NOT count
        assert not _pad_violations_grew(
            {"clearance_segment_segment": 0},
            {"clearance_segment_segment": 5},
        )

        # Pad-segment shrunk -- does not count as growth
        assert not _pad_violations_grew(
            {"clearance_pad_segment": 5},
            {"clearance_pad_segment": 1},
        )


class TestDrcAwareStatsFields:
    """Test that new stats fields appear correctly."""

    def test_stats_default_values(self):
        """New stats fields should have sensible defaults."""
        from kicad_tools.router.optimizer.config import OptimizationStats

        stats = OptimizationStats()
        assert stats.nets_rolled_back == 0
        assert stats.drc_errors_before == 0
        assert stats.drc_errors_after == 0

    def test_config_drc_defaults(self):
        """DRC config fields should have sensible defaults."""
        from kicad_tools.router.optimizer.config import OptimizationConfig

        config = OptimizationConfig()
        assert config.drc_aware is False
        assert config.drc_manufacturer is None
        assert config.drc_layers == 2
        assert config.drc_copper_oz == 1.0


# ---------------------------------------------------------------------------
# Tests: DRC-aware display message formatting
# ---------------------------------------------------------------------------


class TestDrcAwareDisplayMessage:
    """Test that the DRC summary line correctly reflects error changes."""

    def _make_stats(self, **kwargs):
        """Create an OptimizationStats with given overrides."""
        from kicad_tools.router.optimizer.config import OptimizationStats

        return OptimizationStats(**kwargs)

    def test_drc_output_shows_regression_count(self, simple_pcb_path: Path, capsys):
        """When errors increase, output should show new error count, not 'no regressions'."""
        stats = self._make_stats(
            drc_errors_before=5,
            drc_errors_after=10,
            nets_rolled_back=3,
            nets_optimized=5,
            segments_before=20,
            segments_after=15,
            length_before=100.0,
            length_after=95.0,
        )
        with patch(
            "kicad_tools.router.optimizer.TraceOptimizer.optimize_pcb",
            return_value=stats,
        ):
            result = main(
                [
                    str(simple_pcb_path),
                    "--drc-aware",
                    "--allow-default-rules",
                    "--mfr",
                    "jlcpcb",
                    "--dry-run",
                ]
            )
        assert result == 0
        captured = capsys.readouterr()
        assert "5 new errors" in captured.out
        assert "3 nets rolled back" in captured.out
        assert "no regressions" not in captured.out

    def test_drc_output_shows_no_regressions_with_rollback(self, simple_pcb_path: Path, capsys):
        """When errors do not increase but rollbacks occurred, show both facts."""
        stats = self._make_stats(
            drc_errors_before=5,
            drc_errors_after=3,
            nets_rolled_back=1,
            nets_optimized=4,
            segments_before=20,
            segments_after=15,
            length_before=100.0,
            length_after=95.0,
        )
        with patch(
            "kicad_tools.router.optimizer.TraceOptimizer.optimize_pcb",
            return_value=stats,
        ):
            result = main(
                [
                    str(simple_pcb_path),
                    "--drc-aware",
                    "--allow-default-rules",
                    "--mfr",
                    "jlcpcb",
                    "--dry-run",
                ]
            )
        assert result == 0
        captured = capsys.readouterr()
        assert "no regressions" in captured.out
        assert "1 nets rolled back" in captured.out
        assert "new errors" not in captured.out

    def test_drc_output_shows_no_regressions_without_rollback(self, simple_pcb_path: Path, capsys):
        """When errors unchanged and no rollbacks, show simple 'no regressions'."""
        stats = self._make_stats(
            drc_errors_before=5,
            drc_errors_after=5,
            nets_rolled_back=0,
            nets_optimized=4,
            segments_before=20,
            segments_after=15,
            length_before=100.0,
            length_after=95.0,
        )
        with patch(
            "kicad_tools.router.optimizer.TraceOptimizer.optimize_pcb",
            return_value=stats,
        ):
            result = main(
                [
                    str(simple_pcb_path),
                    "--drc-aware",
                    "--allow-default-rules",
                    "--mfr",
                    "jlcpcb",
                    "--dry-run",
                ]
            )
        assert result == 0
        captured = capsys.readouterr()
        assert "(no regressions)" in captured.out
        assert "nets rolled back" not in captured.out
        assert "new errors" not in captured.out

    def test_drc_output_shows_no_regressions_when_errors_decrease(
        self, simple_pcb_path: Path, capsys
    ):
        """When errors decrease with no rollbacks, show simple 'no regressions'."""
        stats = self._make_stats(
            drc_errors_before=10,
            drc_errors_after=7,
            nets_rolled_back=0,
            nets_optimized=4,
            segments_before=20,
            segments_after=15,
            length_before=100.0,
            length_after=95.0,
        )
        with patch(
            "kicad_tools.router.optimizer.TraceOptimizer.optimize_pcb",
            return_value=stats,
        ):
            result = main(
                [
                    str(simple_pcb_path),
                    "--drc-aware",
                    "--allow-default-rules",
                    "--mfr",
                    "jlcpcb",
                    "--dry-run",
                ]
            )
        assert result == 0
        captured = capsys.readouterr()
        assert "(no regressions)" in captured.out
        assert "new errors" not in captured.out

    def test_drc_output_error_count_is_correct_delta(self, simple_pcb_path: Path, capsys):
        """The new error count should be exactly drc_errors_after - drc_errors_before."""
        stats = self._make_stats(
            drc_errors_before=79,
            drc_errors_after=120,
            nets_rolled_back=3,
            nets_optimized=10,
            segments_before=200,
            segments_after=150,
            length_before=500.0,
            length_after=480.0,
        )
        with patch(
            "kicad_tools.router.optimizer.TraceOptimizer.optimize_pcb",
            return_value=stats,
        ):
            result = main(
                [
                    str(simple_pcb_path),
                    "--drc-aware",
                    "--allow-default-rules",
                    "--mfr",
                    "jlcpcb",
                    "--dry-run",
                ]
            )
        assert result == 0
        captured = capsys.readouterr()
        # delta = 120 - 79 = 41
        assert "41 new errors" in captured.out
        assert "3 nets rolled back" in captured.out


# ---------------------------------------------------------------------------
# Tests: --verbose per-net rows and stats.per_net (Issue #7065)
# ---------------------------------------------------------------------------


class TestVerbosePerNet:
    def test_verbose_prints_row_for_known_net(self, simple_pcb_path, capsys):
        rc = main([str(simple_pcb_path), "--dry-run", "--verbose"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Per-net statistics:" in out
        row = next(line for line in out.splitlines() if "NET1" in line and "segments" in line)
        assert "2 ->    1" in row

    def test_non_verbose_output_has_no_rows(self, simple_pcb_path, capsys):
        rc = main([str(simple_pcb_path), "--dry-run"])
        assert rc == 0
        assert "Per-net statistics" not in capsys.readouterr().out

    def test_json_always_carries_per_net_and_single_document(self, simple_pcb_path, capsys):
        import json

        for extra in ([], ["--verbose"]):
            rc = main([str(simple_pcb_path), "--dry-run", "--format", "json", *extra])
            assert rc == 0
            data = json.loads(capsys.readouterr().out)
            rows = {r["net"]: r for r in data["stats"]["per_net"]}
            assert rows["NET1"]["segments_before"] == 2
            assert rows["NET1"]["segments_after"] == 1
            assert rows["NET1"]["rolled_back"] is False
            assert data["stats"]["nets_optimized"] == 1


# ---------------------------------------------------------------------------
# Tests: per-rewrite collision checking (Issue #8214)
# ---------------------------------------------------------------------------


class _BlockAll:
    """Collision checker that refuses every candidate path."""

    def path_is_clear(self, **kwargs) -> bool:
        return False


class TestCollisionCheckedRewrites:
    def test_cli_passes_board_checker_to_optimizer(self, simple_pcb_path, monkeypatch):
        seen = {}
        from kicad_tools.router.optimizer import TraceOptimizer as Real

        def spy(config=None, collision_checker=None, net_anchors=None):
            seen["checker"] = collision_checker
            seen["anchors"] = net_anchors
            return Real(config, collision_checker=collision_checker, net_anchors=net_anchors)

        monkeypatch.setattr("kicad_tools.router.optimizer.TraceOptimizer", spy)
        assert main([str(simple_pcb_path), "--dry-run", "--quiet"]) == 0
        assert seen["checker"] is not None
        assert seen["anchors"], "detour pass needs pad/via anchors (Issue #8217)"

    def test_clear_path_is_still_merged(self, simple_pcb_path, capsys):
        assert main([str(simple_pcb_path), "--dry-run", "--verbose"]) == 0
        row = next(
            ln for ln in capsys.readouterr().out.splitlines() if "NET1" in ln and "segments" in ln
        )
        assert "2 ->    1" in row

    def test_blocked_rewrite_is_skipped(self, simple_pcb_path, monkeypatch, capsys):
        from kicad_tools.cli import optimize_cmd

        real_build = optimize_cmd._build_collision_checker
        monkeypatch.setattr(
            "kicad_tools.cli.optimize_cmd._build_collision_checker",
            lambda p: (real_build(p)[0], _BlockAll()),
        )
        assert main([str(simple_pcb_path), "--dry-run", "--verbose"]) == 0
        row = next(
            ln for ln in capsys.readouterr().out.splitlines() if "NET1" in ln and "segments" in ln
        )
        assert "2 ->    2" in row

    def test_checker_build_failure_is_reported_not_bypassed(
        self, simple_pcb_path, monkeypatch, capsys
    ):
        def boom(p):
            raise ValueError("unparseable rules")

        monkeypatch.setattr("kicad_tools.cli.optimize_cmd._build_collision_checker", boom)
        out = simple_pcb_path.parent / "out.kicad_pcb"
        assert main([str(simple_pcb_path), "-o", str(out)]) == 1
        assert "unparseable rules" in capsys.readouterr().err
        assert not out.exists()


def _name_only(text: str) -> str:
    """SIMPLE_PCB as a KiCad-10 name-only board: no header net table."""
    import re

    text = re.sub(r'\t\(net \d+ "[^"]*"\)\n', "", text)
    text = text.replace('(net 1 "NET1")', '(net "NET1")').replace('(net 2 "GND")', '(net "GND")')
    return text.replace("(net 1)", '(net "NET1")')


class TestNameOnlyBoardAndGridSync:
    def test_name_only_board_still_merges_safe_collinear(self, tmp_path, capsys):
        pcb = tmp_path / "name_only.kicad_pcb"
        pcb.write_text(_name_only(SIMPLE_PCB))
        assert "(net 1" not in pcb.read_text()
        assert main([str(pcb), "--dry-run", "--verbose"]) == 0
        row = next(
            ln for ln in capsys.readouterr().out.splitlines() if "NET1" in ln and "segments" in ln
        )
        assert "2 ->    1" in row

    @staticmethod
    def _run_shifting_rewrite(pcb_path, monkeypatch, veto: bool):
        """Run optimize_pcb with a base optimizer that moves NET1 copper 4 mm.

        Returns a ``clear(y)`` probe: would a foreign net's path at row ``y``
        be accepted by the checker the later nets are checked against?
        """
        from dataclasses import replace

        from kicad_tools.cli.optimize_cmd import (
            _build_collision_checker,
            _make_grid_synced_optimize_fn,
        )
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
        from kicad_tools.router.optimizer.kelvin_gate import KelvinBoardGate

        router, checker = _build_collision_checker(pcb_path)
        optimizer = TraceOptimizer(OptimizationConfig(), collision_checker=checker)
        optimizer.optimize_segments = lambda segs: [
            replace(s, y1=s.y1 + 4, y2=s.y2 + 4) for s in segs
        ]
        optimize, commit = _make_grid_synced_optimize_fn(router, optimizer)
        optimizer.optimize_segments = optimize

        if veto:
            # A Kelvin gate that runs the rewrite and then keeps the original
            # branch (the #6889 / #5398 veto).
            class _Vetoing:
                class gate:  # noqa: N801
                    vetoes = 0

                    @staticmethod
                    def report():
                        pass

                @staticmethod
                def optimize_net(net_name, segments, optimize_fn):
                    optimize_fn(list(segments))
                    return list(segments)

            monkeypatch.setattr(
                KelvinBoardGate, "from_pcb_text", classmethod(lambda cls, text: _Vetoing())
            )

        optimizer.optimize_pcb(str(pcb_path), dry_run=True, on_net_optimized=commit)

        def clear(y: float) -> bool:
            return checker.path_is_clear(
                x1=105, y1=y, x2=110, y2=y, layer=Layer.F_CU, width=0.25, exclude_net=999
            )

        return clear

    def test_accepted_rewrite_is_synced_into_the_grid(self, simple_pcb_path, monkeypatch):
        clear = self._run_shifting_rewrite(simple_pcb_path, monkeypatch, veto=False)
        assert clear(50), "old copper must be unmarked once the rewrite is kept"
        assert not clear(54), "new copper must be marked for later nets"

    def test_vetoed_rewrite_is_not_committed_to_the_grid(self, simple_pcb_path, monkeypatch):
        clear = self._run_shifting_rewrite(simple_pcb_path, monkeypatch, veto=True)
        assert not clear(50), "vetoed branch's original copper must stay blocked"
        assert clear(54), "rejected rewrite's copper must not be marked"


# ---------------------------------------------------------------------------
# Issue #8217: detour pass reachable from optimize-traces
# ---------------------------------------------------------------------------

_TENT_PCB = (
    SIMPLE_PCB.split("\t(segment")[0]
    + "".join(
        f'\t(segment (start {a}) (end {b}) (width 0.25) (layer "F.Cu") (net 1) (uuid "t{i}"))\n'
        for i, (a, b) in enumerate(
            [
                ("100.8 50", "105 50"),
                ("105 50", "108 47"),
                ("108 47", "122 47"),
                ("122 47", "125 50"),
                ("125 50", "129.2 50"),
            ]
        )
    )
    + ")\n"
)

# A foreign-net via sitting on the straight path between the two pads.
_OBSTACLE = (
    '\t(via (at 115 50) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2) (uuid "obs"))\n'
)


def _tent_length(pcb_path: Path) -> float:
    import math
    import re

    text = pcb_path.read_text()
    total = 0.0
    num = r"(-?[\d.]+)"
    pat = rf"\(segment\s+\(start {num} {num}\)\s+\(end {num} {num}\)"
    for m in re.finditer(pat, text):
        x1, y1, x2, y2 = map(float, m.groups())
        total += math.hypot(x2 - x1, y2 - y1)
    return total


class TestDetourShortening:
    def test_tent_detour_is_shortened(self, tmp_path):
        pcb = tmp_path / "tent.kicad_pcb"
        pcb.write_text(_TENT_PCB)
        before = _tent_length(pcb)
        assert main([str(pcb), "--quiet"]) == 0
        assert _tent_length(pcb) < before - 1.0

    def test_obstacle_on_shorter_path_leaves_tent_longer(self, tmp_path):
        free = tmp_path / "free.kicad_pcb"
        free.write_text(_TENT_PCB)
        blocked = tmp_path / "blocked.kicad_pcb"
        blocked.write_text(_TENT_PCB.rstrip().rstrip(")") + _OBSTACLE + ")\n")
        assert main([str(free), "--quiet"]) == 0
        assert main([str(blocked), "--quiet"]) == 0
        assert _tent_length(blocked) > _tent_length(free) + 1.0


class TestDeclaredDesignRules:
    """Issue #8269: the optimizer's checker uses the clearance the board's own
    files declare, not the 0.20 mm dataclass default."""

    @staticmethod
    def _board(tmp_path, clearance=None):
        import json

        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator "t") (general (thickness 1.6)) '
            '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)) '
            "(setup (pad_to_mask_clearance 0)))"
        )
        if clearance is not None:
            (tmp_path / "b.kicad_pro").write_text(
                json.dumps(
                    {
                        "board": {"design_settings": {"rules": {"min_clearance": clearance}}},
                        "net_settings": {
                            "classes": [{"name": "Default", "clearance": clearance}],
                            "meta": {"version": 3},
                        },
                    }
                )
            )
        return pcb

    def test_project_clearance_replaces_the_dataclass_default(self, tmp_path):
        from kicad_tools.cli.optimize_cmd import _declared_design_rules

        rules = _declared_design_rules(self._board(tmp_path, 0.1016))
        assert rules is not None
        assert rules.trace_clearance == pytest.approx(0.1016)
        assert rules.via_clearance == pytest.approx(0.1016)
        assert rules.grid_resolution <= 0.1016 / 2 + 1e-9

    def test_nothing_declared_keeps_the_loader_behaviour(self, tmp_path):
        from kicad_tools.cli.optimize_cmd import _declared_design_rules

        assert _declared_design_rules(self._board(tmp_path)) is None
