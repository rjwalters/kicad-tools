"""Integration tests for optimize-traces via the centralized CLI parser.

These tests exercise the full path:
  kicad_tools.cli.main(["optimize-traces", ...])
    -> parser.py (_add_optimize_parser)
    -> routing.py (run_optimize_command)
    -> optimize_cmd.main(sub_argv)

This catches regressions where the centralized parser is missing arguments
that the standalone optimize_cmd.main parser accepts (the bug fixed by #1270).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import main as cli_main

# ---------------------------------------------------------------------------
# Fixtures: Minimal KiCad PCB file
# ---------------------------------------------------------------------------

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
# Tests: centralized CLI parser accepts DRC-aware flags
# ---------------------------------------------------------------------------


class TestCentralizedCliDrcAwareFlags:
    """Test that --drc-aware flags work through the centralized CLI path."""

    def test_drc_aware_with_mfr_dry_run_succeeds(self, simple_pcb_path: Path):
        """The centralized CLI should accept --drc-aware --mfr and forward them."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--drc-aware",
                "--mfr",
                "jlcpcb",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0

    def test_drc_aware_with_all_flags(self, simple_pcb_path: Path, tmp_path: Path):
        """All four DRC-aware flags should be accepted and forwarded."""
        output = tmp_path / "out.kicad_pcb"
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--drc-aware",
                "--mfr",
                "jlcpcb",
                "--layers",
                "4",
                "--copper",
                "2.0",
                "-o",
                str(output),
                "--quiet",
            ]
        )
        assert result == 0
        assert output.exists()

    def test_drc_aware_without_mfr_returns_error(self, simple_pcb_path: Path):
        """--drc-aware without --mfr should return exit code 1 via centralized CLI."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--drc-aware",
                "--quiet",
            ]
        )
        assert result == 1

    def test_layers_default_not_forwarded(self, simple_pcb_path: Path):
        """--layers at default (2) should still work; optimize_cmd gets its own default."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--drc-aware",
                "--mfr",
                "jlcpcb",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0

    def test_copper_default_not_forwarded(self, simple_pcb_path: Path):
        """--copper at default (1.0) should still work."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--drc-aware",
                "--mfr",
                "jlcpcb",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0


# ---------------------------------------------------------------------------
# Tests: --help output includes DRC-aware flags
# ---------------------------------------------------------------------------


class TestCentralizedCliHelpOutput:
    """Test that --help output includes the DRC-aware flags."""

    def test_help_shows_drc_aware_flag(self, capsys):
        """kct optimize-traces --help should mention --drc-aware."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["optimize-traces", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--drc-aware" in captured.out

    def test_help_shows_mfr_flag(self, capsys):
        """kct optimize-traces --help should mention --mfr."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["optimize-traces", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--mfr" in captured.out

    def test_help_shows_layers_flag(self, capsys):
        """kct optimize-traces --help should mention --layers."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["optimize-traces", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--layers" in captured.out

    def test_help_shows_copper_flag(self, capsys):
        """kct optimize-traces --help should mention --copper."""
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["optimize-traces", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--copper" in captured.out


# ---------------------------------------------------------------------------
# Tests: no regression in pre-existing flags
# ---------------------------------------------------------------------------


class TestCentralizedCliExistingFlags:
    """Test that pre-existing optimize-traces flags still work."""

    def test_basic_optimize_dry_run(self, simple_pcb_path: Path):
        """Basic optimize-traces --dry-run should still work."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0

    def test_no_merge_flag(self, simple_pcb_path: Path):
        """--no-merge should still work via centralized CLI."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--no-merge",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0

    def test_net_filter_flag(self, simple_pcb_path: Path):
        """--net should still work via centralized CLI."""
        result = cli_main(
            [
                "optimize-traces",
                str(simple_pcb_path),
                "--net",
                "NET1",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 0
