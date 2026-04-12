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

    def test_builds_correct_command_in_place(self, tmp_pcb):
        from kicad_tools.cli.runner import run_fill_zones

        with patch(_SUBPROCESS_RUN) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/kicad-cli"
        assert cmd[1:3] == ["pcb", "fill-zones"]
        # No --output flag for in-place
        assert "--output" not in cmd
        assert str(tmp_pcb) == cmd[-1]

    def test_builds_correct_command_with_output(self, tmp_pcb, tmp_path):
        from kicad_tools.cli.runner import run_fill_zones

        out = tmp_path / "filled.kicad_pcb"
        with patch(_SUBPROCESS_RUN) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_fill_zones(tmp_pcb, output_path=out, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert "--output" in cmd
        idx = cmd.index("--output")
        assert cmd[idx + 1] == str(out)

    def test_nonzero_return_code_is_failure(self, tmp_pcb):
        from kicad_tools.cli.runner import run_fill_zones

        with patch(_SUBPROCESS_RUN) as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="zone fill error")
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is False
        assert "zone fill error" in result.stderr

    def test_file_not_found_error(self):
        from kicad_tools.cli.runner import run_fill_zones

        with patch(
            _SUBPROCESS_RUN,
            side_effect=FileNotFoundError("not found"),
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

        with patch(
            _SUBPROCESS_RUN,
            side_effect=subprocess.SubprocessError("boom"),
        ):
            result = run_fill_zones(tmp_pcb, kicad_cli=Path("/usr/bin/kicad-cli"))
        assert result.success is False
        assert "Failed to fill zones" in result.stderr


# ---------------------------------------------------------------------------
# Integration test: requires kicad-cli
# ---------------------------------------------------------------------------


def _kicad_cli_has_fill_zones() -> bool:
    """Check whether the installed kicad-cli supports 'pcb fill-zones'."""
    import subprocess

    cli = shutil.which("kicad-cli")
    if cli is None:
        return False
    try:
        result = subprocess.run(
            [cli, "pcb", "fill-zones", "--help"],
            capture_output=True,
            text=True,
        )
        # kicad-cli returns 0 and prints the parent help when a subcommand
        # is unknown, so we check stdout for "fill-zones" to confirm support.
        return result.returncode == 0 and "fill-zones" in result.stdout
    except Exception:
        return False


@pytest.mark.skipif(
    not _kicad_cli_has_fill_zones(),
    reason="kicad-cli does not support 'pcb fill-zones'",
)
class TestFillIntegration:
    """Integration tests that actually run kicad-cli fill-zones.

    Note: 'kicad-cli pcb fill-zones' may not exist in all KiCad versions.
    These tests are skipped when the subcommand is unavailable.
    """

    def test_fill_zones_on_fixture(self, tmp_pcb, tmp_path):
        """Fill zones on the multilayer fixture and check output has filled_polygon."""
        out = tmp_path / "filled_board.kicad_pcb"
        ret = main(["fill", str(tmp_pcb), "-o", str(out)])
        assert ret == 0
        assert out.exists()
        content = out.read_text()
        assert "filled_polygon" in content
