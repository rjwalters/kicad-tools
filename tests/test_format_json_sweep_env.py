"""Tests for the #4674 machine-output sweep (environment/integration batch).

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  Batch 1 swept the
grouped families (``tests/test_format_json_sweep.py``), batch 2 the 16
mutating ``sch`` leaves (``tests/test_format_json_sweep_sch.py``) and batch 3
the four families' holdouts (``tests/test_format_json_sweep_families.py``).

**This batch sweeps the environment/integration singles** -- the five leaves
that report on or configure ``kct``'s surroundings rather than a board file:

* ``config``          -- all five modes (``--show`` / ``--paths`` / ``--init``
                         / ``get`` / ``set``) plus the bare default
* ``ipc status``      -- socket discovery + live-KiCad handshake
* ``ipc connect``     -- connection probe
* ``ipc push-routes`` -- track/via push summary
* ``mcp setup``       -- MCP client config wiring

It finishes the ``ipc`` family outright and finishes ``mcp`` (``mcp serve`` is
on the #4543 exemption list -- a long-running server whose machine contract is
the MCP protocol itself).

Same conventions as the three sibling modules (a separate file per batch so
concurrent batches never conflict on a shared ``SWEPT_SURFACES`` literal):

* Outer-parser surface: every swept leaf accepts ``--format`` with a ``json``
  choice, and the default stays ``text``.
* Shim forwarding: the outer ``--format json`` reaches the inner parser argv
  for the argv-reserializing ``config`` shim -- the drift bug class
  ``tests/test_cli_parser_drift.py`` exists for -- and the default (text)
  invocation must NOT forward it.
* Emission: a single valid JSON document on stdout, byte-identical across two
  runs on the same input, structure assertions rather than byte-golden
  payloads, ``{"error": ...}`` documents on failure with exit codes unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.parser import create_parser

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = FIXTURES / "routing-diagnostic.kicad_pcb"

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "config": [],
    "ipc connect": [],
    "ipc push-routes": ["board.kicad_pcb"],
    "ipc status": [],
    "mcp setup": [],
}


def _iter_leaves(parser, path=()):
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        yield path, parser
        return
    for subaction in subactions:
        for name, sub in subaction.choices.items():
            yield from _iter_leaves(sub, (*path, name))


@pytest.fixture(scope="module")
def leaves():
    return {" ".join(path): leaf for path, leaf in _iter_leaves(create_parser())}


# ---------------------------------------------------------------------------
# Outer-parser surface guard
# ---------------------------------------------------------------------------


class TestOuterSurface:
    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_leaf_has_format_json_choice(self, leaves, command):
        """Each swept leaf declares --format with a json choice."""
        leaf = leaves.get(command)
        assert leaf is not None, f"outer parser has no leaf {command!r}"
        for action in leaf._actions:
            if "--format" in action.option_strings:
                assert action.choices and "json" in action.choices, (
                    f"kct {command} has --format without a 'json' choice "
                    f"(choices={action.choices}); regresses #4674"
                )
                break
        else:
            pytest.fail(f"kct {command} lost its --format flag (regresses #4674)")

    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_parse_accepts_format_json(self, command):
        """`kct <command> ... --format json` parses on the real outer parser."""
        argv = [*command.split(), *SWEPT_SURFACES[command], "--format", "json"]
        args = create_parser().parse_args(argv)
        assert args.format == "json"

    @pytest.mark.parametrize("command", sorted(SWEPT_SURFACES))
    def test_default_format_is_text(self, command):
        """The default stays text, so no existing invocation changes shape."""
        args = create_parser().parse_args([*command.split(), *SWEPT_SURFACES[command]])
        assert args.format == "text"

    def test_mcp_serve_stays_exempt(self, leaves):
        """`mcp serve` is on the #4543 exemption list, so it gains no flag."""
        serve = leaves["mcp serve"]
        assert not any("--format" in a.option_strings for a in serve._actions)


# ---------------------------------------------------------------------------
# Shim forwarding (outer --format json must reach the inner parser argv)
# ---------------------------------------------------------------------------


