"""Command protocol for kicad-tools CLI.

Defines the interface that new-style CLI commands must implement.
Commands following this protocol own their argument parser configuration
and execution logic, eliminating the dual-parsing anti-pattern where
parser.py defines arguments and _dispatch_command() reconstructs them.

Usage:
    from kicad_tools.cli.command_protocol import Command

    class MyCommand:
        name = "my-command"
        help = "Description of my command"

        @staticmethod
        def add_arguments(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("input", help="Input file")
            parser.add_argument("--format", default="table", help="Output format")

        @staticmethod
        def run(args: argparse.Namespace) -> int:
            # Direct access to parsed args - no re-parsing needed
            print(f"Processing {args.input} as {args.format}")
            return 0
"""

import argparse
from typing import Protocol, runtime_checkable


@runtime_checkable
class Command(Protocol):
    """Protocol for CLI command modules.

    Each command owns its argument definitions and execution logic.
    This eliminates the dual-parsing anti-pattern where arguments are
    defined in parser.py and then reconstructed as strings for re-parsing.

    Attributes:
        name: The subcommand name (e.g., "config", "route").
        help: Brief help text shown in the top-level --help output.

    Methods:
        add_arguments: Register arguments on the provided subparser.
        run: Execute the command with the parsed argument namespace.
    """

    name: str
    help: str

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        """Add command-specific arguments to the parser.

        Args:
            parser: The argparse subparser for this command.
        """
        ...

    @staticmethod
    def run(args: argparse.Namespace) -> int:
        """Execute the command.

        Args:
            args: Parsed arguments from argparse. All arguments added
                  in add_arguments() are available as attributes.

        Returns:
            Exit code (0 for success, non-zero for errors).
        """
        ...
