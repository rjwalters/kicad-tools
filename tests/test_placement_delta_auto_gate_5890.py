"""Auto-gating of the placement-delta feedback loop on plan infeasibility (#5890).

Epic #5511 Phase 3's load-bearing promise: classifier-driven placement feedback
is **on by default only when the plan is infeasible**.  Everything here pins the
decision surface rather than any particular board's congestion:

1. The toggle is tri-state -- ``--placement-delta-feedback`` forces on,
   ``--no-placement-delta-feedback`` forces off, and *absence* is AUTO.
2. AUTO + an infeasible plan (Epic #5510's ``overflow_report.feasible`` false)
   runs the loop.
3. AUTO + a feasible plan does NOT -- the flag-off path is unchanged, which is
   the #4053/#4051 precedent the epic cites.
4. AUTO + no plan verdict at all (``--no-routing-plan``) does NOT: the default
   path only changes where the plan positively proves overflow.
5. ``--no-placement-delta-feedback`` beats an infeasible plan.
6. The explicit flag still beats a feasible plan (pre-#5890 behaviour kept).

Infeasibility is forced by monkeypatching ``RegionGraph.get_total_overflow``
(the same lever ``tests/test_route_plan_gate_5521.py`` uses), and the loop
itself is replaced by a spy -- these tests are about the GATE, not about the
loop's keep/revert physics (``tests/router/test_placement_delta_feedback.py``
owns that).
"""

from __future__ import annotations

import argparse

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.core import Autorouter
from kicad_tools.router.region_graph import RegionGraph


@pytest.fixture
def infeasible_plan(monkeypatch):
    """Make every plan report overflow, whatever the board really looks like."""
    monkeypatch.setattr(RegionGraph, "get_total_overflow", lambda self: 7)


@pytest.fixture
def always_failed_nets(monkeypatch):
    """Keep one net unrouted so the loop's precondition is satisfied.

    Patches BOTH reach reporters: ``get_failed_nets`` is what the hook's own
    precondition reads, while ``get_statistics`` is what the escalation loop
    scores an attempt by -- a run that "succeeded" never reaches the stalled
    tail at all (``successful_result is not None``), which is exactly the
    escalation-path shape ``--auto-layers`` gives every default invocation.
    """
    real_stats = Autorouter.get_statistics

    def _stalled_stats(self, *a, **kw):
        stats = dict(real_stats(self, *a, **kw))
        stats["nets_routed"] = 0
        return stats

    monkeypatch.setattr(Autorouter, "get_failed_nets", lambda self: [1])
    monkeypatch.setattr(Autorouter, "get_statistics", _stalled_stats)


@pytest.fixture
def delta_loop_spy(monkeypatch):
    """Replace the feedback loop with a call recorder."""
    calls: list[dict] = []

    def _spy(*, router, pcb_path, output_path, args, quiet):
        calls.append({"pcb_path": pcb_path, "output_path": output_path})
        return None

    monkeypatch.setattr(route_cmd, "_run_placement_delta_feedback", _spy)
    return calls


def _argv(pcb, out, *extra: str) -> list[str]:
    # ``--max-layers 2`` keeps a stalled run to a single escalation attempt.
    return [str(pcb), "-o", str(out), "--skip-drc", "--max-layers", "2", *extra]


