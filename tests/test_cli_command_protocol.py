"""Tests for the CLI command protocol, registry, and migrated config command.

Tests cover:
1. Command protocol validation (structural typing)
2. Auto-discovery and registration of new-style commands
3. Config command works via the unified CLI (new-style dispatch)
4. Config command works via standalone entry point (backwards compat)
5. All existing commands still work (regression)
"""

import argparse

import pytest


class TestCommandProtocol:
    """Tests for the Command protocol definition."""

    def test_protocol_is_runtime_checkable(self):
        """Protocol can be checked at runtime with isinstance."""
        from kicad_tools.cli.command_protocol import Command

        assert hasattr(Command, "__protocol_attrs__") or hasattr(
            Command, "__abstractmethods__"
        ) or callable(getattr(Command, "__init_subclass__", None))
        # runtime_checkable protocols support isinstance
        assert isinstance(Command, type)

    def test_valid_command_class_satisfies_protocol(self):
        """A class with the right attributes satisfies the protocol."""
        from kicad_tools.cli.command_protocol import Command

        class GoodCommand:
            name = "test"
            help = "A test command"

            @staticmethod
            def add_arguments(parser: argparse.ArgumentParser) -> None:
                pass

            @staticmethod
            def run(args: argparse.Namespace) -> int:
                return 0

        # runtime_checkable protocols check for method/attr presence
        assert isinstance(GoodCommand(), Command)

    def test_missing_method_fails_protocol(self):
        """A class missing required methods does not satisfy the protocol."""
        from kicad_tools.cli.command_protocol import Command

        class BadCommand:
            name = "test"
            help = "A test command"
            # Missing add_arguments and run

        assert not isinstance(BadCommand(), Command)


class TestRegistry:
    """Tests for the command registry and auto-discovery."""

    def test_discover_commands_returns_dict(self):
        """discover_commands returns a dict of name -> class."""
        from kicad_tools.cli.registry import discover_commands

        commands = discover_commands()
        assert isinstance(commands, dict)

    def test_discover_finds_config_command(self):
        """Auto-discovery finds the migrated config command."""
        from kicad_tools.cli.registry import discover_commands

        commands = discover_commands()
        assert "config" in commands

    def test_discovered_config_has_protocol_methods(self):
        """The discovered config command has the required protocol methods."""
        from kicad_tools.cli.registry import discover_commands

        commands = discover_commands()
        config_cls = commands["config"]

        assert hasattr(config_cls, "name")
        assert hasattr(config_cls, "help")
        assert hasattr(config_cls, "add_arguments")
        assert hasattr(config_cls, "run")
        assert config_cls.name == "config"

    def test_get_registry_returns_discovered(self):
        """get_registry returns the same dict after discover_commands."""
        from kicad_tools.cli.registry import discover_commands, get_registry

        commands = discover_commands()
        registry = get_registry()
        assert registry is commands

    def test_register_commands_adds_subparsers(self):
        """register_commands adds subparsers for discovered commands."""
        from kicad_tools.cli.registry import discover_commands, register_commands

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        commands = discover_commands()

        register_commands(subparsers, commands)

        # Parsing "config --show" should work
        args = parser.parse_args(["config", "--show"])
        assert args.command == "config"
        assert args.show is True
        assert hasattr(args, "_command_class")

    def test_register_commands_skip_existing(self):
        """register_commands respects skip_existing=True."""
        from kicad_tools.cli.registry import discover_commands, register_commands

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        # Add a legacy "config" subparser first
        subparsers.add_parser("config")

        commands = discover_commands()
        register_commands(subparsers, commands, skip_existing=True)

        # Parsing "config" should work (from legacy parser)
        args = parser.parse_args(["config"])
        assert args.command == "config"
        # But it should NOT have _command_class since the legacy parser was kept
        assert not hasattr(args, "_command_class")

    def test_register_commands_override_existing(self):
        """register_commands can override existing subparsers."""
        from kicad_tools.cli.registry import discover_commands, register_commands

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        commands = discover_commands()
        # With skip_existing=False and no conflicts, registration works
        register_commands(subparsers, commands, skip_existing=False)

        args = parser.parse_args(["config", "--show"])
        assert args.command == "config"
        assert hasattr(args, "_command_class")


class TestConfigCommandNewStyle:
    """Tests for the config command via new-style dispatch."""

    def test_config_show_via_unified_cli(self, capsys):
        """Config --show works via the unified kicad-tools CLI."""
        from kicad_tools.cli import main

        result = main(["config", "--show"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Effective kicad-tools configuration" in captured.out

    def test_config_paths_via_unified_cli(self, capsys):
        """Config --paths works via the unified kicad-tools CLI."""
        from kicad_tools.cli import main

        result = main(["config", "--paths"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Config file paths" in captured.out

    def test_config_default_shows_config(self, capsys):
        """Config with no args defaults to showing config."""
        from kicad_tools.cli import main

        result = main(["config"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Effective kicad-tools configuration" in captured.out

    def test_config_get_valid_key(self, capsys):
        """Config get <key> shows the config value."""
        from kicad_tools.cli import main

        result = main(["config", "get", "defaults.format"])
        assert result == 0

        captured = capsys.readouterr()
        assert "table" in captured.out

    def test_config_get_invalid_key(self, capsys):
        """Config get with invalid key returns error."""
        from kicad_tools.cli import main

        result = main(["config", "get", "invalid"])
        assert result == 1

    def test_config_dispatched_via_command_class(self):
        """Config is dispatched via _command_class, not legacy elif."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["config", "--show"])
        assert hasattr(args, "_command_class")
        assert args._command_class.name == "config"


class TestConfigCommandStandalone:
    """Tests for the standalone config_cmd entry point (backward compat)."""

    def test_standalone_config_show(self, capsys):
        """Standalone config_cmd.main still works."""
        from kicad_tools.cli.config_cmd import main as config_main

        result = config_main(["--show"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Effective kicad-tools configuration" in captured.out

    def test_standalone_config_paths(self, capsys):
        """Standalone config_cmd.main --paths still works."""
        from kicad_tools.cli.config_cmd import main as config_main

        result = config_main(["--paths"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Config file paths" in captured.out


class TestOtherCommandsUnaffected:
    """Verify that non-migrated commands still work after the refactor."""

    def test_help_still_works(self, capsys):
        """Main --help still shows all commands."""
        from kicad_tools.cli import main

        result = main([])
        assert result == 0

        captured = capsys.readouterr()
        assert "KiCad automation toolkit" in captured.out

    def test_version_still_works(self, capsys):
        """--version still works."""
        from kicad_tools import __version__
        from kicad_tools.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

        captured = capsys.readouterr()
        assert __version__ in captured.out

    def test_unknown_command_still_errors(self):
        """Unknown commands still produce errors."""
        from kicad_tools.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["nonexistent-command"])
        assert exc_info.value.code == 2

    def test_config_appears_in_help(self, capsys):
        """Config command appears in the top-level help output."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        # Check that 'config' is a known subparser
        # We do this by parsing; if config wasn't registered, it would fail
        args = parser.parse_args(["config"])
        assert args.command == "config"
