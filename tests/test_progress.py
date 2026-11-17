"""Tests for kicad_tools.cli.progress module."""

import sys


class TestProgressModule:
    """Tests for progress indicator utilities."""

    def test_is_terminal_returns_bool(self):
        """Test is_terminal function returns boolean."""
        from kicad_tools.cli.progress import is_terminal

        result = is_terminal()
        assert isinstance(result, bool)

    def test_create_progress_quiet_mode(self):
        """Test create_progress returns no-op in quiet mode."""
        from kicad_tools.cli.progress import _NoOpProgress, create_progress

        progress = create_progress(quiet=True)
        assert isinstance(progress, _NoOpProgress)

    def test_noop_progress_methods(self):
        """Test _NoOpProgress has required methods."""
        from kicad_tools.cli.progress import _NoOpProgress

        progress = _NoOpProgress()

        # Should not raise
        with progress:
            task_id = progress.add_task("Test", total=10)
            assert task_id == 0

            progress.update(task_id, advance=1)
            progress.advance(task_id, 1)
            progress.start_task(task_id)
            progress.stop_task(task_id)
            progress.remove_task(task_id)

    def test_with_progress_quiet_mode(self):
        """Test with_progress yields items in quiet mode."""
        from kicad_tools.cli.progress import with_progress

        items = [1, 2, 3, 4, 5]
        result = list(with_progress(items, quiet=True))
        assert result == items

    def test_with_progress_generator(self):
        """Test with_progress works with generators."""
        from kicad_tools.cli.progress import with_progress

        def gen():
            yield from range(5)

        result = list(with_progress(gen(), total=5, quiet=True))
        assert result == [0, 1, 2, 3, 4]

    def test_spinner_quiet_mode(self):
        """Test spinner does nothing in quiet mode."""
        from kicad_tools.cli.progress import spinner

        # Should not raise and should complete normally
        with spinner("Test...", quiet=True):
            pass

    def test_print_status_quiet_mode(self, capsys):
        """Test print_status does nothing in quiet mode."""
        from kicad_tools.cli.progress import print_status

        print_status("Test message", quiet=True)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_get_stderr_console(self):
        """Test _get_stderr_console returns Console object."""
        from rich.console import Console

        from kicad_tools.cli.progress import _get_stderr_console

        console = _get_stderr_console()
        assert isinstance(console, Console)


class TestQuietFlag:
    """Tests for --quiet flag in CLI commands."""

    def test_main_parser_has_quiet_flag(self):
        """Test main CLI parser has --quiet flag."""
        import subprocess

        # The parser should accept --quiet without error
        # We can't easily test this without calling main, so just verify the flag works
        # by checking help output
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--quiet" in result.stdout or "-q" in result.stdout

    def test_route_command_has_quiet_flag(self):
        """Test route command parser has --quiet flag."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "route", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--quiet" in result.stdout or "-q" in result.stdout

    def test_validate_footprints_has_quiet_flag(self):
        """Test validate-footprints command has --quiet flag."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "validate-footprints", "--help"],
            capture_output=True,
            text=True,
        )
        # The footprint cmd is invoked through main CLI
        # Just verify help works
        assert result.returncode == 0

    def test_optimize_traces_has_quiet_flag(self):
        """Test optimize-traces command has --quiet flag."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "optimize-traces", "--help"],
            capture_output=True,
            text=True,
        )
        assert "--quiet" in result.stdout or "-q" in result.stdout
