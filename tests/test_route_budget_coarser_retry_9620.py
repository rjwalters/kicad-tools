"""Budget-only coarser-grid retry and route-auto ``--grid`` (Issue #9620).

Two halves of the PCBJam failure-mode fix:

* ``kct route``: a batch whose only failures are budget-labelled (per-net
  timeout / iteration cap) on an auto-selected grid gets exactly one retry at
  2x the resolution (capped at the clearance ceiling); any geometric failure,
  an explicit ``--grid``, or a retry that routes fewer nets keeps the first
  result.
* ``route-auto``: ``--grid {auto|<mm>}`` -- the default ``auto`` reuses the
  ``kct route --grid auto`` selector (pad-aware, memory-capped) instead of the
  legacy fixed clearance/2 lattice; an explicit value is honoured as given.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.cli.parser import create_parser
from kicad_tools.cli.route_cmd import _budget_retry_argv, _chain_budget_coarser_pass
from kicad_tools.mcp.tools.routing import _resolve_route_auto_grid
from kicad_tools.router.stuck_classifier import write_search_failures_sidecar


def _args(out: Path, **overrides) -> argparse.Namespace:
    base = {
        "pcb": str(out.with_name("in.kicad_pcb")),
        "output": str(out),
        "route_engine": "grid",
        "manufacturer": "jlcpcb",
        "quiet": True,
        "_grid_selection": "auto",
        "grid": 0.1,
        "clearance": 0.2,
        "format": "text",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    out = tmp_path / "out.kicad_pcb"
    out.write_text("negotiated")
    write_search_failures_sidecar(out, {"VCP": "timeout"})
    return out


def _stub_open(monkeypatch, states: list[list[str]]):
    seq = iter(states)
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(seq),
    )


# --- kct route: retry fires on a budget-only failed batch ------------------


def test_retry_on_budget_only_failure(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"], []])
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        Path(argv[argv.index("-o") + 1]).write_text("coarser")
        return 0

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 0
    assert board.read_text() == "coarser"
    # The retry names the coarser grid (0.1 x2, under the 0.2 ceiling) and
    # stages its output; the first result is replaced only by promotion.
    assert seen["argv"][seen["argv"].index("--grid") + 1] == "0.2"
    staged = board.with_name(board.stem + ".coarser" + board.suffix)
    assert str(staged) in seen["argv"]
    assert not staged.exists()


def test_retry_grid_capped_at_clearance(board, monkeypatch):
    """2x the auto grid past the clearance ceiling is clamped to the ceiling."""
    write_search_failures_sidecar(board, {"VCP": "iteration_limit"})
    _stub_open(monkeypatch, [["VCP"], []])
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        Path(argv[argv.index("-o") + 1]).write_text("coarser")
        return 0

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_budget_coarser_pass(_args(board, grid=0.15, clearance=0.2), None, 2) == 0
    assert seen["argv"][seen["argv"].index("--grid") + 1] == "0.2"


def test_retry_routed_fewer_nets_keeps_first_result(board, monkeypatch):
    """A retry that routes fewer nets (more open) is discarded entirely."""
    _stub_open(monkeypatch, [["VCP"], ["VCP", "UART_TX"]])

    def fake_main(argv):
        Path(argv[argv.index("-o") + 1]).write_text("worse")
        return 2

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"
    assert not board.with_name(board.stem + ".coarser" + board.suffix).exists()


def test_retry_partial_improvement_promotes_board_keeps_exit(board, monkeypatch):
    write_search_failures_sidecar(board, {"VCP": "timeout", "UART_TX": "iteration_limit"})
    _stub_open(monkeypatch, [["VCP", "UART_TX"], ["UART_TX"]])

    def fake_main(argv):
        Path(argv[argv.index("-o") + 1]).write_text("better")
        return 2

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2
    assert board.read_text() == "better"


def test_failed_retry_restores_output(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"], ["VCP"]])

    def fake_main(argv):
        Path(argv[argv.index("-o") + 1]).write_text("garbage")
        return 1

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"


def test_exception_in_retry_restores_output(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"]])

    def boom(argv):
        raise RuntimeError("x")

    monkeypatch.setattr(route_cmd, "_main_impl", boom)
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"


# --- kct route: the retry must NOT fire ------------------------------------


@pytest.mark.parametrize(
    "labels",
    [
        {"VCP": "no_path"},  # geometric only
        {"VCP": "timeout", "UART_TX": "via_via_blocked"},  # mixed budget + geometric
    ],
)
def test_no_retry_on_geometric_failure(board, monkeypatch, labels):
    write_search_failures_sidecar(board, labels)
    _stub_open(monkeypatch, [sorted(labels)] if len(labels) > 1 else [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"


def test_no_retry_on_unlabelled_open_net(board, monkeypatch):
    """An open net with no recorded reason is not budget-only -- fail closed."""
    write_search_failures_sidecar(board, {"OTHER": "timeout"})
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2


def test_no_retry_without_sidecar(board, monkeypatch):
    sidecar = board.parent / (board.stem + ".search_failures.json")
    sidecar.unlink()
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board), None, 2) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"_grid_selection": "explicit"},  # explicit --grid is honoured (#6741)
        {"route_engine": "lattice"},
        {"format": "json"},
        {"complete": True},
        {"resume": "ck.kicad_pcb"},
        {"checkpoint": "ck.kicad_pcb"},
        {"nets": "VCP"},
        {"skip_nets": "GND"},
        {"region": "0,0,10,10"},
        {"dry_run": True},
    ],
)
def test_opt_out_and_ineligible(board, monkeypatch, overrides):
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board, **overrides), None, 2) == 2


@pytest.mark.parametrize("result", [0, 1, 3, 4, 10])
def test_only_partial_exit_triggers_retry(board, monkeypatch, result):
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board), None, result) == result


def test_no_retry_when_already_at_clearance_ceiling(board, monkeypatch):
    """An auto grid already at the ceiling has no coarser legal pitch."""
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(board, grid=0.2, clearance=0.2), None, 2) == 2


def test_no_retry_without_output_board(tmp_path, monkeypatch):
    out = tmp_path / "never-written.kicad_pcb"
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_budget_coarser_pass(_args(out), None, 2) == 2


# --- the retry argv ---------------------------------------------------------


def test_retry_argv_reuses_operator_flags():
    argv = [
        "in.kicad_pcb",
        "--grid",
        "auto",
        "--seed",
        "42",
        "--output=out.kicad_pcb",
        "--lint-gate",
        "--lint-gate-waivers",
        "w.yaml",
        "--lint-gate-strict",
    ]
    out = _budget_retry_argv(argv, 0.254, Path("staged.kicad_pcb"))
    assert out[0] == "in.kicad_pcb"
    assert out[out.index("--grid") + 1] == "0.254"
    assert out[out.index("--seed") + 1] == "42"
    assert "--output=staged.kicad_pcb" in out
    for flag in ("--lint-gate", "--lint-gate-waivers", "--lint-gate-strict", "w.yaml", "auto"):
        assert flag not in out


def test_retry_argv_appends_missing_grid_and_output():
    out = _budget_retry_argv(["in.kicad_pcb", "-v"], 0.2, Path("s.kicad_pcb"))
    assert out[out.index("--grid") + 1] == "0.2"
    assert out[out.index("-o") + 1] == "s.kicad_pcb"


def test_retry_argv_replaces_short_output_flag():
    out = _budget_retry_argv(["in.kicad_pcb", "-o", "out.kicad_pcb"], 0.2, Path("s.kicad_pcb"))
    assert out.count("-o") == 1
    assert out[out.index("-o") + 1] == "s.kicad_pcb"
    assert "out.kicad_pcb" not in out


# --- route-auto: --grid flag and selection ---------------------------------


def test_route_auto_parser_has_grid_flag():
    parser = create_parser()
    args = parser.parse_args(["route-auto", "board.kicad_pcb", "--net", "GND"])
    assert args.grid == "auto"
    args = parser.parse_args(["route-auto", "board.kicad_pcb", "--net", "GND", "--grid", "0.2"])
    assert args.grid == "0.2"


def test_resolve_grid_explicit_value_honoured(tmp_path):
    pcb = SimpleNamespace(get_board_outline=lambda: [(0, 0), (10, 8)])
    rules = SimpleNamespace(min_clearance=0.2)
    assert _resolve_route_auto_grid(tmp_path / "b.kicad_pcb", pcb, rules, 0.25) == (0.25, [])
    assert _resolve_route_auto_grid(tmp_path / "b.kicad_pcb", pcb, rules, "0.25") == (0.25, [])


@pytest.mark.parametrize("bad", ["abc", -0.1, 0])
def test_resolve_grid_rejects_invalid_explicit_value(tmp_path, bad):
    pcb = SimpleNamespace(get_board_outline=lambda: [(0, 0), (10, 8)])
    rules = SimpleNamespace(min_clearance=0.2)
    with pytest.raises(ValueError):
        _resolve_route_auto_grid(tmp_path / "b.kicad_pcb", pcb, rules, bad)


def test_resolve_grid_auto_uses_selector(tmp_path, monkeypatch):
    """Auto feeds the whole-board pads, declared-aware clearance and the board
    dimensions into auto_select_grid_resolution and returns its pitch."""
    from kicad_tools.router import io as router_io

    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    pcb = SimpleNamespace(get_board_outline=lambda: [(1, 2), (11, 10)])
    rules = SimpleNamespace(min_clearance=0.2)
    seen = {}

    class _Sel:
        resolution = 0.127
        memory_forced_unsafe_grid = False

    def fake_selector(**kwargs):
        seen.update(kwargs)
        return _Sel()

    monkeypatch.setattr(router_io, "auto_select_grid_resolution", fake_selector)
    monkeypatch.setattr(router_io, "extract_pad_positions", lambda p: [object(), object()])
    monkeypatch.setattr(
        "kicad_tools.router.clearance_resolver.resolve_routing_clearances",
        lambda path, target_mm: SimpleNamespace(trace_mm=0.15),
    )

    resolution, warnings = _resolve_route_auto_grid(board, pcb, rules, None)
    assert resolution == 0.127
    assert warnings == []
    assert seen["clearance"] == 0.15
    assert seen["board_width"] == 10.0
    assert seen["board_height"] == 8.0
    assert len(seen["pads"]) == 2


def test_resolve_grid_auto_warns_on_memory_forced_unsafe_grid(tmp_path, monkeypatch):
    from kicad_tools.router import io as router_io

    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    pcb = SimpleNamespace(get_board_outline=lambda: None)
    rules = SimpleNamespace(min_clearance=0.2)

    class _Sel:
        resolution = 0.1
        memory_forced_unsafe_grid = True

    monkeypatch.setattr(router_io, "auto_select_grid_resolution", lambda **k: _Sel())
    monkeypatch.setattr(router_io, "extract_pad_positions", lambda p: [object()])
    monkeypatch.setattr(
        "kicad_tools.router.clearance_resolver.resolve_routing_clearances",
        lambda path, target_mm: SimpleNamespace(trace_mm=0.15),
    )

    resolution, warnings = _resolve_route_auto_grid(board, pcb, rules, "auto")
    assert resolution == 0.1
    assert len(warnings) == 1
    assert "0.1" in warnings[0] and "memory cap" in warnings[0]


def test_resolve_grid_auto_falls_back_without_pads(tmp_path, monkeypatch):
    from kicad_tools.router import io as router_io

    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)")
    pcb = SimpleNamespace(get_board_outline=lambda: None)
    rules = SimpleNamespace(min_clearance=0.2)
    monkeypatch.setattr(router_io, "extract_pad_positions", lambda p: [])
    assert _resolve_route_auto_grid(board, pcb, rules, None) == (None, [])


def test_cli_forwards_grid_to_route_net_auto():
    from unittest.mock import patch

    from kicad_tools.cli.commands.routing import run_route_auto_command

    args = SimpleNamespace(
        pcb="/tmp/x.kicad_pcb",
        net="NET1",
        output=None,
        strategy="auto",
        no_repair=False,
        no_via_resolution=False,
        dry_run=False,
        region=None,
        allow_partial=False,
        via_drill=None,
        via_diameter=None,
        verbose=False,
        grid="0.2",
    )
    success = {
        "success": True,
        "net_name": "NET1",
        "strategy_used": "global",
        "metrics": {},
        "warnings": [],
    }
    with patch("kicad_tools.mcp.tools.routing.route_net_auto", return_value=success) as mock:
        assert run_route_auto_command(args) == 0
    _, kwargs = mock.call_args
    assert kwargs["grid"] == "0.2"


# --- the hook: _main_impl runs the retry before the chained completion pass --


def test_main_impl_hooks_retry_before_chain_complete(board, monkeypatch):
    """The real ``_main_impl`` wiring: a budget-only partial batch that entered
    with ``--grid auto`` reaches the coarser retry (and a promoted full board
    makes the chained completion pass a no-op)."""
    write_search_failures_sidecar(board, {"VCP": "timeout", "UART_TX": "iteration_limit"})
    states = iter([["VCP", "UART_TX"], []])
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(states),
    )
    calls = {"outer": 0, "inner": 0}

    def fake_run_main_impl(args, parser, argv):
        calls["outer"] += 1
        if calls["outer"] == 1:
            # The first (outer) pass: auto grid, partial batch, board written.
            args._grid_selection = "auto"
            args.grid = 0.1
            args.clearance = 0.2
            Path(args.output).write_text("negotiated")
            return 2
        # The retry pass: explicit coarser grid, everything routed.
        Path(args.output).write_text("coarser")
        return 0

    seen = {}

    def fake_inner_main(argv):
        calls["inner"] += 1
        seen["argv"] = argv
        staged = Path(argv[argv.index("-o") + 1])
        staged.write_text("coarser-retry")
        return 0

    monkeypatch.setattr(route_cmd, "_run_main_impl", fake_run_main_impl)
    real_main_impl = route_cmd._main_impl
    monkeypatch.setattr(route_cmd, "_main_impl", fake_inner_main)
    rc = real_main_impl(
        [str(board.with_name("in.kicad_pcb")), "-o", str(board), "--grid", "auto", "--quiet"]
    )
    assert rc == 0
    assert calls == {"outer": 1, "inner": 1}
    assert board.read_text() == "coarser-retry"
    assert seen["argv"][seen["argv"].index("--grid") + 1] == "0.2"
