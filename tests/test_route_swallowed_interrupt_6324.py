"""An interrupt whose exit is swallowed must still stop ``kct route`` (#6324).

``_handle_interrupt`` saves ``<output>_partial.kicad_pcb`` and raises
``SystemExit``; the deadline handler raises ``RouteDeadlineExpired``.  When the
signal lands while a ``__del__`` runs, Python discards that raise ("Exception
ignored in ...") and routing used to carry on to its normal save.  These tests
deliver both from a real ``__del__`` -- the interpreter itself throws the
exception away, not the test -- and check that the run stops at the next stage
boundary, never reaches a later stage, and never rewrites the first partial.

Real ``route_cmd.main`` / ``_in_process_main_impl`` on a tiny two-net board
(Python backend, basic strategy); only the stage the interrupt lands in is
patched.
"""

from __future__ import annotations

import gc
import json
import signal
from pathlib import Path

import pytest

from kicad_tools import router as router_module
from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router import drc_nudge, optimizer
from kicad_tools.router.placement_feedback import PlacementFeedbackLoop
from tests.router.test_placement_delta_feedback import FakeRouter

# The point of these tests: the interpreter reports the handler's exception as
# "ignored" (pytest surfaces that as an unraisable-exception warning).
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


def _board_text() -> str:
    parts = []
    for ref, x, y, net, name in [
        ("R1", 105, 103, 1, "A"),
        ("R2", 115, 103, 1, "A"),
        ("R3", 105, 108, 2, "B"),
        ("R4", 115, 108, 2, "B"),
    ]:
        parts.append(
            f'(footprint "test" (layer "F.Cu") (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
            f'  (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net {net} "{name}")))'
        )
    return (
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        "  (setup (pad_to_mask_clearance 0))\n"
        '  (net 0 "") (net 1 "A") (net 2 "B")\n'
        "  (gr_rect (start 100 100) (end 120 112) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts"))\n  ' + "\n  ".join(parts) + "\n)\n"
    )


def _swallowed(deliver) -> None:
    """Call ``deliver`` from inside a ``__del__``, where Python drops its raise."""

    class _Dying:
        def __del__(self) -> None:
            deliver()

    _Dying()
    gc.collect()


def _ctrl_c() -> None:
    route_cmd._handle_interrupt(signal.SIGINT, None)


def _deadline() -> None:
    route_deadline._deadline_signal(signal.SIGTERM, None)


class _Spy:
    def __init__(self, real=None):
        self.calls = 0
        self.real = real

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.real is not None:
            return self.real(*args, **kwargs)
        return None


@pytest.fixture(autouse=True)
def _isolate_interrupt_state():
    """``_interrupt_state`` and the deadline flag are process-global."""
    saved = dict(route_cmd._interrupt_state)
    yield
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)
    route_deadline.reset_deadline_state()


