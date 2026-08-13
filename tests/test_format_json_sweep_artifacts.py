"""Tests for the #4674 machine-output sweep (board-artifact batch).

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  Batch 1 swept the
grouped families (``tests/test_format_json_sweep.py``), batch 2 the 16
mutating ``sch`` leaves (``tests/test_format_json_sweep_sch.py``), batch 3 the
four families' holdouts (``tests/test_format_json_sweep_families.py``) and
batch 4 the environment/integration singles
(``tests/test_format_json_sweep_env.py``).

**This batch sweeps the board-artifact producers** -- the five singles that
turn a board or schematic into a file on disk:

* ``board-metrics``   -- the normalized ``board.json`` per demo board
* ``create-pcb``      -- a PCB generated from a schematic
* ``panel``           -- a manufacturing panel generated from a board
* ``report generate`` -- a versioned Markdown/PDF design report
* ``screenshot``      -- a PNG capture of a board or schematic

Same conventions as the four sibling modules (a separate file per batch so
concurrent batches never conflict on a shared ``SWEPT_SURFACES`` literal):

* Outer-parser surface: every swept leaf accepts ``--format`` with a ``json``
  choice, and the default stays ``text``.
* Shim forwarding: the outer ``--format json`` reaches the inner parser argv
  for all four argv-reserializing shims -- the drift bug class
  ``tests/test_cli_parser_drift.py`` exists for -- and the default (text)
  invocation must NOT forward it.
* Emission: a single valid JSON document on stdout, deterministic across two
  runs on the same input, structure assertions rather than byte-golden
  payloads, ``{"error": ...}`` documents on failure with exit codes unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.parser import create_parser

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = FIXTURES / "routing-diagnostic.kicad_pcb"
SCHEMATIC = FIXTURES / "simple_rc.kicad_sch"

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "board-metrics": ["boards/00-demo"],
    "create-pcb": ["design.kicad_sch"],
    "panel": ["board.kicad_pcb"],
    "report generate": ["board.kicad_pcb"],
    "screenshot": ["board.kicad_pcb"],
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


# ---------------------------------------------------------------------------
# Shim forwarding (outer --format json must reach the inner parser argv)
# ---------------------------------------------------------------------------


class TestShimForwarding:
    def test_board_metrics_shim_forwards_format(self):
        from kicad_tools.cli.commands.board_metrics import run_board_metrics_command

        args = create_parser().parse_args(
            ["board-metrics", "boards/00-demo", "--dry-run", "--format", "json"]
        )
        with patch("kicad_tools.cli.board_metrics_cmd.main", return_value=0) as inner:
            assert run_board_metrics_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"board-metrics shim dropped --format: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_board_metrics_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.board_metrics import run_board_metrics_command

        args = create_parser().parse_args(["board-metrics", "boards/00-demo"])
        with patch("kicad_tools.cli.board_metrics_cmd.main", return_value=0) as inner:
            assert run_board_metrics_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_create_pcb_shim_forwards_format(self):
        from kicad_tools.cli.commands.create_pcb import run_create_pcb_command

        args = create_parser().parse_args(["create-pcb", "design.kicad_sch", "--format", "json"])
        with patch("kicad_tools.cli.create_pcb_cmd.main", return_value=0) as inner:
            assert run_create_pcb_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_create_pcb_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.create_pcb import run_create_pcb_command

        args = create_parser().parse_args(["create-pcb", "design.kicad_sch"])
        with patch("kicad_tools.cli.create_pcb_cmd.main", return_value=0) as inner:
            assert run_create_pcb_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_screenshot_shim_forwards_format(self):
        from kicad_tools.cli import _run_screenshot_command

        args = create_parser().parse_args(["screenshot", "board.kicad_pcb", "--format", "json"])
        with patch("kicad_tools.cli.screenshot_cmd.main", return_value=0) as inner:
            assert _run_screenshot_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_screenshot_shim_omits_format_for_text_default(self):
        from kicad_tools.cli import _run_screenshot_command

        args = create_parser().parse_args(["screenshot", "board.kicad_pcb"])
        with patch("kicad_tools.cli.screenshot_cmd.main", return_value=0) as inner:
            assert _run_screenshot_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_report_shim_forwards_format(self):
        from kicad_tools.cli import _run_report_command

        args = create_parser().parse_args(
            ["report", "generate", "board.kicad_pcb", "--format", "json"]
        )
        with patch("kicad_tools.cli.report_cmd.main", return_value=0) as inner:
            assert _run_report_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[0] == "generate"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_report_shim_omits_format_for_text_default(self):
        from kicad_tools.cli import _run_report_command

        args = create_parser().parse_args(["report", "generate", "board.kicad_pcb"])
        with patch("kicad_tools.cli.report_cmd.main", return_value=0) as inner:
            assert _run_report_command(args) == 0
        assert "--format" not in inner.call_args[0][0]


# ---------------------------------------------------------------------------
# board-metrics emission
# ---------------------------------------------------------------------------


def _run_board_metrics(argv, capsys):
    from kicad_tools.cli.commands.board_metrics import run_board_metrics_command

    rc = run_board_metrics_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


@pytest.fixture
def boards_tree(tmp_path):
    """A minimal ``boards/`` tree with one board carrying a report.md."""
    board = tmp_path / "boards" / "01-demo"
    mfg = board / "output" / "manufacturing"
    mfg.mkdir(parents=True)
    (mfg / "report.md").write_text(
        "| Layers | 2 copper |\n"
        "| Board Size | 50.0 x 40.0 mm |\n"
        "| Footprints | 12 |\n"
        "| Signal Net Completion | 100.0 % |\n"
        "## DRC Status\n| Errors | 0 |\n"
    )
    return tmp_path / "boards"


def _strip_volatile(payload: dict) -> dict:
    """Drop the board.json ``generated_at`` wall-clock field.

    ``generated_at`` is a deliberately volatile field of the *board.json*
    contract (it is the artifact's own timestamp), so determinism is asserted
    on everything else.
    """
    stripped = json.loads(json.dumps(payload))
    for board in stripped.get("boards", []):
        board.get("metrics", {}).pop("generated_at", None)
    return stripped


class TestBoardMetricsEmission:
    def test_dry_run_document(self, boards_tree, capsys):
        rc, out = _run_board_metrics(
            ["board-metrics", str(boards_tree / "01-demo"), "--dry-run", "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "board-metrics"
        assert payload["mode"] == "single"
        assert payload["dry_run"] is True
        assert payload["success"] is True
        assert len(payload["boards"]) == 1
        entry = payload["boards"][0]
        assert entry["slug"] == "01-demo"
        assert entry["status"] == "ok"
        assert entry["output_path"] is None, "--dry-run must not claim a written file"
        assert entry["metrics"]["layer_count"] == 2
        assert entry["metrics"]["board_size_mm"] == {"width": 50.0, "height": 40.0}

    def test_dry_run_writes_nothing(self, boards_tree, capsys):
        _run_board_metrics(
            ["board-metrics", str(boards_tree / "01-demo"), "--dry-run", "--format", "json"],
            capsys,
        )
        assert not (boards_tree / "01-demo" / "output" / "board.json").exists()

    def test_write_document_names_the_artifact(self, boards_tree, capsys):
        rc, out = _run_board_metrics(
            ["board-metrics", str(boards_tree / "01-demo"), "--format", "json"], capsys
        )
        assert rc == 0
        entry = json.loads(out)["boards"][0]
        written = Path(entry["output_path"])
        assert written.is_file()
        assert json.loads(written.read_text())["slug"] == "01-demo"

    def test_document_is_deterministic(self, boards_tree, capsys):
        argv = ["board-metrics", str(boards_tree / "01-demo"), "--dry-run", "--format", "json"]
        _, first = _run_board_metrics(argv, capsys)
        _, second = _run_board_metrics(argv, capsys)
        assert _strip_volatile(json.loads(first)) == _strip_volatile(json.loads(second))

    def test_all_mode_document(self, boards_tree, capsys):
        rc, out = _run_board_metrics(
            ["board-metrics", "--all", "--boards-dir", str(boards_tree), "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["mode"] == "all"
        assert [entry["slug"] for entry in payload["boards"]] == ["01-demo"]

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_board_metrics(
            ["board-metrics", str(tmp_path / "nope"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["boards"] == []
        assert "board directory not found" in payload["error"]

    def test_empty_boards_dir_is_error_document(self, tmp_path, capsys):
        (tmp_path / "empty").mkdir()
        rc, out = _run_board_metrics(
            ["board-metrics", "--all", "--boards-dir", str(tmp_path / "empty"), "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["mode"] == "all"
        assert payload["success"] is False
        assert "no board subdirectories" in payload["error"]

    def test_text_mode_still_prints_the_bare_board_json(self, boards_tree, capsys):
        """--dry-run text mode keeps printing the board.json artifact itself."""
        rc, out = _run_board_metrics(
            ["board-metrics", str(boards_tree / "01-demo"), "--dry-run"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["slug"] == "01-demo"
        assert "command" not in payload, "text mode must not gain the JSON envelope"

    def test_text_mode_write_prints_the_status_line(self, boards_tree, capsys):
        rc, out = _run_board_metrics(["board-metrics", str(boards_tree / "01-demo")], capsys)
        assert rc == 0
        assert "01-demo" in out and "board.json" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# create-pcb emission
# ---------------------------------------------------------------------------


def _run_create_pcb(argv, capsys):
    from kicad_tools.cli.commands.create_pcb import run_create_pcb_command

    rc = run_create_pcb_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestCreatePcbEmission:
    def test_dry_run_document(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        rc, out = _run_create_pcb(["create-pcb", str(sch), "--dry-run", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "create-pcb"
        assert payload["schematic"] == str(sch)
        assert payload["output"].endswith("simple_rc.kicad_pcb")
        assert payload["board"] == {"width_mm": 100.0, "height_mm": 100.0, "layers": 2}
        assert payload["components_found"] == 2
        assert payload["placement"]["skipped"] is False
        assert payload["placement"]["placed"] == 2
        assert payload["placement"]["failed"] == []
        assert payload["nets"]["assigned"] >= 1
        assert payload["nets"]["missing_footprints"] == sorted(
            payload["nets"]["missing_footprints"]
        )
        assert payload["dry_run"] is True
        assert payload["saved"] is False
        assert payload["success"] is True
        assert "component_count" in payload["summary"]
        assert not (tmp_path / "simple_rc.kicad_pcb").exists()

    def test_write_document_reports_saved(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        out_pcb = tmp_path / "board.kicad_pcb"
        rc, out = _run_create_pcb(
            ["create-pcb", str(sch), "-o", str(out_pcb), "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["saved"] is True
        assert payload["output"] == str(out_pcb)
        assert out_pcb.is_file()

    def test_no_place_is_flagged(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        rc, out = _run_create_pcb(
            ["create-pcb", str(sch), "--dry-run", "--no-place", "--format", "json"], capsys
        )
        assert rc == 0
        placement = json.loads(out)["placement"]
        assert placement["skipped"] is True
        assert placement["placed"] == 0

    def test_document_is_deterministic(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        argv = ["create-pcb", str(sch), "--dry-run", "--format", "json"]
        _, first = _run_create_pcb(argv, capsys)
        _, second = _run_create_pcb(argv, capsys)
        assert first == second

    def test_missing_schematic_is_error_document(self, tmp_path, capsys):
        missing = tmp_path / "nope.kicad_sch"
        rc, out = _run_create_pcb(["create-pcb", str(missing), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "create-pcb"
        assert payload["success"] is False
        assert payload["saved"] is False
        assert "Schematic not found" in payload["error"]

    def test_unparseable_schematic_is_error_document(self, tmp_path, capsys):
        bad = tmp_path / "garbage.kicad_sch"
        bad.write_text("this is not a schematic")
        rc, out = _run_create_pcb(["create-pcb", str(bad), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["error"]

    def test_text_mode_is_not_json(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        rc, out = _run_create_pcb(["create-pcb", str(sch), "--dry-run"], capsys)
        assert rc == 0
        assert "Dry run" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# panel emission
# ---------------------------------------------------------------------------


def _run_panel(argv, capsys):
    from kicad_tools.cli.commands.panel import run_panel_command

    rc = run_panel_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


@pytest.fixture
def board_copy(tmp_path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text(BOARD.read_text())
    return board


class TestPanelEmission:
    def test_document(self, board_copy, capsys):
        pytest.importorskip("shapely")
        rc, out = _run_panel(["panel", str(board_copy), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "panel"
        assert payload["input"] == str(board_copy)
        assert payload["output"].endswith("board_panel.kicad_pcb")
        assert payload["grid"] == {"rows": 2, "cols": 2, "spacing_mm": 2.0}
        assert payload["board_count"] == 4
        assert payload["cut_method"] == "mousebite"
        assert payload["tabs"] >= 1
        assert payload["frame"] is False
        assert payload["tooling_holes"] is False
        assert payload["fiducials"] is False
        assert payload["success"] is True
        assert Path(payload["output"]).is_file()

    def test_frame_features_are_reported(self, board_copy, capsys):
        pytest.importorskip("shapely")
        rc, out = _run_panel(
            [
                "panel",
                str(board_copy),
                "--rows",
                "1",
                "--cols",
                "3",
                "--cut",
                "vcut",
                "--frame",
                "--tooling-holes",
                "--fiducials",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["grid"]["rows"] == 1
        assert payload["grid"]["cols"] == 3
        assert payload["board_count"] == 3
        assert payload["cut_method"] == "vcut"
        assert payload["frame"] is True
        assert payload["tooling_holes"] is True
        assert payload["fiducials"] is True

    def test_document_is_deterministic(self, board_copy, capsys):
        pytest.importorskip("shapely")
        argv = ["panel", str(board_copy), "--format", "json"]
        _, first = _run_panel(argv, capsys)
        _, second = _run_panel(argv, capsys)
        assert first == second

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_panel(
            ["panel", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "panel"
        assert payload["success"] is False
        assert "File not found" in payload["error"]

    def test_panelization_failure_is_error_document(self, board_copy, capsys):
        pytest.importorskip("shapely")
        with patch("kicad_tools.panel.Panel.from_config", side_effect=RuntimeError("no outline")):
            rc, out = _run_panel(["panel", str(board_copy), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "no outline" in payload["error"]

    def test_text_mode_is_not_json(self, board_copy, capsys):
        pytest.importorskip("shapely")
        rc, out = _run_panel(["panel", str(board_copy)], capsys)
        assert rc == 0
        assert "Panel created:" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# screenshot emission
# ---------------------------------------------------------------------------


def _run_screenshot(argv, capsys):
    from kicad_tools.cli import _run_screenshot_command

    rc = _run_screenshot_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


def _capture_result(output_path, success=True, error=None):
    return {
        "success": success,
        "error_message": error,
        "output_path": str(output_path),
        "width_px": 800,
        "height_px": 600,
        "layers_rendered": ["F.Cu", "B.Cu", "Edge.Cuts"],
    }


class TestScreenshotEmission:
    def test_board_document(self, board_copy, tmp_path, capsys):
        png = tmp_path / "shot.png"
        with patch(
            "kicad_tools.mcp.tools.screenshot.screenshot_board",
            return_value=_capture_result(png),
        ):
            rc, out = _run_screenshot(
                ["screenshot", str(board_copy), "-o", str(png), "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "screenshot"
        assert payload["input"] == str(board_copy)
        assert payload["output"] == str(png)
        assert payload["width_px"] == 800
        assert payload["height_px"] == 600
        # Render order is meaningful (compositing order), so it is preserved.
        assert payload["layers_rendered"] == ["F.Cu", "B.Cu", "Edge.Cuts"]
        assert payload["success"] is True

    def test_schematic_document(self, tmp_path, capsys):
        sch = tmp_path / "simple_rc.kicad_sch"
        sch.write_text(SCHEMATIC.read_text())
        png = tmp_path / "sch.png"
        result = _capture_result(png)
        result["layers_rendered"] = None
        with patch("kicad_tools.mcp.tools.screenshot.screenshot_schematic", return_value=result):
            rc, out = _run_screenshot(
                ["screenshot", str(sch), "-o", str(png), "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(out)
        assert payload["layers_rendered"] == []
        assert payload["success"] is True

    def test_document_is_deterministic(self, board_copy, tmp_path, capsys):
        png = tmp_path / "shot.png"
        argv = ["screenshot", str(board_copy), "-o", str(png), "--format", "json"]
        with patch(
            "kicad_tools.mcp.tools.screenshot.screenshot_board",
            return_value=_capture_result(png),
        ):
            _, first = _run_screenshot(argv, capsys)
            _, second = _run_screenshot(argv, capsys)
        assert first == second

    def test_missing_file_is_error_document(self, tmp_path, capsys):
        missing = tmp_path / "nope.kicad_pcb"
        rc, out = _run_screenshot(["screenshot", str(missing), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "screenshot"
        assert payload["success"] is False
        assert "File not found" in payload["error"]

    def test_unsupported_suffix_is_error_document(self, tmp_path, capsys):
        other = tmp_path / "notes.txt"
        other.write_text("hello")
        rc, out = _run_screenshot(["screenshot", str(other), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "Unsupported file type" in payload["error"]

    def test_capture_failure_is_error_document(self, board_copy, tmp_path, capsys):
        png = tmp_path / "shot.png"
        with patch(
            "kicad_tools.mcp.tools.screenshot.screenshot_board",
            return_value=_capture_result(png, success=False, error="kicad-cli not found"),
        ):
            rc, out = _run_screenshot(
                ["screenshot", str(board_copy), "-o", str(png), "--format", "json"], capsys
            )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "kicad-cli not found" in payload["error"]

    def test_text_mode_is_not_json(self, board_copy, tmp_path, capsys):
        png = tmp_path / "shot.png"
        with patch(
            "kicad_tools.mcp.tools.screenshot.screenshot_board",
            return_value=_capture_result(png),
        ):
            rc, out = _run_screenshot(["screenshot", str(board_copy), "-o", str(png)], capsys)
        assert rc == 0
        assert "Screenshot saved to" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# report generate emission
# ---------------------------------------------------------------------------


def _run_report(argv, capsys):
    from kicad_tools.cli import _run_report_command

    rc = _run_report_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestReportGenerateEmission:
    def test_skeleton_document(self, board_copy, tmp_path, capsys):
        out_dir = tmp_path / "reports"
        rc, out = _run_report(
            [
                "report",
                "generate",
                str(board_copy),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--skip-collect",
                "--no-figures",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        # json.loads() fails outright if stdout carries anything but the one
        # document -- e.g. WeasyPrint's stdout banner when its native libs are
        # missing, which is why the PDF block redirects stdout in JSON mode.
        payload = json.loads(out)
        assert payload["command"] == "generate"
        assert payload["input"] == str(board_copy)
        assert payload["manufacturer"] == "jlcpcb"
        assert payload["output_dir"] == str(out_dir)
        assert payload["project_name"] == "board"
        assert payload["data_source"] == "skeleton"
        assert payload["figures"] == {
            "generated": False,
            "skipped_reason": "disabled by --no-figures",
        }
        assert payload["success"] is True
        assert Path(payload["report_path"]).is_file()
        assert payload["pdf_path"] is None or Path(payload["pdf_path"]).is_file()

    def test_data_dir_source_is_reported(self, board_copy, tmp_path, capsys):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rc, out = _run_report(
            [
                "report",
                "generate",
                str(board_copy),
                "-o",
                str(tmp_path / "reports"),
                "--data-dir",
                str(data_dir),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["data_source"] == "data-dir"
        assert payload["figures"]["generated"] is False
        assert payload["figures"]["skipped_reason"] == "pre-collected via --data-dir"

    def test_missing_pcb_for_pro_input_is_error_document(self, tmp_path, capsys):
        pro = tmp_path / "design.kicad_pro"
        pro.write_text("{}")
        rc, out = _run_report(
            ["report", "generate", str(pro), "-o", str(tmp_path / "reports"), "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "generate"
        assert payload["success"] is False
        assert "PCB file not found" in payload["error"]

    def test_text_mode_is_not_json(self, board_copy, tmp_path, capsys):
        rc, out = _run_report(
            [
                "report",
                "generate",
                str(board_copy),
                "-o",
                str(tmp_path / "reports"),
                "--skip-collect",
                "--no-figures",
            ],
            capsys,
        )
        assert rc == 0
        assert "Report written to" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
