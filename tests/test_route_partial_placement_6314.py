"""A partial save during placement-delta feedback must be self-consistent (#6314).

``kct route`` saves ``<output>_partial.kicad_pcb`` when its deadline fires or
the user presses Ctrl+C.  The board is built from a placement text plus the
copper the router holds.  While the placement-delta feedback loop is probing a
trial, the router holds copper routed against a MOVED footprint (and only the
part of it the re-route reached), while the placement text was the original
board: copper to nowhere.

These tests interrupt a real ``PlacementDeltaFeedbackLoop`` mid-trial, driven
through the real ``_run_placement_delta_feedback`` and the real
``Autorouter.route_with_placement_delta_feedback``.  Only the router is a
stub; its copper is frozen at the pad coordinates it was routed against, the
way real copper is, so a save that pairs it with the wrong placement leaves
visibly stranded pads.
"""

from __future__ import annotations

import json
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router import PlacementDelta, PlacementDeltaFeedbackLoop
from kicad_tools.router.core import Autorouter
from kicad_tools.schema.pcb import PCB
from tests.router.test_placement_delta_feedback import FakePad, FakeRouter

# Four two-pad parts.  Nets A and B join R1 to R2; nets C and D join R3 to R4.
_PARTS = {"R1": (10.0, 10.0), "R2": (30.0, 10.0), "R3": (10.0, 30.0), "R4": (30.0, 30.0)}
_PINS = {"1": -0.5, "2": 0.5}
_NETS = {1: "A", 2: "B", 3: "C", 4: "D"}
_NET_OF = {("R1", "1"): 1, ("R2", "1"): 1, ("R1", "2"): 2, ("R2", "2"): 2}
_NET_OF.update({("R3", "1"): 3, ("R4", "1"): 3, ("R3", "2"): 4, ("R4", "2"): 4})
_MOVE = 2.0


def _board_text() -> str:
    footprints = ""
    for ref, (x, y) in _PARTS.items():
        pads = "".join(
            f'    (pad "{pin}" smd rect (at {dx} 0) (size 0.6 0.6) (layers "F.Cu") '
            f'(net {_NET_OF[(ref, pin)]} "{_NETS[_NET_OF[(ref, pin)]]}"))\n'
            for pin, dx in _PINS.items()
        )
        footprints += (
            f'  (footprint "R_0402" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}")\n{pads}  )\n'
        )
    nets = "".join(f'  (net {num} "{name}")\n' for num, name in _NETS.items())
    return (
        '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers\n    (0 "F.Cu" signal)\n    (44 "Edge.Cuts" user)\n  )\n'
        '  (gr_rect (start 0 0) (end 60 40) (layer "Edge.Cuts") (width 0.1))\n'
        '  (net 0 "")\n' + nets + footprints + ")\n"
    )


@dataclass
class _Trace:
    """One net's copper, frozen at the pad coordinates it was routed against."""

    net: int
    x1: float
    y1: float
    x2: float
    y2: float

    def to_sexp(self, name_only: bool = False) -> str:
        return (
            f"(segment (start {self.x1} {self.y1}) (end {self.x2} {self.y2}) "
            f'(width 0.2) (layer "F.Cu") (net {self.net}))'
        )


class _Deadline:
    """Interrupt the Nth routing call the way the SIGTERM handler does.

    ``rerouted`` is how many nets the trial's re-route had reached: the loop
    clears every route before it re-routes, so 0 is a deadline that lands
    before the first net is back.
    """

    def __init__(self, on_call: int, rerouted: int = 1):
        self.on_call = on_call
        self.rerouted = rerouted

    def __call__(self) -> None:
        raise route_deadline.RouteDeadlineExpired()


class _CtrlC(_Deadline):
    """Interrupt the Nth routing call the way Ctrl+C does: save in the handler."""

    def __call__(self) -> None:
        route_cmd._handle_interrupt(signal.SIGINT, None)


class _Crash(_Deadline):
    """An ordinary failure inside the re-route; the run carries on after it."""

    def __call__(self) -> None:
        raise RuntimeError("router exploded")


