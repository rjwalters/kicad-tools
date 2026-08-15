"""Tests for the #4674 machine-output sweep (orchestrators batch -- the last).

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  Batch 1 swept the
grouped families (``tests/test_format_json_sweep.py``), batch 2 the 16
mutating ``sch`` leaves (``tests/test_format_json_sweep_sch.py``), batch 3 the
four families' holdouts (``tests/test_format_json_sweep_families.py``), batch 4
the environment/integration singles (``tests/test_format_json_sweep_env.py``),
batch 5 the board-artifact producers
(``tests/test_format_json_sweep_artifacts.py``) and batch 6 the
board-improvement drivers (``tests/test_format_json_sweep_drivers.py``).

**This batch sweeps the multi-stage orchestrators** -- the last three
actionable leaves on the #4674 backlog:

* ``build``     -- spec -> schematic -> PCB -> route -> verify -> export
* ``pipeline``  -- end-to-end repair pipeline for an existing PCB
* ``stitch``    -- single command, but a bespoke multi-phase via report

Same conventions as the six sibling modules (a separate file per batch so
concurrent batches never conflict on a shared ``SWEPT_SURFACES`` literal):

* Outer-parser surface: every swept leaf accepts ``--format`` with a ``json``
  choice, and the default stays ``text``.
* Shim threading: the outer ``--format json`` reaches the inner parser argv,
  and the default (text) invocation must NOT (a default run has to build a
  byte-identical inner argv).
* Emission: a single valid JSON document on stdout even though all three
  commands drive sub-tools that print on stdout, deterministic across two runs
  on the same input modulo the named volatile timing fields, and
  ``{"error": ...}`` documents on failure with exit codes unchanged.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.parser import create_parser

FIXTURES = Path(__file__).parent / "fixtures"

# A tiny multi-layer board with SMD pads on GND / +3.3V and no zones, so
# `kct stitch --net GND` has real work to do without needing a zone fill.
STITCH_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 120 110)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
)
"""  # noqa: E501

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "build": ["project"],
    "pipeline": ["board.kicad_pcb"],
    "stitch": ["board.kicad_pcb"],
}

# Fields whose value is wall-clock time and therefore deliberately volatile.
# Named rather than hidden (batch-6 convention); determinism is asserted
# modulo them.
VOLATILE_KEYS = ("wall_time_s", "elapsed_s")


def _iter_leaves(parser, path=()):
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subactions:
        yield path, parser
        return
    for subaction in subactions:
        for name, sub in subaction.choices.items():
            yield from _iter_leaves(sub, (*path, name))


def _strip_volatile(payload):
    """Recursively drop the named wall-clock fields before comparing."""
    if isinstance(payload, dict):
        return {k: _strip_volatile(v) for k, v in payload.items() if k not in VOLATILE_KEYS}
    if isinstance(payload, list):
        return [_strip_volatile(v) for v in payload]
    return payload


@pytest.fixture(scope="module")
def leaves():
    return {" ".join(path): leaf for path, leaf in _iter_leaves(create_parser())}


