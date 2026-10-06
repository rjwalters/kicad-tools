"""``kct mcp setup --client codex|opencode`` (issue #5953).

Golden tests for the pure renderers in
:mod:`kicad_tools.cli.commands.mcp_clients`, plus CLI-level tests that point
``HOME`` / ``CODEX_HOME`` / ``XDG_CONFIG_HOME`` at ``tmp_path`` so no test can
ever read or write a real home directory.

Goldens live in ``tests/fixtures/mcp_setup_golden/``: ``*.in.*`` is the
existing config, ``*.out.*`` the exact expected file after setup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.commands import mcp as mcp_cmd
from kicad_tools.cli.commands.mcp_clients import (
    STATUS_ADDED,
    STATUS_REPLACED,
    STATUS_UNCHANGED,
    ClientConfigError,
    parse_opencode_version,
    render_codex_config,
    render_opencode_config,
)
from kicad_tools.cli.parser import create_parser

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

GOLDEN = Path(__file__).parent / "fixtures" / "mcp_setup_golden"
KCT = "/opt/kct/bin/kct"
ARGS = ["mcp", "serve"]


def _golden(name: str) -> str:
    return (GOLDEN / name).read_text()


# ---------------------------------------------------------------------------
# Codex renderer
# ---------------------------------------------------------------------------


class TestCodexRenderer:
    @pytest.mark.parametrize(
        ("source", "golden", "status"),
        [
            (None, "codex_new.out.toml", STATUS_ADDED),
            ("codex_merge.in.toml", "codex_merge.out.toml", STATUS_ADDED),
            ("codex_replace.in.toml", "codex_replace.out.toml", STATUS_REPLACED),
        ],
    )
    def test_golden(self, source, golden, status):
        existing = _golden(source) if source else None
        text, got_status, entry = render_codex_config(existing, KCT, ARGS)
        assert text == _golden(golden)
        assert got_status == status
        assert entry == {"command": KCT, "args": ARGS}

    @pytest.mark.parametrize("golden", ["codex_new.out.toml", "codex_merge.out.toml"])
    def test_rerun_is_noop(self, golden):
        text = _golden(golden)
        assert render_codex_config(text, KCT, ARGS) == (
            text,
            STATUS_UNCHANGED,
            {"command": KCT, "args": ARGS},
        )

    def test_replace_preserves_everything_else(self):
        before = tomllib.loads(_golden("codex_replace.in.toml"))
        text, _, _ = render_codex_config(_golden("codex_replace.in.toml"), KCT, ARGS)
        after = tomllib.loads(text)
        kct = after["mcp_servers"]["kct"]
        assert kct["command"] == KCT and kct["args"] == ARGS
        assert kct["startup_timeout_sec"] == 30
        assert kct["env"] == {"KCT_LOG": "debug"}
        assert after["mcp_servers"]["other"] == before["mcp_servers"]["other"]
        assert after["model"] == before["model"]

    def test_no_trailing_newline_is_handled(self):
        text, status, _ = render_codex_config('model = "o3"', KCT, ARGS)
        assert status == STATUS_ADDED
        assert text.startswith('model = "o3"\n\n[mcp_servers.kct]\n')
        assert tomllib.loads(text)["mcp_servers"]["kct"]["command"] == KCT

    def test_paths_are_escaped(self):
        odd = 'C:\\Program Files\\kct "beta"\\kct.exe'
        text, _, _ = render_codex_config(None, odd, ARGS)
        assert tomllib.loads(text)["mcp_servers"]["kct"]["command"] == odd

    def test_quoted_header_is_recognised(self):
        existing = '[mcp_servers."kct"]\ncommand = "/old"\nargs = []\n'
        text, status, _ = render_codex_config(existing, KCT, ARGS)
        assert status == STATUS_REPLACED
        assert text.count("[mcp_servers") == 1

    @pytest.mark.parametrize(
        "existing",
        [
            "this is = = not toml\n",
            'mcp_servers = { kct = { command = "/old" } }\n',
            '[mcp_servers]\nkct.command = "/old"\n',
            'mcp_servers = "nope"\n',
        ],
        ids=["invalid", "inline-table", "dotted-keys", "non-table"],
    )
    def test_refuses_what_it_cannot_edit_safely(self, existing):
        with pytest.raises(ClientConfigError):
            render_codex_config(existing, KCT, ARGS)

    def test_inline_servers_table_without_kct_is_refused(self):
        # Appending a [mcp_servers.kct] header would redefine an inline table.
        with pytest.raises(ClientConfigError):
            render_codex_config('mcp_servers = { other = { command = "x" } }\n', KCT, ARGS)


# ---------------------------------------------------------------------------
# opencode renderer
# ---------------------------------------------------------------------------


class TestOpencodeRenderer:
    @pytest.mark.parametrize(
        ("source", "schema", "golden", "status"),
        [
            (None, "v2", "opencode_new_v2.out.json", STATUS_ADDED),
            (None, "v1", "opencode_new_v1.out.json", STATUS_ADDED),
            ("opencode_merge.in.json", "v2", "opencode_merge.out.json", STATUS_REPLACED),
            ("opencode_legacy.in.json", "v2", "opencode_legacy_to_v2.out.json", STATUS_REPLACED),
            ("opencode_legacy.in.json", "v1", "opencode_legacy_v1.out.json", STATUS_REPLACED),
        ],
    )
    def test_golden(self, source, schema, golden, status):
        existing = _golden(source) if source else None
        text, got_status, entry = render_opencode_config(existing, KCT, ARGS, schema)
        assert text == _golden(golden)
        assert got_status == status
        assert entry["type"] == "local"
        assert entry["command"] == [KCT, *ARGS]

    @pytest.mark.parametrize(
        ("golden", "schema"),
        [
            ("opencode_new_v2.out.json", "v2"),
            ("opencode_new_v1.out.json", "v1"),
            ("opencode_merge.out.json", "v2"),
            ("opencode_legacy_to_v2.out.json", "v2"),
            ("opencode_legacy_v1.out.json", "v1"),
        ],
    )
    def test_rerun_is_noop(self, golden, schema):
        text = _golden(golden)
        new_text, status, _ = render_opencode_config(text, KCT, ARGS, schema)
        assert status == STATUS_UNCHANGED
        assert new_text == text

    def test_v2_matches_opencode_mcp_add_shape(self):
        # `opencode mcp add kct -- <kct> mcp serve` (v2.0.22) writes exactly this.
        data = json.loads(render_opencode_config(None, KCT, ARGS, "v2")[0])
        assert data == {
            "mcp": {"servers": {"kct": {"type": "local", "command": [KCT, "mcp", "serve"]}}}
        }

    def test_v2_drops_legacy_duplicate(self):
        data = json.loads(_golden("opencode_legacy_to_v2.out.json"))
        assert "kct" not in data["mcp"]
        assert "enabled" not in data["mcp"]["servers"]["kct"]
        assert data["mcp"]["other"]["enabled"] is True  # untouched

    def test_empty_file_is_treated_as_new(self):
        text, status, _ = render_opencode_config("  \n", KCT, ARGS, "v2")
        assert status == STATUS_ADDED
        assert text == _golden("opencode_new_v2.out.json")

    @pytest.mark.parametrize(
        ("existing", "schema"),
        [
            ('{\n  // a JSONC comment\n  "mcp": {}\n}\n', "v2"),
            ("[1, 2]", "v2"),
            ('{"mcp": []}', "v2"),
            ('{"mcp": {"servers": []}}', "v2"),
            ('{"mcp": {"servers": {"x": {"type": "local", "command": ["x"]}}}}', "v1"),
        ],
        ids=["jsonc", "not-object", "mcp-not-object", "servers-not-object", "v1-on-v2-file"],
    )
    def test_refuses_what_it_cannot_edit_safely(self, existing, schema):
        with pytest.raises(ClientConfigError):
            render_opencode_config(existing, KCT, ARGS, schema)

    @pytest.mark.parametrize(
        ("output", "major"),
        [("2.0.22\n", 2), ("opencode v2.0.22", 2), ("1.14.3", 1), ("dev build", None)],
    )
    def test_parse_opencode_version(self, output, major):
        assert parse_opencode_version(output) == major


# ---------------------------------------------------------------------------
# CLI: `kct mcp setup --client codex|opencode`, sandboxed in tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every home-ish location at tmp_path and pin the kct command."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(mcp_cmd, "_find_kct_command", lambda: (KCT, list(ARGS)))
    return tmp_path


def _setup(argv, capsys):
    rc = mcp_cmd.run_mcp_command(create_parser().parse_args(["mcp", "setup", *argv]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _no_opencode_binary():
    return patch.object(mcp_cmd.shutil, "which", return_value=None)


class TestCodexCli:
    def test_default_path_is_home_codex(self, sandbox, capsys):
        rc, out, _ = _setup(["--client", "codex", "--format", "json"], capsys)
        payload = json.loads(out)
        target = sandbox / "home" / ".codex" / "config.toml"
        assert rc == 0
        assert payload["config_path"] == str(target)
        assert payload["written"] is True and payload["unchanged"] is False
        assert payload["server"] == {"command": KCT, "args": ARGS}
        assert payload["server_name"] == "kct"
        assert target.read_text() == _golden("codex_new.out.toml")

    def test_codex_home_is_honoured(self, sandbox, capsys, monkeypatch):
        codex_home = sandbox / "bench-run-7"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        rc, out, _ = _setup(["--client", "codex", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["config_path"] == str(codex_home / "config.toml")
        assert (codex_home / "config.toml").exists()
        assert not (sandbox / "home" / ".codex").exists()

    def test_dry_run_writes_nothing(self, sandbox, capsys):
        rc, out, _ = _setup(["--client", "codex", "--dry-run"], capsys)
        assert rc == 0
        assert "Dry run" in out
        assert "[mcp_servers.kct]" in out
        assert f'command = "{KCT}"' in out
        assert not (sandbox / "home" / ".codex").exists()

    def test_merge_then_rerun_is_noop(self, sandbox, capsys):
        target = sandbox / "home" / ".codex" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text(_golden("codex_merge.in.toml"))
        assert _setup(["--client", "codex"], capsys)[0] == 0
        assert target.read_text() == _golden("codex_merge.out.toml")
        mtime = target.stat().st_mtime_ns

        rc, out, _ = _setup(["--client", "codex", "--format", "json"], capsys)
        payload = json.loads(out)
        assert rc == 0
        assert payload["unchanged"] is True and payload["written"] is False
        assert target.stat().st_mtime_ns == mtime
        assert "Already configured" in _setup(["--client", "codex"], capsys)[1]

    def test_unparseable_config_is_not_clobbered(self, sandbox, capsys):
        target = sandbox / "home" / ".codex" / "config.toml"
        target.parent.mkdir(parents=True)
        target.write_text("not = = toml\n")
        rc, out, _ = _setup(["--client", "codex", "--format", "json"], capsys)
        payload = json.loads(out)
        assert rc == 1
        assert payload["success"] is False and "not valid TOML" in payload["error"]
        assert target.read_text() == "not = = toml\n"

    def test_project_flag_rejected(self, sandbox, capsys):
        rc, _, err = _setup(["--client", "codex", "--project"], capsys)
        assert rc == 1
        assert "--project only applies" in err


class TestOpencodeCli:
    def test_user_config_under_xdg_default(self, sandbox, capsys):
        with _no_opencode_binary():
            rc, out, _ = _setup(["--client", "opencode", "--format", "json"], capsys)
        payload = json.loads(out)
        target = sandbox / "home" / ".config" / "opencode" / "opencode.json"
        assert rc == 0
        assert payload["config_path"] == str(target)
        assert payload["opencode_schema"] == "v2"
        assert target.read_text() == _golden("opencode_new_v2.out.json")

    def test_xdg_config_home_is_honoured(self, sandbox, capsys, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(sandbox / "xdg"))
        with _no_opencode_binary():
            rc, out, _ = _setup(["--client", "opencode", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["config_path"] == str(sandbox / "xdg" / "opencode" / "opencode.json")

    def test_project_flag_writes_project_file(self, sandbox, capsys):
        project = sandbox / "board-repo"
        project.mkdir()
        with _no_opencode_binary():
            rc, out, _ = _setup(
                ["--client", "opencode", "--project", str(project), "--format", "json"], capsys
            )
        assert rc == 0
        assert json.loads(out)["config_path"] == str(project.resolve() / "opencode.json")
        assert (project / "opencode.json").exists()
        assert not (sandbox / "home" / ".config").exists()

    def test_project_flag_defaults_to_cwd(self, sandbox, capsys, monkeypatch):
        monkeypatch.chdir(sandbox)
        with _no_opencode_binary():
            rc, out, _ = _setup(["--client", "opencode", "--project", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["config_path"] == str(sandbox.resolve() / "opencode.json")

    def test_existing_jsonc_is_targeted_not_shadowed(self, sandbox, capsys):
        project = sandbox / "p"
        project.mkdir()
        (project / "opencode.jsonc").write_text('{\n  // keep\n  "mcp": {}\n}\n')
        with _no_opencode_binary():
            rc, out, _ = _setup(
                ["--client", "opencode", "--project", str(project), "--format", "json"], capsys
            )
        payload = json.loads(out)
        assert rc == 1
        assert payload["config_path"].endswith("opencode.jsonc")
        assert not (project / "opencode.json").exists()

    def test_dry_run_writes_nothing(self, sandbox, capsys):
        with _no_opencode_binary():
            rc, out, _ = _setup(["--client", "opencode", "--dry-run"], capsys)
        assert rc == 0
        assert '"servers"' in out and KCT in out
        assert not (sandbox / "home" / ".config").exists()

    def test_merge_then_rerun_is_noop(self, sandbox, capsys):
        target = sandbox / "home" / ".config" / "opencode" / "opencode.json"
        target.parent.mkdir(parents=True)
        target.write_text(_golden("opencode_merge.in.json"))
        with _no_opencode_binary():
            assert _setup(["--client", "opencode"], capsys)[0] == 0
            assert target.read_text() == _golden("opencode_merge.out.json")
            rc, out, _ = _setup(["--client", "opencode", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["unchanged"] is True

    def test_legacy_file_without_binary_keeps_v1(self, sandbox, capsys):
        target = sandbox / "home" / ".config" / "opencode" / "opencode.json"
        target.parent.mkdir(parents=True)
        target.write_text(_golden("opencode_legacy.in.json"))
        with _no_opencode_binary():
            rc, out, _ = _setup(["--client", "opencode", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["opencode_schema"] == "v1"
        assert target.read_text() == _golden("opencode_legacy_v1.out.json")

    @pytest.mark.parametrize(("version", "schema"), [("2.0.22\n", "v2"), ("1.14.3\n", "v1")])
    def test_installed_version_picks_schema(self, sandbox, capsys, version, schema):
        class Proc:
            stdout = version
            stderr = ""

        with (
            patch.object(mcp_cmd.shutil, "which", return_value="/usr/local/bin/opencode"),
            patch.object(mcp_cmd.subprocess, "run", return_value=Proc()) as run,
        ):
            rc, out, _ = _setup(["--client", "opencode", "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["opencode_schema"] == schema
        assert run.call_args.args[0] == ["/usr/local/bin/opencode", "--version"]
        assert run.call_args.kwargs["timeout"] > 0

    def test_explicit_schema_skips_detection(self, sandbox, capsys):
        with patch.object(mcp_cmd, "_detect_opencode_schema") as detect:
            rc, out, _ = _setup(
                ["--client", "opencode", "--opencode-schema", "v1", "--format", "json"], capsys
            )
        detect.assert_not_called()
        assert rc == 0
        assert json.loads(out)["server"]["enabled"] is True


def test_project_flag_rejected_for_claude_clients(sandbox, capsys):
    rc, _, err = _setup(["--client", "claude-code", "--project", "--dry-run"], capsys)
    assert rc == 2
    assert "--project only applies" in err


def test_help_lists_all_four_clients():
    parser = create_parser()
    mcp_action = next(a for a in parser._subparsers._group_actions if "mcp" in a.choices)
    setup = mcp_action.choices["mcp"]._subparsers._group_actions[0].choices["setup"]
    help_text = setup.format_help()
    for client in ("claude-code", "claude-desktop", "codex", "opencode"):
        assert client in help_text