class _CopperRouter(FakeRouter):
    """A router whose copper stays where the pads were when it was routed.

    Net B fails until R2 has moved; net D fails until R4 has moved.  That makes
    the R2 translate a KEPT delta and gives the loop a second trial to run.
    """

    def __init__(self, interrupt: _Deadline | None = None, r2_move_helps: bool = True):
        pads = [
            FakePad(x + dx, y, ref, pin, net=_NET_OF[(ref, pin)])
            for ref, (x, y) in _PARTS.items()
            for pin, dx in _PINS.items()
        ]
        super().__init__(pads, len(_NETS), self._failing)
        self.nets = {0: []}
        for key, net in _NET_OF.items():
            self.nets.setdefault(net, []).append(key)
        self.interrupt = interrupt
        self.r2_move_helps = r2_move_helps
        self.routes = self._route_everything_reachable()

    @staticmethod
    def _failing(router: _CopperRouter) -> list[int]:
        failed = []
        if not (router.r2_move_helps and router.pads[("R2", "1")].x > 29.6):
            failed.append(2)
        if router.pads[("R4", "1")].x < 29.6:
            failed.append(4)
        return failed

    def _route_everything_reachable(self) -> list[_Trace]:
        failed = set(self.get_failed_nets())
        routes = []
        for net, keys in self.nets.items():
            if net == 0 or net in failed:
                continue
            a, b = (self.pads[key] for key in keys)
            routes.append(_Trace(net, a.x, a.y, b.x, b.y))
        return routes

    def route_all_negotiated(self, **kwargs):
        self.route_calls += 1
        routes = self._route_everything_reachable()
        if self.interrupt is not None and self.route_calls == self.interrupt.on_call:
            # A deadline lands part-way through the re-route: the loop cleared
            # every route first, and only some nets have been re-routed.
            self.routes = routes[: self.interrupt.rerouted]
            self.interrupt()
        self.routes = routes
        return self.routes

    def to_sexp(self, *, skip_cleanup: bool = False, name_only: bool = False) -> str:
        return "\n\t".join(route.to_sexp(name_only=name_only) for route in self.routes)

    def get_statistics(self, nets_to_route_ids=None) -> dict:
        return {"nets_routed": len(self.routes), "segments": len(self.routes), "vias": 0}

    def route_with_placement_delta_feedback(self, **kwargs):
        # The REAL Autorouter entry point (it only needs ``self`` as the loop's
        # router); the test supplies the proposals the classifier would make.
        return Autorouter.route_with_placement_delta_feedback(
            self, delta_proposer=lambda _pcb: _deltas(), **kwargs
        )


def _deltas() -> list[PlacementDelta]:
    return [
        PlacementDelta(net_name="B", target_ref="R2", kind="translate", dx=_MOVE, dy=0.0),
        PlacementDelta(net_name="D", target_ref="R4", kind="translate", dx=_MOVE, dy=0.0),
    ]


def _args(output: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output=str(output),
        placement_delta_feedback=True,
        placement_delta_feedback_budget=3,
        placement_feedback_max_movement=5.0,
        placement_feedback_anchor=None,
        placement_feedback_no_anchor=None,
        strategy="negotiated",
        timeout=None,
        per_net_timeout=None,
        skip_nets=None,
        verbose=False,
        quiet=True,
    )


@pytest.fixture
def board(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "board.kicad_pcb"
    source.write_text(_board_text())
    return source, tmp_path / "board_routed.kicad_pcb"


@pytest.fixture(autouse=True)
def _clean_interrupt_state():
    """``_interrupt_state`` is process-global; never leak it between tests."""
    saved = dict(route_cmd._interrupt_state)
    yield
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)


def _arm(router, source: Path, output: Path) -> None:
    """What every route flow does before routing starts."""
    route_cmd._reset_partial_save_state()
    route_cmd._interrupt_state.update(
        router=router,
        output_path=output,
        pcb_path=source,
        quiet=True,
        interrupted=False,
        best_completed_attempt=False,
    )


def _run_loop(router, source: Path, output: Path):
    _arm(router, source, output)
    return route_cmd._run_placement_delta_feedback(
        router=router, pcb_path=source, output_path=output, args=_args(output), quiet=True
    )


def _interrupted_run(monkeypatch, router, source: Path, output: Path) -> int:
    """Drive the loop under the real deadline handler in ``_in_process_main_impl``."""

    def work(argv):
        _run_loop(router, source, output)
        raise AssertionError("the trial must not complete")

    monkeypatch.setattr(route_cmd, "_main_impl", work)
    return route_cmd._in_process_main_impl([])


