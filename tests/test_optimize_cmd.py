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
\t\t(pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET1"))
\t\t(pad "2" smd roundrect (at 0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
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

        def mock_drc_error_count(pcb_text, manufacturer, layers, copper_oz):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Baseline: 0 errors
                return 0
            elif call_count == 2:
                # After full optimization: 3 errors (bad!)
                return 3
            else:
                # Reverting the net: 0 errors (good!)
                return 0

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count",
            side_effect=mock_drc_error_count,
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

        def mock_drc(pcb_text, manufacturer, layers, copper_oz):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 2  # baseline: 2 errors
            elif call_count == 2:
                return 5  # after optimization: 5 errors (worse)
            else:
                return 2  # after reverting: back to 2 (good)

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count",
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

        def mock_drc(pcb_text, manufacturer, layers, copper_oz):
            # Always return 0 errors -- optimization is safe
            return 0

        config = OptimizationConfig(
            drc_aware=True,
            drc_manufacturer="jlcpcb",
            drc_layers=2,
        )

        with patch(
            "kicad_tools.router.optimizer.pcb._run_drc_error_count",
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
            result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "jlcpcb", "--dry-run"])
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
            result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "jlcpcb", "--dry-run"])
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
            result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "jlcpcb", "--dry-run"])
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
            result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "jlcpcb", "--dry-run"])
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
            result = main([str(simple_pcb_path), "--drc-aware", "--mfr", "jlcpcb", "--dry-run"])
        assert result == 0
        captured = capsys.readouterr()
        # delta = 120 - 79 = 41
        assert "41 new errors" in captured.out
        assert "3 nets rolled back" in captured.out
