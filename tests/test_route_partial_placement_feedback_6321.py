"""A partial save during ``--placement-feedback`` keeps the loop's best routes (#6321).

``PlacementFeedbackLoop.run`` (the blocker-geometry loop) clears every route at
the start of each iteration and re-routes, while its best result so far waits
in a snapshot.  A deadline or Ctrl+C that lands mid-iteration used to save the
half-finished re-route in place of that best result.

These tests drive the real ``_run_placement_feedback`` through the real
``Autorouter.route_with_placement_feedback`` and ``PlacementFeedbackLoop``.
Only the router is a stub (the #6314 stubs, scripted per routing call), and the
strategy search is replaced by a no-op move so the loop actually iterates.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.recovery import (
    Action,
    ApplicationResult,
    Difficulty,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.router import PlacementFeedbackLoop
from kicad_tools.router.core import Autorouter
from tests.test_route_partial_placement_6314 import (
    _NETS,
    _PARTS,
    _arm,
    _assert_grid_matches_the_copper,
    _board_copper,
    _board_text,
    _CtrlC,
    _Deadline,
    _GridRouter,
    _JournalRouter,
    _live_copper,
    _partial,
    _positions,
    _routed_nets_with_copper_on_every_pad,
    _SwallowedCtrlC,
    _witness_sidecar,
)

_ALL = frozenset(n for n in _NETS)

# Failing nets per routing call.  Call 0 is the routing pass BEFORE the loop
# (its routes seed the loop's best state); call N >= 1 is loop iteration N-1.
#
# BEST_IS_ITER_0: the pre-loop pass routes A, iteration 0 routes A+B+C (the
# best), iteration 1 regresses to A.
BEST_IS_ITER_0 = {0: {2, 3, 4}, 1: {4}, 2: {2, 3, 4}, 3: {2, 3, 4}, 4: {2, 3, 4}}
# BEST_IS_PRE_LOOP: the pre-loop pass routes A+B+C and every iteration does
# worse, so the best state is the loop's input (``snapshot_trial`` -1).
BEST_IS_PRE_LOOP = {0: {4}, 1: {2, 3, 4}, 2: {2, 3, 4}, 3: {2, 3, 4}, 4: {2, 3, 4}}


class _Scripted:
    """Mixin: which nets fail is scripted per routing call, not by geometry.

    This loop never moves the router's pads, so the #6314 stubs' geometric
    predicate would route the same nets every time.
    """

    def __init__(self, script: dict[int, set[int]], *args, **kwargs):
        self.script = script
        super().__init__(*args, **kwargs)

    @staticmethod
    def _failing(router) -> list[int]:
        return sorted(router.script[min(router.route_calls, max(router.script))])

    def route_with_placement_feedback(self, **kwargs):
        # The REAL Autorouter entry point; it only needs ``self`` as the router.
        return Autorouter.route_with_placement_feedback(self, **kwargs)


class _ScriptedRouter(_Scripted, _GridRouter):
    pass


class _ScriptedJournalRouter(_Scripted, _JournalRouter):
    pass


class _Observe(_Deadline):
    """Not an interrupt: record what the partial save would see mid-iteration."""

    def __init__(self, on_call: int, rerouted: int = 1):
        super().__init__(on_call, rerouted)
        self.hook = "unset"

    def __call__(self) -> None:
        self.hook = route_cmd._interrupt_state.get("pre_save_hook")


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


@pytest.fixture(autouse=True)
def _always_a_strategy(monkeypatch):
    """A no-op move every iteration, so the loop iterates instead of giving up."""
    strategy = ResolutionStrategy(
        type=StrategyType.MOVE_COMPONENT,
        difficulty=Difficulty.EASY,
        confidence=0.99,
        actions=[Action(type="move", target="R1", params={"x": 0.0, "y": 0.0})],
    )
    monkeypatch.setattr(
        PlacementFeedbackLoop,
        "_find_best_placement_strategy",
        lambda self, failed, conf: strategy,
    )
    monkeypatch.setattr(
        StrategyApplicator,
        "apply_strategy",
        lambda self, pcb, strat: ApplicationResult(
            success=True, components_moved=[], message="(no-op move)"
        ),
    )


def _args(output: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output=str(output),
        placement_feedback=True,
        placement_feedback_budget=3,
        placement_feedback_max_movement=5.0,
        placement_feedback_stagnation_patience=0,
        placement_feedback_outer_timeout=None,
        placement_feedback_anchor=None,
        placement_feedback_no_anchor=None,
        strategy="negotiated",
        timeout=None,
        per_net_timeout=None,
        verbose=False,
        quiet=True,
    )


def _run_loop(router, source: Path, output: Path, pinned=None):
    _arm(pinned if pinned is not None else router, source, output)
    return route_cmd._run_placement_feedback(
        router=router, pcb_path=source, args=_args(output), quiet=True
    )


def _interrupted_run(monkeypatch, router, source: Path, output: Path) -> int:
    """Drive the loop under the real deadline handler in ``_in_process_main_impl``."""

    def work(argv):
        _run_loop(router, source, output)
        raise AssertionError("the iteration must not complete")

    monkeypatch.setattr(route_cmd, "_main_impl", work)
    return route_cmd._in_process_main_impl([])


def _nets(router) -> list[int]:
    return sorted(route.net for route in router.routes)


# --------------------------------------------------------------------------- #
# Deadline                                                                     #
# --------------------------------------------------------------------------- #


class TestDeadlineDuringAnIteration:
    @pytest.mark.parametrize("rerouted", [0, 1], ids=["no-net-back", "one-net-back"])
    def test_saves_the_best_iteration_not_the_in_progress_re_route(
        self, monkeypatch, board, rerouted
    ):
        source, output = board
        router = _ScriptedRouter(BEST_IS_ITER_0, interrupt=_Deadline(on_call=2, rerouted=rerouted))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        saved = _partial(output)
        # Iteration 0's copper -- not the one net (or none) iteration 1 had
        # re-routed when the deadline landed.
        assert _routed_nets_with_copper_on_every_pad(saved) == {"A", "B", "C"}
        assert _positions(saved) == _PARTS, "this loop never persists a moved placement"
        assert not output.exists()

    def test_the_receipt_names_the_best_iteration(self, monkeypatch, tmp_path, board):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _ScriptedRouter(BEST_IS_ITER_0, interrupt=_Deadline(on_call=2))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        state = json.loads(control.read_text())
        assert state["snapshot_saved"] is True
        assert state["snapshot_state"] == "best-trial"
        assert state["snapshot_strategy"] == "placement-feedback"
        assert state["snapshot_trial"] == 0
        assert state["snapshot_placement"] == "source"

    def test_the_pre_loop_seed_is_reported_as_trial_minus_one(self, monkeypatch, tmp_path, board):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text(json.dumps({"stage": "routing", "output": str(output)}))
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        router = _ScriptedRouter(BEST_IS_PRE_LOOP, interrupt=_Deadline(on_call=1))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "B", "C"}
        state = json.loads(control.read_text())
        assert state["snapshot_state"] == "best-trial"
        assert state["snapshot_strategy"] == "placement-feedback"
        assert state["snapshot_trial"] == -1


# --------------------------------------------------------------------------- #
# Ctrl+C                                                                       #
# --------------------------------------------------------------------------- #


class TestCtrlCDuringAnIteration:
    def test_the_in_handler_save_holds_the_best_routes(self, board):
        source, output = board
        router = _ScriptedRouter(BEST_IS_ITER_0, interrupt=_CtrlC(on_call=2))

        with pytest.raises(SystemExit) as exit_info:
            _run_loop(router, source, output)

        assert exit_info.value.code == 5, "exit 5 = interrupted with partial results saved"
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "B", "C"}
        info = route_cmd._interrupt_state["partial_save_info"]
        assert info["snapshot_state"] == "best-trial"
        assert info["snapshot_strategy"] == "placement-feedback"
        assert info["snapshot_trial"] == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
class TestCtrlCThatDoesNotStopTheRun:
    """Ctrl+C in a destructor: the handler saves, Python discards the exit (#6324)."""

    @pytest.mark.parametrize(
        "in_place", [False, True], ids=["publishes-at-end", "appends-in-place"]
    )
    def test_the_loop_finishes_on_the_best_routes(self, board, in_place):
        source, output = board
        router = _ScriptedRouter(
            BEST_IS_ITER_0, interrupt=_SwallowedCtrlC(on_call=2), in_place=in_place
        )

        _run_loop(router, source, output)

        # The handler ran and saved the best routes ...
        assert route_cmd._interrupt_state["interrupted"] is True
        assert route_cmd._interrupt_state["partial_save_info"]["snapshot_trial"] == 0
        assert _routed_nets_with_copper_on_every_pad(_partial(output)) == {"A", "B", "C"}
        # ... the run carried on, stopped after the rewound iteration, and
        # left the best routes (with a matching grid) for the later stages.
        assert router.route_calls == 2, "no further iteration after the rewind"
        assert _nets(router) == [1, 2, 3]
        _assert_grid_matches_the_copper(router)
        assert route_cmd._interrupt_state["pre_save_hook"] is None


# --------------------------------------------------------------------------- #
# Access-witness journal (#6320)                                               #
# --------------------------------------------------------------------------- #


class TestAccessWitnessOfAnInterruptedIteration:
    def test_the_sidecar_describes_the_saved_board(self, monkeypatch, board):
        source, output = board
        router = _ScriptedJournalRouter(BEST_IS_ITER_0, interrupt=_Deadline(on_call=2))

        assert _interrupted_run(monkeypatch, router, source, output) == route_deadline.TIMEOUT_EXIT

        saved = _partial(output)
        journal = _witness_sidecar(saved)
        # Cut back to the journal as it stood when the best snapshot was taken
        # (the end of iteration 0's re-route), dropping iteration 1's reset
        # and re-route.
        assert len(journal) == router.journal_len_after_call[1]
        assert _live_copper(journal) == _board_copper(saved)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    def test_a_swallowed_ctrl_c_leaves_a_journal_matching_the_routes(self, board):
        source, output = board
        router = _ScriptedJournalRouter(BEST_IS_ITER_0, interrupt=_SwallowedCtrlC(on_call=2))

        _run_loop(router, source, output)

        saved = _partial(output)
        assert _live_copper(_witness_sidecar(saved)) == _board_copper(saved)
        assert _live_copper(router.commit_journal) == sorted(
            (t.net, t.x1, t.y1, t.x2, t.y2) for t in router.routes
        )


# --------------------------------------------------------------------------- #
# Without an interrupt, and for a router the save does not serialize           #
# --------------------------------------------------------------------------- #


class TestHookLifetime:
    def test_registered_during_the_loop_and_retired_after(self, board):
        source, output = board
        observe = _Observe(on_call=2)
        router = _ScriptedRouter(BEST_IS_ITER_0, interrupt=observe)

        _run_loop(router, source, output)

        assert callable(observe.hook)
        assert route_cmd._interrupt_state["pre_save_hook"] is None

    def test_not_registered_for_a_router_the_save_does_not_serialize(self, board):
        source, output = board
        observe = _Observe(on_call=2)
        router = _ScriptedRouter(BEST_IS_ITER_0, interrupt=observe)
        pinned = _ScriptedRouter(BEST_IS_ITER_0)

        _run_loop(router, source, output, pinned=pinned)

        assert observe.hook is None
        assert route_cmd._interrupt_state["pre_save_hook"] is None

    def test_an_uninterrupted_run_ends_on_the_best_routes(self, board):
        source, output = board
        router = _ScriptedRouter(BEST_IS_ITER_0)

        _run_loop(router, source, output)

        assert router.route_calls == 4, "pre-loop pass + max_adjustments + 1 iterations"
        assert _nets(router) == [1, 2, 3]
        _assert_grid_matches_the_copper(router)


class TestLoopRestore:
    def _loop(self, router) -> PlacementFeedbackLoop:
        return PlacementFeedbackLoop(router, pcb=None, verbose=False, stagnation_patience=0)

    def test_nothing_to_restore_before_or_after_a_run(self):
        router = _ScriptedRouter(BEST_IS_ITER_0)
        loop = self._loop(router)
        assert loop.restore_best_for_partial_save() is False

        loop.run(max_adjustments=0)

        assert loop.restore_best_for_partial_save() is False
        assert _nets(router) == [1, 2, 3]

    def test_no_best_yet_leaves_the_live_routes(self):
        """Nothing was routed before the loop and iteration 0 is still running."""
        router = _ScriptedRouter({0: set(_ALL), 1: {4}})
        loop = self._loop(router)
        seen: list[bool] = []

        class _Probe(_Deadline):
            def __call__(self) -> None:
                seen.append(loop.restore_best_for_partial_save())

        router.interrupt = _Probe(on_call=1)
        loop.run(max_adjustments=0)

        assert seen == [False]
        assert _nets(router) == [1, 2, 3]