class TestConfigShimForwarding:
    def test_shim_forwards_format(self):
        from kicad_tools.cli.commands.config import run_config_command

        args = create_parser().parse_args(["config", "--show", "--format", "json"])
        with patch("kicad_tools.cli.config_cmd.main", return_value=0) as inner:
            assert run_config_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"config shim dropped --format: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_shim_forwards_format_with_positional_action(self):
        """`config get <key> --format json` keeps both the flag and the argv."""
        from kicad_tools.cli.commands.config import run_config_command

        args = create_parser().parse_args(["config", "get", "defaults.format", "--format", "json"])
        with patch("kicad_tools.cli.config_cmd.main", return_value=0) as inner:
            assert run_config_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"
        assert "get" in sub_argv and "defaults.format" in sub_argv

    def test_shim_omits_format_for_text_default(self):
        """Default (text) invocations must not forward --format (behaviour pin)."""
        from kicad_tools.cli.commands.config import run_config_command

        args = create_parser().parse_args(["config", "--show"])
        with patch("kicad_tools.cli.config_cmd.main", return_value=0) as inner:
            assert run_config_command(args) == 0
        assert "--format" not in inner.call_args[0][0]


# ---------------------------------------------------------------------------
# config emission
# ---------------------------------------------------------------------------


def _run_config(argv, capsys):
    from kicad_tools.cli.commands.config import run_config_command

    rc = run_config_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run inside an empty directory so no project config leaks in."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestConfigEmission:
    def test_show_document(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "--show", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "config"
        assert payload["action"] == "show"
        assert payload["success"] is True
        # Every advertised section is present, each key carrying value+source.
        for section in ("defaults", "drc", "export", "route", "parts"):
            assert section in payload["sections"], section
        entry = payload["sections"]["route"]["clearance"]
        assert set(entry) == {"value", "source"}
        assert set(payload["native_backends"]) == {"router", "placement", "drc"}
        for info in payload["native_backends"].values():
            assert set(info) == {"available", "version"}

    def test_bare_config_defaults_to_show(self, isolated_cwd, capsys):
        """`kct config --format json` (no mode) emits the show document."""
        rc, out = _run_config(["config", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["action"] == "show"

    def test_show_is_deterministic(self, isolated_cwd, capsys):
        argv = ["config", "--show", "--format", "json"]
        _, first = _run_config(argv, capsys)
        _, second = _run_config(argv, capsys)
        assert first == second

    def test_paths_document(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "--paths", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["action"] == "paths"
        assert payload["project_config"]["exists"] is False
        assert payload["project_config"]["path"] is None
        assert ".kicad-tools.toml" in payload["project_config"]["search_filenames"]
        assert payload["project_config"]["search_filenames"] == sorted(
            payload["project_config"]["search_filenames"]
        )
        assert isinstance(payload["user_config"]["path"], str)
        assert isinstance(payload["user_config"]["exists"], bool)

    def test_get_document(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "get", "route.clearance", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["action"] == "get"
        assert payload["key"] == "route.clearance"
        assert payload["section"] == "route"
        assert payload["option"] == "clearance"
        assert payload["source"] == "default"
        assert payload["success"] is True
        assert "value" in payload

    @pytest.mark.parametrize(
        "key,fragment",
        [
            ("bogus", "Invalid key format"),
            ("nosuch.key", "Unknown config section"),
            ("route.nosuch", "Unknown key"),
        ],
    )
    def test_get_errors_are_documents(self, isolated_cwd, capsys, key, fragment):
        rc, out = _run_config(["config", "get", key, "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["action"] == "get"
        assert payload["success"] is False
        assert fragment in payload["error"]
        assert payload["key"] == key

    def test_set_document_reports_it_did_not_write(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "set", "drc.strict", "yes", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["action"] == "set"
        assert payload["applied"] is False, "config set never writes; say so structurally"
        assert payload["value"] == "true"
        assert payload["toml"] == "[drc]\nstrict = true"
        assert payload["config_file"].endswith(".kicad-tools.toml")
        assert not list(isolated_cwd.glob("*.toml")), "config set must not write a file"

    def test_set_invalid_value_is_document(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "set", "drc.strict", "maybe", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "Invalid boolean value" in payload["error"]

    def test_init_document(self, isolated_cwd, capsys):
        rc, out = _run_config(["config", "--init", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["action"] == "init"
        assert payload["created"] is True
        assert payload["scope"] == "project"
        assert Path(payload["path"]).exists()

    def test_init_existing_file_is_document(self, isolated_cwd, capsys):
        _run_config(["config", "--init", "--format", "json"], capsys)
        rc, out = _run_config(["config", "--init", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["created"] is False
        assert payload["success"] is False
        assert "already exists" in payload["error"]

    @pytest.mark.parametrize(
        "argv,fragment",
        [
            (["config", "--show"], "# Effective kicad-tools configuration"),
            (["config", "--paths"], "Config file paths:"),
            (["config", "set", "drc.strict", "yes"], "add to your config file:"),
        ],
    )
    def test_text_mode_still_prints_prose(self, isolated_cwd, capsys, argv, fragment):
        """Text mode still prints prose, unchanged by this batch."""
        rc, out = _run_config(argv, capsys)
        assert rc == 0
        assert fragment in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_text_mode_get_prints_the_bare_value(self, isolated_cwd, capsys):
        """`config get` prints the raw value (a scalar, not a document)."""
        rc, out = _run_config(["config", "get", "route.strategy"], capsys)
        assert rc == 0
        assert out.strip() and not out.lstrip().startswith("{")


# ---------------------------------------------------------------------------
# ipc emission
# ---------------------------------------------------------------------------


def _run_ipc(argv, capsys):
    from kicad_tools.cli.commands.ipc import run_ipc_command

    rc = run_ipc_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


def _fake_client(version="9.0.1", docs=None, ping=True):
    """A context-manager mock standing in for :class:`IPCClient`."""
    client = MagicMock()
    client.ping.return_value = ping
    client.get_version.return_value = version
    client.get_open_documents.return_value = docs if docs is not None else []
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    factory.return_value.__exit__.return_value = False
    return factory


class TestIpcStatusEmission:
    def test_no_instances_is_error_document(self, capsys):
        with (
            patch("kicad_tools.ipc.discovery.discover_instances", return_value=[]),
            patch("kicad_tools.ipc.discovery.discover_socket", return_value=None),
        ):
            rc, out = _run_ipc(["ipc", "status", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "status"
        assert payload["connected"] is False
        assert payload["success"] is False
        assert payload["instances"] == []
        assert "No running KiCad instances" in payload["error"]

    def test_explicit_socket_not_found_is_error_document(self, capsys):
        with patch("kicad_tools.ipc.discovery.discover_socket", return_value=None):
            rc, out = _run_ipc(
                ["ipc", "status", "--socket", "/nope/kicad.sock", "--format", "json"], capsys
            )
        assert rc == 1
        payload = json.loads(out)
        assert payload["socket"] == "/nope/kicad.sock"
        assert "Socket not found" in payload["error"]

    def test_connected_document(self, capsys):
        docs = [{"path": "/b/second.kicad_pcb"}, {"path": "/a/first.kicad_sch"}]
        with (
            patch("kicad_tools.ipc.discovery.discover_socket", return_value="/tmp/k.sock"),
            patch("kicad_tools.ipc.client.IPCClient", _fake_client(docs=docs)),
        ):
            rc, out = _run_ipc(
                ["ipc", "status", "--socket", "/tmp/k.sock", "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "status"
        assert payload["connected"] is True
        assert payload["kicad_version"] == "9.0.1"
        assert payload["socket"] == "/tmp/k.sock"
        # Collections are sorted so the document is deterministic (#4674).
        assert payload["open_documents"] == ["/a/first.kicad_sch", "/b/second.kicad_pcb"]
        assert payload["success"] is True

    def test_connected_document_is_deterministic(self, capsys):
        docs = [{"path": "/b/second.kicad_pcb"}, {"path": "/a/first.kicad_sch"}]
        argv = ["ipc", "status", "--socket", "/tmp/k.sock", "--format", "json"]
        with (
            patch("kicad_tools.ipc.discovery.discover_socket", return_value="/tmp/k.sock"),
            patch("kicad_tools.ipc.client.IPCClient", _fake_client(docs=docs)),
        ):
            _, first = _run_ipc(argv, capsys)
            _, second = _run_ipc(argv, capsys)
        assert first == second

    def test_unhealthy_instance_is_error_document(self, capsys):
        with (
            patch("kicad_tools.ipc.discovery.discover_socket", return_value="/tmp/k.sock"),
            patch("kicad_tools.ipc.client.IPCClient", _fake_client(ping=False)),
        ):
            rc, out = _run_ipc(
                ["ipc", "status", "--socket", "/tmp/k.sock", "--format", "json"], capsys
            )
        assert rc == 1
        payload = json.loads(out)
        assert payload["connected"] is False
        assert "health checks" in payload["error"]

    def test_text_mode_is_not_json(self, capsys):
        with (
            patch("kicad_tools.ipc.discovery.discover_instances", return_value=[]),
            patch("kicad_tools.ipc.discovery.discover_socket", return_value=None),
        ):
            rc, out = _run_ipc(["ipc", "status"], capsys)
        assert rc == 1
        assert "No running KiCad instances found." in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestIpcConnectEmission:
    def test_no_socket_is_error_document(self, capsys):
        with patch("kicad_tools.ipc.discovery.discover_socket", return_value=None):
            rc, out = _run_ipc(["ipc", "connect", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "connect"
        assert payload["connected"] is False
        assert payload["socket"] is None
        assert "No KiCad IPC socket found" in payload["error"]

    def test_connected_document(self, capsys):
        with (
            patch("kicad_tools.ipc.discovery.discover_socket", return_value="/tmp/k.sock"),
            patch("kicad_tools.ipc.client.IPCClient", _fake_client(version="9.0.2")),
        ):
            rc, out = _run_ipc(["ipc", "connect", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["connected"] is True
        assert payload["kicad_version"] == "9.0.2"
        assert payload["socket"] == "/tmp/k.sock"
        assert payload["success"] is True

    def test_connection_failure_is_error_document(self, capsys):
        from kicad_tools.ipc.client import IPCError

        factory = MagicMock()
        factory.return_value.__enter__.side_effect = IPCError("socket refused")
        with (
            patch("kicad_tools.ipc.discovery.discover_socket", return_value="/tmp/k.sock"),
            patch("kicad_tools.ipc.client.IPCClient", factory),
        ):
            rc, out = _run_ipc(["ipc", "connect", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "socket refused" in payload["error"]

    def test_text_mode_is_not_json(self, capsys):
        with patch("kicad_tools.ipc.discovery.discover_socket", return_value=None):
            rc, out = _run_ipc(["ipc", "connect"], capsys)
        assert rc == 1
        assert "No KiCad IPC socket found." in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestIpcPushRoutesEmission:
    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_ipc(
            ["ipc", "push-routes", str(tmp_path / "nope.kicad_pcb"), "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "push-routes"
        assert payload["success"] is False
        assert payload["pushed"] == 0
        assert "not found" in payload["error"]

    def test_dry_run_document(self, capsys):
        """A real board yields one well-formed document either way.

        ``push-routes`` imports ``kicad_tools.pcb.parser``, a module that does
        not exist (dead since #2363), so today every existing board short-
        circuits into the "PCB parser not available." branch -- which this
        batch turns from prose into a structured document.  The assertions are
        written to hold both before and after that unrelated bug is fixed.
        """
        rc, out = _run_ipc(
            ["ipc", "push-routes", str(BOARD), "--dry-run", "--format", "json"], capsys
        )
        payload = json.loads(out)
        assert payload["command"] == "push-routes"
        assert payload["pcb"] == str(BOARD)
        assert payload["pushed"] == 0
        if payload["success"]:
            assert rc == 0
            assert payload["dry_run"] is True
            assert payload["net_filter"] is None
            assert isinstance(payload["tracks"], int)
            assert isinstance(payload["vias"], int)
        else:
            assert rc == 1
            assert payload["error"]

    def test_dry_run_is_deterministic(self, capsys):
        argv = ["ipc", "push-routes", str(BOARD), "--dry-run", "--format", "json"]
        _, first = _run_ipc(argv, capsys)
        _, second = _run_ipc(argv, capsys)
        assert first == second

    def test_parser_unavailable_is_error_document(self, capsys):
        """The pre-parse failure path is a document, not prose (#4674)."""
        with patch.dict("sys.modules", {"kicad_tools.pcb.parser": None}):
            rc, out = _run_ipc(["ipc", "push-routes", str(BOARD), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "push-routes"
        assert payload["success"] is False
        assert payload["pushed"] == 0
        assert "PCB parser not available" in payload["error"]

    def test_no_socket_is_error_document(self, capsys):
        """With tracks in hand but no socket, the failure is still a document."""
        board = MagicMock()
        board.tracks = [MagicMock()]
        board.vias = []
        board.nets = []
        parser_module = MagicMock()
        parser_module.parse_pcb.return_value = board
        with (
            patch.dict("sys.modules", {"kicad_tools.pcb.parser": parser_module}),
            patch("kicad_tools.ipc.discovery.discover_socket", return_value=None),
        ):
            rc, out = _run_ipc(["ipc", "push-routes", str(BOARD), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["pushed"] == 0
        assert payload["tracks"] == 1
        assert "No KiCad IPC socket found" in payload["error"]

    def test_dry_run_text_mode_is_not_json(self, capsys):
        rc, out = _run_ipc(["ipc", "push-routes", str(BOARD), "--dry-run"], capsys)
        assert "Error" in out or "Dry run" in out
        assert rc in (0, 1)
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# mcp setup emission
# ---------------------------------------------------------------------------


def _run_mcp(argv, capsys):
    from kicad_tools.cli.commands.mcp import run_mcp_command

    rc = run_mcp_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestMcpSetupEmission:
    def test_dry_run_document(self, capsys):
        rc, out = _run_mcp(["mcp", "setup", "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "setup"
        assert payload["client"] == "claude-code"
        assert payload["dry_run"] is True
        assert payload["written"] is False
        assert payload["replaced"] is False
        assert payload["success"] is True
        assert isinstance(payload["config_path"], str)
        assert set(payload["server"]) == {"command", "args", "env"}

    def test_dry_run_is_deterministic(self, capsys):
        argv = ["mcp", "setup", "--dry-run", "--format", "json"]
        _, first = _run_mcp(argv, capsys)
        _, second = _run_mcp(argv, capsys)
        assert first == second

    def test_write_document(self, tmp_path, capsys):
        target = tmp_path / "mcp.json"
        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path", return_value=target
        ):
            rc, out = _run_mcp(["mcp", "setup", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["written"] is True
        assert payload["replaced"] is False
        assert payload["config_path"] == str(target)
        written = json.loads(target.read_text())
        assert written["mcpServers"]["kicad-tools"] == payload["server"]

    def test_replacing_existing_entry_is_flagged(self, tmp_path, capsys):
        target = tmp_path / "mcp.json"
        target.write_text(json.dumps({"mcpServers": {"kicad-tools": {"command": "old"}}}))
        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_code_config_path", return_value=target
        ):
            rc, out = _run_mcp(["mcp", "setup", "--format", "json"], capsys)
        assert rc == 0
        assert json.loads(out)["replaced"] is True

    def test_claude_desktop_client_is_reported(self, tmp_path, capsys):
        target = tmp_path / "claude_desktop_config.json"
        with patch(
            "kicad_tools.cli.commands.mcp._get_claude_desktop_config_path", return_value=target
        ):
            rc, out = _run_mcp(
                ["mcp", "setup", "--client", "claude-desktop", "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["client"] == "claude-desktop"
        assert payload["config_path"] == str(target)

    def test_text_mode_is_not_json(self, capsys):
        """Text mode prints prose plus an indented preview, not one document."""
        rc, out = _run_mcp(["mcp", "setup", "--dry-run"], capsys)
        assert rc == 0
        assert "MCP client: claude-code" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
