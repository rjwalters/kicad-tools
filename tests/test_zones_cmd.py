"""Tests for the zones CLI command, focusing on the fill subcommand."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.zones_cmd import main

# Patch targets -- _run_fill imports from .runner at call time, so we patch
# the names in the runner module itself.
_FIND_CLI = "kicad_tools.cli.runner.find_kicad_cli"
_RUN_FILL = "kicad_tools.cli.runner.run_fill_zones"
_SUBPROCESS_RUN = "kicad_tools.cli.runner.subprocess.run"
_HAS_FILL_ZONES = "kicad_tools.cli.runner._kicad_cli_has_fill_zones"
_DRC_SUPPORTS_REFILL = "kicad_tools.cli.runner._kicad_drc_supports_refill"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_PCB = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"


@pytest.fixture
def tmp_pcb(tmp_path: Path) -> Path:
    """Copy the multilayer_zones fixture to a temp dir so tests can write."""
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy2(FIXTURE_PCB, dest)
    return dest


@pytest.fixture
def _mock_no_kicad_cli():
    """Patch find_kicad_cli to return None (kicad-cli not available)."""
    with patch(_FIND_CLI, return_value=None):
        yield


@pytest.fixture
def _mock_kicad_cli():
    """Patch find_kicad_cli to return a fake path."""
    with patch(_FIND_CLI, return_value=Path("/usr/bin/kicad-cli")):
        yield


# ---------------------------------------------------------------------------
# Test: fill subcommand appears in help
# ---------------------------------------------------------------------------


class TestFillSubcommandPresence:
    """Verify the fill subcommand is registered."""

    def test_fill_in_zones_help(self, capsys):
        """The fill subcommand should appear in `zones --help` output."""
        # main() with no args prints help and returns 0
        ret = main([])
        assert ret == 0
        captured = capsys.readouterr()
        assert "fill" in captured.out


# ---------------------------------------------------------------------------
# Test: missing input file
# ---------------------------------------------------------------------------


class TestFillMissingFile:
    """Verify error when input file does not exist."""

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_missing_pcb_returns_1(self, capsys):
        ret = main(["fill", "/nonexistent/board.kicad_pcb"])
        assert ret == 1
        captured = capsys.readouterr()
        assert "File not found" in captured.err


# ---------------------------------------------------------------------------
# Test: kicad-cli not found
# ---------------------------------------------------------------------------


class TestFillNoKicadCli:
    """Verify error when kicad-cli is not installed."""

    @pytest.mark.usefixtures("_mock_no_kicad_cli")
    def test_no_kicad_cli_returns_1(self, tmp_pcb, capsys):
        ret = main(["fill", str(tmp_pcb)])
        assert ret == 1
        captured = capsys.readouterr()
        assert "kicad-cli not found" in captured.err


# ---------------------------------------------------------------------------
# Test: --dry-run does not invoke run_fill_zones
# ---------------------------------------------------------------------------


class TestFillDryRun:
    """Verify --dry-run prints info but does not call run_fill_zones."""

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_dry_run_does_not_fill(self, tmp_pcb, capsys):
        with patch(_RUN_FILL) as mock_fill:
            ret = main(["fill", str(tmp_pcb), "--dry-run"])
            assert ret == 0
            mock_fill.assert_not_called()
        captured = capsys.readouterr()
        assert "Would fill zones in" in captured.out

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_dry_run_shows_net_filter(self, tmp_pcb, capsys):
        with patch(_RUN_FILL):
            ret = main(["fill", str(tmp_pcb), "--dry-run", "--net", "GND"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "GND" in captured.out

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_dry_run_shows_output_path(self, tmp_pcb, tmp_path, capsys):
        out = tmp_path / "filled.kicad_pcb"
        with patch(_RUN_FILL):
            ret = main(["fill", str(tmp_pcb), "--dry-run", "-o", str(out)])
        assert ret == 0
        captured = capsys.readouterr()
        assert str(out) in captured.out


# ---------------------------------------------------------------------------
# Test: successful fill delegates to run_fill_zones
# ---------------------------------------------------------------------------


class TestFillSuccess:
    """Verify a successful fill invocation."""

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_fill_calls_run_fill_zones(self, tmp_pcb, capsys):
        from kicad_tools.cli.runner import KiCadCLIResult

        mock_result = KiCadCLIResult(
            success=True,
            output_path=tmp_pcb,
            stdout="",
            stderr="",
            return_code=0,
        )
        with patch(_RUN_FILL, return_value=mock_result) as mock_fill:
            ret = main(["fill", str(tmp_pcb)])
            assert ret == 0
            mock_fill.assert_called_once()
            call_args = mock_fill.call_args
            assert call_args[0][0] == tmp_pcb  # pcb_path
            assert call_args[0][1] is None  # output_path (in-place)
        captured = capsys.readouterr()
        assert "Zones filled" in captured.out

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_fill_with_output_path(self, tmp_pcb, tmp_path):
        from kicad_tools.cli.runner import KiCadCLIResult

        out = tmp_path / "filled.kicad_pcb"
        mock_result = KiCadCLIResult(
            success=True,
            output_path=out,
            stdout="",
            stderr="",
            return_code=0,
        )
        with patch(_RUN_FILL, return_value=mock_result) as mock_fill:
            ret = main(["fill", str(tmp_pcb), "-o", str(out)])
            assert ret == 0
            call_args = mock_fill.call_args
            assert call_args[0][1] == out


# ---------------------------------------------------------------------------
# Test: fill failure propagates error
# ---------------------------------------------------------------------------


class TestFillFailure:
    """Verify errors from kicad-cli are reported."""

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_fill_failure_returns_1(self, tmp_pcb, capsys):
        from kicad_tools.cli.runner import KiCadCLIResult

        mock_result = KiCadCLIResult(
            success=False,
            stderr="kicad-cli crashed",
            return_code=1,
        )
        with patch(_RUN_FILL, return_value=mock_result):
            ret = main(["fill", str(tmp_pcb)])
            assert ret == 1
        captured = capsys.readouterr()
        assert "kicad-cli crashed" in captured.err


# ---------------------------------------------------------------------------
# Test: --net filter note
# ---------------------------------------------------------------------------


class TestFillNetFilter:
    """Verify --net filter behavior (not supported by kicad-cli)."""

    @pytest.mark.usefixtures("_mock_kicad_cli")
    def test_net_filter_prints_note(self, tmp_pcb, capsys):
        from kicad_tools.cli.runner import KiCadCLIResult

        mock_result = KiCadCLIResult(success=True, output_path=tmp_pcb, return_code=0)
        with patch(_RUN_FILL, return_value=mock_result):
            ret = main(["fill", str(tmp_pcb), "--net", "GND"])
            assert ret == 0
        captured = capsys.readouterr()
        assert "--net filter is not supported" in captured.out


# ---------------------------------------------------------------------------
# Test: run_fill_zones function in runner.py
# ---------------------------------------------------------------------------


class TestRunFillZones:
    """Unit tests for the run_fill_zones runner function."""

    def test_no_kicad_cli_returns_failure(self):
        from kicad_tools.cli.runner import run_fill_zones

        with patch(_FIND_CLI, return_value=None):
            result = run_fill_zones(Path("/some/board.kicad_pcb"))
        assert result.success is False
        assert "kicad-cli not found" in result.stderr

    def test_file_not_found_error(self):
        from kicad_tools.cli.runner import run_fill_zones

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(
                _SUBPROCESS_RUN,
                side_effect=FileNotFoundError("not found"),
            ),
        ):
            result = run_fill_zones(
                Path("/some/board.kicad_pcb"),
                kicad_cli=Path("/usr/bin/kicad-cli"),
            )
        assert result.success is False
        assert "kicad-cli not found" in result.stderr

    def test_subprocess_error(self, tmp_pcb):
        import subprocess

        from kicad_tools.cli.runner import run_fill_zones

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(
                _SUBPROCESS_RUN,
                side_effect=subprocess.SubprocessError("boom"),
            ),
        ):
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is False
        assert "Failed to fill zones" in result.stderr


class TestRunFillZonesNative:
    """Unit tests for the native fill-zones path (future KiCad versions)."""

    def test_uses_native_when_available(self, tmp_pcb):
        from kicad_tools.cli.runner import run_fill_zones

        with (
            patch(_HAS_FILL_ZONES, return_value=True),
            patch(_SUBPROCESS_RUN) as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/kicad-cli"
        assert cmd[1:3] == ["pcb", "fill-zones"]
        assert str(tmp_pcb) == cmd[-1]

    def test_native_with_output(self, tmp_pcb, tmp_path):
        from kicad_tools.cli.runner import run_fill_zones

        out = tmp_path / "filled.kicad_pcb"
        with (
            patch(_HAS_FILL_ZONES, return_value=True),
            patch(_SUBPROCESS_RUN) as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert "--output" in cmd
        idx = cmd.index("--output")
        assert cmd[idx + 1] == str(out)

    def test_native_nonzero_return_code_is_failure(self, tmp_pcb):
        from kicad_tools.cli.runner import run_fill_zones

        with (
            patch(_HAS_FILL_ZONES, return_value=True),
            patch(_SUBPROCESS_RUN) as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="zone fill error")
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is False
        assert "zone fill error" in result.stderr


class TestRunFillZonesDRCFallback:
    """Unit tests for the DRC-based zone fill fallback (KiCad 8/9/10)."""

    def test_drc_fallback_runs_drc_command(self, tmp_pcb, tmp_path):
        """When fill-zones is unavailable, run_fill_zones uses DRC."""
        from kicad_tools.cli.runner import run_fill_zones

        # Create a fake DRC report so the success check passes
        def fake_run(cmd, **kwargs):
            # The DRC command writes a report to the --output path
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run) as mock_run,
        ):
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert cmd[1:3] == ["pcb", "drc"]
        assert str(tmp_pcb) == cmd[-1]

    def test_drc_fallback_with_output_copies_input(self, tmp_pcb, tmp_path):
        """When output_path is set, input is copied and DRC runs on the copy."""
        from kicad_tools.cli.runner import run_fill_zones

        out = tmp_path / "filled.kicad_pcb"

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run) as mock_run,
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        assert result.output_path == out
        # The copy should exist (made from the source PCB)
        assert out.exists()
        # DRC should have been run on the copy, not the original
        cmd = mock_run.call_args[0][0]
        assert str(out) == cmd[-1]

    def test_drc_violations_do_not_cause_failure(self, tmp_pcb):
        """DRC exit code != 0 due to violations is still a fill success."""
        from kicad_tools.cli.runner import run_fill_zones

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text(
                '{"violations": [{"type": "clearance", "severity": "error"}]}'
            )
            # Non-zero exit code because of DRC violations
            return MagicMock(returncode=5, stdout="", stderr="DRC violations found")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run),
        ):
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        # Fill succeeded even though DRC found violations
        assert result.success is True

    def test_drc_no_report_is_failure(self, tmp_pcb):
        """If DRC produces no report file, the fill is treated as failure."""
        from kicad_tools.cli.runner import run_fill_zones

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN) as mock_run,
        ):
            # DRC fails to produce a report
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="DRC execution error")
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is False

    def test_drc_report_cleaned_up(self, tmp_pcb, tmp_path):
        """The temporary DRC report file is cleaned up after fill."""
        from kicad_tools.cli.runner import run_fill_zones

        created_reports = []

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            created_reports.append(report_path)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run),
        ):
            run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        # The temp DRC report should have been deleted
        assert len(created_reports) == 1
        assert not Path(created_reports[0]).exists()

    def test_kicad8_no_refill_flags(self, tmp_pcb):
        """KiCad 8/9: DRC command should NOT include --refill-zones."""
        from kicad_tools.cli.runner import run_fill_zones

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run) as mock_run,
        ):
            run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        cmd = mock_run.call_args[0][0]
        assert "--refill-zones" not in cmd
        assert "--save-board" not in cmd

    def test_kicad10_includes_refill_flags(self, tmp_pcb):
        """KiCad 10+: DRC command should include --refill-zones --save-board."""
        from kicad_tools.cli.runner import run_fill_zones

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=True),
            patch(_SUBPROCESS_RUN, side_effect=fake_run) as mock_run,
        ):
            run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        cmd = mock_run.call_args[0][0]
        assert "--refill-zones" in cmd
        assert "--save-board" in cmd

    def test_drc_fallback_preserves_net_count(self, tmp_pcb, tmp_path):
        """Net declarations survive DRC fallback even when kicad-cli strips them."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_net_count = input_pcb.net_count
        assert input_net_count > 1, "Fixture must have named nets for this test"

        def fake_run_strips_nets(cmd, **kwargs):
            """Simulate kicad-cli stripping net declarations from the PCB."""
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            # Read the target PCB (last arg), strip net declarations,
            # and write it back -- mimicking kicad-cli's broken behaviour.
            target = Path(cmd[-1])
            content = target.read_text()
            import re

            # Remove all (net N "name") top-level declarations but keep
            # the rest (zones, segments, etc.) intact.  Replace with a
            # single (net 0 "") to simulate kicad-cli's output.
            content = re.sub(r'\n\t\(net \d+ "[^"]*"\)', "", content)
            # Ensure at least the empty net 0 exists (kicad-cli always writes it)
            content = content.replace('(net 0 "")', '(net 0 "")', 1)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))
        assert output_pcb.net_count == input_net_count

    def test_drc_fallback_preserves_nets_inplace(self, tmp_pcb):
        """Net declarations are restored when DRC modifies the PCB in place."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_net_count = input_pcb.net_count
        input_net_names = {n.name for n in input_pcb.nets.values()}

        def fake_run_strips_nets(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            target = Path(cmd[-1])
            content = target.read_text()
            import re

            content = re.sub(r'\n\t\(net \d+ "[^"]*"\)', "", content)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_nets),
        ):
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(tmp_pcb))
        assert output_pcb.net_count == input_net_count
        output_net_names = {n.name for n in output_pcb.nets.values()}
        assert output_net_names == input_net_names

    def test_drc_fallback_noop_when_nets_intact(self, tmp_pcb, tmp_path):
        """Restoration is a no-op when kicad-cli keeps nets intact."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_net_count = input_pcb.net_count

        def fake_run_keeps_nets(cmd, **kwargs):
            """Simulate kicad-cli that does NOT strip nets."""
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            # Don't modify the PCB at all -- nets stay intact.
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_keeps_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))
        assert output_pcb.net_count == input_net_count

    def test_drc_fallback_preserves_net_names(self, tmp_pcb, tmp_path):
        """Individual net names (GND, +3V3, +5V) are preserved after restoration."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_nets = {n.number: n.name for n in input_pcb.nets.values()}

        def fake_run_strips_nets(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            target = Path(cmd[-1])
            content = target.read_text()
            import re

            content = re.sub(r'\n\t\(net \d+ "[^"]*"\)', "", content)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))
        output_nets = {n.number: n.name for n in output_pcb.nets.values()}
        assert output_nets == input_nets

    def test_drc_fallback_only_net0_noop(self, tmp_path):
        """A PCB with only net 0 should not trigger restoration."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        # Create a minimal PCB with only the default net 0
        pcb = PCB.create(width=50, height=50)
        pcb_path = tmp_path / "minimal.kicad_pcb"
        pcb.save(str(pcb_path))

        def fake_run(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run),
        ):
            result = run_fill_zones(pcb_path, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(pcb_path))
        # Should still have net 0 at minimum
        assert output_pcb.net_count >= 1


