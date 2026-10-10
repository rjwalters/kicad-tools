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

import copy
import json
import signal
import sys
import tempfile
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


class _Rewind(_Deadline):
    """Discard the trial from inside its own re-route, then let it carry on.

    The effect of an interrupt whose partial save ran but whose process did
    not stop.  Not hypothetical: ``_handle_interrupt`` saves and then raises
    ``SystemExit``, and Python throws that away when the signal was delivered
    inside a ``__del__`` (#6324).
    """

    loop: PlacementDeltaFeedbackLoop

    def __call__(self) -> None:
        assert self.loop.restore_last_accepted() is True


class _SwallowedCtrlC(_Deadline):
    """Ctrl+C delivered while a destructor runs: saved, but not stopped.

    The real handler, called from a real ``__del__``, so the ``SystemExit`` is
    discarded by the interpreter itself rather than by this test.
    """

    def __call__(self) -> None:
        class _Dying:
            def __del__(self) -> None:
                route_cmd._handle_interrupt(signal.SIGINT, None)

        _Dying()


class _SwallowedCtrlCThenCrash(_SwallowedCtrlC):
    """... and the re-route, resumed over the rewound state, then blows up."""

    def __call__(self) -> None:
        super().__call__()
        raise RuntimeError("router exploded")


class _GridRouter(_CopperRouter):
    """A ``_CopperRouter`` that is like the real router in three more ways.

    * It has a **grid** (the two internals the loop's revert drives,
      ``_reset_for_new_trial`` and ``_mark_route``): the pad positions it was
      last reset from, plus every route marked on it since.
    * A net counts as routed when the router **holds copper for it**, which is
      how ``Autorouter.get_failed_nets`` decides.  ``_CopperRouter`` asks the
      live pad geometry instead, which hides a re-route that finished over
      pads someone moved back: its copper says "routed", its pads say "not".
    * Its re-route does not stop when an interrupt's save returns to it.  It
      either still holds its results and publishes them at the end
      (``in_place=False``) or goes on appending to ``self.routes``, whatever
      list that has become in the meantime (``in_place=True``).
    """

    def __init__(
        self,
        interrupt: _Deadline | None = None,
        r2_move_helps: bool = True,
        in_place: bool = False,
    ):
        self.in_place = in_place
        self.grid: dict = {}
        super().__init__(interrupt, r2_move_helps)
        initial = self.routes
        self._reset_for_new_trial()
        for route in initial:
            self._lay(route)

    def _reset_for_new_trial(self) -> None:
        self.routes = []
        self.grid = {"pads": {key: (pad.x, pad.y) for key, pad in self.pads.items()}, "copper": []}

    def _mark_route(self, route: _Trace) -> None:
        self.grid["copper"].append(route)

    def _lay(self, route: _Trace) -> None:
        self.routes.append(route)
        self._mark_route(route)

    def get_failed_nets(self) -> list[int]:
        return sorted(set(self.nets) - {0} - {route.net for route in self.routes})

    def _route_everything_reachable(self) -> list[_Trace]:
        unreachable = set(self._failing(self))
        routes = []
        for net, keys in self.nets.items():
            if net == 0 or net in unreachable:
                continue
            a, b = (self.pads[key] for key in keys)
            routes.append(_Trace(net, a.x, a.y, b.x, b.y))
        return routes

    def route_all_negotiated(self, **kwargs):
        self.route_calls += 1
        # Routed against the pads as they are NOW (moved, inside a trial).
        routes = self._route_everything_reachable()
        done = 0
        if self.interrupt is not None and self.route_calls == self.interrupt.on_call:
            done = self.interrupt.rerouted
            for route in routes[:done]:
                self._lay(route)
            self.interrupt()
        if self.in_place:
            for route in routes[done:]:
                self._lay(route)
        else:
            for route in routes[done:]:
                self._mark_route(route)
            self.routes = routes
        return self.routes