class TestAutoGate:
    def test_infeasible_plan_enables_the_loop_by_default(
        self,
        routing_test_pcb,
        tmp_path,
        capsys,
        infeasible_plan,
        always_failed_nets,
        delta_loop_spy,
    ):
        route_cmd.main(_argv(routing_test_pcb, tmp_path / "out.kicad_pcb"))
        assert len(delta_loop_spy) == 1, "an infeasible plan must enable the loop by default"
        out = capsys.readouterr().out
        assert "INFEASIBLE board" in out
        # The run says how to opt out, not just that it turned something on.
        assert "--no-placement-delta-feedback" in out

    def test_feasible_plan_leaves_the_default_path_alone(
        self, routing_test_pcb, tmp_path, capsys, always_failed_nets, delta_loop_spy
    ):
        route_cmd.main(_argv(routing_test_pcb, tmp_path / "out.kicad_pcb"))
        assert delta_loop_spy == [], "a feasible plan must not enable the loop"
        assert "INFEASIBLE board" not in capsys.readouterr().out

    def test_no_routing_plan_means_no_verdict_and_no_loop(
        self, routing_test_pcb, tmp_path, infeasible_plan, always_failed_nets, delta_loop_spy
    ):
        route_cmd.main(_argv(routing_test_pcb, tmp_path / "out.kicad_pcb", "--no-routing-plan"))
        assert delta_loop_spy == [], "no plan verdict must not enable the loop"

    def test_explicit_opt_out_beats_an_infeasible_plan(
        self, routing_test_pcb, tmp_path, infeasible_plan, always_failed_nets, delta_loop_spy
    ):
        route_cmd.main(
            _argv(
                routing_test_pcb,
                tmp_path / "out.kicad_pcb",
                "--no-placement-delta-feedback",
            )
        )
        assert delta_loop_spy == []

    def test_explicit_opt_in_still_runs_on_a_feasible_plan(
        self, routing_test_pcb, tmp_path, always_failed_nets, delta_loop_spy
    ):
        route_cmd.main(
            _argv(routing_test_pcb, tmp_path / "out.kicad_pcb", "--placement-delta-feedback")
        )
        assert len(delta_loop_spy) == 1, "the explicit flag keeps its pre-#5890 meaning"

    def test_no_loop_when_every_net_routed_even_if_plan_is_infeasible(
        self, routing_test_pcb, tmp_path, infeasible_plan, delta_loop_spy
    ):
        """The reach precondition is unchanged: nothing to fix, nothing to move.

        This is the state the in-repo acceptance board, board-05, is in.
        Re-measured 2026-10-02 on ``boards/05-bldc-motor-controller/output``:

        * ``kct route output/bldc_controller.kicad_pcb --plan-gate --skip-drc``
          -> ``Routing plan: overflow 10 on 8 edge(s) -- NOT feasible``, with
          every overflowed corridor naming U2 and a ``move U2 +/-2.0mm`` relief;
        * ``kct net-status output/bldc_controller_routed.kicad_pcb`` ->
          ``All nets are fully connected!`` (the ISENSE Kelvin cluster included).

        So board-05's access witness says no move is required, and the gate must
        agree: an infeasible plan alone is never enough to start moving parts on
        a board that already closed.
        """
        route_cmd.main(_argv(routing_test_pcb, tmp_path / "out.kicad_pcb"))
        assert delta_loop_spy == []


class TestRecordedDeltaArtifact:
    """The real loop -- not a spy -- runs and records when the gate fires.

    Without this, every assertion above would be satisfied by a gate that
    enables a loop which then never writes the reviewable delta Epic #5511
    Phase 3 requires ("never a silent artifact edit").
    """

    def test_auto_run_writes_the_placement_delta_json(
        self, routing_test_pcb, tmp_path, infeasible_plan, always_failed_nets
    ):
        out = tmp_path / "out.kicad_pcb"
        route_cmd.main(_argv(routing_test_pcb, out))
        delta_json = out.with_name(out.stem + "_placement_delta.json")
        assert delta_json.exists(), (
            "an auto-enabled run must leave the reviewable delta artifact behind, "
            "whether or not any delta survived the keep-if-improves guard"
        )
        import json

        payload = json.loads(delta_json.read_text())
        # The artifact is a record, not a diff of the board: it names what was
        # applied and what was only proposed.
        assert "applied" in payload and "proposed" in payload

    def test_no_artifact_when_the_plan_is_feasible(
        self, routing_test_pcb, tmp_path, always_failed_nets
    ):
        out = tmp_path / "out.kicad_pcb"
        route_cmd.main(_argv(routing_test_pcb, out))
        assert not out.with_name(out.stem + "_placement_delta.json").exists()


