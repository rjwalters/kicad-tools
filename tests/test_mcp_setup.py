"""Tests for the MCP setup command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.commands import mcp as mcp_cmd
from kicad_tools.cli.commands.mcp import (
    _find_kct_command,
    _find_project_root,
)
from kicad_tools.cli.parser import create_parser


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


# ---------------------------------------------------------------------------
# Claude Code (issue #6418): ~/.claude.json, never ~/.claude/mcp.json
# ---------------------------------------------------------------------------
#
# No test may reach a real ``claude`` binary or a real home directory: the
# ``claude_home`` fixture points HOME / CLAUDE_CONFIG_DIR at tmp_path and
# ``shutil.which("claude")`` resolves to nothing unless a test installs the
# ``FakeClaude`` stand-in, which replaces ``subprocess.run``.

KCT = "/opt/kct/bin/kct"
ENTRY = {"type": "stdio", "command": KCT, "args": ["mcp", "serve"], "env": {}}
FAKE_CLAUDE = "/fake/bin/claude"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(mcp_cmd, "_find_kct_command", lambda: (KCT, ["mcp", "serve"]))
    monkeypatch.setattr(
        mcp_cmd.shutil, "which", lambda name: None if name == "claude" else f"/usr/bin/{name}"
    )

    def no_subprocess(*args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(mcp_cmd.subprocess, "run", no_subprocess)
    monkeypatch.chdir(tmp_path)
    return home


class FakeClaude:
    """Stand-in for the ``claude`` CLI: edits ``.claude.json`` like the real one."""

    def __init__(self, monkeypatch, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on
        monkeypatch.setattr(
            mcp_cmd.shutil,
            "which",
            lambda name: FAKE_CLAUDE if name == "claude" else f"/usr/bin/{name}",
        )
        monkeypatch.setattr(mcp_cmd.subprocess, "run", self.run)

    def run(self, argv, **kwargs):
        assert argv[0] == FAKE_CLAUDE
        assert kwargs.get("timeout"), "claude calls must be time-bounded"
        self.calls.append(list(argv[1:]))
        verb = argv[2]
        if verb == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", f"{verb} broke")
        env = kwargs["env"]
        base = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else Path(env["HOME"])
        path = base / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        servers = data.setdefault("mcpServers", {})
        name = argv[5]
        if verb == "remove":
            servers.pop(name)
        elif verb == "add-json":
            if name in servers:
                return subprocess.CompletedProcess(argv, 1, "", "already exists")
            servers[name] = json.loads(argv[6])
        path.write_text(json.dumps(data))
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _setup(argv, capsys):
    rc = mcp_cmd.run_mcp_command(create_parser().parse_args(["mcp", "setup", *argv]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _setup_json(argv, capsys):
    rc, out, err = _setup([*argv, "--format", "json"], capsys)
    return rc, json.loads(out), err


class TestClaudeCodePaths:
    def test_user_path_defaults_to_home_claude_json(self, claude_home):
        assert mcp_cmd._get_claude_code_user_config_path() == claude_home / ".claude.json"

    def test_user_path_honours_claude_config_dir(self, claude_home, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert mcp_cmd._get_claude_code_user_config_path() == tmp_path / "cfg" / ".claude.json"

    def test_project_path_is_cwd_mcp_json(self, claude_home, tmp_path):
        assert mcp_cmd._get_claude_code_project_config_path() == tmp_path / ".mcp.json"


class TestClaudeCodeJsonMerge:
    def test_ac1_fresh_home_writes_claude_json(self, claude_home, capsys):
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        data = json.loads((claude_home / ".claude.json").read_text())
        assert data == {"mcpServers": {"kicad-tools": ENTRY}}
        assert not (claude_home / ".claude" / "mcp.json").exists()
        assert payload["method"] == "json-merge"
        assert payload["written"] is True
        assert payload["config_path"] == str(claude_home / ".claude.json")
        assert payload["server"] == ENTRY

    def test_ac2_claude_config_dir(self, claude_home, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert json.loads((cfg / ".claude.json").read_text())["mcpServers"]["kicad-tools"] == ENTRY
        assert not (claude_home / ".claude.json").exists()

    def test_ac3_other_keys_preserved(self, claude_home, capsys):
        before = {
            "numStartups": 7,
            "oauthAccount": {"emailAddress": "x@example.com"},
            "projects": {"/x": {"mcpServers": {"p": {"command": "y"}}, "allowedTools": []}},
            "mcpServers": {"other": {"type": "stdio", "command": "foo", "args": [], "env": {}}},
            "unicode": "Ωmega",
        }
        path = claude_home / ".claude.json"
        path.write_text(json.dumps(before))
        path.chmod(0o600)
        rc, _, _ = _setup_json([], capsys)
        assert rc == 0
        after = json.loads(path.read_text())
        assert after["mcpServers"].pop("kicad-tools") == ENTRY
        assert after == before
        assert path.stat().st_mode & 0o777 == 0o600

    def test_replaces_stale_entry_only(self, claude_home, capsys):
        path = claude_home / ".claude.json"
        path.write_text(json.dumps({"mcpServers": {"kicad-tools": {"command": "old"}, "o": {}}}))
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["replaced"] is True
        assert json.loads(path.read_text())["mcpServers"] == {"kicad-tools": ENTRY, "o": {}}

    @pytest.mark.parametrize(
        "text", ['{"projects": {', "[1, 2]", '"str"', '{"mcpServers": []}', ""]
    )
    @pytest.mark.parametrize("as_json", [True, False])
    def test_ac4_unparseable_config_is_never_clobbered(self, claude_home, capsys, text, as_json):
        path = claude_home / ".claude.json"
        path.write_text(text)
        before = path.read_bytes()
        argv = ["--format", "json"] if as_json else []
        rc, out, err = _setup(argv, capsys)
        assert rc != 0
        assert path.read_bytes() == before
        if as_json:
            payload = json.loads(out)
            assert payload["success"] is False
            assert payload["written"] is False
            assert str(path) in payload["error"]
        else:
            assert str(path) in err

    def test_ac4_cli_never_runs_against_unparseable_config(self, claude_home, monkeypatch, capsys):
        fake = FakeClaude(monkeypatch)
        path = claude_home / ".claude.json"
        path.write_text("{oops")
        rc, _, _ = _setup([], capsys)
        assert rc == 1
        assert fake.calls == []
        assert path.read_text() == "{oops"

    def test_ac6_idempotent(self, claude_home, monkeypatch, capsys):
        assert _setup_json([], capsys)[0] == 0
        path = claude_home / ".claude.json"
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns

        def no_write(*a, **k):
            raise AssertionError("second run must not write")

        monkeypatch.setattr(mcp_cmd, "_write_atomic", no_write)
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["written"] is False
        assert payload["unchanged"] is True
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime
        rc, out, _ = _setup([], capsys)
        assert rc == 0
        assert "Already configured" in out


class TestClaudeCodeCli:
    def test_ac5_cli_first_fresh(self, claude_home, monkeypatch, capsys):
        fake = FakeClaude(monkeypatch)
        monkeypatch.setattr(
            mcp_cmd, "_merge_mcp_server", lambda *a: pytest.fail("must not hand-edit")
        )
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert fake.calls == [
            ["mcp", "add-json", "--scope", "user", "kicad-tools", json.dumps(ENTRY)]
        ]
        assert payload["method"] == "claude-cli"
        assert payload["written"] is True
        assert payload["cli_error"] is None
        data = json.loads((claude_home / ".claude.json").read_text())
        assert data["mcpServers"]["kicad-tools"] == ENTRY

    def test_ac5_removes_differing_entry_first(self, claude_home, monkeypatch, capsys):
        path = claude_home / ".claude.json"
        path.write_text(json.dumps({"mcpServers": {"kicad-tools": {"command": "old"}}}))
        fake = FakeClaude(monkeypatch)
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert fake.verbs == ["remove", "add-json"]
        assert fake.calls[0] == ["mcp", "remove", "--scope", "user", "kicad-tools"]
        assert payload["replaced"] is True
        assert payload["method"] == "claude-cli"

    def test_ac5_cli_passes_environment(self, claude_home, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        FakeClaude(monkeypatch)
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["method"] == "claude-cli"
        assert json.loads((cfg / ".claude.json").read_text())["mcpServers"]["kicad-tools"] == ENTRY

    def test_ac6_idempotent_via_cli(self, claude_home, monkeypatch, capsys):
        fake = FakeClaude(monkeypatch)
        assert _setup_json([], capsys)[0] == 0
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert fake.verbs == ["add-json"]  # the second run made no call at all
        assert payload["written"] is False
        assert payload["unchanged"] is True

    @pytest.mark.parametrize("fail_on", ["add-json", "remove"])
    def test_ac7_cli_failure_falls_back(self, claude_home, monkeypatch, capsys, fail_on):
        path = claude_home / ".claude.json"
        path.write_text(json.dumps({"keep": 1, "mcpServers": {"kicad-tools": {"command": "o"}}}))
        FakeClaude(monkeypatch, fail_on=fail_on)
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["method"] == "json-merge"
        assert payload["written"] is True
        assert fail_on in payload["cli_error"]
        data = json.loads(path.read_text())
        assert data == {"keep": 1, "mcpServers": {"kicad-tools": ENTRY}}

    def test_ac7_cli_timeout_falls_back(self, claude_home, monkeypatch, capsys):
        FakeClaude(monkeypatch)

        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(mcp_cmd.subprocess, "run", hang)
        rc, out, _ = _setup([], capsys)
        assert rc == 0
        assert "Fell back" in out
        data = json.loads((claude_home / ".claude.json").read_text())
        assert data["mcpServers"]["kicad-tools"] == ENTRY

    def test_cli_success_without_entry_falls_back(self, claude_home, monkeypatch, capsys):
        FakeClaude(monkeypatch)
        monkeypatch.setattr(
            mcp_cmd.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["method"] == "json-merge"
        assert "lacks the entry" in payload["cli_error"]
        data = json.loads((claude_home / ".claude.json").read_text())
        assert data["mcpServers"]["kicad-tools"] == ENTRY


class TestClaudeCodeLegacyAndDryRun:
    def _write_legacy(self, home: Path) -> Path:
        legacy = home / ".claude" / "mcp.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({"mcpServers": {"kicad-tools": {"command": "x"}}}))
        return legacy

    def test_ac8_legacy_warning_json(self, claude_home, capsys):
        legacy = self._write_legacy(claude_home)
        before = legacy.read_bytes()
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["legacy_config"] == str(legacy)
        assert legacy.read_bytes() == before

    def test_ac8_legacy_warning_text(self, claude_home, capsys):
        legacy = self._write_legacy(claude_home)
        rc, out, _ = _setup([], capsys)
        assert rc == 0
        assert str(legacy) in out
        assert "never reads" in out
        assert legacy.exists()

    def test_no_legacy_warning_without_entry(self, claude_home, capsys):
        rc, payload, _ = _setup_json([], capsys)
        assert rc == 0
        assert payload["legacy_config"] is None

    @pytest.mark.parametrize("cli", [True, False])
    def test_ac9_dry_run(self, claude_home, monkeypatch, capsys, cli):
        if cli:
            fake = FakeClaude(monkeypatch)
        rc, payload, _ = _setup_json(["--dry-run"], capsys)
        assert rc == 0
        assert payload["dry_run"] is True
        assert payload["written"] is False
        assert payload["config_path"] == str(claude_home / ".claude.json")
        assert payload["method"] == ("claude-cli" if cli else "json-merge")
        assert not (claude_home / ".claude.json").exists()
        rc, out, _ = _setup(["--dry-run"], capsys)
        assert rc == 0
        assert str(claude_home / ".claude.json") in out
        assert "Method: " + ("claude-cli" if cli else "json-merge") in out
        assert "Dry run" in out
        if cli:
            assert fake.calls == []


class TestClaudeCodeProjectScope:
    def test_project_scope_writes_mcp_json(self, claude_home, tmp_path, monkeypatch, capsys):
        fake = FakeClaude(monkeypatch)  # the CLI is not used for project scope
        existing = {"mcpServers": {"other": {"command": "z"}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(existing))
        rc, payload, _ = _setup_json(["--scope", "project"], capsys)
        assert rc == 0
        assert payload["scope"] == "project"
        assert payload["method"] == "json-merge"
        assert payload["config_path"] == str(tmp_path / ".mcp.json")
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert data["mcpServers"] == {"other": {"command": "z"}, "kicad-tools": ENTRY}
        assert fake.calls == []
        assert not (claude_home / ".claude.json").exists()

    @pytest.mark.parametrize("client", ["claude-desktop", "codex", "opencode"])
    def test_scope_rejected_for_other_clients(self, claude_home, capsys, client):
        rc, _, err = _setup(["--client", client, "--scope", "user", "--dry-run"], capsys)
        assert rc == 2
        assert "--scope only applies" in err


class TestClaudeDesktopNoClobber:
    def test_unparseable_desktop_config_is_left_alone(self, claude_home, monkeypatch, capsys):
        target = claude_home / "claude_desktop_config.json"
        target.write_text("{broken")
        monkeypatch.setattr(mcp_cmd, "_get_claude_desktop_config_path", lambda: target)
        rc, _, err = _setup(["--client", "claude-desktop"], capsys)
        assert rc == 1
        assert str(target) in err
        assert target.read_text() == "{broken"

    def test_desktop_merge_keeps_other_keys(self, claude_home, monkeypatch, capsys):
        target = claude_home / "claude_desktop_config.json"
        target.write_text(json.dumps({"globalShortcut": "x", "mcpServers": {"o": {}}}))
        monkeypatch.setattr(mcp_cmd, "_get_claude_desktop_config_path", lambda: target)
        rc, _, _ = _setup(["--client", "claude-desktop"], capsys)
        assert rc == 0
        data = json.loads(target.read_text())
        assert data["globalShortcut"] == "x"
        assert set(data["mcpServers"]) == {"o", "kicad-tools"}
        assert "type" not in data["mcpServers"]["kicad-tools"]