def _positions(board_path: Path) -> dict[str, tuple[float, float]]:
    return {
        fp.reference: (fp.position[0], fp.position[1]) for fp in PCB.load(board_path).footprints
    }


def _routed_nets_with_copper_on_every_pad(board_path: Path) -> set[str]:
    """Net names carrying copper; fails if any pad of one has no trace end on it.

    This is the property the issue's render made visible: a routed net's trace
    has to END ON its pads, in the saved file's own footprint positions.
    """
    pcb = PCB.load(board_path)
    ends: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for segment in pcb.segments:
        ends[segment.net_number] += [segment.start, segment.end]
    stranded = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number not in ends:
                continue
            x, y = fp.position[0] + pad.position[0], fp.position[1] + pad.position[1]
            if not any(
                abs(x - ex) < 0.01 and abs(y - ey) < 0.01 for ex, ey in ends[pad.net_number]
            ):
                stranded.append(f"{fp.reference}.{pad.number} ({pad.net_name}) at ({x}, {y})")
    assert not stranded, f"routed pads with no same-net copper on them: {stranded}"
    return {_NETS[net] for net in ends}


def _partial(output: Path) -> Path:
    return output.with_stem(output.stem + "_partial")


# --------------------------------------------------------------------------- #
# Loop level                                                                   #
# --------------------------------------------------------------------------- #


class TestLoopRestore:
    def _loop(self, router, tmp_path: Path):
        source = tmp_path / "board.kicad_pcb"
        source.write_text(_board_text())
        pcb = PCB.load(source)
        loop = PlacementDeltaFeedbackLoop(
            router=router, pcb=pcb, verbose=False, delta_proposer=lambda _pcb: _deltas()
        )
        return loop, pcb

    def test_restore_undoes_a_trial_interrupted_mid_route(self, tmp_path: Path):
        router = _CopperRouter(interrupt=_Deadline(on_call=1))
        baseline = list(router.routes)
        loop, pcb = self._loop(router, tmp_path)

        with pytest.raises(route_deadline.RouteDeadlineExpired):
            loop.run_delta(reuse_existing_routes=True)

        # The interrupt left the trial's state live: moved pad, one route.
        assert router.pads[("R2", "1")].x == pytest.approx(29.5 + _MOVE)
        assert len(router.routes) == 1
        assert loop.trial_in_flight is not None
        assert loop.trial_in_flight.target_ref == "R2"

        assert loop.restore_last_accepted() is True

        assert router.routes == baseline
        assert router.pads[("R2", "1")].x == pytest.approx(29.5)
        assert next(fp for fp in pcb.footprints if fp.reference == "R2").position[0] == 30.0
        assert loop.trial_in_flight is None
        assert loop.accepted_deltas == []
        # Nothing in flight any more: a second call must not undo anything.
        assert loop.restore_last_accepted() is False

    def test_restore_keeps_the_delta_accepted_before_the_interrupted_trial(self, tmp_path: Path):
        router = _CopperRouter(interrupt=_Deadline(on_call=2))
        loop, pcb = self._loop(router, tmp_path)

        with pytest.raises(route_deadline.RouteDeadlineExpired):
            loop.run_delta(reuse_existing_routes=True)
        assert loop.restore_last_accepted() is True

        assert [d.target_ref for d in loop.accepted_deltas] == ["R2"]
        positions = {fp.reference: fp.position[0] for fp in pcb.footprints}
        assert positions["R2"] == 30.0 + _MOVE, "the kept move stays"
        assert positions["R4"] == 30.0, "the interrupted trial's move is undone"
        assert sorted(route.net for route in router.routes) == [1, 2, 3]
        # The accepted copper was routed against the moved R2.
        net_b = next(route for route in router.routes if route.net == 2)
        assert net_b.x2 == pytest.approx(30.5 + _MOVE)

    def test_nothing_is_in_flight_after_a_completed_run(self, tmp_path: Path):
        router = _CopperRouter()
        loop, _pcb = self._loop(router, tmp_path)
        result = loop.run_delta(reuse_existing_routes=True)

        assert [d.target_ref for d in result.applied_deltas] == ["R2", "R4"]
        assert loop.trial_in_flight is None
        assert loop.restore_last_accepted() is False

    def test_a_reverted_trial_is_not_left_in_flight(self, tmp_path: Path):
        router = _CopperRouter(r2_move_helps=False)
        loop, _pcb = self._loop(router, tmp_path)
        loop.run_delta(reuse_existing_routes=True, max_adjustments=1)

        assert loop.trial_in_flight is None
        assert loop.accepted_deltas == []


