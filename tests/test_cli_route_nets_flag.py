"""Tests for the ``--nets`` route-only flag (Issue #4322).

``kct route --nets "/A,/B"`` routes ONLY the listed nets and treats every
other board net as an obstacle -- the inverse of ``--skip-nets``.  It is
implemented at the CLI layer by inverting the request into the existing
``--skip-nets`` machinery (skip every board net NOT listed), so the router is
unchanged.  ``kct route-auto`` gains a matching ``--nets`` that routes several
nets in sequence.

The issue this fixes: ``kct route --region``'s help advertised a ``--nets``
flag that did not exist, so ``kct route board.kicad_pcb --nets "/GND"`` failed
with ``error: unrecognized arguments``.
"""

from __future__ import annotations

import argparse

import pytest


# ---------------------------------------------------------------------------
# A tiny fake board so the net-selection logic can be tested without a real
# .kicad_pcb file or the C++ router backend.
# ---------------------------------------------------------------------------
class _Pad:
    def __init__(self, net_name: str) -> None:
        self.net_name = net_name


class _FP:
    def __init__(self, pads: list[_Pad]) -> None:
        self.pads = pads


class _FakePCB:
    """Board with nets: /A(2), /B(2), /C(2), GND(3), /SOLO(1)."""

    def __init__(self) -> None:
        self.footprints = [
            _FP([_Pad("/A"), _Pad("/A")]),
            _FP([_Pad("/B"), _Pad("/B")]),
            _FP([_Pad("/C"), _Pad("/C")]),
            _FP([_Pad("GND"), _Pad("GND"), _Pad("GND")]),
            _FP([_Pad("/SOLO")]),  # single-pad net -- nothing to route
        ]


ALL_NETS = {"/A", "/B", "/C", "GND", "/SOLO"}


@pytest.fixture
def fake_board(monkeypatch):
    """Patch ``PCB.load`` so ``_resolve_route_only_nets`` sees ``_FakePCB``."""
    from kicad_tools.schema import pcb as pcb_mod

    monkeypatch.setattr(pcb_mod.PCB, "load", lambda _path: _FakePCB())
    return _FakePCB()


def _route_args(**overrides):
    ns = argparse.Namespace(nets=None, skip_nets=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# --nets inversion into the skip set
# ---------------------------------------------------------------------------
def test_nets_selects_only_listed_nets(fake_board):
    """``--nets`` routes only the listed nets; all others become the skip set."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="/A,/B")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc == 0
    # The route-only marker records exactly the requested nets.
    assert args._route_only_nets == ["/A", "/B"]
    # Every OTHER board net is skipped (inverse of --skip-nets).
    skip_set = set(args.skip_nets.split(","))
    assert skip_set == ALL_NETS - {"/A", "/B"}
    # The routable set is precisely the requested nets.
    assert ALL_NETS - skip_set == {"/A", "/B"}


def test_nets_absent_is_noop(fake_board):
    """No ``--nets`` -> untouched skip set, no route-only marker."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets=None, skip_nets="GND")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc == 0
    assert args.skip_nets == "GND"  # unchanged
    assert getattr(args, "_route_only_nets", None) is None


def test_nets_whitespace_is_trimmed(fake_board):
    """Whitespace around each name is trimmed, like ``--skip-nets``."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="  /A , /B  ")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc == 0
    assert args._route_only_nets == ["/A", "/B"]
    assert set(args.skip_nets.split(",")) == ALL_NETS - {"/A", "/B"}


def test_nets_deduplicates_preserving_order(fake_board):
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="/B,/A,/B")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc == 0
    assert args._route_only_nets == ["/B", "/A"]


def test_nets_unknown_net_errors_clearly(fake_board, capsys):
    """An unknown net name exits non-zero and NAMES the missing net."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="/A,/DOES_NOT_EXIST")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc != 0
    err = capsys.readouterr().err
    assert "/DOES_NOT_EXIST" in err
    assert "not present on the board" in err


def test_nets_mutually_exclusive_with_skip_nets(fake_board, capsys):
    """``--nets`` + ``--skip-nets`` together is a clear, non-zero error."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="/A", skip_nets="GND")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc != 0
    err = capsys.readouterr().err
    assert "mutually exclusive" in err


def test_nets_empty_list_errors(fake_board, capsys):
    """``--nets`` with only whitespace/commas names no nets -> error."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="  ,  ")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc != 0
    err = capsys.readouterr().err
    assert "no net names" in err


def test_nets_sub_two_pad_net_is_reported(fake_board, capsys):
    """A listed net with <2 pads is reported (can't route), not silently dropped."""
    from pathlib import Path

    from kicad_tools.cli.route_cmd import _resolve_route_only_nets

    args = _route_args(nets="/A,/SOLO")
    rc = _resolve_route_only_nets(args, Path("dummy.kicad_pcb"))

    assert rc == 0  # still proceeds -- the net just contributes nothing
    err = capsys.readouterr().err
    assert "/SOLO" in err
    assert "fewer than 2 pads" in err
    # Both requested nets are still in the route set (neither skipped).
    assert args._route_only_nets == ["/A", "/SOLO"]
    assert "/SOLO" not in args.skip_nets.split(",")


# ---------------------------------------------------------------------------
# Parser wiring (drift-adjacent): --nets present on both route parsers + shim.
# ---------------------------------------------------------------------------
def test_nets_flag_on_outer_route_parser():
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["route", "board.kicad_pcb", "--nets", "/A,/B"])
    assert args.nets == "/A,/B"


