"""The default ``kct route`` flow saves a ``_partial`` board when cut short (#6322).

``--auto-layers`` is on by default, so a plain ``kct route`` runs
``route_with_layer_escalation``.  That flow never bound the partial-save state
(router / output / pcb path) nor installed the SIGINT/SIGTERM handler, so a
``--timeout`` deadline or Ctrl+C saved no ``_partial`` board at all.

These tests drive the real CLI entry point on a real two-net board with the
real loader and router.  Only the end of the routing call is patched: it routes
for real, forces a periodic checkpoint onto ``<output>`` (the staged placement
source the save reads, so already carrying this copper), and then the deadline
or Ctrl+C lands.  Every saved board is checked for exactly the router's copper
plus any preserved input copper -- no more, no less.
"""

from __future__ import annotations

import contextlib
import json
import re
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import route_cmd, route_deadline
from kicad_tools.router.core import Autorouter

_PARTS = {"R1": (10.0, 10.0), "R2": (30.0, 10.0)}
_NET_OF = {("R1", "1"): 1, ("R2", "1"): 1, ("R1", "2"): 2, ("R2", "2"): 2}
_NETS = {1: "A", 2: "B"}


def _board_text() -> str:
    footprints = ""
    for ref, (x, y) in _PARTS.items():
        pads = "".join(
            f'    (pad "{pin}" smd rect (at {dx} 0) (size 0.6 0.6) (layers "F.Cu") '
            f'(net {_NET_OF[(ref, pin)]} "{_NETS[_NET_OF[(ref, pin)]]}"))\n'
            for pin, dx in (("1", -0.5), ("2", 0.5))
        )
        footprints += (
            f'  (footprint "R_0402" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}")\n{pads}  )\n'
        )
    nets = "".join(f'  (net {num} "{name}")\n' for num, name in _NETS.items())
    return (
        '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (layers\n    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n  )\n'
        '  (gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (width 0.1))\n'
        '  (net 0 "")\n' + nets + footprints + ")\n"
    )


