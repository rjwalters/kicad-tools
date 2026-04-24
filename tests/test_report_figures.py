"""Tests for kicad_tools.report.figures — ReportFigureGenerator."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.report.figures import (
    REPORT_MAX_SIZE_PX,
    FigureEntry,
    ReportFigureGenerator,
    _BLANK_SVG_THRESHOLD_BYTES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_result(**overrides):
    """Return a dict matching the shape of ``screenshot_board`` success."""
    base = {
        "success": True,
        "image_base64": "AAAA",
        "width_px": 800,
        "height_px": 600,
        "layers_rendered": ["F.Cu"],
        "output_path": "/tmp/out.png",
        "error_message": None,
    }
    base.update(overrides)
    return base


def _make_failure_result(msg: str = "render failed"):
    """Return a dict matching ``screenshot_board`` failure."""
    return {
        "success": False,
        "image_base64": None,
        "width_px": 0,
        "height_px": 0,
        "layers_rendered": [],
        "output_path": None,
        "error_message": msg,
    }


#: Default SVG size for "valid" (non-blank) schematic sheets in tests.
#: Must exceed ``_BLANK_SVG_THRESHOLD_BYTES`` so sheets are not excluded.
_VALID_SVG_SIZE = _BLANK_SVG_THRESHOLD_BYTES + 50_000

#: Default SVG size for "blank" (title-block-only) schematic sheets in tests.
#: Must be below ``_BLANK_SVG_THRESHOLD_BYTES`` so sheets are excluded.
_BLANK_SVG_SIZE = _BLANK_SVG_THRESHOLD_BYTES - 50_000


def _write_svg_stub(path: Path, size_bytes: int = _VALID_SVG_SIZE) -> None:
    """Write an SVG stub file of approximately *size_bytes*."""
    header = b"<svg>"
    padding = max(0, size_bytes - len(header))
    path.write_bytes(header + b" " * padding)


def _svg_to_png_side_effect(size_bytes: int = 100_000):
    """Return a side_effect callable for ``_svg_to_png`` that writes a
    PNG stub of *size_bytes* to the output path.
    """

    def _side_effect(svg_path, png_path, max_size_px=3000):
        png_path.write_bytes(b"\x00" * size_bytes)
        return (True, "", 2000, 1500)

    return _side_effect


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestReportFigureGeneratorDefaults:
    """Verify constructor defaults."""

    def test_default_max_size(self):
        gen = ReportFigureGenerator()
        assert gen.max_size_px == REPORT_MAX_SIZE_PX
        assert gen.max_size_px == 3000

    def test_custom_max_size(self):
        gen = ReportFigureGenerator(max_size_px=2400)
        assert gen.max_size_px == 2400


class TestGenerateAll:
    """Unit tests for ``generate_all`` with mocked dependencies."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_generate_all_happy_path(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        """All five figure types generated successfully."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        # PCB calls all succeed
        mock_board.return_value = _make_success_result()

        # Schematic: simulate kicad-cli producing two SVGs
        def fake_subprocess_run(cmd, **kwargs):
            # Write two SVG files into the temp directory.
            # The --output arg is at index 4 in the command list.
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "main_sheet.svg")
            _write_svg_stub(svg_dir / "power_sheet.svg")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        entries = gen.generate_all(pcb, sch, out_dir)

        # 4 PCB + 2 schematic = 6
        assert len(entries) == 6

        # Check PCB entries
        pcb_entries = [e for e in entries if e.figure_type != "schematic"]
        assert len(pcb_entries) == 4
        pcb_types = {e.figure_type for e in pcb_entries}
        assert pcb_types == {"pcb_front", "pcb_back", "pcb_copper", "assembly"}

        pcb_filenames = {e.filename for e in pcb_entries}
        assert pcb_filenames == {"pcb_front.png", "pcb_back.png", "pcb_copper.png", "assembly.png"}

        # Check schematic entries
        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 2
        sch_filenames = {e.filename for e in sch_entries}
        assert sch_filenames == {"schematic_main_sheet.png", "schematic_power_sheet.png"}

        # Verify captions
        for e in sch_entries:
            assert e.caption.startswith("Schematic: ")

        # Verify screenshot_board was called with correct presets
        assert mock_board.call_count == 4

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_partial_pcb_failure(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """One PCB preset fails; others still generated."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        # First call (front) fails, rest succeed
        mock_board.side_effect = [
            _make_failure_result("front render failed"),
            _make_success_result(),
            _make_success_result(),
            _make_success_result(),
        ]

        # No schematic SVGs produced
        mock_subprocess.return_value = MagicMock(returncode=0, stderr="")

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            entries = gen.generate_all(pcb, sch, out_dir)

        # 3 PCB (one failed) + 0 schematic
        assert len(entries) == 3
        assert all(e.figure_type != "pcb_front" for e in entries)

        # Warning should be logged
        assert "front render failed" in caplog.text

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_schematic_svg_conversion_failure(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """One schematic SVG fails to convert; the other succeeds."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "good_sheet.svg")
            _write_svg_stub(svg_dir / "bad_sheet.svg")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run

        # First SVG fails PNG conversion, second succeeds
        def _mixed_side_effect(svg_path, png_path, max_size_px=3000):
            if "bad_sheet" in str(svg_path):
                return (False, "bad conversion", 0, 0)
            png_path.write_bytes(b"\x00" * 100_000)
            return (True, "", 2000, 1500)

        mock_svg_to_png.side_effect = _mixed_side_effect

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            entries = gen.generate_all(pcb, sch, out_dir)

        # 4 PCB + 1 successful schematic
        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 1
        assert sch_entries[0].filename == "schematic_good_sheet.png"
        assert "bad conversion" in caplog.text


class TestMissingDependencies:
    """Tests for missing kicad-cli and cairosvg."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=None)
    def test_missing_kicad_cli_raises(self, mock_find_cli, mock_cairosvg, tmp_path):
        gen = ReportFigureGenerator()
        with pytest.raises(RuntimeError, match="kicad-cli not found"):
            gen.generate_all(
                tmp_path / "board.kicad_pcb",
                tmp_path / "main.kicad_sch",
                tmp_path / "out",
            )

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=False)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    def test_missing_cairosvg_raises(self, mock_find_cli, mock_cairosvg, tmp_path):
        gen = ReportFigureGenerator()
        with pytest.raises(RuntimeError, match="cairosvg is required"):
            gen.generate_all(
                tmp_path / "board.kicad_pcb",
                tmp_path / "main.kicad_sch",
                tmp_path / "out",
            )


class TestCheckCairosvgProbe:
    """Tests for _check_cairosvg() native library probe."""

    def test_returns_false_when_svg2png_raises_os_error(self):
        """_check_cairosvg() returns False when cairosvg.svg2png raises OSError
        (native cairo library missing)."""
        import types

        fake_cairosvg = types.ModuleType("cairosvg")

        def _raise_os_error(**kwargs):
            raise OSError('no library called "cairo-2" was found')

        fake_cairosvg.svg2png = _raise_os_error

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            from kicad_tools.mcp.tools.screenshot import _check_cairosvg

            assert _check_cairosvg() is False

    def test_returns_false_when_import_fails(self):
        """_check_cairosvg() returns False when cairosvg cannot be imported."""
        with patch.dict(sys.modules, {"cairosvg": None}):
            from kicad_tools.mcp.tools.screenshot import _check_cairosvg

            assert _check_cairosvg() is False

    def test_returns_true_when_probe_succeeds(self):
        """_check_cairosvg() returns True when cairosvg.svg2png succeeds."""
        import types

        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: b"\x89PNG"

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            from kicad_tools.mcp.tools.screenshot import _check_cairosvg

            assert _check_cairosvg() is True


class TestMultiSheetSchematics:
    """Verify multi-sheet schematic handling."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_three_sheets(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        """Three schematic sheets produce three FigureEntry objects."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "main.svg")
            _write_svg_stub(svg_dir / "logic.svg")
            _write_svg_stub(svg_dir / "output.svg")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        entries = gen.generate_all(pcb, sch, out_dir)

        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 3
        assert {e.filename for e in sch_entries} == {
            "schematic_main.png",
            "schematic_logic.png",
            "schematic_output.png",
        }


class TestFilenameDeterminism:
    """Verify deterministic output filenames."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_same_filenames_on_repeated_calls(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        """Running generate_all twice produces identical manifest filenames."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "sheet1.svg")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        entries1 = gen.generate_all(pcb, sch, out_dir)
        entries2 = gen.generate_all(pcb, sch, out_dir)

        filenames1 = [e.filename for e in entries1]
        filenames2 = [e.filename for e in entries2]
        assert filenames1 == filenames2


class TestFigureEntry:
    """Verify FigureEntry dataclass behavior."""

    def test_fields(self):
        entry = FigureEntry(
            filename="pcb_front.png",
            caption="PCB Front",
            figure_type="pcb_front",
        )
        assert entry.filename == "pcb_front.png"
        assert entry.caption == "PCB Front"
        assert entry.figure_type == "pcb_front"

    def test_equality(self):
        a = FigureEntry("f.png", "cap", "pcb_front")
        b = FigureEntry("f.png", "cap", "pcb_front")
        assert a == b

    def test_repr(self):
        entry = FigureEntry("f.png", "cap", "pcb_front")
        r = repr(entry)
        assert "f.png" in r
        assert "pcb_front" in r


class TestMaxSizePassedThrough:
    """Verify max_size_px is forwarded to screenshot_board and _svg_to_png."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_custom_max_size_forwarded(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "sheet.svg")
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator(max_size_px=2400)
        gen.generate_all(pcb, sch, out_dir)

        # Verify screenshot_board received max_size_px=2400
        for call in mock_board.call_args_list:
            assert call.kwargs.get("max_size_px") == 2400 or call[1].get("max_size_px") == 2400

        # Verify _svg_to_png received max_size_px=2400
        for call in mock_svg_to_png.call_args_list:
            # Third positional arg is max_size_px
            assert call[0][2] == 2400


class TestOutputDirCreation:
    """Verify output directory is created if missing."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_output_dir_created(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "nested" / "figures"

        mock_board.return_value = _make_success_result()
        mock_subprocess.return_value = MagicMock(returncode=0, stderr="")

        gen = ReportFigureGenerator()
        gen.generate_all(pcb, sch, out_dir)

        assert out_dir.exists()
        assert out_dir.is_dir()


class TestBlankSchematicDetection:
    """Verify that blank schematic sheets are detected via SVG file size
    and excluded from the manifest before PNG conversion."""

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_blank_sheet_excluded(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """A schematic SVG below the size threshold is excluded with a warning."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            # Write a small SVG (below threshold) to simulate a blank sheet
            _write_svg_stub(svg_dir / "blank_sheet.svg", size_bytes=_BLANK_SVG_SIZE)
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            entries = gen.generate_all(pcb, sch, out_dir)

        # Only PCB entries, no schematic
        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 0
        assert "Excluding blank schematic sheet" in caplog.text
        assert "blank_sheet" in caplog.text
        assert "SVG size" in caplog.text

        # PNG conversion should NOT have been called for the blank sheet
        mock_svg_to_png.assert_not_called()

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_mixed_blank_and_valid_sheets(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """Blank sheets are excluded while valid sheets are kept."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "dac.svg", size_bytes=_BLANK_SVG_SIZE)     # blank
            _write_svg_stub(svg_dir / "power.svg", size_bytes=_VALID_SVG_SIZE)   # valid
            _write_svg_stub(svg_dir / "mcu.svg", size_bytes=_BLANK_SVG_SIZE)     # blank
            _write_svg_stub(svg_dir / "sync.svg", size_bytes=_VALID_SVG_SIZE)    # valid
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            entries = gen.generate_all(pcb, sch, out_dir)

        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 2
        sch_filenames = {e.filename for e in sch_entries}
        assert sch_filenames == {"schematic_power.png", "schematic_sync.png"}

        # Warnings logged for blank sheets
        assert "dac" in caplog.text
        assert "mcu" in caplog.text

        # PNG conversion should only be called for the two valid sheets
        assert mock_svg_to_png.call_count == 2

        # Summary warning should be logged
        assert "Excluded 2 of 4 schematic sheets as blank" in caplog.text

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_valid_sheet_above_threshold_included(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
    ):
        """A schematic SVG at or above the threshold is included normally."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            # Write an SVG exactly at the threshold -- should be included
            _write_svg_stub(
                svg_dir / "valid_sheet.svg",
                size_bytes=_BLANK_SVG_THRESHOLD_BYTES,
            )
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        entries = gen.generate_all(pcb, sch, out_dir)

        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 1
        assert sch_entries[0].filename == "schematic_valid_sheet.png"

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_all_blank_sheets_produces_empty_manifest(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """When all schematic sheets are blank, no schematic entries are returned."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "sheet_a.svg", size_bytes=_BLANK_SVG_SIZE)
            _write_svg_stub(svg_dir / "sheet_b.svg", size_bytes=_BLANK_SVG_SIZE)
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            entries = gen.generate_all(pcb, sch, out_dir)

        sch_entries = [e for e in entries if e.figure_type == "schematic"]
        assert len(sch_entries) == 0
        # PCB entries still present
        pcb_entries = [e for e in entries if e.figure_type != "schematic"]
        assert len(pcb_entries) == 4
        # Warnings for both sheets
        assert "sheet_a" in caplog.text
        assert "sheet_b" in caplog.text

        # PNG conversion should NOT have been called at all
        mock_svg_to_png.assert_not_called()

        # Summary warning should be logged
        assert "Excluded 2 of 2 schematic sheets as blank" in caplog.text

    @patch("kicad_tools.report.figures._check_cairosvg", return_value=True)
    @patch("kicad_tools.report.figures.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli"))
    @patch("kicad_tools.report.figures.subprocess.run")
    @patch("kicad_tools.report.figures._svg_to_png")
    @patch("kicad_tools.report.figures.screenshot_board")
    def test_summary_warning_logged(
        self,
        mock_board,
        mock_svg_to_png,
        mock_subprocess,
        mock_find_cli,
        mock_cairosvg,
        tmp_path,
        caplog,
    ):
        """Summary warning is logged after the loop when sheets are excluded."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.touch()
        sch = tmp_path / "main.kicad_sch"
        sch.touch()
        out_dir = tmp_path / "figures"

        mock_board.return_value = _make_success_result()

        def fake_subprocess_run(cmd, **kwargs):
            output_idx = cmd.index("--output") + 1
            svg_dir = Path(cmd[output_idx])
            _write_svg_stub(svg_dir / "blank.svg", size_bytes=_BLANK_SVG_SIZE)
            _write_svg_stub(svg_dir / "valid1.svg", size_bytes=_VALID_SVG_SIZE)
            _write_svg_stub(svg_dir / "valid2.svg", size_bytes=_VALID_SVG_SIZE)
            return MagicMock(returncode=0, stderr="")

        mock_subprocess.side_effect = fake_subprocess_run
        mock_svg_to_png.side_effect = _svg_to_png_side_effect()

        gen = ReportFigureGenerator()
        with caplog.at_level(logging.WARNING):
            gen.generate_all(pcb, sch, out_dir)

        assert "Excluded 1 of 3 schematic sheets as blank" in caplog.text


class TestReportPackageInit:
    """Verify the report package re-exports."""

    def test_import_from_package(self):
        from kicad_tools.report import FigureEntry, ReportFigureGenerator

        assert FigureEntry is not None
        assert ReportFigureGenerator is not None
