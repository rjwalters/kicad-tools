"""Tests for find_kicad_cli() and sch_validate.run_erc() kicad-cli lookup.

Verifies:
1. find_kicad_cli() includes ~/Applications path with proper tilde expansion.
2. sch_validate.run_erc() uses find_kicad_cli() instead of subprocess 'which'.
"""

from pathlib import Path
from unittest.mock import patch

from kicad_tools.cli.runner import find_kicad_cli


class TestFindKicadCliUserApplications:
    """Verify ~/Applications path is checked with proper tilde expansion."""

    @patch("shutil.which", return_value=None)
    def test_user_applications_path_uses_home(self, mock_which):
        """The locations list must contain the user-local macOS path
        built via Path.home(), not a literal '~' that Path() won't expand."""
        # We don't need kicad-cli to actually exist; we just need to
        # confirm the candidate list includes the expanded home path.
        user_app_path = str(
            Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
        )

        # Patch Path.exists so only the user-local path "exists"
        original_exists = Path.exists

        def fake_exists(self):
            if str(self) == user_app_path:
                return True
            # All other candidate paths should not exist
            return False

        with patch.object(Path, "exists", fake_exists):
            result = find_kicad_cli()

        assert result is not None
        assert str(result) == user_app_path

    @patch("shutil.which", return_value=None)
    def test_no_tilde_literal_in_locations(self, mock_which):
        """Ensure we never pass a literal '~' to Path() because it
        won't be expanded and the file will never be found."""
        # Capture all paths that find_kicad_cli checks by patching Path.exists
        checked_paths: list[str] = []
        original_exists = Path.exists

        def tracking_exists(self):
            checked_paths.append(str(self))
            return False

        with patch.object(Path, "exists", tracking_exists):
            find_kicad_cli()

        # None of the checked paths should start with '~'
        for p in checked_paths:
            assert not p.startswith("~"), (
                f"Path starts with literal tilde (won't expand): {p}"
            )


class TestSchValidateUsesFinderFunction:
    """Verify sch_validate.run_erc() delegates to find_kicad_cli()."""

    @patch("kicad_tools.cli.sch_validate.find_kicad_cli", return_value=None)
    def test_run_erc_calls_find_kicad_cli(self, mock_find):
        """When find_kicad_cli returns None, run_erc should return a
        warning about kicad-cli not being found."""
        from kicad_tools.cli.sch_validate import run_erc

        issues = run_erc("dummy.kicad_sch")

        mock_find.assert_called_once()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "not found" in issues[0].message

    @patch("subprocess.run")
    @patch(
        "kicad_tools.cli.sch_validate.find_kicad_cli",
        return_value=Path("/usr/bin/kicad-cli"),
    )
    def test_run_erc_uses_returned_path(self, mock_find, mock_subprocess):
        """When find_kicad_cli returns a path, run_erc should use it
        to invoke the ERC subprocess."""
        import subprocess

        # Make the ERC subprocess call succeed but produce no output file
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        from kicad_tools.cli.sch_validate import run_erc

        run_erc("dummy.kicad_sch")

        mock_find.assert_called_once()
        # The subprocess should be called with the path from find_kicad_cli
        assert mock_subprocess.called
        call_args = mock_subprocess.call_args[0][0]
        assert call_args[0] == "/usr/bin/kicad-cli"
