"""Tests for the post-route zone-fill wiring (issue #2516).

The bug: ``kct route`` and ``kct export`` defined ``(zone ...)`` blocks via
auto-pour but never filled them, so exported Gerbers contained zero
``G36..G37`` polygon-fill regions and manufactured boards lacked plane
copper.

The fix wires :func:`runner.run_fill_zones` into:
  - ``route_cmd._fill_zones_after_route`` (called by all four route paths)
  - ``gerber.GerberExporter._export_gerbers`` (safety-net at export time)

These tests verify the wiring at the unit level using mocks so they run
without kicad-cli installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Patch targets -- _fill_zones_after_route imports from .runner at call time,
# so we patch the names in the runner module itself.
# ---------------------------------------------------------------------------
_FIND_CLI = "kicad_tools.cli.runner.find_kicad_cli"
_RUN_FILL = "kicad_tools.cli.runner.run_fill_zones"
_VALIDATE_NET = "kicad_tools.cli.runner.validate_net_format"


# ---------------------------------------------------------------------------
# Fixture: minimal PCB with a single (zone ...) block
# ---------------------------------------------------------------------------


@pytest.fixture
def pcb_with_zone(tmp_path: Path) -> Path:
    """Write a minimal .kicad_pcb file containing one unfilled zone."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "zone-test")\n'
        "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))))\n"
        ")\n"
    )
    return pcb


@pytest.fixture
def pcb_no_zone(tmp_path: Path) -> Path:
    """Write a minimal .kicad_pcb file with no (zone ...) blocks."""
    pcb = tmp_path / "no_zone.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "")\n'
        ")\n"
    )
    return pcb


# ---------------------------------------------------------------------------
# Tests for _fill_zones_after_route
# ---------------------------------------------------------------------------