# --------------------------------------------------------------------------- #
# CLI level: the deadline path                                                 #
# --------------------------------------------------------------------------- #


class TestDeadlineDuringATrial:
    def test_no_kept_delta_saves_the_pre_loop_routes_on_the_original_placement(
        self, monkeypatch, board
    ):
        source, output = board
        router = _CopperRouter(interrupt=_Deadline(on_call=1))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        saved = _partial(output)
        assert saved.exists()
        # The pre-loop routes -- not the one net the trial had re-routed.
        assert _routed_nets_with_copper_on_every_pad(saved) == {"A", "C"}
        assert _positions(saved) == _PARTS
        assert not output.exists(), "nothing may be staged at the final output path"

    def test_kept_delta_then_deadline_saves_accepted_copper_on_the_moved_placement(
        self, monkeypatch, board
    ):
        source, output = board
        router = _CopperRouter(interrupt=_Deadline(on_call=2))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        saved = _partial(output)
        assert _routed_nets_with_copper_on_every_pad(saved) == {"A", "B", "C"}
        positions = _positions(saved)
        assert positions["R2"] == (30.0 + _MOVE, 10.0), "the kept move must be in the file"
        assert positions["R4"] == _PARTS["R4"], "the interrupted trial's move must not be"
        assert not output.exists(), "the moved placement must not be staged at <output>"
        assert _positions(source) == _PARTS, "the input board is never rewritten"

    def test_a_deadline_before_the_trial_re_routes_anything_still_saves(self, monkeypatch, board):
        """The trial clears every route first; the accepted ones must not be lost."""
        source, output = board
        router = _CopperRouter(interrupt=_Deadline(on_call=1, rerouted=0))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "C"}
        assert _positions(_partial(output)) == _PARTS

    def test_the_sidecar_says_which_state_was_saved(self, monkeypatch, tmp_path, board):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _CopperRouter(interrupt=_Deadline(on_call=2))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        state = json.loads(control.read_text())
        # Existing keys are untouched ...
        assert state["stage"] == "partial-save"
        assert state["snapshot_saved"] is True
        assert state["snapshot"] == str(_partial(output))
        assert state["output"] == str(output)
        # ... and the new ones say what the snapshot holds.
        assert state["snapshot_state"] == "last-accepted"
        assert state["snapshot_placement"] == "moved"
        assert state["snapshot_discarded_trial"] == "R4 translate"

    def test_the_sidecar_reports_the_source_placement_when_nothing_was_kept(
        self, monkeypatch, tmp_path, board
    ):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _CopperRouter(interrupt=_Deadline(on_call=1))

        _interrupted_run(monkeypatch, router, source, output)

        state = json.loads(control.read_text())
        assert state["snapshot_state"] == "last-accepted"
        assert state["snapshot_placement"] == "source"
        assert state["snapshot_discarded_trial"] == "R2 translate"

    def test_a_deadline_outside_the_loop_reports_the_live_state(self, monkeypatch, tmp_path, board):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _CopperRouter()

        def work(argv):
            _arm(router, source, output)
            raise route_deadline.RouteDeadlineExpired()

        monkeypatch.setattr(route_cmd, "_main_impl", work)
        assert route_cmd._in_process_main_impl([]) == route_deadline.TIMEOUT_EXIT

        state = json.loads(control.read_text())
        assert state["snapshot_saved"] is True
        assert state["snapshot_state"] == "live"
        assert state["snapshot_placement"] == "source"
        assert state["snapshot_discarded_trial"] is None

    def test_a_failed_restore_is_reported_not_hidden(self, monkeypatch, tmp_path, board):
        """If the hook cannot restore, still save -- and say the file may not agree."""
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _CopperRouter(interrupt=_Deadline(on_call=1))

        def boom(self, **kwargs):
            raise RuntimeError("restore exploded")

        monkeypatch.setattr(PlacementDeltaFeedbackLoop, "restore_last_accepted", boom)
        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        state = json.loads(control.read_text())
        assert state["snapshot_saved"] is True
        assert state["snapshot_state"] == "inconsistent"
        assert "restore exploded" in state["snapshot_restore_error"]


