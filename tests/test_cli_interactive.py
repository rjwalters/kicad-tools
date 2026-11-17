"""Tests for interactive CLI mode."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.interactive import InteractiveSession, InteractiveShell, main


class TestInteractiveSession:
    """Tests for InteractiveSession dataclass."""

    def test_default_session(self):
        """Test default session state."""
        session = InteractiveSession()
        assert session.schematic is None
        assert session.pcb is None
        assert session.project is None
        assert session.output_dir == Path("./output")

    def test_status_summary_empty(self):
        """Test status summary with no files loaded."""
        session = InteractiveSession()
        assert session.status_summary() == "no files loaded"

    def test_status_summary_with_schematic(self):
        """Test status summary with schematic loaded."""
        session = InteractiveSession(schematic=Path("/path/to/design.kicad_sch"))
        assert "sch: design.kicad_sch" in session.status_summary()

    def test_status_summary_with_pcb(self):
        """Test status summary with PCB loaded."""
        session = InteractiveSession(pcb=Path("/path/to/board.kicad_pcb"))
        assert "pcb: board.kicad_pcb" in session.status_summary()

    def test_status_summary_with_all_files(self):
        """Test status summary with all files loaded."""
        session = InteractiveSession(
            schematic=Path("/path/to/design.kicad_sch"),
            pcb=Path("/path/to/board.kicad_pcb"),
            project=Path("/path/to/project.kicad_pro"),
        )
        summary = session.status_summary()
        assert "sch: design.kicad_sch" in summary
        assert "pcb: board.kicad_pcb" in summary
        assert "project: project.kicad_pro" in summary


class TestInteractiveShell:
    """Tests for InteractiveShell class."""

    def test_shell_creation(self):
        """Test shell can be created."""
        shell = InteractiveShell()
        assert shell.session is not None
        assert shell.prompt == "kicad-tools> "

    def test_load_nonexistent_file(self, capsys):
        """Test loading a file that doesn't exist."""
        shell = InteractiveShell()
        shell.do_load("/nonexistent/file.kicad_sch")
        captured = capsys.readouterr()
        assert "Error: File not found" in captured.out

    def test_load_unknown_extension(self, tmp_path, capsys):
        """Test loading a file with unknown extension."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("test")
        shell = InteractiveShell()
        shell.do_load(str(test_file))
        captured = capsys.readouterr()
        assert "Unknown file type" in captured.out

    def test_load_schematic(self, tmp_path, capsys):
        """Test loading a schematic file."""
        sch_file = tmp_path / "design.kicad_sch"
        sch_file.write_text("(kicad_sch)")
        shell = InteractiveShell()
        shell.do_load(str(sch_file))
        captured = capsys.readouterr()
        assert "Loaded schematic" in captured.out
        # Use resolve() to handle macOS /tmp -> /private/tmp symlink
        assert shell.session.schematic.resolve() == sch_file.resolve()

    def test_load_pcb(self, tmp_path, capsys):
        """Test loading a PCB file."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        shell = InteractiveShell()
        shell.do_load(str(pcb_file))
        captured = capsys.readouterr()
        assert "Loaded PCB" in captured.out
        assert shell.session.pcb.resolve() == pcb_file.resolve()

    def test_load_project(self, tmp_path, capsys):
        """Test loading a project file with associated schematic and PCB."""
        pro_file = tmp_path / "project.kicad_pro"
        sch_file = tmp_path / "project.kicad_sch"
        pcb_file = tmp_path / "project.kicad_pcb"
        pro_file.write_text("{}")
        sch_file.write_text("(kicad_sch)")
        pcb_file.write_text("(kicad_pcb)")

        shell = InteractiveShell()
        shell.do_load(str(pro_file))
        captured = capsys.readouterr()
        assert "Loaded project" in captured.out
        assert shell.session.project.resolve() == pro_file.resolve()
        assert shell.session.schematic.resolve() == sch_file.resolve()
        assert shell.session.pcb.resolve() == pcb_file.resolve()

    def test_status_command(self, capsys):
        """Test status command output."""
        shell = InteractiveShell()
        shell.do_status("")
        captured = capsys.readouterr()
        assert "Session Status:" in captured.out
        assert "Schematic:" in captured.out
        assert "PCB:" in captured.out

    def test_clear_command(self, tmp_path, capsys):
        """Test clear command resets session."""
        sch_file = tmp_path / "design.kicad_sch"
        sch_file.write_text("(kicad_sch)")
        shell = InteractiveShell()
        shell.do_load(str(sch_file))
        shell.do_clear("")
        captured = capsys.readouterr()
        assert "Session cleared" in captured.out
        assert shell.session.schematic is None

    def test_output_command_show(self, capsys):
        """Test output command shows current directory."""
        shell = InteractiveShell()
        shell.do_output("")
        captured = capsys.readouterr()
        assert "Output directory:" in captured.out

    def test_output_command_set(self, tmp_path, capsys):
        """Test output command sets directory."""
        shell = InteractiveShell()
        shell.do_output(str(tmp_path / "output"))
        captured = capsys.readouterr()
        assert "Output directory set to" in captured.out
        # The path gets resolved, so just check the name
        assert shell.session.output_dir.name == "output"

    def test_quit_returns_true(self):
        """Test quit command returns True to stop loop."""
        shell = InteractiveShell()
        result = shell.do_quit("")
        assert result is True

    def test_exit_calls_quit(self, capsys):
        """Test exit command calls quit."""
        shell = InteractiveShell()
        result = shell.do_exit("")
        assert result is True
        captured = capsys.readouterr()
        assert "Goodbye" in captured.out

    def test_eof_exits(self, capsys):
        """Test EOF (Ctrl+D) exits with goodbye."""
        shell = InteractiveShell()
        result = shell.do_EOF("")
        assert result is True
        captured = capsys.readouterr()
        assert "Goodbye" in captured.out

    def test_emptyline_returns_false(self):
        """Test emptyline returns False (don't exit)."""
        shell = InteractiveShell()
        result = shell.emptyline()
        assert result is False

    def test_unknown_command(self, capsys):
        """Test unknown command shows error."""
        shell = InteractiveShell()
        shell.default("unknowncmd arg1 arg2")
        captured = capsys.readouterr()
        assert "Unknown command: unknowncmd" in captured.out

    def test_prompt_updates_after_load(self, tmp_path):
        """Test prompt includes file info after loading."""
        sch_file = tmp_path / "design.kicad_sch"
        sch_file.write_text("(kicad_sch)")
        shell = InteractiveShell()
        shell.do_load(str(sch_file))
        shell.postcmd(False, "load")
        assert "design.kicad_sch" in shell.prompt

    def test_symbols_without_schematic(self, capsys):
        """Test symbols command without schematic loaded."""
        shell = InteractiveShell()
        shell.do_symbols("")
        captured = capsys.readouterr()
        assert "No schematic loaded" in captured.out

    def test_bom_without_schematic(self, capsys):
        """Test bom command without schematic loaded."""
        shell = InteractiveShell()
        shell.do_bom("")
        captured = capsys.readouterr()
        assert "No schematic loaded" in captured.out

    def test_nets_without_schematic(self, capsys):
        """Test nets command without schematic loaded."""
        shell = InteractiveShell()
        shell.do_nets("")
        captured = capsys.readouterr()
        assert "No schematic loaded" in captured.out

    def test_summary_without_file(self, capsys):
        """Test summary command without file loaded."""
        shell = InteractiveShell()
        shell.do_summary("")  # defaults to sch
        captured = capsys.readouterr()
        assert "No schematic loaded" in captured.out

    def test_summary_pcb_without_file(self, capsys):
        """Test summary pcb command without file loaded."""
        shell = InteractiveShell()
        shell.do_summary("pcb")
        captured = capsys.readouterr()
        assert "No PCB loaded" in captured.out

    def test_erc_without_args(self, capsys):
        """Test erc command without arguments."""
        shell = InteractiveShell()
        shell.do_erc("")
        captured = capsys.readouterr()
        assert "Usage: erc" in captured.out

    def test_drc_without_args(self, capsys):
        """Test drc command without arguments."""
        shell = InteractiveShell()
        shell.do_drc("")
        captured = capsys.readouterr()
        assert "Usage: drc" in captured.out

    def test_load_without_args(self, capsys):
        """Test load command without arguments."""
        shell = InteractiveShell()
        shell.do_load("")
        captured = capsys.readouterr()
        assert "Usage: load" in captured.out


