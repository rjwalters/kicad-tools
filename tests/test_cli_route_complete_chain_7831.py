"""Chained ``--complete`` pass after a grid route (Issue #7831, epic #4410)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.cli.route_cmd import _chain_complete_pass, _route_parser


def _args(out: Path, **overrides) -> argparse.Namespace:
    base = {
        "pcb": str(out.with_name("in.kicad_pcb")),
        "output": str(out),
        "route_engine": "grid",
        "manufacturer": "jlcpcb",
        "quiet": True,
        "seed": 42,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    out = tmp_path / "out.kicad_pcb"
    out.write_text("negotiated")
    return out


def _stub_open(monkeypatch, states: list[list[str]]):
    seq = iter(states)
    monkeypatch.setattr(
        "kicad_tools.router.partial_rescue.partially_connected_signal_nets",
        lambda *a, **k: next(seq),
    )


def test_flag_registered():
    assert _route_parser().parse_args(["b.kicad_pcb"]).no_chain_complete is False
    assert _route_parser().parse_args(["b.kicad_pcb", "--no-chain-complete"]).no_chain_complete


def test_pass_closes_open_net(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"], []])
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        Path(argv[argv.index("-o") + 1]).write_text("completed")
        return 0

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_complete_pass(_args(board), None, 2) == 0
    assert board.read_text() == "completed"
    assert "--complete" in seen["argv"] and seen["argv"][seen["argv"].index("--seed") + 1] == "42"
    assert not board.with_name(board.name + ".pre-complete").exists()


def test_noop_on_complete_board(board, monkeypatch):
    _stub_open(monkeypatch, [[]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_complete_pass(_args(board), None, 0) == 0
    assert board.read_text() == "negotiated"


@pytest.mark.parametrize(
    "overrides",
    [{"no_chain_complete": True}, {"route_engine": "lattice"}, {"complete": True}, {"nets": "A"}],
)
def test_opt_out_and_ineligible(board, monkeypatch, overrides):
    _stub_open(monkeypatch, [["VCP"]])
    monkeypatch.setattr(route_cmd, "_main_impl", lambda a: pytest.fail("must not run"))
    assert _chain_complete_pass(_args(board, **overrides), None, 2) == 2


def test_failed_pass_restores_output(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"], ["VCP"]])

    def fake_main(argv):
        Path(argv[argv.index("-o") + 1]).write_text("garbage")
        return 8

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_complete_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"


def test_exception_restores_output(board, monkeypatch):
    _stub_open(monkeypatch, [["VCP"]])

    def boom(argv):
        raise RuntimeError("x")

    monkeypatch.setattr(route_cmd, "_main_impl", boom)
    assert _chain_complete_pass(_args(board), None, 2) == 2
    assert board.read_text() == "negotiated"


def test_partial_improvement_keeps_board_and_exit(board, monkeypatch):
    _stub_open(monkeypatch, [["A", "B"], ["B"]])

    def fake_main(argv):
        Path(argv[argv.index("-o") + 1]).write_text("better")
        return 8

    monkeypatch.setattr(route_cmd, "_main_impl", fake_main)
    assert _chain_complete_pass(_args(board), None, 2) == 2
    assert board.read_text() == "better"
