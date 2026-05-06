"""Tests for the safety-net zone-fill in Gerber export (issue #2516).

When a PCB defines ``(zone ...)`` blocks but contains no ``filled_polygon``
children, the resulting Gerbers would lack ``G36..G37`` polygon-fill regions
and the manufactured board would have no plane copper.

The fix in :meth:`GerberExporter._export_gerbers` performs a safety-net fill
into a temp file before invoking ``kicad-cli pcb export gerbers`` so the
source PCB is never silently mutated.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.export.gerber import GerberConfig, GerberExporter, _pcb_has_unfilled_zones

# ---------------------------------------------------------------------------
# _pcb_has_unfilled_zones unit tests
# ---------------------------------------------------------------------------


class TestPcbHasUnfilledZones:
    """Verify the cheap text-scan that drives the safety-net fill."""

    def test_returns_false_for_no_zones(self, tmp_path):
        pcb = tmp_path / "no_zone.kicad_pcb"
        pcb.write_text('(kicad_pcb (version 20240108) (net 0 ""))\n')
        assert _pcb_has_unfilled_zones(pcb) is False

    def test_returns_true_for_unfilled_zone(self, tmp_path):
        pcb = tmp_path / "unfilled.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb\n"
            '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")\n'
            "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))))\n"
        )
        assert _pcb_has_unfilled_zones(pcb) is True

    def test_returns_true_for_multiline_zone_format(self, tmp_path):
        """KiCad's serializer wraps zones with a newline after ``(zone``.

        Regression test for the bug where the original "(zone " (with
        trailing space) text scan missed the multi-line form actually
        emitted by ``kct route``'s zone generator.
        """
        pcb = tmp_path / "multiline.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb\n"
            "\t(zone\n"
            "\t\t(net 10)\n"
            '\t\t(net_name "GND")\n'
            '\t\t(layer "B.Cu")\n'
            '\t\t(uuid "z1")\n'
            "\t\t(polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))\n"
            ")\n"
        )
        assert _pcb_has_unfilled_zones(pcb) is True

    def test_returns_false_for_filled_zone(self, tmp_path):
        pcb = tmp_path / "filled.kicad_pcb"
        pcb.write_text(
            "(kicad_pcb\n"
            '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")\n'
            "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n"
            '    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 10 0)))))\n'
        )
        assert _pcb_has_unfilled_zones(pcb) is False

    def test_returns_false_on_unreadable_file(self, tmp_path):
        # A path that doesn't exist returns False (best-effort -- we'd
        # rather skip the safety-net fill than crash).
        assert _pcb_has_unfilled_zones(tmp_path / "missing.kicad_pcb") is False


# ---------------------------------------------------------------------------
# Safety-net fill integration with _export_gerbers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_exporter_with_unfilled(tmp_path):
    """A GerberExporter pointing at a PCB that defines unfilled zones."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20240108)\n"
        '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")\n'
        "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))))\n"
    )

    with patch.object(GerberExporter, "__init__", lambda self, path: None):
        exporter = GerberExporter.__new__(GerberExporter)
        exporter.pcb_path = pcb
        exporter.kicad_cli = Path("/usr/bin/kicad-cli")
        return exporter


@pytest.fixture
def mock_exporter_no_zones(tmp_path):
    """A GerberExporter pointing at a PCB with no zones at all."""
    pcb = tmp_path / "no_zone.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20240108))\n")

    with patch.object(GerberExporter, "__init__", lambda self, path: None):
        exporter = GerberExporter.__new__(GerberExporter)
        exporter.pcb_path = pcb
        exporter.kicad_cli = Path("/usr/bin/kicad-cli")
        return exporter


@pytest.fixture
def mock_exporter_filled(tmp_path):
    """A GerberExporter pointing at a PCB whose zones are already filled."""
    pcb = tmp_path / "filled.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20240108)\n"
        '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")\n'
        "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n"
        '    (filled_polygon (layer "B.Cu") (pts (xy 0 0)))))\n'
    )

    with patch.object(GerberExporter, "__init__", lambda self, path: None):
        exporter = GerberExporter.__new__(GerberExporter)
        exporter.pcb_path = pcb
        exporter.kicad_cli = Path("/usr/bin/kicad-cli")
        return exporter


