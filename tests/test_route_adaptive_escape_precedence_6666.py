"""Adaptive grid vs the dense-package escape pre-phase (Issue #6666).

``do_routing`` used to take the adaptive multi-resolution branch first and
return from it, so ``Autorouter.route_with_escape`` never ran on a board with
a multi-resolution plan.  That was latent until PR #4877 let
``compute_multi_resolution_plan`` see KiCad 7+ refs.  Once #6646 declined
full-stack micro vias, chorus-test's adaptive Phase 1 escaped 0/14 pads of U8
and hard-failed with ``FinePitchEscapeFailure`` instead of reaching the escape
pre-phase that used to route it.

The fix evaluates the escape decision once, previews the adaptive Phase 1
without touching the grid, and falls back to the escape pre-phase only when
that Phase 1 would hard-fail.  Boards whose Phase 1 succeeds keep the adaptive
branch (board 07 regressed when every dense package pre-empted it).

The ``do_routing`` tests drive the real ``kct route`` single-attempt path on a
small board, force a multi-resolution plan, and stub the routing entry points
so only the branch selection is exercised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.adaptive_grid import AdaptiveGridRouter, FinePitchEscapeFailure
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import FineZone, MultiResolutionGridPlan
from tests.test_adaptive_grid import TestAdaptiveGridSSOP, make_grid_and_rules


def _plan() -> MultiResolutionGridPlan:
    zone = FineZone(ref="U1", x_min=0.0, y_min=0.0, x_max=10.0, y_max=10.0, resolution=0.05)
    return MultiResolutionGridPlan(
        coarse_resolution=0.1,
        fine_zones=[zone],
        total_cell_estimate=1000,
        uniform_fallback=0.1,
    )


class _Dense:
    """Minimal stand-in for ``PackageInfo`` (only ``.ref`` is read)."""

    ref = "U1"


_PHASE1_FAILS = FinePitchEscapeFailure(component_ref="U1", attempted_pads=14, suggested_grid=0.05)


def _run(
    pcb: Path,
    tmp_path: Path,
    *,
    dense: bool,
    phase1_failure: FinePitchEscapeFailure | None = None,
    extra: list[str] | None = None,
):
    calls: list[str] = []

    def _escape(self, *args, **kwargs):
        calls.append("route_with_escape")
        return []

    def _adaptive(self, *args, **kwargs):
        calls.append("route_adaptive")
        raise RuntimeError("stub: stop after branch selection")

    def _preview(self, pads):
        calls.append("preview_phase1")
        return phase1_failure

    detect_calls: list[int] = []

    def _detect(self):
        detect_calls.append(1)
        return [_Dense()] if dense else []

    with (
        patch("kicad_tools.router.io.compute_multi_resolution_plan", return_value=_plan()),
        patch.object(Autorouter, "detect_dense_packages", _detect),
        patch.object(Autorouter, "route_with_escape", _escape),
        patch.object(AdaptiveGridRouter, "route_adaptive", _adaptive),
        patch.object(AdaptiveGridRouter, "preview_phase1", _preview),
    ):
        route_cmd.main(
            [
                str(pcb),
                "-o",
                str(tmp_path / "routed.kicad_pcb"),
                "--grid",
                "auto",
                "--layers",
                "2",
                "--no-auto-layers",
                "--no-cache",
                "--skip-drc",
                "--allow-unsafe-grid",
                "--force",
                # No --timeout: a finite budget runs the route in a supervised
                # worker process, where these in-process patches don't apply.
                *(extra or []),
            ]
        )
    return calls, len(detect_calls)


def test_phase1_hard_fail_falls_back_to_escape_prephase(routing_test_pcb, tmp_path, capsys):
    """Dense package + adaptive Phase 1 would escape 0 pads -> route_with_escape."""
    calls, detects = _run(routing_test_pcb, tmp_path, dense=True, phase1_failure=_PHASE1_FAILS)
    out = capsys.readouterr().out
    assert "route_with_escape" in calls
    assert "route_adaptive" not in calls
    assert "Escape routing: auto-enabled" in out
    assert "Using adaptive multi-resolution grid strategy" not in out
    assert "would escape 0/14 pads of U1" in out
    # The decision prints, so it is evaluated (and announced) exactly once.
    assert out.count("Escape routing: auto-enabled") == 1
    assert detects == 1


def test_phase1_success_keeps_adaptive_branch(routing_test_pcb, tmp_path, capsys):
    """Dense package but adaptive Phase 1 escapes every part -> adaptive, as before."""
    calls, detects = _run(routing_test_pcb, tmp_path, dense=True, phase1_failure=None)
    out = capsys.readouterr().out
    assert calls[:2] == ["preview_phase1", "route_adaptive"]
    assert "route_with_escape" not in calls
    assert "Using adaptive multi-resolution grid strategy" in out
    assert "Escape routing: auto-enabled" not in out
    assert detects == 1


def test_no_dense_package_keeps_adaptive_branch(routing_test_pcb, tmp_path, capsys):
    calls, detects = _run(routing_test_pcb, tmp_path, dense=False)
    out = capsys.readouterr().out
    assert "route_adaptive" in calls
    assert "preview_phase1" not in calls
    assert "route_with_escape" not in calls
    assert "Using adaptive multi-resolution grid strategy" in out
    assert detects == 1


def test_no_escape_routing_flag_keeps_adaptive_branch(routing_test_pcb, tmp_path, capsys):
    """``--no-escape-routing`` opts out of the pre-phase entirely."""
    calls, detects = _run(
        routing_test_pcb,
        tmp_path,
        dense=True,
        phase1_failure=_PHASE1_FAILS,
        extra=["--no-escape-routing"],
    )
    out = capsys.readouterr().out
    assert "route_adaptive" in calls
    assert "preview_phase1" not in calls
    assert "route_with_escape" not in calls
    assert "Using adaptive multi-resolution grid strategy" in out
    assert detects == 0


# ---------------------------------------------------------------------------
# AdaptiveGridRouter.preview_phase1
# ---------------------------------------------------------------------------


def _ssop_board():
    grid, rules = make_grid_and_rules(width=30.0, height=30.0)
    pads = TestAdaptiveGridSSOP()._make_ssop20_pads()
    for p in pads.values():
        grid.add_pad(p)
    return grid, rules, pads


def _cell_state(grid):
    return [
        (cell.blocked, cell.net)
        for layer in range(grid.num_layers)
        for row in range(grid.rows)
        for col in range(grid.cols)
        for cell in (grid.cell_at(layer, row, col),)
    ]


def test_preview_phase1_does_not_touch_the_grid():
    grid, rules, pads = _ssop_board()
    before = _cell_state(grid)
    assert AdaptiveGridRouter(grid, rules).preview_phase1(pads) is None
    assert _cell_state(grid) == before


def test_route_adaptive_after_preview_matches_route_adaptive_alone():
    """The cached preview search is applied exactly as a fresh Phase 1 would be."""
    grid_a, rules_a, pads_a = _ssop_board()
    fresh = AdaptiveGridRouter(grid_a, rules_a)._phase1_pad_escape(pads_a)

    grid_b, rules_b, pads_b = _ssop_board()
    previewed = AdaptiveGridRouter(grid_b, rules_b)
    assert previewed.preview_phase1(pads_b) is None
    reused = previewed._phase1_pad_escape(pads_b)

    assert reused[2] == fresh[2]
    assert reused[0].success_count == fresh[0].success_count > 0
    assert reused[0].unblocked_count == fresh[0].unblocked_count
    assert [(r.net, len(r.segments), len(r.vias)) for r in reused[1]] == [
        (r.net, len(r.segments), len(r.vias)) for r in fresh[1]
    ]
    assert _cell_state(grid_b) == _cell_state(grid_a)
    # The preview is consumed: a second Phase 1 searches afresh.
    assert previewed._phase1_preview is None


def test_preview_phase1_reports_the_hard_fail(monkeypatch):
    grid, rules, pads = _ssop_board()
    adaptive = AdaptiveGridRouter(grid, rules)
    before = _cell_state(grid)

    def _all_fail(analysis):
        from kicad_tools.router.subgrid import SubGridResult

        return SubGridResult(analysis=analysis, failed_pads=[s.pad for s in analysis.off_grid_pads])

    monkeypatch.setattr(adaptive._subgrid, "generate_escape_segments", _all_fail)
    failure = adaptive.preview_phase1(pads)
    assert isinstance(failure, FinePitchEscapeFailure)
    assert failure.component_ref == "U1"
    assert _cell_state(grid) == before
    # route_adaptive would raise the same failure from the cached search.
    with pytest.raises(FinePitchEscapeFailure):
        adaptive._phase1_pad_escape(pads)


# ---------------------------------------------------------------------------
# --grid-strategy through ``kct route``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["uniform", "adaptive"])
def test_grid_strategy_reaches_route_cmd_through_kct_route(strategy):
    """``kct route ... --grid-strategy X`` lands as ``args.grid_strategy == X``
    on the ``route_cmd`` parser (it used to die with "unrecognized
    arguments" because only ``route_cmd`` declared it)."""
    from tests.route_cli import kct_route

    argv = ["board.kicad_pcb", "--grid-strategy", strategy, "--dry-run"]
    forwarded, args = kct_route(argv)
    assert forwarded == argv  # passed through verbatim (issue #7017)
    assert args.grid_strategy == strategy


def test_unset_grid_strategy_is_not_invented():
    from tests.route_cli import kct_route

    forwarded, args = kct_route(["board.kicad_pcb", "--dry-run"])
    assert "--grid-strategy" not in forwarded
    assert args.grid_strategy == "adaptive"
