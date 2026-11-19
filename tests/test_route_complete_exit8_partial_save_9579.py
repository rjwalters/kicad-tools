"""Issue #9579: the clean-partial-exit save must not misreport a finished save.

``kct route --complete`` ending on the exit-8 path (input-clearance banner,
unrouted links) prints ``Error saving partial results: index 0 is out of
bounds for axis 0 with size 0`` after the routed board and its sidecars were
already written.  The summary statistics are computed *after* the write, and a
failure there was reported as a failed save with no artifact named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

_BOARD = (
    '(kicad_pcb (version 20240108) (generator "test")\n'
    "(general (thickness 1.6))\n"
    '(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
    '(net 0 "")\n(net 1 "SIG")\n'
    ")\n"
)

_EMPTY_INDEX = "index 0 is out of bounds for axis 0 with size 0"


@pytest.fixture
def partial_state(tmp_path: Path):
    """A router holding one routed net, wired into the interrupt state."""
    pcb = tmp_path / "in.kicad_pcb"
    pcb.write_text(_BOARD)
    router = Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )
    router.routes.append(
        Route(
            net=1,
            net_name="SIG",
            segments=[
                Segment(x1=5, y1=5, x2=9, y2=5, layer=Layer.F_CU, width=0.25, net=1),
            ],
            vias=[],
        )
    )
    saved = dict(route_cmd._interrupt_state)
    route_cmd._interrupt_state.update(
        router=router,
        output_path=tmp_path / "routed.kicad_pcb",
        pcb_path=pcb,
        quiet=False,
        dry_run=False,
        pre_save_hook=None,
        placement_text=None,
        source_copper="",
        source_routes=None,
        best_completed_attempt=None,
    )
    yield router, tmp_path
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)


def test_statistics_failure_after_save_is_not_a_failed_save(
    partial_state, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    router, tmp_path = partial_state

    def boom(*_a, **_k):
        raise IndexError(_EMPTY_INDEX)

    monkeypatch.setattr(router, "get_statistics", boom)

    assert route_cmd._save_partial_results() is True
    out = capsys.readouterr().out

    assert "Error saving partial results" not in out
    assert "Raw partial snapshot saved to" in out
    assert (tmp_path / "routed_partial.kicad_pcb").exists()
    # The summary says what is missing instead of silently dropping it.
    assert "routing statistics unavailable" in out
    assert _EMPTY_INDEX in out


def test_save_failure_names_the_step(
    partial_state, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _router, tmp_path = partial_state

    def boom(*_a, **_k):
        raise IndexError(_EMPTY_INDEX)

    monkeypatch.setattr(route_cmd, "_dedupe_preserved_sexp", boom)

    assert route_cmd._save_partial_results() is False
    out = capsys.readouterr().out

    assert "Error saving partial results while serialising the routed copper" in out
    assert f"IndexError: {_EMPTY_INDEX}" in out
    assert not (tmp_path / "routed_partial.kicad_pcb").exists()


def test_sidecar_failure_names_the_sidecar(
    partial_state, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _router, tmp_path = partial_state

    def boom(*_a, **_k):
        raise IndexError(_EMPTY_INDEX)

    monkeypatch.setattr(route_cmd, "_write_access_witness_sidecar", boom)

    assert route_cmd._save_partial_results() is False
    out = capsys.readouterr().out

    assert "while writing the access-witness sidecar" in out
    # The board was already on disk: the message must not imply otherwise.
    assert (tmp_path / "routed_partial.kicad_pcb").exists()