class TestExportGerbersSafetyFill:
    """Verify the safety-net fill behaviour in _export_gerbers."""

    def test_unfilled_pcb_triggers_run_fill_zones(self, mock_exporter_with_unfilled, tmp_path):
        """When zones are unfilled, run_fill_zones must be invoked."""
        from kicad_tools.cli.runner import KiCadCLIResult

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        config = GerberConfig()

        # Mock fill: write a "filled" copy of the input to the requested path.
        def fake_fill(pcb_path, output_path=None, kicad_cli=None):
            if output_path is not None:
                output_path.write_text(pcb_path.read_text() + "\n;filled\n")
            return KiCadCLIResult(success=True, output_path=output_path, return_code=0)

        impl_calls: list[Path] = []

        def fake_impl(self, cfg, out, pcb_path):
            impl_calls.append(pcb_path)

        with (
            patch(
                "kicad_tools.cli.runner.run_fill_zones",
                side_effect=fake_fill,
            ) as mock_fill,
            patch.object(GerberExporter, "_export_gerbers_impl", fake_impl),
        ):
            mock_exporter_with_unfilled._export_gerbers(config, out_dir)

        mock_fill.assert_called_once()
        # The PCB passed to _export_gerbers_impl must NOT be the user's
        # original file -- it should be a temp filled copy.
        assert len(impl_calls) == 1
        assert impl_calls[0] != mock_exporter_with_unfilled.pcb_path
        assert impl_calls[0].name == mock_exporter_with_unfilled.pcb_path.name

    def test_no_zones_skips_fill(self, mock_exporter_no_zones, tmp_path):
        """When the PCB has no zones, run_fill_zones must not be invoked."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        config = GerberConfig()

        impl_calls: list[Path] = []

        def fake_impl(self, cfg, out, pcb_path):
            impl_calls.append(pcb_path)

        with (
            patch("kicad_tools.cli.runner.run_fill_zones") as mock_fill,
            patch.object(GerberExporter, "_export_gerbers_impl", fake_impl),
        ):
            mock_exporter_no_zones._export_gerbers(config, out_dir)

        mock_fill.assert_not_called()
        # The original PCB is exported directly.
        assert impl_calls == [mock_exporter_no_zones.pcb_path]

    def test_already_filled_pcb_skips_fill(self, mock_exporter_filled, tmp_path):
        """A PCB whose zones are already filled does not need re-filling."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        config = GerberConfig()

        impl_calls: list[Path] = []

        def fake_impl(self, cfg, out, pcb_path):
            impl_calls.append(pcb_path)

        with (
            patch("kicad_tools.cli.runner.run_fill_zones") as mock_fill,
            patch.object(GerberExporter, "_export_gerbers_impl", fake_impl),
        ):
            mock_exporter_filled._export_gerbers(config, out_dir)

        mock_fill.assert_not_called()
        assert impl_calls == [mock_exporter_filled.pcb_path]

    def test_source_pcb_never_mutated(self, mock_exporter_with_unfilled, tmp_path):
        """The user's source PCB file must not be written to during fill.

        We capture the on-disk content before and after the export and
        verify it is byte-identical -- the safety-net fill must use a temp
        file.
        """
        from kicad_tools.cli.runner import KiCadCLIResult

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        original_content = mock_exporter_with_unfilled.pcb_path.read_bytes()

        def fake_fill(pcb_path, output_path=None, kicad_cli=None):
            # Simulate kicad-cli writing fill polygons into the *output*
            # path so we can prove the input is untouched.
            if output_path is not None:
                output_path.write_text(
                    pcb_path.read_text().replace("(polygon", "(filled_polygon ...) (polygon")
                )
            return KiCadCLIResult(success=True, output_path=output_path, return_code=0)

        with (
            patch(
                "kicad_tools.cli.runner.run_fill_zones",
                side_effect=fake_fill,
            ),
            patch.object(GerberExporter, "_export_gerbers_impl", lambda *a, **k: None),
        ):
            mock_exporter_with_unfilled._export_gerbers(GerberConfig(), out_dir)

        # Source PCB must still match its original content.
        assert mock_exporter_with_unfilled.pcb_path.read_bytes() == original_content

    def test_fill_failure_falls_back_to_unfilled_pcb(
        self, mock_exporter_with_unfilled, tmp_path, caplog
    ):
        """If the safety-net fill fails, export proceeds against the original PCB."""
        import logging

        from kicad_tools.cli.runner import KiCadCLIResult

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        impl_calls: list[Path] = []

        def fake_impl(self, cfg, out, pcb_path):
            impl_calls.append(pcb_path)

        with (
            patch(
                "kicad_tools.cli.runner.run_fill_zones",
                return_value=KiCadCLIResult(success=False, stderr="boom", return_code=1),
            ),
            patch.object(GerberExporter, "_export_gerbers_impl", fake_impl),
            caplog.at_level(logging.WARNING, logger="kicad_tools.export.gerber"),
        ):
            mock_exporter_with_unfilled._export_gerbers(GerberConfig(), out_dir)

        # Falls back to the original (unfilled) PCB.
        assert impl_calls == [mock_exporter_with_unfilled.pcb_path]
        # Warning logged so the user knows the Gerbers will lack plane copper.
        assert any("zone fill failed" in r.message.lower() for r in caplog.records)

    def test_temp_file_cleaned_up(self, mock_exporter_with_unfilled, tmp_path):
        """The temp directory used for the fill must be cleaned up after export."""
        from kicad_tools.cli.runner import KiCadCLIResult

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        captured_temp_path: list[Path] = []

        def fake_fill(pcb_path, output_path=None, kicad_cli=None):
            assert output_path is not None
            captured_temp_path.append(output_path)
            output_path.write_text("filled")
            return KiCadCLIResult(success=True, output_path=output_path, return_code=0)

        with (
            patch(
                "kicad_tools.cli.runner.run_fill_zones",
                side_effect=fake_fill,
            ),
            patch.object(GerberExporter, "_export_gerbers_impl", lambda *a, **k: None),
        ):
            mock_exporter_with_unfilled._export_gerbers(GerberConfig(), out_dir)

        assert len(captured_temp_path) == 1
        # The temp file's containing directory must be gone after _export_gerbers
        # returns (TemporaryDirectory.cleanup() removes it).
        assert not captured_temp_path[0].parent.exists()
