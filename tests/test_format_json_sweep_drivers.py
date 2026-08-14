"""Tests for the #4674 machine-output sweep (board-improvement drivers batch).

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  Batch 1 swept the
grouped families (``tests/test_format_json_sweep.py``), batch 2 the 16
mutating ``sch`` leaves (``tests/test_format_json_sweep_sch.py``), batch 3 the
four families' holdouts (``tests/test_format_json_sweep_families.py``), batch 4
the environment/integration singles (``tests/test_format_json_sweep_env.py``)
and batch 5 the board-artifact producers
(``tests/test_format_json_sweep_artifacts.py``).

**This batch sweeps the board-improvement / rule-derivation drivers** -- the
singles that take an existing board and derive an improvement or an
enforceable rule set from it:

* ``optimize-placement``      -- CMA-ES placement optimization
* ``optimize-traces``         -- trace geometry cleanup
* ``route-auto``              -- orchestrator-driven per-net routing
* ``reason``                  -- LLM-oriented board state / analysis / auto-route
* ``creepage-export-rules``   -- voltage-domain netclasses + pairwise DRU rules

Same conventions as the five sibling modules (a separate file per batch so
concurrent batches never conflict on a shared ``SWEPT_SURFACES`` literal):

* Outer-parser surface: every swept leaf accepts ``--format`` with a ``json``
  choice, and the default stays ``text``.
* Shim threading: the outer ``--format json`` reaches the inner parser argv
  (``optimize-traces``, ``reason``) or the inner keyword
  (``optimize-placement``), and the default (text) invocation must NOT.
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

# Every subcommand swept by this batch, as (command path, minimal extra argv).
SWEPT_SURFACES: dict[str, list[str]] = {
    "creepage-export-rules": ["board.kicad_pro"],
    "optimize-placement": ["board.kicad_pcb"],
    "optimize-traces": ["board.kicad_pcb"],
    "reason": ["board.kicad_pcb"],
    "route-auto": ["board.kicad_pcb", "--net", "GND"],
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


@pytest.fixture
def board(tmp_path):
    """A writable copy of the small routing fixture board."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD.read_text())
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


# ---------------------------------------------------------------------------
# Shim threading (outer --format json must reach the inner surface)
# ---------------------------------------------------------------------------


class TestShimThreading:
    def test_optimize_traces_shim_forwards_format(self):
        from kicad_tools.cli.commands.routing import run_optimize_command

        args = create_parser().parse_args(
            ["optimize-traces", "board.kicad_pcb", "--format", "json"]
        )
        with patch("kicad_tools.cli.optimize_cmd.main", return_value=0) as inner:
            assert run_optimize_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert "--format" in sub_argv, f"optimize-traces shim dropped --format: {sub_argv}"
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_optimize_traces_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.routing import run_optimize_command

        args = create_parser().parse_args(["optimize-traces", "board.kicad_pcb"])
        with patch("kicad_tools.cli.optimize_cmd.main", return_value=0) as inner:
            assert run_optimize_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_reason_shim_forwards_format(self):
        from kicad_tools.cli.commands.reasoning import run_reason_command

        args = create_parser().parse_args(["reason", "board.kicad_pcb", "--format", "json"])
        with patch("kicad_tools.cli.reason_cmd.main", return_value=0) as inner:
            assert run_reason_command(args) == 0
        sub_argv = inner.call_args[0][0]
        assert sub_argv[sub_argv.index("--format") + 1] == "json"

    def test_reason_shim_omits_format_for_text_default(self):
        from kicad_tools.cli.commands.reasoning import run_reason_command

        args = create_parser().parse_args(["reason", "board.kicad_pcb"])
        with patch("kicad_tools.cli.reason_cmd.main", return_value=0) as inner:
            assert run_reason_command(args) == 0
        assert "--format" not in inner.call_args[0][0]

    def test_optimize_placement_shim_passes_as_json(self):
        """This shim calls the inner function directly, so it passes a keyword."""
        from kicad_tools.cli.commands.optimize_placement import run_optimize_placement_command

        args = create_parser().parse_args(
            ["optimize-placement", "board.kicad_pcb", "--format", "json"]
        )
        with patch(
            "kicad_tools.cli.optimize_placement_cmd.run_optimize_placement", return_value=0
        ) as inner:
            assert run_optimize_placement_command(args) == 0
        assert inner.call_args.kwargs["as_json"] is True

    def test_optimize_placement_shim_defaults_to_text(self):
        from kicad_tools.cli.commands.optimize_placement import run_optimize_placement_command

        args = create_parser().parse_args(["optimize-placement", "board.kicad_pcb"])
        with patch(
            "kicad_tools.cli.optimize_placement_cmd.run_optimize_placement", return_value=0
        ) as inner:
            assert run_optimize_placement_command(args) == 0
        assert inner.call_args.kwargs["as_json"] is False