def test_the_supervisor_copies_the_new_keys_into_the_timeout_report(tmp_path, board):
    """End to end through the real supervisor: ``<output>.timeout.json``."""
    source, output = board
    repo = Path(__file__).resolve().parents[1]
    script = f"""
import sys
sys.path.insert(0, {str(repo)!r})
from pathlib import Path
from types import SimpleNamespace
from kicad_tools.cli import route_cmd, route_deadline
from tests import test_route_partial_placement_6314 as t
source, output = Path({str(source)!r}), Path({str(output)!r})
def work(argv):
    route_deadline.configure_output(SimpleNamespace(pcb=str(source), output=str(output)))
    route_deadline.record_stage("placement-delta-feedback")
    t._run_loop(t._CopperRouter(interrupt=t._Deadline(on_call=2)), source, output)
    raise AssertionError("the trial must not complete")
route_cmd._main_impl = work
raise SystemExit(route_cmd._in_process_main([]))
"""
    assert (
        route_deadline._supervise([sys.executable, "-c", script], 60, tmp_path / "control.json")
        == route_deadline.TIMEOUT_EXIT
    )

    report = json.loads(output.with_suffix(".timeout.json").read_text())
    assert report["status"] == "partial"
    assert report["snapshot_saved"] is True
    assert report["snapshot"] == str(_partial(output))
    assert "unverified_output" not in report, "nothing was staged at <output> to quarantine"
    assert report["snapshot_state"] == "last-accepted"
    assert report["snapshot_placement"] == "moved"
    assert report["snapshot_discarded_trial"] == "R4 translate"
    assert report["snapshot_restore_seconds"] < route_deadline.SAVE_SECONDS
    saved = Path(report["snapshot"])
    assert _routed_nets_with_copper_on_every_pad(saved) == {"A", "B", "C"}
    assert _positions(saved)["R2"] == (30.0 + _MOVE, 10.0)


# --------------------------------------------------------------------------- #
# CLI level: Ctrl+C saves inside the signal handler, before anything unwinds   #
# --------------------------------------------------------------------------- #


class TestCtrlCDuringATrial:
    @pytest.mark.parametrize(
        ("on_call", "expected_nets", "r2_x"),
        [(1, {"A", "C"}, 30.0), (2, {"A", "B", "C"}, 30.0 + _MOVE)],
        ids=["no-kept-delta", "after-a-kept-delta"],
    )
    def test_the_in_handler_save_is_consistent(self, board, on_call, expected_nets, r2_x):
        source, output = board
        router = _CopperRouter(interrupt=_CtrlC(on_call=on_call))

        with pytest.raises(SystemExit) as exit_info:
            _run_loop(router, source, output)

        assert exit_info.value.code == 5, "exit 5 = interrupted with partial results saved"
        saved = _partial(output)
        assert _routed_nets_with_copper_on_every_pad(saved) == expected_nets
        assert _positions(saved)["R2"] == (r2_x, 10.0)
        assert _positions(saved)["R4"] == _PARTS["R4"]
        assert not output.exists()


# --------------------------------------------------------------------------- #
# After the loop: a later stage's partial save reads the moved placement       #
# --------------------------------------------------------------------------- #


