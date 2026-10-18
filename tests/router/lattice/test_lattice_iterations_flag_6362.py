"""``kct route --lattice-iterations`` reaches the lattice round cap (Issue #6362).

Before this, ``_negotiate_lattice_netset`` hardcoded ``max_iterations = 12``
(#6381) and ``--iterations`` (default 15) fed only the grid negotiators, so
the lattice round cap was not settable from the CLI.  An unconverged run also
printed only ``converged=False``, which does not say whether the round cap or
the wall-clock deadline stopped it.

These tests pin:

* both ``kct route`` parsers accept ``--lattice-iterations`` and reject N < 1;
* the outer ``kct route`` shim forwards it to the inner parser;
* the value reaches ``LatticePathfinder.route_netset(max_iterations=...)``,
  and the default stays 12 when the flag is absent;
* the stop-reason log line names the round cap or the deadline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.lattice.pathfinder import LatticeNegotiationStats, LatticePathfinder
from kicad_tools.router.lattice.round_cap import (
    LATTICE_NEGOTIATION_DEFAULT_ROUNDS,
    lattice_stop_reason,
)

# Two 0402s, NET1 pre-routed, NET2 stranded; routes in ~1s through the lattice.
from tests.test_cli_route_complete_4471 import STRANDED_BOARD


@pytest.fixture
def board(tmp_path: Path) -> Path:
    pcb = tmp_path / "stranded.kicad_pcb"
    pcb.write_text(STRANDED_BOARD)
    return pcb


def _stats(**overrides) -> LatticeNegotiationStats:
    base: dict = {
        "iterations": 12,
        "converged": False,
        "routed": 1,
        "total": 2,
        "lattice_builds": 1,
    }
    base.update(overrides)
    return LatticeNegotiationStats(**base)


def _stub_route_netset(seen: list[dict], stats: LatticeNegotiationStats):
    def stub(self, connections, **kwargs):
        seen.append(kwargs)
        return {}, stats

    return stub


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
class TestParsers:
    def test_inner_parser_accepts_flag(self):
        from kicad_tools.cli.route_cmd import _route_parser

        args = _route_parser().parse_args(["b.kicad_pcb", "--lattice-iterations", "3"])
        assert args.lattice_iterations == 3

    def test_inner_parser_default_is_unset(self):
        from kicad_tools.cli.route_cmd import _route_parser

        assert _route_parser().parse_args(["b.kicad_pcb"]).lattice_iterations is None

    def test_kct_route_accepts_flag(self):
        from tests.route_cli import kct_route

        _, args = kct_route(["b.kicad_pcb", "--lattice-iterations", "20"])
        assert args.lattice_iterations == 20

    @pytest.mark.parametrize("bad", ["0", "-1", "abc"])
    def test_inner_parser_rejects_non_positive(self, bad, capsys):
        from kicad_tools.cli.route_cmd import _route_parser

        with pytest.raises(SystemExit):
            _route_parser().parse_args(["b.kicad_pcb", "--lattice-iterations", bad])
        assert "--lattice-iterations" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["0", "-5"])
    def test_kct_route_rejects_non_positive(self, bad):
        from kicad_tools.cli import main

        with pytest.raises(SystemExit):
            main(["route", "b.kicad_pcb", "--lattice-iterations", bad])

    def test_iterations_help_points_at_lattice_flag(self):
        from kicad_tools.cli.route_cmd import _route_parser

        parser = _route_parser()
        assert "--lattice-iterations" in parser.format_help()
        iterations = next(a for a in parser._actions if "--iterations" in a.option_strings)
        assert "--lattice-iterations" in iterations.help


class TestKctRoutePassthrough:
    def test_flag_reaches_router_only_when_set(self, tmp_path):
        from tests.route_cli import kct_route

        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(STRANDED_BOARD)

        with_flag, args = kct_route([str(pcb), "--lattice-iterations", "7"])
        without_flag, plain = kct_route([str(pcb)])

        assert with_flag[with_flag.index("--lattice-iterations") + 1] == "7"
        assert args.lattice_iterations == 7
        assert "--lattice-iterations" not in without_flag
        assert plain.lattice_iterations is None


# ---------------------------------------------------------------------------
# Plumbing to route_netset
# ---------------------------------------------------------------------------
class TestRoundCapPlumbing:
    def test_flag_value_reaches_route_netset(self, board, monkeypatch, capsys):
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(
            board, strategy="lattice", lattice_max_iterations=3, force_python=True
        )
        seen: list[dict] = []
        monkeypatch.setattr(
            LatticePathfinder,
            "route_netset",
            _stub_route_netset(seen, _stats(iterations=3)),
        )
        router._negotiate_lattice_netset()
        assert [kw["max_iterations"] for kw in seen] == [3]
        out = capsys.readouterr().out
        assert "stopped by: round cap (3)" in out
        assert "--lattice-iterations" in out

    def test_absent_flag_keeps_default_twelve(self, board, monkeypatch, capsys):
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(board, strategy="lattice", force_python=True)
        seen: list[dict] = []
        monkeypatch.setattr(
            LatticePathfinder,
            "route_netset",
            _stub_route_netset(seen, _stats(iterations=4, converged=True, routed=2)),
        )
        router._negotiate_lattice_netset()
        assert LATTICE_NEGOTIATION_DEFAULT_ROUNDS == 12
        assert [kw["max_iterations"] for kw in seen] == [12]
        # A converged run prints no stop-reason clause.
        assert "stopped by" not in capsys.readouterr().out

    def test_deadline_named_instead_of_cap(self, board, monkeypatch, capsys):
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(
            board, strategy="lattice", lattice_max_iterations=5, force_python=True
        )
        seen: list[dict] = []
        monkeypatch.setattr(
            LatticePathfinder,
            "route_netset",
            _stub_route_netset(seen, _stats(iterations=2, deadline_hit=True, elapsed_s=4.2)),
        )
        router._negotiate_lattice_netset()
        out = capsys.readouterr().out
        assert "stopped by: deadline after 2/5 round(s)" in out
        assert "round cap" not in out

    def test_cli_flag_reaches_route_netset(self, board, tmp_path, monkeypatch):
        """End to end through the worker CLI seam (real negotiation)."""
        from kicad_tools.cli.route_cmd import _in_process_main as route_main

        original = LatticePathfinder.route_netset
        seen: list[int] = []

        def spy(self, connections, **kwargs):
            seen.append(kwargs.get("max_iterations"))
            return original(self, connections, **kwargs)

        monkeypatch.setattr(LatticePathfinder, "route_netset", spy)
        rc = route_main(
            [
                str(board),
                "-o",
                str(tmp_path / "out.kicad_pcb"),
                "--route-engine",
                "lattice",
                "--strategy",
                "basic",
                "--layers",
                "2",
                "--no-cache",
                "--lattice-iterations",
                "4",
            ]
        )
        assert rc == 0
        assert seen and all(m == 4 for m in seen)


# ---------------------------------------------------------------------------
# lattice_stop_reason unit cases
# ---------------------------------------------------------------------------
class TestStopReason:
    def test_converged_is_silent(self):
        assert lattice_stop_reason(_stats(converged=True, iterations=12), 12) is None

    def test_cap_reached(self):
        msg = lattice_stop_reason(_stats(iterations=12), 12)
        assert msg is not None and "round cap (12)" in msg

    def test_deadline_wins_even_on_last_round(self):
        msg = lattice_stop_reason(_stats(iterations=12, deadline_hit=True), 12)
        assert msg is not None and "deadline" in msg and "round cap" not in msg

    def test_neither_limit_is_silent(self):
        assert lattice_stop_reason(_stats(iterations=3), 12) is None
