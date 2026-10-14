"""``route --dry-run`` on the escalation flows routes nothing (Issue #6563).

The #4263 analytical short-circuit sat after every escalation dispatch, so
only the single-attempt path (``--layers N``) reached it.  The default flow
(``--auto-layers``, #2388) and ``--auto-pcb-size`` / ``--auto-mfr-tier`` /
``--adaptive-rules`` routed for real under ``--dry-run`` and wrote checkpoint,
partial and final boards.  These tests run each flow WITHOUT ``--layers`` and
assert: exit 0, the plan is printed, no router entry point is reached, and no
file appears.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli import route_cmd


def _write_board(path: Path) -> Path:
    """A small 2-layer board with an outline and a few SOIC footprints."""
    footprints = []
    for gx in (10, 25, 40):
        for gy in (10, 25):
            pads = "\n".join(
                f'    (pad "{i + 1}" smd rect (at {-1.905 + i * 1.27:.3f} 0) '
                f'(size 0.6 1.5) (layers "F.Cu"))'
                for i in range(4)
            )
            footprints.append(f'  (footprint "SOIC" (layer "F.Cu") (at {gx} {gy})\n{pads}\n  )')
    path.write_text(
        "(kicad_pcb (version 20221018) (generator test)\n"
        "  (general (thickness 1.6))\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (gr_rect (start 0 0) (end 50 35) (layer "Edge.Cuts") (width 0.1))\n'
        + "\n".join(footprints)
        + "\n)\n"
    )
    return path


_ROUTER_ENTRY_POINTS = (
    "route_with_layer_escalation",
    "route_with_size_escalation",
    "route_with_mfr_tier_escalation",
    "route_with_combined_escalation",
    "route_with_rule_relaxation",
)


def _snapshot(directory: Path) -> set[str]:
    return {str(p.relative_to(directory)) for p in directory.rglob("*")}


@pytest.mark.parametrize(
    "extra",
    [
        [],  # the default flow: --auto-layers (#2388)
        ["--auto-layers"],
        ["--auto-pcb-size"],
        ["--auto-mfr-tier"],
        ["--adaptive-rules"],
        ["--auto-layers", "--adaptive-rules"],
    ],
    ids=["default", "auto-layers", "auto-pcb-size", "auto-mfr-tier", "adaptive-rules", "combined"],
)
def test_dry_run_escalation_flow_prints_plan_and_routes_nothing(tmp_path, capsys, extra):
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    output = tmp_path / "board_routed.kicad_pcb"
    before = _snapshot(tmp_path)

    def _explode(name):
        def _f(*a, **k):
            raise AssertionError(f"{name} must not run under --dry-run")

        return _f

    patches = [
        patch.object(route_cmd, name, side_effect=_explode(name)) for name in _ROUTER_ENTRY_POINTS
    ]
    patches.append(
        patch(
            "kicad_tools.router.io.load_pcb_for_routing",
            side_effect=_explode("load_pcb_for_routing"),
        )
    )
    for p in patches:
        p.start()
    try:
        start = time.perf_counter()
        rc = route_cmd.main([str(pcb), "-o", str(output), "--dry-run", "--grid", "0.1", *extra])
        elapsed = time.perf_counter() - start
    finally:
        for p in patches:
            p.stop()

    assert rc == 0
    assert elapsed < 10.0
    out = capsys.readouterr().out
    assert "Layer Escalation Mode" not in out
    assert "Dry run: routing plan" in out
    assert "Analytical Grid Plan" in out
    assert _snapshot(tmp_path) == before
    assert not output.exists()


def test_default_dry_run_reports_each_ladder_rung(tmp_path, capsys):
    """The default flow prints one plan per layer count on the ladder."""
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    with patch.object(
        route_cmd,
        "route_with_layer_escalation",
        side_effect=AssertionError("must not route under --dry-run"),
    ):
        rc = route_cmd.main([str(pcb), "--dry-run", "--grid", "0.1", "--max-layers", "4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Layer counts:   2, 4" in out
    assert "[2-layer rung]" in out
    assert "[4-layer rung]" in out


def test_layers_flag_single_attempt_writes_nothing(tmp_path, capsys):
    """``--layers N`` prints one plan and no longer stages a board at --output.

    Before #6563 the single-attempt path staged the input at ``--output`` for
    auto-pour before reaching the #4263 short-circuit.
    """
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    before = _snapshot(tmp_path)
    with patch(
        "kicad_tools.router.io.load_pcb_for_routing",
        side_effect=AssertionError("must not load under --dry-run"),
    ):
        rc = route_cmd.main([str(pcb), "--dry-run", "--grid", "0.1", "--layers", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Flow:           single attempt" in out
    assert out.count("Analytical Grid Plan") == 1
    assert _snapshot(tmp_path) == before


def test_dry_run_still_refuses_grid_coarser_than_clearance(tmp_path, capsys):
    """The explicit-grid vs clearance refusal still applies to a dry run."""
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    rc = route_cmd.main([str(pcb), "--dry-run", "--grid", "0.25", "--clearance", "0.127"])
    assert rc == 1
    assert "exceeds clearance" in capsys.readouterr().err


def test_checkpoint_callback_is_disabled_under_dry_run(tmp_path):
    """Defense in depth: no checkpoint writer exists for a dry run."""
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    cb = route_cmd._make_checkpoint_callback(
        pcb, tmp_path / "out.kicad_pcb", 30.0, quiet=True, dry_run=True
    )
    assert cb is None
    # Sanity: the same call without dry_run does build a callback.
    assert route_cmd._make_checkpoint_callback(pcb, tmp_path / "out.kicad_pcb", 30.0) is not None


def test_save_partial_results_writes_nothing_under_dry_run(tmp_path, monkeypatch):
    """Defense in depth: an interrupted dry run saves no partial board."""
    pcb = _write_board(tmp_path / "board.kicad_pcb")
    before = _snapshot(tmp_path)
    state = dict(route_cmd._interrupt_state)
    state.update(
        {
            "router": MagicMock(),
            "output_path": tmp_path / "out.kicad_pcb",
            "pcb_path": pcb,
            "quiet": True,
            "dry_run": True,
        }
    )
    monkeypatch.setattr(route_cmd, "_interrupt_state", state)
    assert route_cmd._save_partial_results() is False
    assert _snapshot(tmp_path) == before