def _state(router: _GridRouter, pcb: PCB | None = None) -> dict:
    """Everything a trial touches, by value: copper, pads, grid and board."""
    state = {
        "routes": copy.deepcopy(router.routes),
        "pads": {key: (pad.x, pad.y) for key, pad in router.pads.items()},
        "grid": copy.deepcopy(router.grid),
    }
    if pcb is not None:
        # The board as it would be SAVED, re-read: a revert re-serializes
        # ``(at 30.0 10.0)`` as ``(at 30 10)``, so the text itself is no guide.
        with tempfile.TemporaryDirectory() as scratch:
            saved = Path(scratch) / "board.kicad_pcb"
            saved.write_text(route_cmd._pcb_text(pcb))
            state["board"] = {
                fp.reference: (
                    fp.position,
                    fp.layer,
                    [(pad.number, pad.position, pad.net_name) for pad in fp.pads],
                )
                for fp in PCB.load(saved).footprints
            }
    return state


def _assert_grid_matches_the_copper(router: _GridRouter) -> None:
    """The grid is the one a fresh reset + mark of the live routes would give."""
    assert router.grid == {
        "pads": {key: (pad.x, pad.y) for key, pad in router.pads.items()},
        "copper": router.routes,
    }


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
    # Looked up, not called by name: against source that predates the fix the
    # tests must fail on the DEFECT (stranded pads), not on a missing helper.
    reset = getattr(route_cmd, "_reset_partial_save_state", None)
    if reset is not None:
        reset()
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
# Loop level: the trial is discarded, but the loop is still running it         #
# --------------------------------------------------------------------------- #


class _RewoundAtTheCommit(PlacementDeltaFeedbackLoop):
    """Fires ``restore_last_accepted`` in the keep's last unguarded instant.

    That instant is after the delta has been listed as kept and before the
    trial is retired.  Nothing the loop calls runs there, so the test hooks
    the retiring assignment itself, just before it takes effect.
    """

    armed = False
    _slot = None

    @property
    def _trial(self):
        return self._slot

    @_trial.setter
    def _trial(self, value) -> None:
        listed = self.accepted_deltas[-1:] if self._slot is not None else []
        if self.armed and value is None and listed and listed[0] is self._slot.delta:
            self.armed = False
            assert self.restore_last_accepted() is True
        self._slot = value