class TestFillZonesAfterRoute:
    """Verify the post-route fill helper invokes run_fill_zones correctly."""

    def test_skips_when_no_kicad_cli(self, pcb_with_zone, capsys):
        """No-op (with optional warning) when kicad-cli is not available."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route

        with (
            patch(_FIND_CLI, return_value=None),
            patch(_RUN_FILL) as mock_fill,
        ):
            _fill_zones_after_route(pcb_with_zone, quiet=False)
            mock_fill.assert_not_called()

        captured = capsys.readouterr()
        assert "kicad-cli not installed" in captured.out

    def test_skips_silently_when_no_zones(self, pcb_no_zone, capsys):
        """Skip silently when the PCB has no zones to fill."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route

        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL) as mock_fill,
        ):
            _fill_zones_after_route(pcb_no_zone, quiet=False)
            mock_fill.assert_not_called()

        # No output -- silent skip
        captured = capsys.readouterr()
        assert "Filling Copper Zones" not in captured.out

    def test_detects_multiline_zone_format(self, tmp_path, capsys):
        """The serializer wraps zones across multiple lines.

        Regression: the original "(zone " (with trailing space) check missed
        the multi-line form actually emitted by the zone generator.
        """
        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        pcb = tmp_path / "multiline.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator "test")\n'
            "\t(zone\n"
            "\t\t(net 10)\n"
            '\t\t(net_name "GND")\n'
            '\t\t(layer "B.Cu")\n'
            '\t\t(uuid "z1")\n'
            "\t\t(polygon (pts (xy 0 0) (xy 10 0) (xy 10 10))))\n"
            ")\n"
        )

        mock_result = KiCadCLIResult(success=True, return_code=0)
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result) as mock_fill,
            patch(_VALIDATE_NET, return_value=NetFormatReport(valid=True)),
        ):
            _fill_zones_after_route(pcb, quiet=False)
            mock_fill.assert_called_once()

        captured = capsys.readouterr()
        assert "Filling Copper Zones" in captured.out

    def test_calls_run_fill_zones_when_zones_present(self, pcb_with_zone, capsys):
        """When zones exist and kicad-cli is available, invoke run_fill_zones."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        mock_result = KiCadCLIResult(
            success=True,
            output_path=pcb_with_zone,
            return_code=0,
        )
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result) as mock_fill,
            patch(_VALIDATE_NET, return_value=NetFormatReport(valid=True)),
        ):
            _fill_zones_after_route(pcb_with_zone, quiet=False)
            mock_fill.assert_called_once()
            # First positional arg is the PCB path.
            assert mock_fill.call_args.args[0] == pcb_with_zone

        captured = capsys.readouterr()
        assert "Filling Copper Zones" in captured.out
        assert "complete" in captured.out

    def test_quiet_mode_suppresses_output(self, pcb_with_zone, capsys):
        """quiet=True suppresses informational prints."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        mock_result = KiCadCLIResult(success=True, return_code=0)
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result),
            patch(_VALIDATE_NET, return_value=NetFormatReport(valid=True)),
        ):
            _fill_zones_after_route(pcb_with_zone, quiet=True)

        captured = capsys.readouterr()
        assert captured.out == ""

    def test_validates_net_format_after_fill(self, pcb_with_zone):
        """validate_net_format must be called after a successful fill."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        mock_result = KiCadCLIResult(success=True, return_code=0)
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result),
            patch(_VALIDATE_NET, return_value=NetFormatReport(valid=True)) as mock_val,
        ):
            _fill_zones_after_route(pcb_with_zone, quiet=True)
            mock_val.assert_called_once_with(pcb_with_zone)

    def test_logs_warning_on_net_corruption(self, pcb_with_zone, caplog):
        """When validate_net_format reports corruption, log a warning."""
        import logging

        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        bad_report = NetFormatReport(valid=False, name_only_segments=3, empty_net_vias=1)
        mock_result = KiCadCLIResult(success=True, return_code=0)

        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result),
            patch(_VALIDATE_NET, return_value=bad_report),
            caplog.at_level(logging.WARNING, logger="kicad_tools.cli.route_cmd"),
        ):
            _fill_zones_after_route(pcb_with_zone, quiet=True)

        assert any("Net format corruption" in r.message for r in caplog.records)

    def test_fill_failure_is_non_fatal(self, pcb_with_zone, caplog, capsys):
        """A fill failure logs a warning but does not raise."""
        import logging

        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult

        bad_result = KiCadCLIResult(
            success=False,
            stderr="kicad-cli crashed",
            return_code=1,
        )
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=bad_result),
            caplog.at_level(logging.WARNING, logger="kicad_tools.cli.route_cmd"),
        ):
            # Must not raise.
            _fill_zones_after_route(pcb_with_zone, quiet=False)

        captured = capsys.readouterr()
        assert "zone fill failed" in captured.out.lower()
        assert any("Zone fill failed" in r.message for r in caplog.records)

    def test_idempotent_on_already_filled_pcb(self, tmp_path):
        """Running twice on a PCB that already has filled zones is safe."""
        from kicad_tools.cli.route_cmd import _fill_zones_after_route
        from kicad_tools.cli.runner import KiCadCLIResult, NetFormatReport

        # PCB that already contains a filled zone.
        pcb = tmp_path / "filled.kicad_pcb"
        pcb.write_text(
            '(kicad_pcb (version 20240108) (generator "test")\n'
            '  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "z1")\n'
            "    (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n"
            '    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 10 0))))\n'
            ")\n"
        )

        mock_result = KiCadCLIResult(success=True, return_code=0)
        with (
            patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")),
            patch(_RUN_FILL, return_value=mock_result) as mock_fill,
            patch(_VALIDATE_NET, return_value=NetFormatReport(valid=True)),
        ):
            _fill_zones_after_route(pcb, quiet=True)
            _fill_zones_after_route(pcb, quiet=True)

        # Both calls should invoke fill (kicad-cli is responsible for the
        # actual idempotency by recomputing the same fill polygons).
        assert mock_fill.call_count == 2


# ---------------------------------------------------------------------------
# Verify the helper is wired into all four route paths
# ---------------------------------------------------------------------------


class TestRouteFillWiring:
    """Verify _fill_zones_after_route is called from all four route paths."""

    def test_route_cmd_module_imports_helper(self):
        """_fill_zones_after_route should be a top-level function."""
        from kicad_tools.cli import route_cmd

        assert hasattr(route_cmd, "_fill_zones_after_route")
        assert callable(route_cmd._fill_zones_after_route)

    def test_helper_is_invoked_from_all_route_functions(self):
        """The four route_with_* functions must each reference the helper.

        This is a simple source-level check that catches regressions where
        a future refactor drops the call site.
        """
        from kicad_tools.cli import route_cmd

        source = Path(route_cmd.__file__).read_text()
        # Definition + four call sites = 5 occurrences minimum.
        # Allow more in case of additional internal users or docstrings.
        call_count = source.count("_fill_zones_after_route(")
        assert call_count >= 5, (
            f"expected >= 5 occurrences of _fill_zones_after_route( "
            f"(1 def + 4 callers), found {call_count}"
        )
