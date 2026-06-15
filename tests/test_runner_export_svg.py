"""Regression tests for ``run_pcb_export_svg`` (issue #3695).

KiCad 10 removed the raster ``pcb export png`` subcommand, so the 2D layer
plots are produced as SVGs via ``pcb export svg --mode-single``. These tests
mock the subprocess call and assert the command array carries the verified
KiCad-10 flags.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from kicad_tools.cli.runner import run_pcb_export_svg

_RUNNER = "kicad_tools.cli.runner"


def test_export_svg_command_uses_svg_subcommand_and_flags(tmp_path: Path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    out = tmp_path / "pcb-front.svg"

    def fake_run(cmd, *args, **kwargs):
        # Simulate kicad-cli writing a non-empty SVG.
        out.write_text("<svg></svg>")
        fake_run.cmd = cmd
        return MagicMock(returncode=0, stdout="Plotted to ...", stderr="")

    with patch(f"{_RUNNER}.subprocess.run", side_effect=fake_run):
        res = run_pcb_export_svg(
            pcb,
            out,
            ["F.Cu", "F.Silkscreen", "Edge.Cuts"],
            kicad_cli=Path("/usr/bin/kicad-cli"),
        )

    assert res.success
    assert res.output_path == out

    cmd = fake_run.cmd
    # Uses the SVG export subcommand, not the removed raster png export.
    assert "svg" in cmd
    assert "png" not in cmd
    assert cmd[cmd.index("svg") - 1] == "export"
    # Single-file mode + board-fit flags verified on kicad-cli 10.0.1.
    assert "--mode-single" in cmd
    assert "--fit-page-to-board" in cmd
    assert "--page-size-mode" in cmd
    assert cmd[cmd.index("--page-size-mode") + 1] == "2"
    # Layers are passed comma-joined.
    assert "--layers" in cmd
    assert cmd[cmd.index("--layers") + 1] == "F.Cu,F.Silkscreen,Edge.Cuts"
    # Output path and PCB are present.
    assert str(out) in cmd
    assert str(pcb) in cmd


def test_export_svg_passes_black_and_white_and_theme(tmp_path: Path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    out = tmp_path / "pcb-back.svg"

    def fake_run(cmd, *args, **kwargs):
        out.write_text("<svg></svg>")
        fake_run.cmd = cmd
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(f"{_RUNNER}.subprocess.run", side_effect=fake_run):
        res = run_pcb_export_svg(
            pcb,
            out,
            ["B.Cu", "B.Silkscreen", "Edge.Cuts"],
            black_and_white=True,
            theme="KiCad Classic",
            kicad_cli=Path("/usr/bin/kicad-cli"),
        )

    assert res.success
    cmd = fake_run.cmd
    assert "--black-and-white" in cmd
    assert "--theme" in cmd
    assert cmd[cmd.index("--theme") + 1] == "KiCad Classic"


def test_export_svg_reports_failure_when_no_output(tmp_path: Path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    out = tmp_path / "missing.svg"  # never created by the fake

    def fake_run(cmd, *args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(f"{_RUNNER}.subprocess.run", side_effect=fake_run):
        res = run_pcb_export_svg(
            pcb,
            out,
            ["F.Cu"],
            kicad_cli=Path("/usr/bin/kicad-cli"),
        )

    assert not res.success
    assert "SVG export produced no output" in res.stderr