@pytest.fixture
def board(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "board.kicad_pcb"
    source.write_text(_board_text())
    return source, tmp_path / "board_routed.kicad_pcb"


@pytest.fixture
def later_stages(monkeypatch) -> dict[str, _Spy]:
    """Spies on the stages after optimize; none may run after an interrupt."""
    spies = {
        "nudge": _Spy(drc_nudge.drc_verify_and_nudge),
        "write": _Spy(route_cmd._write_routed_pcb),
        "finalize": _Spy(route_cmd._finalize_routes),
    }
    monkeypatch.setattr(drc_nudge, "drc_verify_and_nudge", spies["nudge"])
    monkeypatch.setattr(route_cmd, "_write_routed_pcb", spies["write"])
    monkeypatch.setattr(route_cmd, "_finalize_routes", spies["finalize"])
    return spies


def _assert_no_routed_output(source: Path, output: Path) -> None:
    """The run seeds ``<output>`` with the source board; no copper may land there."""
    assert not output.exists() or output.read_text() == source.read_text()


def _partial(output: Path) -> Path:
    return output.with_stem(output.stem + "_partial")


def _argv(source: Path, output: Path, *extra: str) -> list[str]:
    return [
        str(source),
        "-o",
        str(output),
        "--backend",
        "python",
        "--strategy",
        "basic",
        "--quiet",
        # The single-pass flow; ``--auto-layers`` (the default) is the
        # layer-escalation flow, which installs no Ctrl+C handler yet (#6322).
        "--no-auto-layers",
        *extra,
    ]


class TestSinglePassFlow:
    def test_swallowed_ctrl_c_in_optimize_stops_the_run(self, board, later_stages, monkeypatch):
        source, output = board
        seen: dict[str, object] = {}

        def interrupted_optimize(router, opt, skip_nets=None):
            _swallowed(_ctrl_c)
            # Still here: the handler's ``SystemExit`` was discarded.
            seen["interrupted"] = route_cmd._interrupt_state["interrupted"]
            seen["first"] = _partial(output).read_bytes()
            seen["first_mtime"] = _partial(output).stat().st_mtime_ns
            # A second Ctrl+C after the first one saved: still swallowed here,
            # and it must not overwrite the first partial.
            _swallowed(_ctrl_c)
            seen["second_mtime"] = _partial(output).stat().st_mtime_ns

        monkeypatch.setattr(optimizer, "optimize_routes_grid_synced", interrupted_optimize)

        rc = route_cmd.main(_argv(source, output))

        assert seen.get("interrupted") is True, "the optimize stage was not reached"
        assert rc == 5
        assert later_stages["nudge"].calls == 0
        assert later_stages["finalize"].calls == 0
        assert later_stages["write"].calls == 0
        _assert_no_routed_output(source, output)
        assert _partial(output).read_bytes() == seen["first"]
        assert seen["second_mtime"] == seen["first_mtime"]
        assert b"(segment" in seen["first"]

    def test_without_an_interrupt_the_run_saves_normally(self, board, later_stages):
        source, output = board

        rc = route_cmd.main(_argv(source, output))

        assert rc == 0
        assert later_stages["finalize"].calls == 1
        assert later_stages["write"].calls == 1
        assert output.exists()
        assert not _partial(output).exists()


class TestRuleRelaxationFlow:
    def test_swallowed_ctrl_c_between_attempts_stops_the_ladder(
        self, board, later_stages, monkeypatch
    ):
        source, output = board
        load = router_module.load_pcb_for_routing
        loads: list[object] = []

        def interrupted_load(*args, **kwargs):
            router, mapping = load(*args, **kwargs)
            loads.append(router)

            def route_all(*a, **kw):
                # Swallowed during the first tier; every net "fails" so the
                # ladder would normally relax the rules and try again.
                _swallowed(_ctrl_c)
                return []

            def stats(*a, **kw):
                return {"nets_routed": 0, "segments": 0, "vias": 0}

            monkeypatch.setattr(router, "route_all", route_all)
            monkeypatch.setattr(router, "get_statistics", stats)
            return router, mapping

        monkeypatch.setattr(router_module, "load_pcb_for_routing", interrupted_load)

        rc = route_cmd.main(_argv(source, output, "--adaptive-rules", "--min-completion", "1.0"))

        assert len(loads) == 1, "the ladder went on to another tier after the interrupt"
        # No attempt had completed, so the handler had nothing to save.
        assert rc == 130
        assert later_stages["write"].calls == 0
        assert later_stages["finalize"].calls == 0
        _assert_no_routed_output(source, output)


class TestDeadlinePath:
    def test_swallowed_deadline_in_optimize_still_ends_in_the_timeout_handler(
        self, board, later_stages, monkeypatch, tmp_path
    ):
        source, output = board
        control = tmp_path / "control.json"
        control.write_text("{}")
        monkeypatch.setenv(route_deadline.CONTROL_ENV, str(control))
        saves = _Spy(route_cmd._save_partial_results)
        monkeypatch.setattr(route_cmd, "_save_partial_results", saves)
        reached: list[bool] = []

        def interrupted_optimize(router, opt, skip_nets=None):
            _swallowed(_deadline)
            reached.append(route_deadline.deadline_fired())

        monkeypatch.setattr(optimizer, "optimize_routes_grid_synced", interrupted_optimize)

        rc = route_cmd._in_process_main_impl(_argv(source, output))

        assert reached == [True], "the deadline did not land in the optimize stage"
        assert rc == route_deadline.TIMEOUT_EXIT
        assert saves.calls == 1, "the _partial must be written exactly once"
        assert later_stages["nudge"].calls == 0
        assert later_stages["write"].calls == 0
        assert _partial(output).exists()
        record = json.loads(control.read_text())
        assert record["stage"] == "partial-save"
        assert record["snapshot_saved"] is True
        assert record["interrupted_stage"] == "optimization"
        # The invocation is over: its deadline does not leak into the next one.
        assert route_deadline.deadline_fired() is False


class TestHandlerRecordsItsOutcome:
    def test_repeat_interrupt_reuses_the_first_outcome_without_saving(self, monkeypatch):
        saves = _Spy()
        monkeypatch.setattr(route_cmd, "_save_partial_results", lambda: saves() or True)
        route_cmd._reset_interrupt_outcome()
        route_cmd._interrupt_state["quiet"] = True

        with pytest.raises(SystemExit) as first:
            _ctrl_c()
        with pytest.raises(SystemExit) as second:
            _ctrl_c()

        assert first.value.code == second.value.code == 5
        assert saves.calls == 1
        assert route_cmd._interrupt_requested_exit() == 5

    def test_no_interrupt_means_no_stop(self):
        route_cmd._reset_interrupt_outcome()
        assert route_cmd._interrupt_requested_exit() is None
        assert route_cmd._interrupt_pending() is False


class TestPlacementFeedbackLoopStopRequested:
    def test_stop_requested_ends_the_loop_before_another_reroute(self):
        router = FakeRouter([], 2, lambda r: [2])
        loop = PlacementFeedbackLoop(router, pcb=None, verbose=False, stop_requested=lambda: True)

        result = loop.run(max_adjustments=3)

        assert result.exit_reason == "pf_interrupted"
        assert router.route_calls == 0

    def test_default_loop_still_routes(self):
        router = FakeRouter([], 2, lambda r: [2])
        loop = PlacementFeedbackLoop(router, pcb=None, verbose=False)

        result = loop.run(max_adjustments=0)

        assert result.exit_reason != "pf_interrupted"
        assert router.route_calls == 1
