"""Command registry for kicad-tools CLI.

Provides auto-discovery and registration of new-style commands that
implement the Command protocol. New-style commands coexist with legacy
commands during the incremental migration.

Usage:
    from kicad_tools.cli.registry import discover_commands, register_commands

    # Discover all new-style command modules
    commands = discover_commands()

    # Register them on an argparse subparsers group
    register_commands(subparsers, commands)
"""

import argparse
import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.cli.command_protocol import Command

# Registry of new-style command classes.
# Populated by discover_commands() at startup.
_registry: dict[str, type["Command"]] = {}


def discover_commands() -> dict[str, type["Command"]]:
    """Discover command classes in the new_commands subpackage.

    Scans kicad_tools.cli.new_commands for modules that export a class
    implementing the Command protocol (has name, help, add_arguments, run).

    Returns:
        Dict mapping command names to command classes.
    """
    from kicad_tools.cli.command_protocol import Command

    commands: dict[str, type[Command]] = {}

    try:
        import kicad_tools.cli.new_commands as pkg
    except ImportError:
        return commands

    for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"kicad_tools.cli.new_commands.{modname}")
        except ImportError:
            continue

        # Look for a class named *Command (e.g., ConfigCommand)
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and obj is not Command
                and hasattr(obj, "name")
                and hasattr(obj, "help")
                and hasattr(obj, "add_arguments")
                and hasattr(obj, "run")
            ):
                commands[obj.name] = obj

    global _registry
    _registry = commands
    return commands


def get_registry() -> dict[str, type["Command"]]:
    """Return the current command registry.

    Returns:
        Dict mapping command names to command classes.
    """
    return _registry


def register_commands(
    subparsers: argparse._SubParsersAction,
    commands: dict[str, type["Command"]],
    *,
    skip_existing: bool = True,
) -> None:
    """Register new-style commands on an argparse subparsers group.

    For each command, creates a subparser and calls the command's
    add_arguments() method to populate it. Sets a ``_command_class``
    default on the subparser so dispatch can find the right run() method.

    Args:
        subparsers: The _SubParsersAction from parser.add_subparsers().
        commands: Dict of command name -> command class.
        skip_existing: If True, skip commands whose names already exist
            as subparsers. This allows old-style commands to take
            precedence during migration (set to False to let new-style
            commands override old-style ones).
    """
    # Get existing subparser names to avoid conflicts
    existing_names: set[str] = set()
    if skip_existing and hasattr(subparsers, "_name_parser_map"):
        existing_names = set(subparsers._name_parser_map.keys())

    for name, cmd_class in sorted(commands.items()):
        if name in existing_names:
            continue

        sub = subparsers.add_parser(name, help=cmd_class.help)
        cmd_class.add_arguments(sub)
        # Store the command class so _dispatch_command can find it
        sub.set_defaults(_command_class=cmd_class)
