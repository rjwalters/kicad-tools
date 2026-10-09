"""Finishing stages fit inside the hard ``--timeout`` (Issue #6273).

``kct route --timeout`` is a HARD total: the supervisor in
:mod:`kicad_tools.cli.route_deadline` kills the worker when it fires and
quarantines the board as ``*_timeout_unverified_*``.  Board 05's legacy
recipe (``--timeout 900``) never produced an accepted board because routing
ran to the very end of the budget and the stages after it -- optimize, save,
zone fill, the KiCad oracle completion loop, the post-route DRC -- were
killed.  These tests pin the pieces of the fix that live outside the routing
loop (the reserve itself is pinned in ``test_route_cmd_total_timeout.py``,
the two-phase best-state comparator in
``test_negotiated_timeout_propagation.py``):

* an escalation attempt's slice is charged for its own setup time;
* the pre-save optimizer yields when routing overran into the reserve;
* the oracle loop never starts a kicad-cli call it cannot finish;
* the post-route DRC writes its sidecars but skips a verification it cannot
  finish.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router.algorithms.two_phase import routed_net_count, two_phase_state_key
from kicad_tools.router.oracle_completion import (
    STOP_CONVERGED,
    STOP_DEADLINE,
    ClosureAttempt,
    run_oracle_completion,
)

# =============================================================================
# Two-phase best-state key
# =============================================================================


class TestTwoPhaseStateKey:
    def test_lost_copper_never_wins_on_overflow(self):
        """The board-05 case: 36 nets at overflow 45 beat 27 nets at overflow 2."""
        complete = two_phase_state_key(0, 36, 45)
        gutted = two_phase_state_key(0, 27, 2)
        assert complete < gutted

    def test_clearance_still_ranks_first(self):
        """#3002: a DRC-clean snapshot still beats a dirty one with more nets."""
        assert two_phase_state_key(0, 30, 10) < two_phase_state_key(1, 36, 0)

    def test_overflow_breaks_ties(self):
        assert two_phase_state_key(0, 36, 3) < two_phase_state_key(0, 36, 4)

    def test_ripped_nets_do_not_count_as_routed(self):
        # rip_up_nets leaves an EMPTY list behind; a failed re-route never
        # refills it; a never-routed net has no key at all.
        net_routes = {1: ["copper"], 2: [], 3: ["copper"]}
        assert routed_net_count([1, 2, 3, 4], net_routes) == 2


# =============================================================================
# Attempt setup is charged to the attempt's slice
# =============================================================================


class TestChargeAttemptSetup:
    def test_unbounded_stays_unbounded(self):
        assert route_cmd._charge_attempt_setup(None, time.monotonic() - 50.0) is None

    def test_setup_time_comes_off_the_slice(self):
        charged = route_cmd._charge_attempt_setup(300.0, time.monotonic() - 40.0)
        assert charged == pytest.approx(260.0, abs=1.0)

    def test_never_negative(self):
        assert route_cmd._charge_attempt_setup(10.0, time.monotonic() - 40.0) == 0.0


# =============================================================================
# The pre-save optimizer yields to save / fill / DRC
# =============================================================================


def _stamped(timeout: float, elapsed: float) -> SimpleNamespace:
    args = SimpleNamespace(timeout=timeout, auto_fix=False, dry_run=False, skip_drc=False)
    route_cmd._set_wall_clock_deadline(args)
    args._wall_clock_deadline -= elapsed
    args._routing_deadline -= elapsed
    return args


class TestOptimizationBudget:
    def test_runs_without_a_timeout(self):
        args = SimpleNamespace(timeout=None)
        route_cmd._set_wall_clock_deadline(args)
        assert route_cmd._optimization_fits_budget(args, quiet=True)

    def test_runs_when_routing_stopped_inside_its_budget(self):
        # 900 s run, reserve 135 s; routing stopped on time at 765 s.
        assert route_cmd._optimization_fits_budget(_stamped(900.0, 765.0), quiet=True)

    def test_skipped_when_routing_overran_into_the_reserve(self, capsys):
        # Routing ran to 820 s: 80 s left, under 0.75 x 135 = ~101 s.
        args = _stamped(900.0, 820.0)
        assert not route_cmd._optimization_fits_budget(args, quiet=False)
        assert "Optimizing traces: skipped" in capsys.readouterr().out


