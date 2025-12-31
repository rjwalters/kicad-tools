"""
Interactive REPL mode for kicad-tools.

Provides a command-line shell for multi-step workflows with session state,
file path completion, and command history.
"""

import cmd
import contextlib
import os
import readline
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InteractiveSession:
    """Holds state for an interactive session."""

    schematic: Path | None = None
    pcb: Path | None = None
    project: Path | None = None
    output_dir: Path = field(default_factory=lambda: Path("./output"))

    def status_summary(self) -> str:
        """Return a brief status summary."""
        parts = []
        if self.schematic:
            parts.append(f"sch: {self.schematic.name}")
        if self.pcb:
            parts.append(f"pcb: {self.pcb.name}")
        if self.project:
            parts.append(f"project: {self.project.name}")
        return ", ".join(parts) if parts else "no files loaded"


class InteractiveShell(cmd.Cmd):
    """Interactive REPL shell for kicad-tools."""

    intro = """
kicad-tools interactive mode
Type 'help' for available commands, 'quit' to exit.
"""
    prompt = "kicad-tools> "

    def __init__(self, project: str | None = None):
        super().__init__()
        self.session = InteractiveSession()

        # Set up readline history
        self.histfile = Path.home() / ".kicad_tools_history"
        with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
            readline.read_history_file(self.histfile)

        # Configure readline for file path completion
        readline.set_completer_delims(" \t\n;")

        # Auto-load project if specified
        if project:
            self._load_file(project)

    def _save_history(self) -> None:
        """Save command history."""
        with contextlib.suppress(OSError):
            readline.write_history_file(self.histfile)

    def _load_file(self, filepath: str) -> bool:
        """Load a file based on its extension."""
        path = Path(filepath).expanduser().resolve()

        if not path.exists():
            print(f"Error: File not found: {path}")
            return False

        suffix = path.suffix.lower()

        if suffix == ".kicad_sch":
            self.session.schematic = path
            print(f"Loaded schematic: {path.name}")
            return True
        elif suffix == ".kicad_pcb":
            self.session.pcb = path
            print(f"Loaded PCB: {path.name}")
            return True
        elif suffix == ".kicad_pro":
            self.session.project = path
            # Auto-detect schematic and PCB from project directory
            proj_dir = path.parent
            stem = path.stem
            sch_path = proj_dir / f"{stem}.kicad_sch"
            pcb_path = proj_dir / f"{stem}.kicad_pcb"
            if sch_path.exists():
                self.session.schematic = sch_path
            if pcb_path.exists():
                self.session.pcb = pcb_path
            print(f"Loaded project: {path.name}")
            if self.session.schematic:
                print(f"  Schematic: {self.session.schematic.name}")
            if self.session.pcb:
                print(f"  PCB: {self.session.pcb.name}")
            return True
        else:
            print(f"Error: Unknown file type: {suffix}")
            print("Supported: .kicad_sch, .kicad_pcb, .kicad_pro")
            return False

    def _complete_path(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Complete file paths."""
        if not text:
            text = "./"

        # Expand user home directory
        if text.startswith("~"):
            text = os.path.expanduser(text)

        # Get directory and prefix
        if os.path.isdir(text):
            directory = text
            prefix = ""
        else:
            directory = os.path.dirname(text) or "."
            prefix = os.path.basename(text)

        try:
            entries = os.listdir(directory)
        except OSError:
            return []

        completions = []
        for entry in entries:
            if entry.startswith(prefix):
                full_path = os.path.join(directory, entry)
                if os.path.isdir(full_path):
                    completions.append(entry + "/")
                else:
                    completions.append(entry)

        return completions

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    def do_load(self, arg: str) -> None:
        """Load a KiCad file (schematic, PCB, or project).

        Usage: load <file>

        Examples:
            load design.kicad_sch
            load board.kicad_pcb
            load project.kicad_pro
        """
        if not arg:
            print("Usage: load <file>")
            return
        self._load_file(arg.strip())

    def complete_load(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Tab completion for load command."""
        return self._complete_path(text, line, begidx, endidx)

    def do_status(self, arg: str) -> None:
        """Show current session status.

        Usage: status
        """
        print("Session Status:")
        print(f"  Schematic: {self.session.schematic or 'not loaded'}")
        print(f"  PCB: {self.session.pcb or 'not loaded'}")
        print(f"  Project: {self.session.project or 'not loaded'}")
        print(f"  Output dir: {self.session.output_dir}")

    def do_symbols(self, arg: str) -> None:
        """List symbols from the loaded schematic.

        Usage: symbols [--filter <pattern>] [--format table|json|csv]

        Requires a schematic to be loaded first.
        """
        if not self.session.schematic:
            print("Error: No schematic loaded. Use 'load <file>' first.")
            return

        from kicad_tools.cli.symbols import main as symbols_cmd

        argv = [str(self.session.schematic)]
        if arg:
            argv.extend(shlex.split(arg))

        with contextlib.suppress(SystemExit):
            symbols_cmd(argv)

    def do_bom(self, arg: str) -> None:
        """Generate bill of materials from loaded schematic.

        Usage: bom [--format table|csv|json] [--group]

        Requires a schematic to be loaded first.
        """
        if not self.session.schematic:
            print("Error: No schematic loaded. Use 'load <file>' first.")
            return

        from kicad_tools.cli.bom_cmd import main as bom_cmd

        argv = [str(self.session.schematic)]
        if arg:
            argv.extend(shlex.split(arg))

        with contextlib.suppress(SystemExit):
            bom_cmd(argv)

    def do_nets(self, arg: str) -> None:
        """Trace and analyze nets in loaded schematic.

        Usage: nets [--net <name>] [--stats] [--format table|json]

        Requires a schematic to be loaded first.
        """
        if not self.session.schematic:
            print("Error: No schematic loaded. Use 'load <file>' first.")
            return

        from kicad_tools.cli.nets import main as nets_cmd

        argv = [str(self.session.schematic)]
        if arg:
            argv.extend(shlex.split(arg))

        with contextlib.suppress(SystemExit):
            nets_cmd(argv)

    def do_summary(self, arg: str) -> None:
        """Show summary of loaded schematic or PCB.

        Usage: summary [sch|pcb]

        With no argument, shows schematic summary if loaded.
        """
        target = arg.strip().lower() if arg else "sch"

        if target in ("sch", "schematic"):
            if not self.session.schematic:
                print("Error: No schematic loaded. Use 'load <file>' first.")
                return
            from kicad_tools.cli.sch_summary import run_summary

            with contextlib.suppress(SystemExit):
                run_summary(self.session.schematic, "text", False)

        elif target in ("pcb", "board"):
            if not self.session.pcb:
                print("Error: No PCB loaded. Use 'load <file>' first.")
                return
            from kicad_tools.cli.pcb_query import main as pcb_main

            with contextlib.suppress(SystemExit):
                pcb_main([str(self.session.pcb), "summary"])

        else:
            print(f"Unknown target: {target}")
            print("Usage: summary [sch|pcb]")

    def do_erc(self, arg: str) -> None:
        """Parse an ERC report file.

        Usage: erc <report> [--format table|json|summary] [--errors-only]
        """
        if not arg:
            print("Usage: erc <report>")
            return

        from kicad_tools.cli.erc_cmd import main as erc_cmd

        with contextlib.suppress(SystemExit):
            erc_cmd(shlex.split(arg))

    def complete_erc(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Tab completion for erc command."""
        return self._complete_path(text, line, begidx, endidx)

    def do_drc(self, arg: str) -> None:
        """Parse a DRC report file.

        Usage: drc <report> [--format table|json|summary] [--errors-only]
        """
        if not arg:
            print("Usage: drc <report>")
            return

        from kicad_tools.cli.drc_cmd import main as drc_cmd

        with contextlib.suppress(SystemExit):
            drc_cmd(shlex.split(arg))

    def complete_drc(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Tab completion for drc command."""
        return self._complete_path(text, line, begidx, endidx)

    def do_output(self, arg: str) -> None:
        """Set or show the output directory.

        Usage: output [<directory>]

        With no argument, shows current output directory.
        """
        if arg:
            path = Path(arg.strip()).expanduser().resolve()
            self.session.output_dir = path
            print(f"Output directory set to: {path}")
        else:
            print(f"Output directory: {self.session.output_dir}")

    def complete_output(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Tab completion for output command."""
        return self._complete_path(text, line, begidx, endidx)

    def do_clear(self, arg: str) -> None:
        """Clear the session (unload all files).

        Usage: clear
        """
        self.session = InteractiveSession()
        print("Session cleared.")

    def do_quit(self, arg: str) -> bool:
        """Exit interactive mode.

        Usage: quit
        """
        self._save_history()
        print("Goodbye!")
        return True

    def do_exit(self, arg: str) -> bool:
        """Exit interactive mode (alias for quit).

        Usage: exit
        """
        return self.do_quit(arg)

    def do_EOF(self, arg: str) -> bool:
        """Handle Ctrl+D."""
        print()  # Print newline for clean exit
        return self.do_quit(arg)

    def emptyline(self) -> bool:
        """Do nothing on empty line (don't repeat last command)."""
        return False

    def default(self, line: str) -> None:
        """Handle unknown commands."""
        print(f"Unknown command: {line.split()[0]}")
        print("Type 'help' for available commands.")

    def postcmd(self, stop: bool, line: str) -> bool:
        """Update prompt after each command."""
        status = self.session.status_summary()
        if status != "no files loaded":
            self.prompt = f"kicad-tools ({status})> "
        else:
            self.prompt = "kicad-tools> "
        return stop


def main(argv: list[str] | None = None) -> int:
    """Entry point for interactive mode."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="kicad-tools interactive",
        description="Launch interactive REPL mode",
    )
    parser.add_argument(
        "--project",
        help="Auto-load a project on startup",
    )

    args = parser.parse_args(argv)

    # Check if running in a TTY
    if not sys.stdin.isatty():
        print("Warning: Running in non-TTY mode (limited features)", file=sys.stderr)

    shell = InteractiveShell(project=args.project)

    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        print("\nInterrupted")
        shell._save_history()
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