class TestSaveAfterAKeptDelta:
    def test_a_later_stage_save_uses_the_moved_placement(self, board):
        source, output = board
        router = _CopperRouter()

        returned = _run_loop(router, source, output)
        assert returned == output, "the kept delta is still persisted for the normal save path"

        assert route_cmd._save_partial_results() is True
        saved = _partial(output)
        assert _routed_nets_with_copper_on_every_pad(saved) == {"A", "B", "C", "D"}
        positions = _positions(saved)
        assert positions["R2"] == (30.0 + _MOVE, 10.0)
        assert positions["R4"] == (30.0 + _MOVE, 30.0)
        assert route_cmd._interrupt_state["partial_save_info"]["snapshot_placement"] == "moved"
        assert route_cmd._interrupt_state["partial_save_info"]["snapshot_state"] == "live"

    def test_a_later_stage_save_survives_the_final_board_landing_at_output(self, board):
        """The routed board is later written to ``<output>`` itself.

        A save that re-read ``<output>`` as its placement would then insert the
        router's copper into a board that already carries it.
        """
        source, output = board
        router = _CopperRouter()
        _run_loop(router, source, output)
        route_cmd._save_partial_results()
        first = _partial(output).read_text()

        # The normal save path now writes the routed board to <output>.
        output.write_text(first)
        route_cmd._save_partial_results()

        assert len(PCB.load(_partial(output)).segments) == 4

    def test_no_kept_delta_leaves_the_save_on_the_source_board(self, board):
        source, output = board
        router = _CopperRouter(r2_move_helps=False)
        # Only the R2 trial runs, and it reverts.
        _arm(router, source, output)
        args = _args(output)
        args.placement_delta_feedback_budget = 1
        assert (
            route_cmd._run_placement_delta_feedback(
                router=router, pcb_path=source, output_path=output, args=args, quiet=True
            )
            is None
        )

        assert route_cmd._save_partial_results() is True
        assert _positions(_partial(output)) == _PARTS
        assert route_cmd._interrupt_state["partial_save_info"]["snapshot_placement"] == "source"
        assert route_cmd._interrupt_state.get("pre_save_hook") is None, (
            "the hook must not outlive a loop that returned"
        )

    def test_a_new_route_flow_forgets_the_previous_flows_placement(self, board):
        source, output = board
        _run_loop(_CopperRouter(), source, output)
        assert route_cmd._interrupt_state["placement_text"] is not None

        route_cmd._reset_partial_save_state()

        assert route_cmd._interrupt_state["placement_text"] is None
        assert route_cmd._interrupt_state["pre_save_hook"] is None


# --------------------------------------------------------------------------- #
# An ordinary failure inside a trial: the run continues on the accepted state  #
# --------------------------------------------------------------------------- #


class TestLoopFailureMidTrial:
    def test_a_crash_in_the_first_trial_really_keeps_the_initial_routes(self, board):
        source, output = board
        router = _CopperRouter(interrupt=_Crash(on_call=1))
        baseline = list(router.routes)

        assert _run_loop(router, source, output) is None

        assert router.routes == baseline
        assert router.pads[("R2", "1")].x == pytest.approx(29.5), "the trial's pad move is undone"
        assert not output.exists()
        assert route_cmd._interrupt_state["pre_save_hook"] is None
        assert route_cmd._interrupt_state["placement_text"] is None

    def test_a_crash_after_a_kept_delta_keeps_that_delta_and_its_placement(self, board):
        source, output = board
        router = _CopperRouter(interrupt=_Crash(on_call=2))

        returned = _run_loop(router, source, output)

        assert returned == output, "the caller must re-source placement from the moved board"
        assert _positions(output)["R2"] == (30.0 + _MOVE, 10.0)
        assert _positions(output)["R4"] == _PARTS["R4"]
        assert sorted(route.net for route in router.routes) == [1, 2, 3]
        assert router.pads[("R4", "1")].x == pytest.approx(29.5)
        # ... and a later partial save agrees with it.
        assert route_cmd._save_partial_results() is True
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "B", "C"}
        assert _positions(_partial(output))["R2"] == (30.0 + _MOVE, 10.0)


# --------------------------------------------------------------------------- #
# The hook only speaks for the router the save will serialize                  #
# --------------------------------------------------------------------------- #


def test_a_loop_on_another_router_leaves_the_pinned_save_alone(board):
    """An escalation flow can run the loop on a router that is not the pinned one."""
    source, output = board
    pinned = _CopperRouter(r2_move_helps=False)
    _arm(pinned, source, output)

    returned = route_cmd._run_placement_delta_feedback(
        router=_CopperRouter(), pcb_path=source, output_path=output, args=_args(output), quiet=True
    )

    assert returned == output
    assert route_cmd._interrupt_state["placement_text"] is None
    assert route_cmd._save_partial_results() is True
    assert _positions(_partial(output)) == _PARTS, "the pinned router routed the source board"