# =============================================================================
# The oracle loop never starts a kicad-cli call it cannot finish
# =============================================================================


def _geo(links: int) -> GeometricDRCResult:
    return GeometricDRCResult(
        ran=True,
        error_count=0,
        unconnected_items=[
            DRCViolation(
                type=ViolationType.UNCONNECTED_ITEMS,
                type_str="unconnected_items",
                severity=Severity.ERROR,
                message="Missing connection between items",
                locations=[Location(10.0 + i, 5.0), Location(20.0 + i, 5.0)],
                items=[f"Pad {i + 1} [GND] of C{i + 1} on F.Cu", "Zone [GND] on In1.Cu"],
            )
            for i in range(links)
        ],
    )


class _Board:
    def __init__(self, tmp_path: Path, links: int):
        self.path = tmp_path / "b.kicad_pcb"
        self.path.write_text(str(links))
        self.oracle_calls = 0
        self.closer_calls = 0

    def oracle(self, _path):
        self.oracle_calls += 1
        return _geo(int(self.path.read_text()))

    def closer(self, path, links, banned):
        self.closer_calls += 1
        self.path.write_text(str(max(0, int(self.path.read_text()) - 1)))
        return ClosureAttempt(applied=1)


class TestOracleDeadline:
    def test_unbounded_loop_is_unchanged(self, tmp_path):
        board = _Board(tmp_path, 2)
        result = run_oracle_completion(
            board.path, oracle=board.oracle, closer=board.closer, nets={"GND"}, max_rounds=3
        )
        assert result.stop_reason == STOP_CONVERGED

    def test_no_drc_started_when_the_estimate_does_not_fit(self, tmp_path):
        board = _Board(tmp_path, 2)
        result = run_oracle_completion(
            board.path,
            oracle=board.oracle,
            closer=board.closer,
            nets={"GND"},
            time_left=lambda: 30.0,
            call_cost_estimate=40.0,  # e.g. the measured zone-fill time
        )
        assert (result.ran, result.stop_reason) == (False, STOP_DEADLINE)
        assert board.oracle_calls == 0
        assert board.path.read_text() == "2"

    def test_round_not_started_when_an_attempt_does_not_fit(self, tmp_path):
        """One DRC fits, a closer + DRC attempt (~2 DRCs) does not."""
        board = _Board(tmp_path, 2)
        result = run_oracle_completion(
            board.path,
            oracle=board.oracle,
            closer=board.closer,
            nets={"GND"},
            time_left=lambda: 60.0,
            call_cost_estimate=40.0,
        )
        assert result.ran
        assert result.stop_reason == STOP_DEADLINE
        assert board.oracle_calls == 1 and board.closer_calls == 0
        # The board is exactly as the loop found it.
        assert board.path.read_text() == "2"
        assert result.final_links == 2

    def test_stops_between_rounds_when_time_runs_out(self, tmp_path):
        board = _Board(tmp_path, 3)
        budget = iter([100.0, 100.0, 0.0, 0.0])  # initial DRC, round 1, round 2 ...
        result = run_oracle_completion(
            board.path,
            oracle=board.oracle,
            closer=board.closer,
            nets={"GND"},
            max_rounds=3,
            time_left=lambda: next(budget),
            call_cost_estimate=1.0,
        )
        assert result.stop_reason == STOP_DEADLINE
        # Round 1 was kept; round 2 never started.
        assert [r.kept for r in result.rounds] == [True, False]
        assert board.closer_calls == 1
        assert board.path.read_text() == "2"


# =============================================================================
# The post-route DRC writes its sidecars but skips an unaffordable verification
# =============================================================================


