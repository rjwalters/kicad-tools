"""``kct route --plan-gate`` exit-code tests (Issue #5521, Epic #5510 Phase 1c).

The gate is opt-in and shares exit code 9 with the crossing-tail census
gate (#4799); the ``[plan-gate]`` / ``[crosstail-gate]`` stderr prefix is
what distinguishes them.  What these tests pin down:

1. With ``--plan-gate`` and an infeasible plan, ``kct route`` exits 9 and
   **no detailed routing runs** -- asserted with a spy that raises if any
   of the three routing entry points is called, not by timing.
2. The same board with ``--plan-gate --force`` routes (the override is the
   EXISTING ``--force``; #5521 adds exactly one flag, ``--plan-gate``).
3. Without ``--plan-gate`` the identical infeasible plan changes nothing --
   the default path stays report-only.
4. The dense (``--two-phase``) path is gated too.  The gate is a CLI
   preflight, so it fires before the router picks a path at all; this test
   is what keeps that true if the plan stage ever moves again.

Infeasibility is forced by monkeypatching ``RegionGraph.get_total_overflow``
rather than by crafting a congested board, so the test is fast and is about
the GATE, not about any particular board's congestion.  ``tests/
test_routing_plan_5510.py`` owns the real over-subscribed fixture.
"""

from __future__ import annotations

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.core import Autorouter
from kicad_tools.router.region_graph import RegionGraph

#: Exit code shared by both pre-route gates.
GATE_EXIT = 9


@pytest.fixture
def infeasible_plan(monkeypatch):
    """Make every plan report overflow, whatever the board really looks like."""
    monkeypatch.setattr(RegionGraph, "get_total_overflow", lambda self: 7)


@pytest.fixture
def no_detailed_routing(monkeypatch):
    """Fail loudly if anything starts routing copper."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("detailed routing started despite --plan-gate")

    monkeypatch.setattr(Autorouter, "route_all_negotiated", _forbidden)
    monkeypatch.setattr(Autorouter, "route_all_two_phase", _forbidden)
    monkeypatch.setattr(Autorouter, "route_with_escape", _forbidden)


def _argv(pcb, out, *extra: str) -> list[str]:
    return [str(pcb), "-o", str(out), "--skip-drc", *extra]


class TestPlanGateExitCode:
    def test_infeasible_plan_exits_nine_before_any_routing(
        self, routing_test_pcb, tmp_path, capsys, infeasible_plan, no_detailed_routing
    ):
        rc = route_cmd.main(_argv(routing_test_pcb, tmp_path / "out.kicad_pcb", "--plan-gate"))
        assert rc == GATE_EXIT

        err = capsys.readouterr().err
        assert "[plan-gate] NO-GO" in err
        # The refusal carries the report, not just a code.
        assert "Routing plan:" in err
        assert "NOT feasible" in err
        # ...and says how to proceed.
        assert "--force" in err

    def test_dense_two_phase_path_is_gated_too(
        self, routing_test_pcb, tmp_path, capsys, infeasible_plan, no_detailed_routing
    ):
        rc = route_cmd.main(
            _argv(routing_test_pcb, tmp_path / "out.kicad_pcb", "--plan-gate", "--two-phase")
        )
        assert rc == GATE_EXIT
        assert "[plan-gate] NO-GO" in capsys.readouterr().err

    def test_force_overrides_the_gate_and_routes(
        self, routing_test_pcb, tmp_path, capsys, infeasible_plan
    ):
        out = tmp_path / "out.kicad_pcb"
        rc = route_cmd.main(_argv(routing_test_pcb, out, "--plan-gate", "--force"))
        assert rc != GATE_EXIT
        err = capsys.readouterr().err
        assert "[plan-gate] overridden by --force" in err
        assert out.exists()

    def test_without_the_flag_an_infeasible_plan_changes_nothing(
        self, routing_test_pcb, tmp_path, capsys, infeasible_plan
    ):
        out = tmp_path / "out.kicad_pcb"
        rc = route_cmd.main(_argv(routing_test_pcb, out))
        assert rc != GATE_EXIT
        err = capsys.readouterr().err
        assert "[plan-gate]" not in err
        assert out.exists()

    def test_feasible_board_passes_the_gate(self, routing_test_pcb, tmp_path, capsys):
        out = tmp_path / "out.kicad_pcb"
        rc = route_cmd.main(_argv(routing_test_pcb, out, "--plan-gate"))
        assert rc != GATE_EXIT
        assert "[plan-gate] GO" in capsys.readouterr().err
        assert out.exists()


class TestPlanGateFlagSurface:
    """Exactly ONE flag is added; ``--force`` is the pre-existing override."""

    def test_plan_gate_is_declared_on_both_parsers(self):
        """Both ``kct route`` and ``route_cmd.main`` must accept the flag.

        A flag on only one of the two is exactly the drift
        ``tests/test_cli_parser_drift.py`` exists to catch: ``kct route
        --plan-gate`` would be rejected by the outer parser, or accepted
        there and dropped on the way to the inner one.
        """
        from kicad_tools.cli.parser import create_parser

        assert "--plan-gate" in route_cmd._route_parser().format_help()
        assert route_cmd._route_parser().parse_args(["b.kicad_pcb", "--plan-gate"]).plan_gate

        outer = create_parser()
        route_sub = next(
            action.choices["route"]
            for action in outer._actions
            if getattr(action, "choices", None) and "route" in action.choices
        )
        assert "--plan-gate" in route_sub.format_help()
        assert outer.parse_args(["route", "b.kicad_pcb", "--plan-gate"]).plan_gate

    def test_plan_gate_defaults_off(self):
        args = route_cmd._route_parser().parse_args(["board.kicad_pcb"])
        assert args.plan_gate is False

    def test_no_second_force_flag_was_added(self):
        """A second ``--force`` would be an argparse conflict; the gate
        reads the existing one."""
        help_text = route_cmd._route_parser().format_help()
        assert help_text.count("--force") >= 1
        args = route_cmd._route_parser().parse_args(["board.kicad_pcb", "--force"])
        assert args.force is True