class TestInteractiveMain:
    """Tests for main entry point."""

    def test_main_help(self):
        """Test main with --help exits cleanly."""
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0

    @patch("sys.stdin.isatty", return_value=False)
    @patch.object(InteractiveShell, "cmdloop")
    def test_main_non_tty_warning(self, mock_cmdloop, mock_isatty, capsys):
        """Test warning when running in non-TTY mode."""
        mock_cmdloop.return_value = None
        main([])
        captured = capsys.readouterr()
        assert "non-TTY mode" in captured.err

    @patch("sys.stdin.isatty", return_value=True)
    @patch.object(InteractiveShell, "cmdloop")
    def test_main_with_project(self, mock_cmdloop, mock_isatty, tmp_path):
        """Test main with --project argument."""
        pro_file = tmp_path / "test.kicad_pro"
        pro_file.write_text("{}")
        mock_cmdloop.return_value = None
        result = main(["--project", str(pro_file)])
        assert result == 0

    @patch("sys.stdin.isatty", return_value=True)
    @patch.object(InteractiveShell, "cmdloop", side_effect=KeyboardInterrupt)
    def test_main_keyboard_interrupt(self, mock_cmdloop, mock_isatty, capsys):
        """Test Ctrl+C handling in main."""
        result = main([])
        assert result == 130
        captured = capsys.readouterr()
        assert "Interrupted" in captured.out