class TestTriStateFlag:
    def test_absence_of_both_flags_is_none_not_false(self):
        from kicad_tools.cli.parser import create_parser

        args = create_parser().parse_args(["route", "board.kicad_pcb"])
        assert args.placement_delta_feedback is None, (
            "the default must be the AUTO sentinel; False would mean "
            "'explicitly off' and defeat the #5890 gate"
        )

    def test_explicit_flags_resolve_to_true_and_false(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        on = parser.parse_args(["route", "b.kicad_pcb", "--placement-delta-feedback"])
        off = parser.parse_args(["route", "b.kicad_pcb", "--no-placement-delta-feedback"])
        assert on.placement_delta_feedback is True
        assert off.placement_delta_feedback is False


class TestDecisionHelper:
    """Unit-level coverage of the resolver, independent of any board."""

    class _Report:
        feasible = False
        total_overflow = 4
        overflowed_edges = 2

    class _FeasibleReport:
        feasible = True
        total_overflow = 0
        overflowed_edges = 0

    class _Plan:
        def __init__(self, report):
            self.overflow_report = report

    class _Router:
        def __init__(self, plan):
            self.routing_plan = plan

    def _args(self, choice):
        return argparse.Namespace(placement_delta_feedback=choice)

    def test_auto_follows_the_plan_verdict(self):
        infeasible = self._Router(self._Plan(self._Report()))
        feasible = self._Router(self._Plan(self._FeasibleReport()))
        no_report = self._Router(self._Plan(None))
        no_plan = self._Router(None)

        assert (
            route_cmd._should_run_placement_delta_feedback(infeasible, self._args(None), quiet=True)
            is True
        )
        for router in (feasible, no_report, no_plan):
            assert (
                route_cmd._should_run_placement_delta_feedback(router, self._args(None), quiet=True)
                is False
            )

    def test_explicit_choices_ignore_the_plan(self):
        infeasible = self._Router(self._Plan(self._Report()))
        feasible = self._Router(self._Plan(self._FeasibleReport()))
        assert (
            route_cmd._should_run_placement_delta_feedback(
                infeasible, self._args(False), quiet=True
            )
            is False
        )
        assert (
            route_cmd._should_run_placement_delta_feedback(feasible, self._args(True), quiet=True)
            is True
        )

    def test_missing_attribute_namespace_defaults_to_auto(self):
        """Library callers / older namespaces must not crash the resolver."""
        feasible = self._Router(self._Plan(self._FeasibleReport()))
        assert (
            route_cmd._should_run_placement_delta_feedback(
                feasible, argparse.Namespace(), quiet=True
            )
            is False
        )


class TestSubcommandForwarding:
    """``kct route``'s wrapper forwards an EXPLICIT choice, and only an explicit one.

    The wrapper (``cli.commands.routing.run_route_command``) re-serializes its
    namespace into an argv for ``route_cmd.main``.  Before #5890 it forwarded
    on truthiness, which would collapse the new AUTO sentinel into "off" --
    turning the whole gate off for every ``kct route`` invocation.
    """

    @staticmethod
    def _forwarded_argv(monkeypatch, tmp_path, *extra: str) -> list[str]:
        from kicad_tools.cli import route_cmd as route_cmd_module
        from kicad_tools.cli.commands.routing import run_route_command
        from kicad_tools.cli.parser import create_parser

        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        args = create_parser().parse_args(["route", str(pcb), *extra])

        captured: list[list[str]] = []
        monkeypatch.setattr(route_cmd_module, "main", lambda argv: captured.append(argv) or 0)
        run_route_command(args)
        assert captured, "the wrapper never reached route_cmd.main"
        return captured[0]

    def test_auto_forwards_neither_flag(self, monkeypatch, tmp_path):
        argv = self._forwarded_argv(monkeypatch, tmp_path)
        assert "--placement-delta-feedback" not in argv
        assert "--no-placement-delta-feedback" not in argv

    def test_explicit_on_is_forwarded(self, monkeypatch, tmp_path):
        argv = self._forwarded_argv(monkeypatch, tmp_path, "--placement-delta-feedback")
        assert "--placement-delta-feedback" in argv

    def test_explicit_off_is_forwarded(self, monkeypatch, tmp_path):
        argv = self._forwarded_argv(monkeypatch, tmp_path, "--no-placement-delta-feedback")
        assert "--no-placement-delta-feedback" in argv