# ---------------------------------------------------------------------------
# Test: per-element net assignment preservation through zone fill
# ---------------------------------------------------------------------------


class TestPadNetPreservation:
    """Tests for per-element (pad, segment, via) net assignment preservation."""

    def test_drc_fallback_preserves_pad_nets(self, tmp_pcb, tmp_path):
        """Pad net assignments are restored when kicad-cli zeroes them out."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        # Collect original pad net assignments
        input_pad_nets = {}
        for fp in input_pcb.footprints:
            for pad in fp.pads:
                if pad.net_number != 0:
                    input_pad_nets[f"{fp.reference}.{pad.number}"] = pad.net_number
        assert len(input_pad_nets) > 0, "Fixture must have pads with net assignments"

        def fake_run_strips_pad_nets(cmd, **kwargs):
            """Simulate kicad-cli zeroing out inline (net N) on pads."""
            import re

            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            target = Path(cmd[-1])
            content = target.read_text()
            # Zero out inline (net N "name") inside pads to (net 0 "")
            content = re.sub(r'\(net \d+ "[^"]*"\)', '(net 0 "")', content)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_pad_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))

        # Verify pad net assignments were restored
        output_pad_nets = {}
        for fp in output_pcb.footprints:
            for pad in fp.pads:
                if pad.net_number != 0:
                    output_pad_nets[f"{fp.reference}.{pad.number}"] = pad.net_number
        assert output_pad_nets == input_pad_nets

    def test_drc_fallback_preserves_segment_nets(self, tmp_pcb, tmp_path):
        """Segment net assignments are restored when kicad-cli zeroes them."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_seg_nets = {
            seg.uuid: seg.net_number for seg in input_pcb.segments if seg.net_number != 0
        }
        assert len(input_seg_nets) > 0, "Fixture must have segments with nets"

        def fake_run_strips_element_nets(cmd, **kwargs):
            """Simulate kicad-cli zeroing out (net N) on segments."""
            import re

            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            target = Path(cmd[-1])
            content = target.read_text()
            # Zero out all inline net references: (net N) -> (net 0)
            # and (net N "name") -> (net 0 "")
            content = re.sub(r'\(net \d+ "[^"]*"\)', '(net 0 "")', content)
            content = re.sub(r"\(net \d+\)", "(net 0)", content)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_element_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))

        output_seg_nets = {
            seg.uuid: seg.net_number for seg in output_pcb.segments if seg.net_number != 0
        }
        assert output_seg_nets == input_seg_nets

    def test_drc_fallback_preserves_via_nets(self, tmp_pcb, tmp_path):
        """Via net assignments are restored when kicad-cli zeroes them."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_via_nets = {via.uuid: via.net_number for via in input_pcb.vias if via.net_number != 0}
        assert len(input_via_nets) > 0, "Fixture must have vias with nets"

        def fake_run_strips_element_nets(cmd, **kwargs):
            """Simulate kicad-cli zeroing out (net N) on vias."""
            import re

            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')

            target = Path(cmd[-1])
            content = target.read_text()
            content = re.sub(r'\(net \d+ "[^"]*"\)', '(net 0 "")', content)
            content = re.sub(r"\(net \d+\)", "(net 0)", content)
            target.write_text(content)
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_strips_element_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))

        output_via_nets = {
            via.uuid: via.net_number for via in output_pcb.vias if via.net_number != 0
        }
        assert output_via_nets == input_via_nets

    def test_drc_fallback_noop_when_element_nets_intact(self, tmp_pcb, tmp_path):
        """Element net restoration is a no-op when nets are not stripped."""
        from kicad_tools.cli.runner import run_fill_zones
        from kicad_tools.schema.pcb import PCB

        input_pcb = PCB.load(str(tmp_pcb))
        input_pad_nets = {}
        for fp in input_pcb.footprints:
            for pad in fp.pads:
                input_pad_nets[f"{fp.reference}.{pad.number}"] = pad.net_number

        def fake_run_keeps_nets(cmd, **kwargs):
            report_path = cmd[cmd.index("--output") + 1]
            Path(report_path).write_text('{"violations": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        out = tmp_path / "filled.kicad_pcb"

        with (
            patch(_HAS_FILL_ZONES, return_value=False),
            patch(_DRC_SUPPORTS_REFILL, return_value=False),
            patch(_SUBPROCESS_RUN, side_effect=fake_run_keeps_nets),
        ):
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))

        assert result.success is True
        output_pcb = PCB.load(str(out))

        output_pad_nets = {}
        for fp in output_pcb.footprints:
            for pad in fp.pads:
                output_pad_nets[f"{fp.reference}.{pad.number}"] = pad.net_number
        assert output_pad_nets == input_pad_nets


# ---------------------------------------------------------------------------
# Integration test: requires kicad-cli
# ---------------------------------------------------------------------------


def _kicad_cli_available() -> bool:
    """Check whether kicad-cli is installed and can run ``pcb drc``."""
    import subprocess

    cli = shutil.which("kicad-cli")
    if cli is None:
        return False
    try:
        result = subprocess.run(
            [cli, "pcb", "drc", "--help"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _kicad_cli_available(),
    reason="kicad-cli not installed or 'pcb drc' unavailable",
)
class TestFillIntegration:
    """Integration tests that actually run kicad-cli to fill zones.

    Uses ``kicad-cli pcb drc`` which fills zones as a side effect.
    These tests are skipped when kicad-cli is not installed.
    """

    def test_fill_zones_on_fixture(self, tmp_pcb, tmp_path):
        """Fill zones on the multilayer fixture and check output has filled_polygon."""
        out = tmp_path / "filled_board.kicad_pcb"
        ret = main(["fill", str(tmp_pcb), "-o", str(out)])
        assert ret == 0
        assert out.exists()
        content = out.read_text()
        assert "filled_polygon" in content
