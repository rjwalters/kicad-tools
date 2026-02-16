"""Tests for the MCP setup command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from kicad_tools.cli.commands.mcp import (
    _find_kct_command,
    _find_project_root,
    _get_claude_code_config_path,
    _run_setup,
)


class TestFindProjectRoot:
    """Tests for _find_project_root."""

    def test_finds_project_root(self):
        """Should find the kicad-tools project root."""
        root = _find_project_root()
        assert root is not None
        assert (root / "pyproject.toml").exists()

    def test_project_root_contains_kicad_tools(self):
        """Project root pyproject.toml should mention kicad-tools."""
        root = _find_project_root()
        assert root is not None
        content = (root / "pyproject.toml").read_text()
        assert "kicad-tools" in content or "kicad_tools" in content


class TestFindKctCommand:
    """Tests for _find_kct_command."""

    def test_returns_command_and_args(self):
        """Should return a (command, args) tuple."""
        command, args = _find_kct_command()
        assert isinstance(command, str)
        assert isinstance(args, list)
        assert len(command) > 0
        assert len(args) > 0

    def test_uv_preferred_for_dev_installs(self):
        """If uv is available and in a project, prefer uv run."""

        def mock_which(cmd):
            if cmd == "uv":
                return "/usr/local/bin/uv"
            if cmd == "kct":
                return "/usr/local/bin/kct"
            return None

        with patch("shutil.which", side_effect=mock_which):
            command, args = _find_kct_command()
            assert command == "/usr/local/bin/uv"
            assert args[0] == "run"
            assert "kct" in args
            assert "mcp" in args
            assert "serve" in args

    def test_global_kct_when_no_uv(self):
        """If kct is globally installed but uv is not, use kct directly."""
        with patch(
            "shutil.which", side_effect=lambda cmd: "/usr/local/bin/kct" if cmd == "kct" else None
        ):
            command, args = _find_kct_command()
            assert command == "/usr/local/bin/kct"
            assert args == ["mcp", "serve"]

    def test_venv_kct_skipped(self):
        """kct inside a .venv should not be used directly."""

        def mock_which(cmd):
            if cmd == "kct":
                return "/some/project/.venv/bin/kct"
            return None

        with patch("shutil.which", side_effect=mock_which):
            command, args = _find_kct_command()
            # Should fall back to python -m, not use the .venv kct
            assert command != "/some/project/.venv/bin/kct"
            assert args == ["-m", "kicad_tools.mcp.server"]

    def test_python_module_fallback(self):
        """If neither kct nor uv is available, fall back to python -m."""
        with patch("shutil.which", return_value=None):
            command, args = _find_kct_command()
            assert "python" in command.lower() or command.endswith("python3")
            assert args == ["-m", "kicad_tools.mcp.server"]


class TestGetClaudeCodeConfigPath:
    """Tests for _get_claude_code_config_path."""

    def test_returns_path(self):
        """Should return a Path to ~/.claude/mcp.json."""
        path = _get_claude_code_config_path()
        assert isinstance(path, Path)
        assert path.name == "mcp.json"
        assert ".claude" in str(path)


class TestRunSetup:
    """Tests for _run_setup."""

    def test_dry_run_does_not_write(self, tmp_path):
        """Dry run should not create any files."""
        config_path = tmp_path / "mcp.json"

        class Args:
            client = "claude-code"
            dry_run = True

        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path",
            return_value=config_path,
        ):
            result = _run_setup(Args())

        assert result == 0
        assert not config_path.exists()

    def test_writes_config_file(self, tmp_path):
        """Setup should write a valid MCP config."""
        config_path = tmp_path / "mcp.json"

        class Args:
            client = "claude-code"
            dry_run = False

        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path",
            return_value=config_path,
        ):
            result = _run_setup(Args())

        assert result == 0
        assert config_path.exists()

        config = json.loads(config_path.read_text())
        assert "mcpServers" in config
        assert "kicad-tools" in config["mcpServers"]

        server = config["mcpServers"]["kicad-tools"]
        assert "command" in server
        assert "args" in server

    def test_merges_with_existing_config(self, tmp_path):
        """Setup should preserve other MCP servers in existing config."""
        config_path = tmp_path / "mcp.json"
        existing = {
            "mcpServers": {
                "other-server": {
                    "command": "other",
                    "args": ["serve"],
                }
            }
        }
        config_path.write_text(json.dumps(existing))

        class Args:
            client = "claude-code"
            dry_run = False

        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path",
            return_value=config_path,
        ):
            result = _run_setup(Args())

        assert result == 0
        config = json.loads(config_path.read_text())
        assert "other-server" in config["mcpServers"]
        assert "kicad-tools" in config["mcpServers"]

    def test_replaces_existing_kicad_tools_config(self, tmp_path):
        """Setup should replace an existing kicad-tools entry."""
        config_path = tmp_path / "mcp.json"
        existing = {
            "mcpServers": {
                "kicad-tools": {
                    "command": "/nonexistent/path/kct",
                    "args": ["mcp", "serve"],
                }
            }
        }
        config_path.write_text(json.dumps(existing))

        class Args:
            client = "claude-code"
            dry_run = False

        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path",
            return_value=config_path,
        ):
            result = _run_setup(Args())

        assert result == 0
        config = json.loads(config_path.read_text())
        server = config["mcpServers"]["kicad-tools"]
        assert server["command"] != "/nonexistent/path/kct"

    def test_creates_parent_directories(self, tmp_path):
        """Setup should create parent dirs if they don't exist."""
        config_path = tmp_path / "nested" / "dir" / "mcp.json"

        class Args:
            client = "claude-code"
            dry_run = False

        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path",
            return_value=config_path,
        ):
            result = _run_setup(Args())

        assert result == 0
        assert config_path.exists()