class TestPostRouteDrcBudget:
    def _forbid_verification(self, monkeypatch):
        import kicad_tools.drc as drc_mod
        import kicad_tools.validate as validate_mod

        def _boom(*_a, **_k):
            raise AssertionError("DRC verification must not start")

        monkeypatch.setattr(validate_mod, "DRCChecker", _boom)
        monkeypatch.setattr(drc_mod, "run_geometric_drc", _boom)

    def test_skipped_when_time_left_is_below_the_estimate(self, monkeypatch, tmp_path, capsys):
        self._forbid_verification(monkeypatch)
        written: list[str] = []
        monkeypatch.setattr(
            route_cmd,
            "_write_drc_constraint_sidecars",
            lambda *a, **k: written.append("dru"),
        )
        errors, warnings = route_cmd.run_post_route_drc(
            output_path=tmp_path / "b.kicad_pcb",
            manufacturer="jlcpcb",
            layers=4,
            time_left=20.0,
            drc_cost_estimate=40.0,
        )
        assert (errors, warnings) == (0, 0)
        # The .kicad_pro / .kicad_dru sidecars still ship with the board.
        assert written == ["dru"]
        out = capsys.readouterr().out
        assert "SKIPPED" in out and "UNVERIFIED" in out

    def test_witness_replay_skipped_without_time_for_it(self, monkeypatch, tmp_path):
        """The witness replay (a diagnostic) yields too; the journal still ships."""
        self._forbid_verification(monkeypatch)
        monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", lambda *a, **k: None)
        seen: list[bool] = []
        monkeypatch.setattr(
            route_cmd,
            "_write_access_witness_sidecar",
            lambda *a, replay=True, **k: seen.append(replay),
        )
        route_cmd.run_post_route_drc(
            output_path=tmp_path / "b.kicad_pcb",
            manufacturer="jlcpcb",
            layers=4,
            time_left=50.0,  # one DRC (1.5 x 40 = 60 s) does not even fit
            drc_cost_estimate=40.0,
        )
        assert seen == [False]

    def test_witness_replay_runs_when_time_allows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", lambda *a, **k: None)
        seen: list[bool] = []
        monkeypatch.setattr(
            route_cmd,
            "_write_access_witness_sidecar",
            lambda *a, replay=True, **k: seen.append(replay),
        )

        def _stop(*_a, **_k):
            raise RuntimeError("stop after the sidecars")

        import kicad_tools.schema.pcb as pcb_mod

        monkeypatch.setattr(pcb_mod.PCB, "load", classmethod(lambda cls, p: _stop()))
        with contextlib.suppress(RuntimeError):
            route_cmd.run_post_route_drc(
                output_path=tmp_path / "b.kicad_pcb",
                manufacturer="jlcpcb",
                layers=4,
                time_left=500.0,
                drc_cost_estimate=40.0,
            )
        assert seen == [True]

    def test_skipped_when_nothing_is_left(self, monkeypatch, tmp_path):
        self._forbid_verification(monkeypatch)
        monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", lambda *a, **k: None)
        assert route_cmd.run_post_route_drc(
            output_path=tmp_path / "b.kicad_pcb",
            manufacturer="jlcpcb",
            layers=2,
            time_left=0.0,
        ) == (0, 0)

    def test_budget_kwargs_follow_the_run(self):
        args = _stamped(900.0, 100.0)
        args._zone_fill_seconds = 37.5
        kwargs = route_cmd._post_route_drc_budget(args)
        assert kwargs["drc_cost_estimate"] == 37.5
        # 800 s left of the hard total, minus the 10 s exit margin.
        assert kwargs["time_left"] == pytest.approx(790.0, abs=1.0)

    def test_unbounded_run_is_not_gated(self):
        args = SimpleNamespace(timeout=None)
        route_cmd._set_wall_clock_deadline(args)
        assert route_cmd._post_route_drc_budget(args) == {
            "time_left": None,
            "drc_cost_estimate": 0.0,
        }