class TestTrialRewoundUnderTheLoop:
    """``restore_last_accepted`` ran mid-trial and the run did NOT stop.

    The loop must notice at whichever point it resumes, finish the undo
    (including the grid, which the save path skips), keep nothing from the
    discarded trial and start no other.
    """

    @staticmethod
    def _loop(router, tmp_path: Path, cls=PlacementDeltaFeedbackLoop):
        source = tmp_path / "board.kicad_pcb"
        source.write_text(_board_text())
        pcb = PCB.load(source)
        loop = cls(router=router, pcb=pcb, verbose=False, delta_proposer=lambda _pcb: _deltas())
        return loop, pcb

    @staticmethod
    def _assert_stopped_on(result, loop, target: str) -> None:
        assert result.exit_reason == "pd_interrupted"
        assert result.interrupted_delta is not None
        assert result.interrupted_delta.target_ref == target
        assert target not in [d.target_ref for d in result.applied_deltas]
        assert result.reverted_deltas == [], "it was never measured, so it is not 'reverted'"
        assert f"interrupted: {target} translate" in result.summary()
        assert loop.trial_in_flight is None
        assert loop.restore_last_accepted() is False

    @pytest.mark.parametrize(
        "in_place", [False, True], ids=["publishes-at-end", "appends-in-place"]
    )
    @pytest.mark.parametrize("rerouted", [0, 1, 3], ids=["no-net-back", "one-net-back", "all-back"])
    def test_a_trial_rewound_during_its_re_route_is_not_kept(
        self, tmp_path: Path, in_place: bool, rerouted: int
    ):
        """The reviewed defect: the re-route finishes over rewound pads and is KEPT."""
        interrupt = _Rewind(on_call=1, rerouted=rerouted)
        router = _GridRouter(interrupt=interrupt, in_place=in_place)
        loop, pcb = self._loop(router, tmp_path)
        interrupt.loop = loop
        before = _state(router, pcb)

        result = loop.run_delta(reuse_existing_routes=True)

        assert result.applied_deltas == [], "a discarded trial must never be kept"
        assert loop.accepted_deltas == []
        self._assert_stopped_on(result, loop, "R2")
        assert router.route_calls == 1, "no further trial may start after an interrupt"
        # Copper, pads, grid and board are the pre-trial ones again.
        assert _state(router, pcb) == before
        assert router.pads[("R2", "1")].x == pytest.approx(29.5)
        assert next(fp for fp in pcb.footprints if fp.reference == "R2").position[0] == 30.0
        assert sorted(route.net for route in router.routes) == [1, 3]
        _assert_grid_matches_the_copper(router)

    @pytest.mark.parametrize(
        "in_place", [False, True], ids=["publishes-at-end", "appends-in-place"]
    )
    def test_a_rewound_second_trial_leaves_the_first_kept_delta_and_its_copper(
        self, tmp_path: Path, in_place: bool
    ):
        # What the accepted state is once R2 alone has been kept.
        reference = _GridRouter(in_place=in_place)
        reference_loop, reference_pcb = self._loop(reference, tmp_path)
        reference_loop.run_delta(reuse_existing_routes=True, max_adjustments=1)
        accepted = _state(reference, reference_pcb)

        interrupt = _Rewind(on_call=2)
        router = _GridRouter(interrupt=interrupt, in_place=in_place)
        loop, pcb = self._loop(router, tmp_path)
        interrupt.loop = loop

        result = loop.run_delta(reuse_existing_routes=True)

        assert [d.target_ref for d in result.applied_deltas] == ["R2"]
        self._assert_stopped_on(result, loop, "R4")
        assert _state(router, pcb) == accepted
        assert router.pads[("R4", "1")].x == pytest.approx(29.5)
        _assert_grid_matches_the_copper(router)

    @pytest.mark.parametrize("where", ["board", "router-pads"])
    def test_a_rewind_while_the_delta_is_being_applied_is_undone_before_routing(
        self, tmp_path: Path, where: str
    ):
        """The rewind lands first; the application then moves things again."""
        router = _GridRouter()
        loop, pcb = self._loop(router, tmp_path)
        before = _state(router, pcb)

        if where == "board":
            applicator = loop._strategy_applicator
            apply_strategy = applicator.apply_strategy

            def rewound_apply(board, strategy):
                assert loop.restore_last_accepted() is True
                return apply_strategy(board, strategy)

            applicator.apply_strategy = rewound_apply
        else:
            move_pads = loop._apply_delta_to_router_pads

            def rewound_move(delta):
                assert loop.restore_last_accepted() is True
                move_pads(delta)

            loop._apply_delta_to_router_pads = rewound_move

        result = loop.run_delta(reuse_existing_routes=True)

        assert result.applied_deltas == []
        self._assert_stopped_on(result, loop, "R2")
        assert router.route_calls == 0, "a discarded trial is not worth a re-route"
        assert _state(router, pcb) == before
        _assert_grid_matches_the_copper(router)

    def test_a_rewind_during_the_loops_own_revert_stops_the_loop(self, tmp_path: Path):
        """Both undo the same trial at once; the result is still the pre-trial state."""
        router = _GridRouter(r2_move_helps=False)
        loop, pcb = self._loop(router, tmp_path)
        before = _state(router, pcb)
        restore_placement = loop._restore_placement
        fired = []

        def rewound_restore(snapshot):
            if not fired:
                fired.append(True)
                assert loop.restore_last_accepted() is True
            restore_placement(snapshot)

        loop._restore_placement = rewound_restore

        result = loop.run_delta(reuse_existing_routes=True)

        assert fired
        # Without the stop, the R4 trial would run next and be kept.
        assert result.applied_deltas == []
        assert router.route_calls == 1
        # This probe WAS measured before the interrupt, so it is a real revert.
        assert [d.target_ref for d in result.reverted_deltas] == ["R2"]
        assert result.exit_reason == "pd_interrupted"
        assert result.interrupted_delta is None
        assert loop.trial_in_flight is None
        assert _state(router, pcb) == before
        _assert_grid_matches_the_copper(router)

    def test_a_rewind_just_before_the_keep_is_committed_takes_the_delta_back(self, tmp_path: Path):
        router = _GridRouter()
        loop, pcb = self._loop(router, tmp_path, cls=_RewoundAtTheCommit)
        before = _state(router, pcb)
        loop.armed = True

        result = loop.run_delta(reuse_existing_routes=True)

        assert loop.armed is False, "the rewind fired"
        assert result.applied_deltas == []
        assert loop.accepted_deltas == []
        self._assert_stopped_on(result, loop, "R2")
        assert router.route_calls == 1
        assert _state(router, pcb) == before
        _assert_grid_matches_the_copper(router)

    def test_a_rewind_between_trials_changes_nothing(self, tmp_path: Path):
        """Nothing is in flight outside a trial, so there is nothing to notice."""
        router = _GridRouter()
        loop, _pcb = self._loop(router, tmp_path)
        propose = loop._propose_deltas
        seen = []

        def rewound_propose():
            seen.append(loop.restore_last_accepted())
            return propose()

        loop._propose_deltas = rewound_propose

        result = loop.run_delta(reuse_existing_routes=True)

        assert seen and not any(seen)
        assert [d.target_ref for d in result.applied_deltas] == ["R2", "R4"]
        assert result.exit_reason != "pd_interrupted"
        _assert_grid_matches_the_copper(router)

    def test_a_crash_after_the_rewind_still_gets_its_grid_back(self, tmp_path: Path):
        """The save skipped the grid; the loop then died before it could rebuild it."""

        class _RewindThenCrash(_Rewind):
            def __call__(self) -> None:
                super().__call__()
                raise RuntimeError("router exploded")

        interrupt = _RewindThenCrash(on_call=1)
        router = _GridRouter(interrupt=interrupt)
        loop, pcb = self._loop(router, tmp_path)
        interrupt.loop = loop
        before = _state(router, pcb)

        with pytest.raises(RuntimeError, match="router exploded"):
            loop.run_delta(reuse_existing_routes=True)
        assert router.grid != before["grid"], "the save-path restore leaves the trial's grid"

        # Nothing is in flight any more, and a save-path call still does nothing ...
        assert loop.restore_last_accepted() is False
        assert router.grid != before["grid"]
        # ... but the caller that carries on asks for the grid, and gets it.
        assert loop.restore_last_accepted(rebuild_grid=True) is False
        assert _state(router, pcb) == before
        _assert_grid_matches_the_copper(router)


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


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
class TestCtrlCThatDoesNotStopTheRun:
    """Ctrl+C lands in a destructor: the handler saves, the exit is discarded.

    Python drops an exception raised out of ``__del__``, ``SystemExit``
    included, so ``_handle_interrupt`` returns into the trial's re-route as if
    nothing had happened (#6324) -- after its save hook put the pads, the
    footprints and the routes back.  Whatever the run goes on to write must
    still be copper on the placement it was routed against.
    """

    @pytest.mark.parametrize(
        "in_place", [False, True], ids=["publishes-at-end", "appends-in-place"]
    )
    def test_the_interrupted_trial_is_not_kept(self, board, in_place):
        source, output = board
        router = _GridRouter(interrupt=_SwallowedCtrlC(on_call=1), in_place=in_place)
        before = _state(router)

        returned = _run_loop(router, source, output)

        # The handler really ran, saved a consistent board, and did not exit.
        assert route_cmd._interrupt_state["interrupted"] is True
        info = route_cmd._interrupt_state["partial_save_info"]
        assert info["snapshot_state"] == "last-accepted"
        assert info["snapshot_discarded_trial"] == "R2 translate"
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "C"}
        assert _positions(_partial(output)) == _PARTS

        # The run carried on.  It must not report the discarded trial as kept
        # (it used to: "Deltas kept: 1", "Moved placement persisted", with the
        # footprint unmoved and NODE's copper ending 1 mm beside its pad).
        assert returned is None, "no delta was kept, so no moved placement is persisted"
        assert not output.exists()
        assert route_cmd._interrupt_state["placement_text"] is None
        assert _state(router) == before
        _assert_grid_matches_the_copper(router)

        # What the run would write next: the router's copper on that placement.
        _partial(output).unlink()
        assert route_cmd._save_partial_results() is True
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "C"}
        assert _positions(_partial(output)) == _PARTS

    def test_after_a_kept_delta_only_that_delta_is_persisted(self, board):
        source, output = board
        router = _GridRouter(interrupt=_SwallowedCtrlC(on_call=2))

        returned = _run_loop(router, source, output)

        assert returned == output
        assert _positions(output)["R2"] == (30.0 + _MOVE, 10.0)
        assert _positions(output)["R4"] == _PARTS["R4"], "the discarded trial moved nothing"
        assert sorted(route.net for route in router.routes) == [1, 2, 3]
        _assert_grid_matches_the_copper(router)
        _partial(output).unlink()
        assert route_cmd._save_partial_results() is True
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "B", "C"}
        assert _positions(_partial(output))["R4"] == _PARTS["R4"]

    def test_a_crash_after_the_swallowed_interrupt_still_restores_the_grid(self, board):
        """The handler's restore skips the grid; the crash handler must not."""
        source, output = board
        router = _GridRouter(interrupt=_SwallowedCtrlCThenCrash(on_call=1))
        before = _state(router)

        assert _run_loop(router, source, output) is None

        assert _state(router) == before
        _assert_grid_matches_the_copper(router)


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

    def test_a_failed_handoff_snapshot_is_retried_at_save_time(self, board, monkeypatch):
        """A transient ``_pcb_text`` failure at the handoff must not strand the save
        on the source placement: the later save retries and gets the moved one."""
        source, output = board
        real = route_cmd._pcb_text
        calls = {"n": 0}

        def flaky(pcb):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(pcb)

        router = _CopperRouter()
        monkeypatch.setattr(route_cmd, "_pcb_text", flaky)
        _run_loop(router, source, output)
        assert route_cmd._interrupt_state["placement_text"] is None
        assert route_cmd._interrupt_state["pre_save_hook"] is not None

        assert route_cmd._save_partial_results() is True
        assert _positions(_partial(output))["R2"] == (30.0 + _MOVE, 10.0)
        info = route_cmd._interrupt_state["partial_save_info"]
        assert info["snapshot_placement"] == "moved"
        assert info["snapshot_state"] == "live"

    def test_a_persistent_handoff_failure_is_never_labelled_a_valid_source_snapshot(
        self, board, monkeypatch
    ):
        source, output = board
        router = _CopperRouter()

        def broken(pcb):
            raise RuntimeError("boom")

        monkeypatch.setattr(route_cmd, "_pcb_text", broken)
        _run_loop(router, source, output)

        assert route_cmd._save_partial_results() is True
        info = route_cmd._interrupt_state["partial_save_info"]
        assert info["snapshot_state"] == "inconsistent"
        assert "boom" in info["snapshot_restore_error"]

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

    @pytest.mark.parametrize("rerouted", [0, 1, 3], ids=["no-net-back", "one-net-back", "all-back"])
    def test_a_crash_in_the_first_trial_restores_the_grid_too(self, board, rerouted):
        """The run carries on, and optimize / DRC nudge / clearance read the grid.

        Restoring the routes and the pads is not enough: the crashed trial
        reset the grid from the MOVED pads and marked its own copper on it.
        """
        source, output = board
        router = _GridRouter(interrupt=_Crash(on_call=1, rerouted=rerouted))
        before = _state(router)

        assert _run_loop(router, source, output) is None

        assert router.grid["pads"][("R2", "1")] == (29.5, 10.0), "reset from the restored pads"
        assert router.grid == before["grid"]
        assert _state(router) == before
        _assert_grid_matches_the_copper(router)

    def test_a_crash_after_a_kept_delta_restores_the_grid_of_the_kept_state(self, board):
        source, output = board
        router = _GridRouter(interrupt=_Crash(on_call=2))

        assert _run_loop(router, source, output) == output

        assert sorted(route.net for route in router.routes) == [1, 2, 3]
        assert router.grid["pads"][("R2", "1")] == (29.5 + _MOVE, 10.0), "the kept move"
        assert router.grid["pads"][("R4", "1")] == (29.5, 30.0), "not the crashed trial's"
        _assert_grid_matches_the_copper(router)


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