def test_nets_flag_on_inner_route_parser():
    """The inner route_cmd parser accepts --nets (no 'unrecognized arguments')."""
    import argparse as _argparse
    from unittest.mock import patch

    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, _argparse.Namespace] = {}
    real_parse_args = _argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *a, **k):
        ns = real_parse_args(self, *a, **k)
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["ns"] = ns
            raise SystemExit(0)
        return ns

    with patch.object(_argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main(["board.kicad_pcb", "--nets", "/A,/B"])

    assert captured["ns"].nets == "/A,/B"


def test_route_command_shim_forwards_nets(monkeypatch):
    """run_route_command forwards --nets into the inner argv."""
    from kicad_tools.cli import route_cmd
    from kicad_tools.cli.commands import routing

    seen: dict[str, list[str]] = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(route_cmd, "main", fake_main)

    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["route", "board.kicad_pcb", "--nets", "/A,/B"])
    routing.run_route_command(args)

    assert "--nets" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--nets") + 1] == "/A,/B"


# ---------------------------------------------------------------------------
# route-auto --nets orchestration
# ---------------------------------------------------------------------------
def _auto_args(**overrides):
    ns = argparse.Namespace(net=None, nets=None)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def test_route_auto_net_singular_still_works():
    from kicad_tools.cli.commands.routing import _parse_route_auto_targets

    net_list, rc = _parse_route_auto_targets(_auto_args(net="GND"))
    assert rc == 0
    assert net_list == ["GND"]


def test_route_auto_nets_parses_and_dedups():
    from kicad_tools.cli.commands.routing import _parse_route_auto_targets

    net_list, rc = _parse_route_auto_targets(_auto_args(nets=" /A , /B , /A "))
    assert rc == 0
    assert net_list == ["/A", "/B"]


def test_route_auto_net_and_nets_mutually_exclusive(capsys):
    from kicad_tools.cli.commands.routing import _parse_route_auto_targets

    net_list, rc = _parse_route_auto_targets(_auto_args(net="GND", nets="/A"))
    assert net_list is None
    assert rc != 0
    assert "mutually exclusive" in capsys.readouterr().err


def test_route_auto_requires_net_or_nets(capsys):
    from kicad_tools.cli.commands.routing import _parse_route_auto_targets

    net_list, rc = _parse_route_auto_targets(_auto_args())
    assert net_list is None
    assert rc != 0
    assert "requires --net" in capsys.readouterr().err


def test_route_auto_empty_nets_errors(capsys):
    from kicad_tools.cli.commands.routing import _parse_route_auto_targets

    net_list, rc = _parse_route_auto_targets(_auto_args(nets="  ,  "))
    assert net_list is None
    assert rc != 0
    assert "no net names" in capsys.readouterr().err


def test_route_auto_nets_on_parser_and_net_optional():
    """route-auto declares --nets and --net is no longer required."""
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    # --nets alone parses (previously --net was required=True).
    args = parser.parse_args(["route-auto", "board.kicad_pcb", "--nets", "/A,/B"])
    assert args.nets == "/A,/B"
    assert args.net is None


def test_route_auto_nets_loop_aggregates_exit_code(monkeypatch):
    """--nets routes each net; exit code is non-zero if ANY net fails."""
    from kicad_tools.cli.commands import routing

    calls: list[str] = []

    def fake_one(args, net_name, pcb_path, output_path):
        calls.append(net_name)
        return 0 if net_name == "/A" else 1  # /B "fails"

    monkeypatch.setattr(routing, "_route_auto_one", fake_one)

    args = _auto_args(nets="/A,/B", dry_run=False, output=None, pcb="board.kicad_pcb")
    rc = routing.run_route_auto_command(args)

    assert calls == ["/A", "/B"]
    assert rc == 1  # aggregated non-zero because /B failed


def test_route_auto_nets_all_success_returns_zero(monkeypatch):
    from kicad_tools.cli.commands import routing

    monkeypatch.setattr(routing, "_route_auto_one", lambda *a, **k: 0)

    args = _auto_args(nets="/A,/B", dry_run=False, output=None, pcb="board.kicad_pcb")
    assert routing.run_route_auto_command(args) == 0


def test_route_auto_nets_chains_output(monkeypatch):
    """With an output path, net N+1 routes from net N's output (accumulation)."""
    from kicad_tools.cli.commands import routing

    sources: list[str] = []

    def fake_one(args, net_name, pcb_path, output_path):
        sources.append(pcb_path)
        return 0

    monkeypatch.setattr(routing, "_route_auto_one", fake_one)

    args = _auto_args(nets="/A,/B,/C", dry_run=False, output="out.kicad_pcb", pcb="board.kicad_pcb")
    routing.run_route_auto_command(args)

    # First net from the original board; subsequent nets from the output.
    assert sources == ["board.kicad_pcb", "out.kicad_pcb", "out.kicad_pcb"]
