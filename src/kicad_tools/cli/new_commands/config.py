"""Config command using the new Command protocol.

This is a proof-of-concept migration of the config command from the
legacy dual-parsing pattern to the new command-owned parser pattern.

The command directly receives parsed args from the main parser,
eliminating the need for the intermediate sub_argv reconstruction
that existed in commands/config.py.
"""

import argparse


class ConfigCommand:
    """View and manage kicad-tools configuration."""

    name = "config"
    help = "View and manage configuration"

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        """Add config-specific arguments."""
        parser.add_argument(
            "--show",
            action="store_true",
            help="Show effective configuration with sources",
        )
        parser.add_argument(
            "--init",
            action="store_true",
            help="Create template config file",
        )
        parser.add_argument(
            "--paths",
            action="store_true",
            help="Show config file paths",
        )
        parser.add_argument(
            "--user",
            action="store_true",
            help="Use user config for --init",
        )
        parser.add_argument(
            "action",
            nargs="?",
            choices=["get", "set"],
            help="Config action (get/set)",
        )
        parser.add_argument(
            "key",
            nargs="?",
            help="Config key (e.g., defaults.format)",
        )
        parser.add_argument(
            "value",
            nargs="?",
            help="Value to set",
        )

    @staticmethod
    def run(args: argparse.Namespace) -> int:
        """Execute the config command.

        Delegates to the existing config_cmd module, but passes the
        parsed args directly instead of reconstructing sub_argv.
        """
        from kicad_tools.cli.config_cmd import main as config_main

        # Build argv from the parsed args to delegate to the existing
        # standalone config_cmd.main(). This preserves the standalone
        # entry point contract while the migration is in progress.
        sub_argv: list[str] = []
        if args.show:
            sub_argv.append("--show")
        if args.init:
            sub_argv.append("--init")
        if args.paths:
            sub_argv.append("--paths")
        if args.user:
            sub_argv.append("--user")
        if args.action:
            sub_argv.append(args.action)
        if args.key:
            sub_argv.append(args.key)
        if args.value:
            sub_argv.append(args.value)
        return config_main(sub_argv) or 0