# ---------------------------------------------------------------------------
# optimize-traces emission
# ---------------------------------------------------------------------------


def _run_optimize_traces(argv, capsys):
    from kicad_tools.cli.commands.routing import run_optimize_command

    rc = run_optimize_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestOptimizeTracesEmission:
    def test_dry_run_document(self, board, capsys):
        rc, out = _run_optimize_traces(
            ["optimize-traces", str(board), "--dry-run", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "optimize-traces"
        assert payload["pcb"] == str(board)
        assert payload["dry_run"] is True
        assert payload["saved"] is False, "--dry-run must not claim a written file"
        assert payload["written_to"] is None
        assert payload["success"] is True
        assert payload["optimizations"] == {
            "merge_collinear": True,
            "eliminate_zigzags": True,
            "convert_45_corners": True,
            "chamfer_size_mm": 0.5,
        }
        assert "segments_before" in payload["stats"]
        assert "length_after_mm" in payload["stats"]

    def test_write_document_names_the_target(self, board, capsys):
        rc, out = _run_optimize_traces(["optimize-traces", str(board), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(out)
        assert payload["saved"] is True
        assert payload["written_to"] == str(board)

    def test_disabled_passes_are_reported(self, board, capsys):
        rc, out = _run_optimize_traces(
            ["optimize-traces", str(board), "--dry-run", "--no-45", "--format", "json"], capsys
        )
        assert rc == 0
        assert json.loads(out)["optimizations"]["convert_45_corners"] is False

    def test_document_is_deterministic(self, board, capsys):
        argv = ["optimize-traces", str(board), "--dry-run", "--format", "json"]
        _, first = _run_optimize_traces(argv, capsys)
        _, second = _run_optimize_traces(argv, capsys)
        assert first == second

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_optimize_traces(
            ["optimize-traces", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "optimize-traces"
        assert payload["success"] is False
        assert payload["saved"] is False
        assert "not found" in payload["error"]

    def test_drc_aware_without_mfr_is_error_document(self, board, capsys):
        rc, out = _run_optimize_traces(
            ["optimize-traces", str(board), "--drc-aware", "--format", "json"], capsys
        )
        assert rc == 1
        assert "--mfr" in json.loads(out)["error"]

    def test_text_mode_is_not_json(self, board, capsys):
        rc, out = _run_optimize_traces(["optimize-traces", str(board), "--dry-run"], capsys)
        assert rc == 0
        assert "Trace Optimization" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# optimize-placement emission
# ---------------------------------------------------------------------------


def _run_optimize_placement(argv, capsys):
    from kicad_tools.cli.commands.optimize_placement import run_optimize_placement_command

    rc = run_optimize_placement_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


def _strip_volatile(payload: dict) -> dict:
    """Drop the deliberately volatile wall-clock field before comparing."""
    stripped = json.loads(json.dumps(payload))
    stripped.pop("wall_time_s", None)
    return stripped


class TestOptimizePlacementEmission:
    def test_dry_run_document(self, board, capsys):
        rc, out = _run_optimize_placement(
            ["optimize-placement", str(board), "--dry-run", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "optimize-placement"
        assert payload["mode"] == "evaluate"
        assert payload["dry_run"] is True
        assert payload["saved"] is False, "--dry-run must not claim a written file"
        assert payload["written_to"] is None
        assert payload["board"]["components"] == 4
        current = payload["scores"]["current"]
        assert isinstance(current["total"], float)
        assert isinstance(current["feasible"], bool)
        # The breakdown comes straight off the cost dataclass.
        assert {"wirelength", "overlap", "boundary", "drc", "area"} <= set(current["breakdown"])

    def test_optimize_document_reports_both_scores(self, board, capsys):
        rc, out = _run_optimize_placement(
            [
                "optimize-placement",
                str(board),
                "--max-iterations",
                "3",
                "--allow-infeasible",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["mode"] == "optimize"
        assert set(payload["scores"]) == {"initial", "final"}
        assert payload["iterations"] >= 1
        assert payload["saved"] is True
        assert payload["written_to"] == str(board)
        assert payload["interrupted"] is False
        assert isinstance(payload["wall_time_s"], float)

    def test_infeasible_exit_carries_the_detail(self, board, capsys):
        """Without --allow-infeasible an infeasible result exits 1 and says why."""
        rc, out = _run_optimize_placement(
            ["optimize-placement", str(board), "--max-iterations", "3", "--format", "json"],
            capsys,
        )
        payload = json.loads(out)
        assert payload["command"] == "optimize-placement"
        if rc == 0:
            assert payload["success"] is True
            assert payload["feasible"] is True
        else:
            assert rc == 1
            assert payload["success"] is False
            assert payload["feasible"] is False
            assert payload["infeasible_detail"]

    def test_document_is_deterministic(self, board, capsys):
        argv = ["optimize-placement", str(board), "--dry-run", "--format", "json"]
        _, first = _run_optimize_placement(argv, capsys)
        _, second = _run_optimize_placement(argv, capsys)
        assert _strip_volatile(json.loads(first)) == _strip_volatile(json.loads(second))

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, out = _run_optimize_placement(
            ["optimize-placement", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["saved"] is False
        assert "not found" in payload["error"]

    def test_bad_anchor_weight_is_error_document(self, board, capsys):
        rc, out = _run_optimize_placement(
            ["optimize-placement", str(board), "--anchor-weight", "-1", "--format", "json"],
            capsys,
        )
        assert rc == 1
        assert "anchor-weight" in json.loads(out)["error"]

    def test_text_mode_is_not_json(self, board, capsys):
        rc, out = _run_optimize_placement(["optimize-placement", str(board), "--dry-run"], capsys)
        assert rc == 0
        assert "Reading board" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# route-auto emission
# ---------------------------------------------------------------------------


def _run_route_auto(argv, capsys):
    from kicad_tools.cli.commands.routing import run_route_auto_command

    rc = run_route_auto_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


def _fake_route_result(net_name: str, **overrides):
    result = {
        "success": True,
        "net_name": net_name,
        "strategy_used": "GLOBAL_WITH_REPAIR",
        "metrics": {"total_length_mm": 12.5, "via_count": 1},
        "segments_written": 3,
        "vias_written": 1,
        "warnings": [],
        "output_path": None,
    }
    result.update(overrides)
    return result


class TestRouteAutoEmission:
    def test_dry_run_document(self, board, capsys):
        rc, out = _run_route_auto(
            ["route-auto", str(board), "--net", "NET1", "--dry-run", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "route-auto"
        assert payload["dry_run"] is True
        assert payload["nets_requested"] == 1
        assert payload["nets_routed"] == 0, "a preview routes nothing"
        entry = payload["nets"][0]
        assert entry["net"] == "NET1"
        assert entry["would_route"] is True
        assert entry["via_drill_source"] in {"board-derived", "explicit override", None}

    def test_dry_run_reports_explicit_via_override(self, board, capsys):
        rc, out = _run_route_auto(
            [
                "route-auto",
                str(board),
                "--net",
                "NET1",
                "--dry-run",
                "--via-drill",
                "0.25",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        entry = json.loads(out)["nets"][0]
        assert entry["via_drill_mm"] == 0.25
        assert entry["via_drill_source"] == "explicit override"

    def test_multi_net_document_has_one_entry_per_net(self, board, capsys):
        with patch(
            "kicad_tools.mcp.tools.routing.route_net_auto",
            side_effect=lambda net_name, **kw: _fake_route_result(net_name),
        ):
            rc, out = _run_route_auto(
                ["route-auto", str(board), "--nets", "NET1,NET2", "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(out)
        assert [entry["net"] for entry in payload["nets"]] == ["NET1", "NET2"]
        assert payload["nets_routed"] == 2
        assert payload["success"] is True
        assert payload["nets"][0]["metrics"]["total_length_mm"] == 12.5

    def test_failed_net_document_keeps_exit_code(self, board, capsys):
        failure = _fake_route_result(
            "NET1",
            success=False,
            error_message="no corridor",
            alternative_strategies=[{"strategy": "HIERARCHICAL", "reason": "try wider"}],
        )
        with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=failure):
            rc, out = _run_route_auto(
                ["route-auto", str(board), "--net", "NET1", "--format", "json"], capsys
            )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["nets"][0]["error"] == "no corridor"
        assert payload["nets"][0]["alternative_strategies"][0]["strategy"] == "HIERARCHICAL"

    def test_partial_net_is_flagged(self, board, capsys):
        partial = _fake_route_result(
            "NET1", success=False, partial=True, pads_connected=2, pads_total=3
        )
        with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=partial):
            rc, out = _run_route_auto(
                ["route-auto", str(board), "--net", "NET1", "--format", "json"], capsys
            )
        assert rc == 1
        entry = json.loads(out)["nets"][0]
        assert entry["partial"] is True
        assert entry["pads_connected"] == 2
        assert entry["pads_total"] == 3

    def test_router_exception_is_error_document(self, board, capsys):
        with patch(
            "kicad_tools.mcp.tools.routing.route_net_auto",
            side_effect=ValueError("net 'NOPE' not found"),
        ):
            rc, out = _run_route_auto(
                ["route-auto", str(board), "--net", "NOPE", "--format", "json"], capsys
            )
        assert rc == 1
        entry = json.loads(out)["nets"][0]
        assert entry["success"] is False
        assert "not found" in entry["error"]

    def test_router_stdout_chatter_does_not_corrupt_the_document(self, board, capsys):
        """Third-party progress logs are replayed on stderr, not stdout."""

        def chatty(net_name, **kw):
            print("=== Negotiated Congestion Routing ===")
            return _fake_route_result(net_name)

        with patch("kicad_tools.mcp.tools.routing.route_net_auto", side_effect=chatty):
            from kicad_tools.cli.commands.routing import run_route_auto_command

            rc = run_route_auto_command(
                create_parser().parse_args(
                    ["route-auto", str(board), "--net", "NET1", "--format", "json"]
                )
            )
        captured = capsys.readouterr()
        assert rc == 0
        assert json.loads(captured.out)["command"] == "route-auto"
        assert "Negotiated Congestion Routing" in captured.err

    def test_missing_net_selector_is_error_document(self, board, capsys):
        rc, out = _run_route_auto(["route-auto", str(board), "--format", "json"], capsys)
        assert rc == 2, "usage exit code is unchanged"
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["nets"] == []
        assert "requires --net" in payload["error"]

    def test_mutually_exclusive_selectors_are_error_document(self, board, capsys):
        rc, out = _run_route_auto(
            ["route-auto", str(board), "--net", "NET1", "--nets", "NET2", "--format", "json"],
            capsys,
        )
        assert rc == 2
        assert "mutually exclusive" in json.loads(out)["error"]

    def test_document_is_deterministic(self, board, capsys):
        argv = ["route-auto", str(board), "--net", "NET1", "--dry-run", "--format", "json"]
        _, first = _run_route_auto(argv, capsys)
        _, second = _run_route_auto(argv, capsys)
        assert first == second

    def test_text_mode_is_not_json(self, board, capsys):
        rc, out = _run_route_auto(["route-auto", str(board), "--net", "NET1", "--dry-run"], capsys)
        assert rc == 0
        assert "[dry-run] Would route net" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


# ---------------------------------------------------------------------------
# reason emission
# ---------------------------------------------------------------------------


def _run_reason(argv, capsys):
    from kicad_tools.cli.commands.reasoning import run_reason_command

    rc = run_reason_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr()


class TestReasonEmission:
    def test_default_prompt_document(self, board, capsys):
        rc, captured = _run_reason(["reason", str(board), "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["command"] == "reason"
        assert payload["mode"] == "prompt"
        assert payload["prompt"]
        assert payload["board"]["components"] == 4
        assert payload["drc"]["ran"] is True
        assert payload["success"] is True

    def test_analyze_document_carries_the_analysis(self, board, capsys):
        rc, captured = _run_reason(["reason", str(board), "--analyze", "--format", "json"], capsys)
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["mode"] == "analyze"
        assert "PCB Analysis" in payload["analysis"]

    def test_export_state_document_embeds_the_state(self, board, capsys):
        rc, captured = _run_reason(
            ["reason", str(board), "--export-state", "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["mode"] == "export-state"
        assert payload["state_output"] is None
        assert len(payload["state"]["components"]) == 4
        assert "prompt" in payload["state"]

    def test_export_state_to_file_names_the_artifact(self, board, tmp_path, capsys):
        state_path = tmp_path / "state.json"
        rc, captured = _run_reason(
            [
                "reason",
                str(board),
                "--export-state",
                "--state-output",
                str(state_path),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["state_output"] == str(state_path)
        assert json.loads(state_path.read_text())["outline"]["width"] > 0

    def test_auto_route_document(self, board, tmp_path, capsys):
        from kicad_tools.reasoning import PCBReasoningAgent
        from kicad_tools.reasoning.commands import CommandResult, CommandType

        results = [
            CommandResult(
                success=True,
                command_type=CommandType.ROUTE_NET,
                message="Routed NET1",
                trace_length=8.0,
                vias_added=1,
            )
        ]
        out_path = tmp_path / "reasoned.kicad_pcb"
        with (
            patch.object(PCBReasoningAgent, "route_priority_nets", return_value=results),
            patch.object(PCBReasoningAgent, "save") as save,
        ):
            rc, captured = _run_reason(
                [
                    "reason",
                    str(board),
                    "--auto-route",
                    "--max-nets",
                    "1",
                    "-o",
                    str(out_path),
                    "--format",
                    "json",
                ],
                capsys,
            )
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["mode"] == "auto-route"
        assert payload["auto_route"]["attempted"] == 1
        assert payload["auto_route"]["routed"] == 1
        assert payload["auto_route"]["nets"][0]["vias_added"] == 1
        assert payload["saved"] is True
        assert payload["written_to"] == str(out_path)
        save.assert_called_once()

    def test_auto_route_dry_run_writes_nothing(self, board, capsys):
        from kicad_tools.reasoning import PCBReasoningAgent

        with (
            patch.object(PCBReasoningAgent, "route_priority_nets", return_value=[]),
            patch.object(PCBReasoningAgent, "save") as save,
        ):
            rc, captured = _run_reason(
                ["reason", str(board), "--auto-route", "--dry-run", "--format", "json"], capsys
            )
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["saved"] is False
        assert payload["written_to"] is None
        save.assert_not_called()

    def test_interactive_is_refused_structurally(self, board, capsys):
        """--interactive is a dialogue: it has no single-document form."""
        rc, captured = _run_reason(
            ["reason", str(board), "--interactive", "--format", "json"], capsys
        )
        assert rc == 2
        payload = json.loads(captured.out)
        assert payload["success"] is False
        assert "--interactive" in payload["error"]

    def test_missing_board_is_error_document(self, tmp_path, capsys):
        rc, captured = _run_reason(
            ["reason", str(tmp_path / "nope.kicad_pcb"), "--format", "json"], capsys
        )
        assert rc == 1
        payload = json.loads(captured.out)
        assert payload["command"] == "reason"
        assert payload["success"] is False
        assert "not found" in payload["error"]

    def test_document_is_deterministic(self, board, capsys):
        argv = ["reason", str(board), "--analyze", "--format", "json"]
        _, first = _run_reason(argv, capsys)
        _, second = _run_reason(argv, capsys)
        assert first.out == second.out

    def test_text_mode_is_not_json(self, board, capsys):
        rc, captured = _run_reason(["reason", str(board), "--analyze"], capsys)
        assert rc == 0
        assert "KiCad LLM-Driven PCB Reasoning" in captured.out
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out)


# ---------------------------------------------------------------------------
# creepage-export-rules emission
# ---------------------------------------------------------------------------

_CREEPAGE_BOARD = """\
(kicad_pcb (version 20240108) (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "AC_LINE")
  (net 2 "GND")
  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 100) (end 160 130) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 130) (end 100 130) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 130) (end 100 100) (layer "Edge.Cuts") (width 0.1))
  (footprint "test:fp" (layer "F.Cu") (at 110 110)
    (property "Reference" "P1" (at 0 0 0) (layer "F.SilkS"))
    (fp_rect (start -1 -1) (end 1 1)
      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")
      (net 1 "AC_LINE"))
  )
  (footprint "test:fp" (layer "F.Cu") (at 150 125)
    (property "Reference" "P2" (at 0 0 0) (layer "F.SilkS"))
    (fp_rect (start -1 -1) (end 1 1)
      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")
      (net 2 "GND"))
  )
)
"""


@pytest.fixture
def creepage_project(tmp_path):
    """A .kicad_pro + sibling .kicad_pcb + voltage map with two HV domains."""
    from kicad_tools.core.project_file import create_minimal_project, save_project

    pro = tmp_path / "board.kicad_pro"
    save_project(create_minimal_project("board.kicad_pro"), pro)
    (tmp_path / "board.kicad_pcb").write_text(_CREEPAGE_BOARD)
    vmap = tmp_path / "voltages.json"
    vmap.write_text(json.dumps({"AC_LINE": 150.0, "GND": 0.0}))
    return pro, vmap


def _run_creepage_export(argv, capsys):
    from kicad_tools.cli.commands.creepage_export_rules import (
        run_creepage_export_rules_command,
    )

    rc = run_creepage_export_rules_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestCreepageExportRulesEmission:
    def test_dry_run_document(self, creepage_project, capsys):
        pro, vmap = creepage_project
        rc, out = _run_creepage_export(
            [
                "creepage-export-rules",
                str(pro),
                "--voltage-map",
                str(vmap),
                "--dry-run",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "creepage-export-rules"
        assert payload["project"] == str(pro)
        assert payload["voltage_map"] == str(vmap)
        assert payload["dry_run"] is True
        assert payload["written"] is False, "--dry-run must not claim written files"
        assert payload["skipped_reason"] == "dry-run"
        assert payload["standard"] == "iec60664"
        assert payload["domains"] == {"kct_0V": 0.0, "kct_150V": 150.0}
        assert payload["net_domains"] == {"AC_LINE": "kct_150V", "GND": "kct_0V"}
        assert payload["nets_assigned"] == 2
        assert payload["rules"], "a 150 V vs 0 V pair must produce a rule"
        assert payload["dru_block"] and "(rule " in payload["dru_block"]
        assert not pro.with_suffix(".kicad_dru").exists()

    def test_write_document_reports_written(self, creepage_project, capsys):
        pro, vmap = creepage_project
        rc, out = _run_creepage_export(
            ["creepage-export-rules", str(pro), "--voltage-map", str(vmap), "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["written"] is True
        assert payload["skipped_reason"] is None
        assert payload["dru_block"] is None, "the written block is the file, not the document"
        assert pro.with_suffix(".kicad_dru").is_file()

    def test_no_voltage_map_is_a_documented_no_op(self, creepage_project, capsys):
        pro, _vmap = creepage_project
        rc, out = _run_creepage_export(
            ["creepage-export-rules", str(pro), "--format", "json"], capsys
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["written"] is False
        assert payload["skipped_reason"] == "no-voltage-map"
        assert payload["rules"] == []

    def test_missing_project_is_error_document(self, tmp_path, capsys):
        rc, out = _run_creepage_export(
            ["creepage-export-rules", str(tmp_path / "nope.kicad_pro"), "--format", "json"],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["written"] is False
        assert "not found" in payload["error"]

    def test_missing_voltage_map_is_error_document(self, creepage_project, tmp_path, capsys):
        pro, _vmap = creepage_project
        rc, out = _run_creepage_export(
            [
                "creepage-export-rules",
                str(pro),
                "--voltage-map",
                str(tmp_path / "nope.json"),
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 1
        assert "voltage-map file not found" in json.loads(out)["error"]

    def test_document_is_deterministic(self, creepage_project, capsys):
        pro, vmap = creepage_project
        argv = [
            "creepage-export-rules",
            str(pro),
            "--voltage-map",
            str(vmap),
            "--dry-run",
            "--format",
            "json",
        ]
        _, first = _run_creepage_export(argv, capsys)
        _, second = _run_creepage_export(argv, capsys)
        assert first == second

    def test_text_mode_is_not_json(self, creepage_project, capsys):
        pro, vmap = creepage_project
        rc, out = _run_creepage_export(
            ["creepage-export-rules", str(pro), "--voltage-map", str(vmap), "--dry-run"], capsys
        )
        assert rc == 0
        assert "kct creepage-export-rules" in out
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)