@pytest.fixture
def board(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "input.kicad_pcb"
    source.write_text(_board_text())
    return source, tmp_path / "output.kicad_pcb"


@pytest.fixture
def control(tmp_path: Path, monkeypatch) -> Path:
    """Attach a deadline-supervisor control file so stage records are kept."""
    path = tmp_path / "control.json"
    monkeypatch.setenv(route_deadline.CONTROL_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    route_cmd._interrupt_state.update(router=None, output_path=None, pcb_path=None)
    route_cmd._reset_interrupt_outcome()
    route_cmd._reset_partial_save_state()
    route_deadline.reset_deadline_state()


def _partial(output: Path) -> Path:
    return output.with_stem(output.stem + "_partial")


def _copper(text: str) -> tuple[int, int]:
    return text.count("(segment"), len(re.findall(r"\(via\s", text))


class _Interrupt:
    """Patch the routing call: route for real, checkpoint, then get cut short.

    ``drop_net`` removes one net's copper after routing so the attempt is
    partial (it keeps escalation from succeeding and leaves a failed net).
    """

    def __init__(self, monkeypatch, *, interrupt=None, drop_net: int | None = None):
        self.interrupt = interrupt
        self.drop_net = drop_net
        self.router = None
        self.expected: tuple[int, int] | None = None
        self.checkpointed = False
        original = Autorouter.route_all_negotiated

        def route(router, *args, **kwargs):
            result = original(router, *args, **kwargs)
            if self.drop_net is not None:
                router.routes = [r for r in router.routes if r.net != self.drop_net]
            callback = kwargs.get("checkpoint_callback")
            if callback is not None:
                # A periodic checkpoint: it rewrites the staged ``<output>`` --
                # the flow's placement source -- with this copper.
                metrics = SimpleNamespace(iteration=0, routed_count=1, overflow=0)
                callback(list(router.routes), metrics)
                self.checkpointed = True
            self.router = router
            self.expected = _copper(router.to_sexp())
            if self.interrupt is not None:
                self.interrupt()
            return result

        monkeypatch.setattr(Autorouter, "route_all_negotiated", route)


def _raise_deadline() -> None:
    raise route_deadline.RouteDeadlineExpired()


def _argv(source: Path, output: Path, *extra: str) -> list[str]:
    return [str(source), "-o", str(output), "--quiet", "--skip-drc", *extra]


class TestDeadlineDuringTheFirstAttempt:
    def test_saves_a_partial_with_the_copper_routed_so_far(self, monkeypatch, board, control):
        source, output = board
        patch = _Interrupt(monkeypatch, interrupt=_raise_deadline)

        rc = route_cmd._in_process_main_impl(_argv(source, output))

        assert rc == route_deadline.TIMEOUT_EXIT
        partial = _partial(output)
        assert partial.is_file(), "the default (--auto-layers) flow saved no _partial board"
        segments, _ = _copper(partial.read_text())
        assert segments > 0
        record = json.loads(control.read_text())
        assert record["stage"] == "partial-save"
        assert record["snapshot_saved"] is True
        assert record["snapshot"] == str(partial)
        assert patch.checkpointed

    def test_the_partial_does_not_duplicate_checkpointed_copper(self, monkeypatch, board, control):
        source, output = board
        patch = _Interrupt(monkeypatch, interrupt=_raise_deadline)

        route_cmd._in_process_main_impl(_argv(source, output))

        # The checkpoint really did land on the placement source...
        assert _copper(output.read_text())[0] > 0
        # ...and the partial holds the router's copper exactly once.
        assert _copper(_partial(output).read_text()) == patch.expected

    def test_preserved_input_copper_is_kept_exactly_once(self, monkeypatch, tmp_path, control):
        source = tmp_path / "input.kicad_pcb"
        fixed = '  (segment (start 2 2) (end 4 2) (width 0.2) (layer "B.Cu") (net 1))\n'
        source.write_text(_board_text()[: -len(")\n")] + fixed + ")\n")
        output = tmp_path / "output.kicad_pcb"
        patch = _Interrupt(monkeypatch, interrupt=_raise_deadline)

        route_cmd._in_process_main_impl(_argv(source, output, "--preserve-existing"))

        text = _partial(output).read_text()
        segments, vias = patch.expected
        assert _copper(text) == (segments + 1, vias)
        assert len(re.findall(r"\(start 2(?:\.0+)? 2(?:\.0+)?\)", text)) == 1


class TestDeadlineAfterAKeptPlacementDelta:
    """A later-stage deadline after a kept delta saves this run's copper (AC 2)."""

    @staticmethod
    def _keep_a_delta(monkeypatch) -> None:
        def feedback(router, pcb=None, **kwargs):
            footprint = pcb.get_footprint("R2")
            x, y = footprint.position
            pcb.update_footprint_position("R2", x + 2.0, y)
            delta = SimpleNamespace(target_ref="R2", kind="translate", net_name="B")
            return SimpleNamespace(
                applied_deltas=[delta],
                iterations=1,
                exit_reason="test",
                proposed_deltas=[delta],
                reverted_deltas=[],
                reverted_counts=[],
                skipped_deltas=[],
                skip_reasons=[],
                interrupted_delta=None,
                failed_nets=[],
            )

        monkeypatch.setattr(Autorouter, "route_with_placement_delta_feedback", feedback)

    def test_an_optimize_deadline_saves_copper_on_the_moved_placement(
        self, monkeypatch, board, control
    ):
        source, output = board
        patch = _Interrupt(monkeypatch, drop_net=2)
        self._keep_a_delta(monkeypatch)
        from kicad_tools.router import optimizer

        def deadline_in_optimize(*args, **kwargs):
            raise route_deadline.RouteDeadlineExpired()

        monkeypatch.setattr(optimizer, "optimize_routes_grid_synced", deadline_in_optimize)

        rc = route_cmd._in_process_main_impl(
            _argv(source, output, "--max-layers", "2", "--placement-delta-feedback")
        )

        assert rc == route_deadline.TIMEOUT_EXIT
        record = json.loads(control.read_text())
        partial = _partial(output)
        assert record["snapshot_saved"] is True
        assert record["snapshot"] == str(partial)
        assert record["snapshot_placement"] == "moved"
        text = partial.read_text()
        assert re.search(r"\(at 32(\.0+)? 10(\.0+)?\)", text), "the moved placement is not saved"
        # This run's copper, exactly once (the moved board was read back from
        # the checkpointed ``<output>``, so it already carried copper).
        assert _copper(text) == patch.expected
        assert patch.expected[0] > 0


class TestCtrlC:
    def test_ctrl_c_saves_the_partial_and_exits_5(self, monkeypatch, board):
        source, output = board

        def ctrl_c() -> None:
            signal.raise_signal(signal.SIGINT)

        patch = _Interrupt(monkeypatch, interrupt=ctrl_c)

        with pytest.raises(SystemExit) as excinfo:
            route_cmd._in_process_main_impl(_argv(source, output))

        assert excinfo.value.code == 5
        assert _copper(_partial(output).read_text()) == patch.expected

    def test_the_flow_restores_the_previous_handlers_itself(self, monkeypatch, board):
        """Not left to ``_process_state_guard``: that guard is disabled here."""
        source, output = board
        monkeypatch.setattr(route_cmd, "_process_state_guard", contextlib.nullcontext)
        seen = {}

        def ctrl_c() -> None:
            seen["sigint"] = signal.getsignal(signal.SIGINT)
            seen["sigterm"] = signal.getsignal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)

        _Interrupt(monkeypatch, interrupt=ctrl_c)

        def previous_int(signum, frame):  # pragma: no cover - never delivered
            pass

        def previous_term(signum, frame):  # pragma: no cover - never delivered
            pass

        old_int = signal.signal(signal.SIGINT, previous_int)
        old_term = signal.signal(signal.SIGTERM, previous_term)
        try:
            with pytest.raises(SystemExit):
                route_cmd._in_process_main_impl(_argv(source, output))
            assert seen["sigint"] is route_cmd._handle_interrupt
            assert seen["sigterm"] is route_cmd._handle_interrupt
            assert signal.getsignal(signal.SIGINT) is previous_int
            assert signal.getsignal(signal.SIGTERM) is previous_term
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)

    def test_a_completed_run_restores_the_previous_handlers(self, monkeypatch, board):
        source, output = board
        monkeypatch.setattr(route_cmd, "_process_state_guard", contextlib.nullcontext)

        def previous_int(signum, frame):  # pragma: no cover - never delivered
            pass

        old_int = signal.signal(signal.SIGINT, previous_int)
        try:
            assert route_cmd._in_process_main_impl(_argv(source, output)) == 0
            assert signal.getsignal(signal.SIGINT) is previous_int
        finally:
            signal.signal(signal.SIGINT, old_int)
        assert not _partial(output).exists()
