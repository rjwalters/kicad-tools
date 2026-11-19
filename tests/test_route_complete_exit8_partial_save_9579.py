"""Issue #9579: the clean-exit-8 save must neither lose nor misreport the board.

``kct route --complete`` ending on the exit-8 path (input-clearance banner,
unrouted links) printed ``Error saving partial results: index 0 is out of
bounds for axis 0 with size 0``.  A reproduction on this code (the PR #9585
review) pins one concrete fault: the save re-ran ``cleanup_artifacts`` after
``_release_routing_engine_state`` had freed the grid's per-cell arrays, and
an acute bend that survived finalization sent the foldback validation into
the freed planes -- both serialisation attempts died and no ``_partial``
board was written.  The save now serializes the already-finalized copper
without querying a released grid (the live interrupt path keeps full
cleanup).  The closing statistics summary is computed after the write and is
non-fatal (an unavailable one is reported as such).  The reporter's original
fault site is unverified -- their board was unavailable -- so this module
does not claim the incident was a statistics failure.  A real save failure
names the step it failed in.
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
    '(net 0 "")\n(net 1 "SIG")\n(net 2 "OTHER")\n(net 3 "FAR")\n'
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


def _pad(x: float, y: float, w: float, h: float, net: int, name: str) -> dict:
    return {"x": x, "y": y, "width": w, "height": h, "net": net, "net_name": name}


@pytest.fixture
def released_exit8_state(tmp_path: Path):
    """The PR-#9585 review reproduction, finalized with the grid released.

    The #9579 board as reproduced in review: a kept SIG trace whose acute
    bend survived finalization (a 0.3 mm foreign pad at (5.5, 4.3) vetoes
    every repair candidate), SIG's second pad still unrouted, and the issue's
    kept near-miss trace -- a different net's 0.25 mm run 0.075 mm from a
    1 mm foreign pad.  ``_finalize_routes`` runs while the grid is live (the
    foldback is attempted there and skipped), then
    ``_release_routing_engine_state`` frees the per-cell arrays -- the state
    ``_save_partial_results`` meets on the clean exit-8 tail.
    """
    pcb = tmp_path / "in.kicad_pcb"
    pcb.write_text(_BOARD)
    router = Autorouter(
        width=40,
        height=40,
        rules=DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1),
    )
    router.add_component("U1", [{"number": "1", **_pad(5, 4, 0.5, 0.5, 1, "SIG")}])
    router.add_component("U2", [{"number": "1", **_pad(15, 3, 0.5, 0.5, 1, "SIG")}])
    router.add_component("U3", [{"number": "1", **_pad(5.5, 4.3, 0.3, 0.3, 2, "OTHER")}])
    router.add_component("U4", [{"number": "1", **_pad(12, 10.7, 1.0, 1.0, 3, "FAR")}])

    sig = Route(
        net=1,
        net_name="SIG",
        segments=[
            Segment(x1=5, y1=4, x2=5, y2=5, layer=Layer.F_CU, width=0.2, net=1, net_name="SIG"),
            Segment(x1=5, y1=5, x2=6, y2=4, layer=Layer.F_CU, width=0.2, net=1, net_name="SIG"),
        ],
        vias=[],
    )
    far = Route(
        net=3,
        net_name="FAR",
        segments=[
            Segment(x1=5, y1=10, x2=19, y2=10, layer=Layer.F_CU, width=0.25, net=3, net_name="FAR"),
        ],
        vias=[],
    )
    router.routes.extend([sig, far])

    _sexp, _stats, cleanup_stats = route_cmd._finalize_routes(
        router,
        {1, 3},
        2,
        quiet=True,
        preserve_existing=True,
        preserved_routes=[sig, far],
        pcb_path=pcb,
    )
    route_cmd._release_routing_engine_state(router)

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
    yield router, tmp_path, cleanup_stats
    route_cmd._interrupt_state.clear()
    route_cmd._interrupt_state.update(saved)


def test_released_grid_save_writes_the_finalized_copper(
    released_exit8_state, capsys: pytest.CaptureFixture[str]
) -> None:
    """The partial save must not query the released grid (#9579, PR #9585 review).

    Before the fix the save re-ran ``cleanup_artifacts``; the surviving acute
    bend sent the foldback validation into the freed per-cell arrays and both
    serialisation attempts died with the numpy empty-index error before
    anything was written.
    """
    _router, tmp_path, cleanup_stats = released_exit8_state

    # Premise: the acute bend really did survive finalization, so the save's
    # (former) second cleanup pass had a foldback to chase on a freed grid.
    assert cleanup_stats.get("acute_foldbacks_skipped") == 1

    assert route_cmd._save_partial_results() is True
    out = capsys.readouterr().out

    assert "Error saving partial results" not in out
    assert "Raw partial snapshot saved to" in out
    partial = tmp_path / "routed_partial.kicad_pcb"
    assert partial.exists()
    text = partial.read_text()
    # The finalized copper is on the saved board: both kept nets, including
    # the acute bend the foldback repair could not rewrite.
    assert "(net 1)" in text
    assert "(net 3)" in text
    assert "(start 5.0000 5.0000)" in text
    assert "(end 19.0000 10.0000)" in text