@pytest.fixture
def stitch_board(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(STITCH_BOARD)
    return pcb


@pytest.fixture
def pipeline_board(tmp_path):
    """A writable copy of the small routing fixture board."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text((FIXTURES / "routing-diagnostic.kicad_pcb").read_text())
    return pcb


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

    def test_no_actionable_prose_only_leaves_remain(self, leaves):
        """#4674's backlog is empty: only the documented exemptions are left.

        The four exemptions and the deferred ``route`` are listed in
        ``docs/reference/machine-output.md``; anything else appearing here is
        a new prose-only leaf that skipped the canonical idiom.
        """
        allowed = {"footprint generate", "interactive", "mcp serve", "route", "run"}
        prose_only = set()
        for command, leaf in leaves.items():
            opts = {opt for action in leaf._actions for opt in action.option_strings}
            if "--format" not in opts and "--json" not in opts:
                prose_only.add(command)
        assert prose_only == allowed, (
            "prose-only bucket drifted from the documented exemption list "
            f"(unexpected: {sorted(prose_only - allowed)}, "
            f"newly covered: {sorted(allowed - prose_only)})"
        )


# ---------------------------------------------------------------------------
# Shim threading (outer --format json must reach the inner parser argv)
# ---------------------------------------------------------------------------


class TestShimThreading:
    def test_build_shim_forwards_format(self):
        from kicad_tools.cli.commands.build import run_build_command

        args = create_parser().parse_args(["build", "project", "--format", "json"])
        with patch("kicad_tools.cli.build_cmd.main", return_value=0) as inner:
            assert run_build_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"build shim dropped --format: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_build_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.build import run_build_command

        args = create_parser().parse_args(["build", "project"])
        with patch("kicad_tools.cli.build_cmd.main", return_value=0) as inner:
            assert run_build_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_pipeline_shim_forwards_format(self):
        from kicad_tools.cli.commands.pipeline import run_pipeline_command

        args = create_parser().parse_args(["pipeline", "board.kicad_pcb", "--format", "json"])
        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as inner:
            assert run_pipeline_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_pipeline_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.pipeline import run_pipeline_command

        args = create_parser().parse_args(["pipeline", "board.kicad_pcb"])
        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as inner:
            assert run_pipeline_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_stitch_shim_forwards_format(self):
        from kicad_tools.cli import _run_stitch_command

        args = create_parser().parse_args(["stitch", "board.kicad_pcb", "--format", "json"])
        with patch("kicad_tools.cli.stitch_cmd.main", return_value=0) as inner:
            assert _run_stitch_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_stitch_shim_omits_format_for_text_default(self):
        from kicad_tools.cli import _run_stitch_command

        args = create_parser().parse_args(["stitch", "board.kicad_pcb"])
        with patch("kicad_tools.cli.stitch_cmd.main", return_value=0) as inner:
            assert _run_stitch_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    @pytest.mark.parametrize("command", ["build", "pipeline"])
    def test_inner_parser_accepts_format(self, command):
        """The inner (authoritative) parsers accept the forwarded flag."""
        if command == "build":
            from kicad_tools.cli.build_cmd import _build_inner_parser

            parser = _build_inner_parser()
            args = parser.parse_args(["project", "--format", "json"])
        else:
            from kicad_tools.cli.pipeline_cmd import main as pipeline_main

            # pipeline builds its parser inside main(); a bogus path exits 1
            # rather than argparse's 2, which proves the flag parsed.
            assert pipeline_main(["/nonexistent.kicad_pcb", "--format", "json"]) == 1
            return
        assert args.format == "json"


# ---------------------------------------------------------------------------
# stitch emission
# ---------------------------------------------------------------------------


def _run_stitch(argv, capsys):
    from kicad_tools.cli import _run_stitch_command

    rc = _run_stitch_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestStitchEmission:
    def test_dry_run_document(self, stitch_board, capsys):
        rc, out = _run_stitch(
            ["stitch", str(stitch_board), "--net", "GND", "--dry-run", "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "stitch"
        assert payload["pcb"] == str(stitch_board)
        assert payload["mode"] == "stitch"
        assert payload["target_nets"] == ["GND"]
        assert payload["nets_auto_detected"] is False
        assert payload["dry_run"] is True
        assert payload["saved"] is False, "--dry-run must not claim a written file"
        assert payload["manufacturer"] is None
        assert payload["via_size_mm"] == 0.45
        assert payload["drill_mm"] == 0.2
        assert payload["pads_found"] == 2
        assert payload["vias_added_count"] >= 1
        assert payload["exit_code"] == 0
        assert payload["success"] is True

    def test_document_is_the_full_untruncated_ledger(self, stitch_board, capsys):
        """Unlike the prose (10 vias / 5 skipped pads), JSON is complete."""
        _, out = _run_stitch(
            ["stitch", str(stitch_board), "--net", "GND", "--dry-run", "--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert len(payload["vias_added"]) == payload["vias_added_count"]
        for via in payload["vias_added"]:
            assert {"reference", "pad", "net", "via_x", "via_y", "layers"} <= set(via)
            assert via["net"] == "GND"

    def test_write_document_reports_saved(self, stitch_board, capsys):
        rc, out = _run_stitch(
            ["stitch", str(stitch_board), "--net", "GND", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["saved"] is True
        assert payload["output"] == str(stitch_board)

    def test_output_flag_names_the_edited_file(self, stitch_board, tmp_path, capsys):
        out_pcb = tmp_path / "stitched.kicad_pcb"
        rc, out = _run_stitch(
            [
                "stitch",
                str(stitch_board),
                "--net",
                "GND",
                "-o",
                str(out_pcb),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["pcb"] == str(stitch_board)
        assert payload["output"] == str(out_pcb)

    def test_dry_run_writes_nothing(self, stitch_board, capsys):
        before = stitch_board.read_text()
        _run_stitch(
            ["stitch", str(stitch_board), "--net", "GND", "--dry-run", "--format", "json"],
            capsys,
        )
        assert stitch_board.read_text() == before

    def test_document_is_deterministic(self, stitch_board, capsys):
        argv = ["stitch", str(stitch_board), "--net", "GND", "--dry-run", "--format", "json"]
        _, first = _run_stitch(argv, capsys)
        _, second = _run_stitch(argv, capsys)
        assert first == second

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_stitch(
            ["stitch", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "stitch"
        assert payload["exit_code"] == 1
        assert payload["success"] is False
        assert "not found" in payload["error"]

    def test_no_plane_nets_is_error_document(self, stitch_board, capsys):
        """Auto-detect with no zones fails; the prose has no `Error:` prefix."""
        rc, out = _run_stitch(["stitch", str(stitch_board), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert "No power plane nets found" in payload["error"]

    def test_unknown_manufacturer_is_error_document(self, stitch_board, capsys):
        rc, out = _run_stitch(
            ["stitch", str(stitch_board), "--net", "GND", "--mfr", "nope", "--format", "json"],
            capsys,
        )
        assert rc == 1
        assert "nope" in json.loads(out)["error"]

    def test_text_mode_is_not_json(self, stitch_board, capsys):
        rc, out = _run_stitch(["stitch", str(stitch_board), "--net", "GND", "--dry-run"], capsys)
        assert rc == 0
        assert "Stitching vias for" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# pipeline emission
# ---------------------------------------------------------------------------


def _run_pipeline(argv, capsys):
    from kicad_tools.cli.commands.pipeline import run_pipeline_command

    rc = run_pipeline_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestPipelineEmission:
    def test_dry_run_document(self, pipeline_board, capsys):
        rc, out = _run_pipeline(
            ["pipeline", str(pipeline_board), "--dry-run", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "pipeline"
        assert payload["pcb"] == str(pipeline_board)
        assert payload["mfr"] == "jlcpcb"
        assert payload["dry_run"] is True
        assert payload["step"] is None
        assert payload["success"] is True
        assert payload["exit_code"] == 0
        assert payload["commit"] == {"requested": False, "created": False}
        assert payload["counts"]["total"] == len(payload["steps"])
        for entry in payload["steps"]:
            assert {"step", "success", "skipped", "warning", "message"} == set(entry)

    def test_single_step_document(self, pipeline_board, capsys):
        rc, out = _run_pipeline(
            [
                "pipeline",
                str(pipeline_board),
                "--step",
                "fix-vias",
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["step"] == "fix-vias"
        assert [entry["step"] for entry in payload["steps"]] == ["fix-vias"]

    def test_document_is_deterministic(self, pipeline_board, capsys):
        argv = ["pipeline", str(pipeline_board), "--dry-run", "--format", "json"]
        _, first = _run_pipeline(argv, capsys)
        _, second = _run_pipeline(argv, capsys)
        assert _strip_volatile(json.loads(first)) == _strip_volatile(json.loads(second))

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_pipeline(
            ["pipeline", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "pipeline"
        assert payload["success"] is False
        assert "not found" in payload["error"].lower()

    def test_unsupported_suffix_is_error_document(self, tmp_path, capsys):
        bogus = tmp_path / "board.txt"
        bogus.write_text("")
        rc, out = _run_pipeline(["pipeline", str(bogus), "--format", "json"], capsys)
        assert rc == 1
        assert "Unsupported file type" in json.loads(out)["error"]

    def test_text_mode_is_not_json(self, pipeline_board, capsys):
        rc, out = _run_pipeline(["pipeline", str(pipeline_board), "--dry-run"], capsys)
        assert rc == 0
        assert "pipeline" in out.lower()
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# build emission
# ---------------------------------------------------------------------------


def _run_build(argv, capsys):
    from kicad_tools.cli.commands.build import run_build_command

    rc = run_build_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestBuildEmission:
    def test_document_for_a_project_without_a_generator(self, tmp_path, capsys):
        """A project with no generator fails at the schematic step.

        Even a failing build emits exactly one document: the ledger of what
        ran is the payload, and the exit code is unchanged.
        """
        rc, out = _run_build(["build", str(tmp_path), "--dry-run", "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "build"
        assert payload["project_dir"] == str(tmp_path.resolve())
        assert payload["spec"] is None
        assert payload["mfr"] == "jlcpcb"
        assert payload["step"] == "all"
        assert payload["dry_run"] is True
        assert payload["success"] is False
        assert payload["exit_code"] == 1
        assert payload["steps"][0]["step"] == "schematic"
        assert payload["steps"][0]["success"] is False
        assert payload["counts"] == {"total": 1, "succeeded": 0, "failed": 1}
        assert isinstance(payload["wall_time_s"], float)

    def test_single_step_document(self, tmp_path, capsys):
        rc, out = _run_build(
            ["build", str(tmp_path), "--step", "verify", "--dry-run", "--format", "json"],
            capsys,
        )
        assert isinstance(rc, int)
        payload = json.loads(out)
        assert payload["step"] == "verify"
        assert [entry["step"] for entry in payload["steps"]] == ["verify"]

    def test_document_is_deterministic(self, tmp_path, capsys):
        argv = ["build", str(tmp_path), "--dry-run", "--format", "json"]
        _, first = _run_build(argv, capsys)
        _, second = _run_build(argv, capsys)
        assert _strip_volatile(json.loads(first)) == _strip_volatile(json.loads(second))

    def test_missing_directory_is_error_document(self, tmp_path, capsys):
        rc, out = _run_build(["build", str(tmp_path / "nope"), "--format", "json"], capsys)
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "build"
        assert payload["success"] is False
        assert "not found" in payload["error"].lower()

    def test_exit_code_no_longer_depends_on_quiet(self, tmp_path, capsys):
        """A failing build exits 1 whether or not --quiet is passed.

        The exit code used to be computed inside the `if not args.quiet`
        summary block, so `kct build --quiet` reported success for a failed
        build -- which would have contradicted the document's "success":
        false.  Hoisted out of the guard by #4674.
        """
        loud, _ = _run_build(["build", str(tmp_path), "--dry-run"], capsys)
        quiet, _ = _run_build(["build", str(tmp_path), "--dry-run", "--quiet"], capsys)
        assert loud == 1
        assert quiet == 1

    def test_text_mode_is_not_json(self, tmp_path, capsys):
        rc, out = _run_build(["build", str(tmp_path), "--dry-run"], capsys)
        assert rc == 1
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# The promoted stdout-diversion helper (was copied in 3 CLI modules)
# ---------------------------------------------------------------------------


class TestStdoutDiversionHelper:
    def test_inactive_is_a_passthrough(self, capsys):
        from kicad_tools.cli.format_options import stdout_to_stderr_when

        with stdout_to_stderr_when(False):
            print("hello")
        captured = capsys.readouterr()
        assert captured.out == "hello\n"
        assert captured.err == ""

    def test_active_replays_stdout_on_stderr(self, capsys):
        from kicad_tools.cli.format_options import stdout_to_stderr_when

        with stdout_to_stderr_when(True):
            print("chatter")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "chatter" in captured.err

    def test_replays_even_when_the_body_raises(self, capsys):
        from kicad_tools.cli.format_options import stdout_to_stderr_when

        with pytest.raises(RuntimeError), stdout_to_stderr_when(True):
            print("chatter")
            raise RuntimeError("boom")
        assert "chatter" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "module",
        [
            "kicad_tools.cli.report_cmd",
            "kicad_tools.cli.reason_cmd",
            "kicad_tools.cli.commands.routing",
            "kicad_tools.cli.build_cmd",
            "kicad_tools.cli.pipeline_cmd",
            "kicad_tools.cli.stitch_cmd",
        ],
    )
    def test_call_sites_use_the_shared_helper(self, module):
        """No module re-implements the helper (it was copied 3x before #4674)."""
        import importlib

        from kicad_tools.cli.format_options import stdout_to_stderr_when

        mod = importlib.import_module(module)
        assert mod.stdout_to_stderr_when is stdout_to_stderr_when
        assert not hasattr(mod, "_stdout_to_stderr_when"), (
            f"{module} still defines a private copy of the helper"
        )
