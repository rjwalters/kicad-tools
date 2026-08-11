"""Issue #3911: the router CLI must refuse a memory-forced unsafe auto-grid.

``auto_select_grid_resolution`` sets ``memory_forced_unsafe_grid`` True when
the memory budget cap coerces the routing grid coarser than ``clearance / 2``
while a finer, clearance-safe candidate existed.  The router's own A*
pathfinder rejects such a grid (``min_res = clearance / 2``), so routing on it
reliably produces cross-net clearance shorts (board 05: NRST<->OSC_IN,
PWM_AH<->OSC_OUT vias).  The CLI gate refuses to route in that case unless the
caller explicitly opts in with ``--allow-unsafe-grid`` or ``--force``.

These tests drive ``route_cmd.main()`` up to the gate by stubbing the
grid-analysis helpers (a real 200x200mm route would take minutes and the gate
fires long before any copper is placed).

Issue #4690 extends this file to the *rest* of the grid advisories that used to
contradict the #4271 gate-skip line in the same log: the auto-grid memory-cap
``UserWarning``, the ``validate_grid_resolution`` ``UserWarning``, and the
fine-pitch component analysis ("N pads off-grid", "Use finer grid: --grid ...",
"Routing quality will be degraded").  All three are now gated on the engine
actually routing on the grid; all three must still fire for ``--route-engine
grid``.
"""

import logging
import re
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.router.io import (
    DesignRules,
    GridAutoSelection,
    PadPosition,
    auto_select_grid_resolution,
    compute_multi_resolution_plan,
    load_pcb_for_routing,
)

# Engines that never emit copper from the grid (#4271/#4283), so every
# grid-quality advisory is inapplicable to them.
_NON_GRID_ENGINES = ["lattice", "mesh"]


class _GateReached(Exception):
    """Sentinel raised just past the gate to prove routing was allowed."""


def _unsafe_selection() -> GridAutoSelection:
    """A selection matching the board-05 memory-coerced unsafe grid."""
    return GridAutoSelection(
        resolution=0.1,
        off_grid_pads=0,
        total_pads=2,
        off_grid_percentage=0.0,
        candidates_tried=[(0.1, 0)],
        memory_capped=True,
        uncapped_resolution=0.065,
        origin_offset=(0.0, 0.0),
        clearance_compliant_at_clearance_over_2=False,
        memory_budget_used=4_000_000,
        lattice_rescued=False,
        memory_forced_unsafe_grid=True,
    )


def _safe_selection() -> GridAutoSelection:
    """A selection that reaches clearance/2 -- the gate must NOT fire."""
    return GridAutoSelection(
        resolution=0.05,
        off_grid_pads=0,
        total_pads=2,
        off_grid_percentage=0.0,
        candidates_tried=[(0.05, 0)],
        memory_capped=False,
        uncapped_resolution=None,
        origin_offset=(0.0, 0.0),
        clearance_compliant_at_clearance_over_2=True,
        memory_budget_used=500_000,
        lattice_rescued=False,
        memory_forced_unsafe_grid=False,
    )


def _run_main_to_gate(tmp_path, selection, extra_args):
    """Invoke route_cmd.main() with grid analysis stubbed to ``selection``.

    Returns (exit_code, gate_reached) where gate_reached is True if control
    passed the safety gate (a sentinel raised just downstream is caught).
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")

    pads = [PadPosition(x=10.0, y=10.0), PadPosition(x=20.0, y=10.0)]

    gate_reached = {"value": False}

    def _sentinel(*_args, **_kwargs):
        gate_reached["value"] = True
        raise _GateReached

    with (
        patch("kicad_tools.router.io.extract_pad_positions", return_value=pads),
        patch("kicad_tools.router.io.extract_board_dimensions", return_value=(200.0, 200.0)),
        patch("kicad_tools.router.io.auto_select_grid_resolution", return_value=selection),
        patch("kicad_tools.router.io.compute_multi_resolution_plan", return_value=None),
        patch("kicad_tools.router.io.load_pads_for_analysis", return_value=pads),
        # First substantive step past the gate -- raise to short-circuit the
        # (slow) real routing while proving the gate was bypassed.
        patch.object(route_cmd, "_resolve_starting_layers", side_effect=_sentinel),
    ):
        try:
            code = route_cmd.main([str(pcb), "--quiet", *extra_args])
        except _GateReached:
            code = None
    return code, gate_reached["value"]


def test_gate_refuses_memory_forced_unsafe_grid(tmp_path, capsys):
    """Without opt-in, the memory-forced unsafe grid is refused with exit 1."""
    code, gate_reached = _run_main_to_gate(tmp_path, _unsafe_selection(), [])

    assert code == 1, "Expected the safety gate to refuse routing (exit 1)"
    assert gate_reached is False, "Routing must be refused BEFORE placing copper"

    err = capsys.readouterr().err
    assert "clearance/2" in err
    assert "0.1mm" in err  # names the offending grid
    # The message must explain the safer alternatives and the opt-in.
    assert "--allow-unsafe-grid" in err
    assert "unrouted net is strictly safer than a short" in err


def test_allow_unsafe_grid_opt_in_permits_routing(tmp_path):
    """--allow-unsafe-grid lets the router proceed past the gate."""
    code, gate_reached = _run_main_to_gate(tmp_path, _unsafe_selection(), ["--allow-unsafe-grid"])
    assert gate_reached is True, "--allow-unsafe-grid must bypass the gate"
    assert code is None  # sentinel fired downstream of the gate


def test_force_flag_also_permits_routing(tmp_path):
    """--force (the pre-existing override) also bypasses the gate."""
    code, gate_reached = _run_main_to_gate(tmp_path, _unsafe_selection(), ["--force"])
    assert gate_reached is True, "--force must bypass the gate"
    assert code is None


def test_safe_grid_never_triggers_gate(tmp_path):
    """A clearance-safe auto-grid routes normally with no opt-in required."""
    code, gate_reached = _run_main_to_gate(tmp_path, _safe_selection(), [])
    assert gate_reached is True, "A safe grid must not be gated"
    assert code is None


def test_lattice_engine_bypasses_gate(tmp_path):
    """--route-engine lattice must NOT be refused by the unsafe-grid gate.

    Issue #4271: the lattice engine never emits copper from the grid (the
    grid object is a coordinate substrate only), so the #3911 refusal is
    grid-engine-only.  Softstart rev-C falls in the #4242 grid gap -- the
    gate used to block exactly the boards the lattice engine exists for.
    """
    code, gate_reached = _run_main_to_gate(
        tmp_path,
        _unsafe_selection(),
        ["--route-engine", "lattice", "--strategy", "basic"],
    )
    assert gate_reached is True, "lattice engine must bypass the grid safety gate"
    assert code is None


def test_mesh_engine_bypasses_gate(tmp_path):
    """--route-engine mesh equally never routes on the grid (issue #4271)."""
    code, gate_reached = _run_main_to_gate(
        tmp_path,
        _unsafe_selection(),
        ["--route-engine", "mesh", "--strategy", "basic"],
    )
    assert gate_reached is True, "mesh engine must bypass the grid safety gate"
    assert code is None


def test_grid_engine_gate_unchanged_by_engine_bypass(tmp_path, capsys):
    """The explicit grid engine still refuses -- #3911 behavior intact."""
    code, gate_reached = _run_main_to_gate(
        tmp_path, _unsafe_selection(), ["--route-engine", "grid"]
    )
    assert code == 1
    assert gate_reached is False
    assert "--allow-unsafe-grid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Issue #4690: the auto-grid memory-cap UserWarning
# ---------------------------------------------------------------------------
#
# The board below is the #3911 reproducer from
# ``tests/test_grid_auto_selection.py`` -- 20 pads on a 0.127mm imperial
# lattice (so the #3441 rescue candidate does not help and never fires) plus
# one 0.5mm-pitch pair that makes the board read as fine-pitch.  The memory
# cap therefore coerces a grid > clearance/2 with a finer safe candidate
# available: exactly the state that emits "memory budget cap forces".


def _memory_capped_fine_pitch_pads() -> list[PadPosition]:
    pads = [PadPosition(x=10.0 + i * 1.27 + 0.0635, y=20.0) for i in range(20)]
    pads += [PadPosition(x=50.0, y=60.0), PadPosition(x=50.5, y=60.0)]
    return pads


def _select_memory_capped(**kwargs) -> tuple[GridAutoSelection, list[str]]:
    """Run the selector on the memory-capped board, capturing warning texts."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = auto_select_grid_resolution(
            _memory_capped_fine_pitch_pads(),
            clearance=0.15,
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
            candidates=[0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508],
            **kwargs,
        )
    return result, [str(w.message) for w in caught]


def test_memory_cap_warning_fires_for_default_engine():
    """Callers that do not pass ``engine`` keep the pre-#4690 behavior."""
    result, texts = _select_memory_capped()
    assert result.memory_forced_unsafe_grid is True
    assert any("memory budget cap forces" in t for t in texts), texts
    assert any("Routing may produce clearance violations" in t for t in texts), texts


def test_memory_cap_warning_fires_for_explicit_grid_engine():
    """``engine="grid"`` is byte-identical to the default."""
    result, texts = _select_memory_capped(engine="grid")
    assert result.memory_forced_unsafe_grid is True
    assert any("memory budget cap forces" in t for t in texts), texts


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_memory_cap_warning_suppressed_for_non_grid_engines(engine):
    """No copper comes off the grid, so the clearance alarm must not fire.

    The alarm both predicts a grid-routing failure ("may produce clearance
    violations at fine-pitch pads") and prescribes a grid-routing remedy
    ("increase max_cells"), directly contradicting the #4271 gate-skip line
    printed in the same log -- and the remedy is actively harmful (it steers
    the user toward a multi-million-cell grid the #4242 cap refuses).
    """
    _result, texts = _select_memory_capped(engine=engine)
    assert not any("memory budget cap forces" in t for t in texts), texts
    assert not any("Routing may produce clearance violations" in t for t in texts), texts


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_memory_forced_unsafe_grid_flag_still_computed_for_non_grid(engine):
    """Suppression is diagnostics-only: the #3911 flag is engine-invariant.

    The #4271 gate-skip line is printed *because* this flag is True, so
    clearing it would silently delete the very message this issue keeps.
    """
    result, _texts = _select_memory_capped(engine=engine)
    assert result.memory_forced_unsafe_grid is True
    assert result.resolution == _select_memory_capped(engine="grid")[0].resolution


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_memory_cap_event_still_observable_at_info_for_non_grid(engine, caplog):
    """Suppressed, not silenced: the event is demoted to an INFO log."""
    with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
        _select_memory_capped(engine=engine)
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "memory budget cap selected grid" in m and f"--route-engine {engine}" in m for m in messages
    ), messages


# ---------------------------------------------------------------------------
# Issue #4765: the sibling #3441 lattice-rescue warning gets the same gate
#
# NOTE: ``lattice_rescued`` is the #3441 PAD-LATTICE GRID rescue (grid
# alignment to the board's dominant pad lattice).  It has nothing to do with
# ``--route-engine lattice``.
# ---------------------------------------------------------------------------


def _lattice_rescue_pads() -> list[PadPosition]:
    """The #3441 reproducer: a dominant 0.1mm lattice + a small off-lattice
    cluster, so the rescue adopts 0.1mm (> clearance/2 = 0.075mm)."""
    pads = [PadPosition(x=10.0 + i * 0.7, y=20.0 + j * 0.9) for i in range(10) for j in range(6)]
    pads += [PadPosition(x=60.635 + k * 1.27, y=50.635) for k in range(6)]
    return pads


def _select_lattice_rescued(**kwargs) -> tuple[GridAutoSelection, list[str]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = auto_select_grid_resolution(
            _lattice_rescue_pads(),
            clearance=0.15,
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
            candidates=[0.5, 0.25, 0.127, 0.1, 0.065, 0.05, 0.0508],
            **kwargs,
        )
    return result, [str(w.message) for w in caught]


def test_lattice_rescue_warning_fires_for_grid_engine():
    """The #3441 heads-up is unchanged where it applies."""
    result, texts = _select_lattice_rescued(engine="grid")
    assert result.lattice_rescued is True
    assert any("lattice rescue selected grid" in t for t in texts), texts
    assert any("Quantisation margin is reduced at fine-pitch pads" in t for t in texts), texts


def test_lattice_rescue_warning_fires_for_default_engine():
    """Callers that do not pass ``engine`` keep the pre-#4765 behavior."""
    _result, texts = _select_lattice_rescued()
    assert any("Quantisation margin is reduced at fine-pitch pads" in t for t in texts), texts


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_lattice_rescue_warning_suppressed_for_non_grid_engines(engine):
    """The reduced-quantisation-margin prediction is a GRID failure mode.

    Under mesh/lattice no copper comes off the grid, so predicting reduced
    quantisation margin at fine-pitch pads contradicts the gate-skip line in
    the same log -- the identical premise #4690 accepted for the sibling
    memory-cap branch.
    """
    result, texts = _select_lattice_rescued(engine=engine)
    assert result.lattice_rescued is True
    assert not any("lattice rescue selected grid" in t for t in texts), texts
    assert not any("Quantisation margin is reduced" in t for t in texts), texts


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_lattice_rescue_event_still_observable_at_info(engine, caplog):
    """Suppressed, not silenced."""
    with caplog.at_level(logging.INFO, logger="kicad_tools.router.io"):
        _select_lattice_rescued(engine=engine)
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "lattice rescue selected grid" in m and f"--route-engine {engine}" in m for m in messages
    ), messages


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_lattice_rescue_selection_is_engine_invariant(engine):
    """Diagnostics-only: every returned field matches the grid engine's."""
    grid_result, _ = _select_lattice_rescued(engine="grid")
    other_result, _ = _select_lattice_rescued(engine=engine)
    assert other_result == grid_result


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_multi_resolution_plan_forwards_engine_to_selector(engine):
    """``compute_multi_resolution_plan`` is the adaptive-strategy call path."""
    pads = _memory_capped_fine_pitch_pads()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compute_multi_resolution_plan(
            pads=pads,
            clearance=0.15,
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
            engine=engine,
        )
    texts = [str(w.message) for w in caught]
    assert not any("memory budget cap forces" in t for t in texts), texts


def test_multi_resolution_plan_warns_for_grid_engine():
    """The same call on the grid engine still emits the advisory."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compute_multi_resolution_plan(
            pads=_memory_capped_fine_pitch_pads(),
            clearance=0.15,
            board_width=100.0,
            board_height=100.0,
            max_cells=500_000,
        )
    texts = [str(w.message) for w in caught]
    assert any("memory budget cap forces" in t for t in texts), texts


# ---------------------------------------------------------------------------
# Issue #4690: the validate_grid_resolution UserWarning at the load site
# ---------------------------------------------------------------------------

_FINE_PITCH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.3 0.3) (layers "F.Cu") (net 0 ""))
  )
)"""


def _load_with_strategy(tmp_path, **kwargs) -> list[str]:
    """Load a fine-pitch board on a grid > clearance/2, capturing warnings."""
    pcb_file = tmp_path / "fine_pitch.kicad_pcb"
    pcb_file.write_text(_FINE_PITCH_PCB)
    # grid=0.15 > clearance/2 (0.1) but <= clearance (0.2): the advisory band.
    rules = DesignRules(grid_resolution=0.15, trace_clearance=0.2)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        load_pcb_for_routing(
            str(pcb_file),
            rules=rules,
            validate_drc=True,
            strict_drc=False,
            **kwargs,
        )
    return [str(w.message) for w in caught]


def test_load_grid_resolution_warning_fires_for_grid_strategy(tmp_path):
    """Default (grid) strategy keeps the #3942 fine-pitch heads-up."""
    texts = _load_with_strategy(tmp_path)
    assert any("may cause clearance violations" in t for t in texts), texts


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_load_grid_resolution_warning_suppressed_for_non_grid(tmp_path, engine):
    """``strategy`` IS ``--route-engine``; mesh/lattice do not route on the grid."""
    texts = _load_with_strategy(tmp_path, strategy=engine)
    assert not any("may cause clearance violations" in t for t in texts), texts


# ---------------------------------------------------------------------------
# Issue #4690: the fine-pitch component analysis block in route_cmd
# ---------------------------------------------------------------------------

_OFF_GRID_ROUTABLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 100 100) (end 130 120) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (footprint "U1"
    (layer "F.Cu")
    (at 105 105)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0.03 0.03) (size 0.3 0.3) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.53 0.03) (size 0.3 0.3) (layers "F.Cu") (net 2 "NET2"))
  )
  (footprint "U2"
    (layer "F.Cu")
    (at 120 115)
    (fp_text reference "U2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0.03 0.03) (size 0.3 0.3) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 0.53 0.03) (size 0.3 0.3) (layers "F.Cu") (net 2 "NET2"))
  )
)"""

# Issue #4765: the same board with every pad ON the 0.15mm grid, so
# ``analyze_fine_pitch_components`` reports ``has_warnings is False`` and the
# GRID path prints nothing at all.  The non-grid path must print nothing
# either -- announcing the skip of an analysis that had nothing to say is
# noise the grid engine never emits.
_ON_GRID_COARSE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 100 100) (end 130 120) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (footprint "U1"
    (layer "F.Cu")
    (at 105 105)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 1.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "NET2"))
  )
  (footprint "U2"
    (layer "F.Cu")
    (at 120 115.5)
    (fp_text reference "U2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 1.5 0) (size 0.6 0.6) (layers "F.Cu") (net 2 "NET2"))
  )
)"""

# Every grid-quality string the lattice log must not contain.
_GRID_ADVISORY_MARKERS = [
    "Fine-Pitch Component Analysis",
    "Fine-pitch components detected",
    "pads off-grid",
    "Use finer grid",
    "Routing quality will be degraded",
    "for better pad alignment",
]


def _route_cli(tmp_path, extra_args, board: str = _OFF_GRID_ROUTABLE_PCB) -> tuple[int, list[str]]:
    """Run ``route_cmd.main`` past the fine-pitch block on a tiny board.

    ``--layers 2`` pins the fixed-layer path (``--auto-layers`` diverts to
    ``route_with_layer_escalation``, which never reaches the block under test).
    Returns the exit code and the texts of any ``warnings.warn`` emissions.
    """
    pcb_file = tmp_path / "off_grid.kicad_pcb"
    pcb_file.write_text(board)
    out = tmp_path / "routed.kicad_pcb"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code = route_cmd.main(
            [
                str(pcb_file),
                "-o",
                str(out),
                "--grid",
                "0.15",
                "--clearance",
                "0.2",
                "--layers",
                "2",
                *extra_args,
            ]
        )
    return code, [str(w.message) for w in caught]


def test_fine_pitch_advisories_fire_for_grid_engine(tmp_path, capsys):
    """The default grid engine keeps every advisory (#2254 behavior intact)."""
    _route_cli(tmp_path, [])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for marker in _GRID_ADVISORY_MARKERS:
        assert marker in combined, f"grid engine lost advisory {marker!r}"
    assert "skipped for --route-engine" not in combined


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_fine_pitch_advisories_suppressed_for_non_grid_engines(tmp_path, engine, capsys):
    """The whole grid-compatibility report is skipped, with a reason line.

    This is the contradiction the issue reported: the same log said "no copper
    is routed on the grid" and then recommended ``--grid 0.0635``.
    """
    _code, warn_texts = _route_cli(tmp_path, ["--route-engine", engine, "--strategy", "basic"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for marker in _GRID_ADVISORY_MARKERS:
        assert marker not in combined, f"{engine} engine still emitted {marker!r}"
    assert f"Fine-pitch grid analysis: skipped for --route-engine {engine}" in combined
    # ...and no grid-resolution UserWarning escaped either (io.py site).
    assert not any("may cause clearance violations" in t for t in warn_texts), warn_texts


@pytest.mark.parametrize("engine", ["grid", *_NON_GRID_ENGINES])
def test_quiet_emits_no_fine_pitch_output_on_any_engine(tmp_path, engine, capsys):
    """--quiet behavior is unchanged: neither branch prints (#4690 AC)."""
    extra = ["--quiet", "--route-engine", engine]
    if engine != "grid":
        extra += ["--strategy", "basic"]
    _route_cli(tmp_path, extra)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for marker in [*_GRID_ADVISORY_MARKERS, "Fine-pitch grid analysis: skipped"]:
        assert marker not in combined, f"--quiet leaked {marker!r} on {engine}"


# ---------------------------------------------------------------------------
# Issue #4765: the skip line must not out-talk the grid path it replaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_skip_line_absent_when_the_board_has_no_fine_pitch_findings(tmp_path, engine, capsys):
    """A clean board prints nothing on the grid path -- so nothing here either."""
    _route_cli(
        tmp_path,
        ["--route-engine", engine, "--strategy", "basic"],
        board=_ON_GRID_COARSE_PCB,
    )
    combined = "".join(capsys.readouterr())
    # Sanity: the run really did reach the fine-pitch block.
    assert "Total nets:" in combined
    assert "Fine-pitch grid analysis: skipped" not in combined


def test_clean_board_grid_output_is_unchanged(tmp_path, capsys):
    """The control: the grid engine says nothing about a clean board either."""
    _route_cli(tmp_path, [], board=_ON_GRID_COARSE_PCB)
    combined = "".join(capsys.readouterr())
    for marker in _GRID_ADVISORY_MARKERS:
        assert marker not in combined, f"clean board emitted {marker!r} on the grid engine"


@pytest.mark.parametrize("engine", _NON_GRID_ENGINES)
def test_skip_line_still_prints_when_findings_exist(tmp_path, engine, capsys):
    """...and the line keeps its exact wording when there IS something to skip."""
    _route_cli(tmp_path, ["--route-engine", engine, "--strategy", "basic"])
    combined = "".join(capsys.readouterr())
    assert (
        f"Fine-pitch grid analysis: skipped for --route-engine {engine} (pads are "
        f"reached by exact geometry, not by grid alignment)" in combined.replace("\n", " ")
    )


# ---------------------------------------------------------------------------
# Issue #4765: one normalization helper for every ``route_engine`` read
# ---------------------------------------------------------------------------


class TestResolveRouteEngine:
    def test_missing_attribute_defaults_to_grid(self):
        assert route_cmd._resolve_route_engine(SimpleNamespace()) == "grid"

    def test_none_normalizes_to_grid(self):
        """The inconsistency this helper removes: six sites returned None here."""
        assert route_cmd._resolve_route_engine(SimpleNamespace(route_engine=None)) == "grid"

    def test_empty_string_normalizes_to_grid(self):
        assert route_cmd._resolve_route_engine(SimpleNamespace(route_engine="")) == "grid"

    @pytest.mark.parametrize("engine", ["grid", "mesh", "lattice"])
    def test_explicit_engine_is_returned_verbatim(self, engine):
        assert route_cmd._resolve_route_engine(SimpleNamespace(route_engine=engine)) == engine

    def test_no_bare_route_engine_reads_remain(self):
        """Every read goes through the helper.

        The only surviving ``getattr(args, "route_engine", ...)`` calls are the
        helper's own body and the ``--complete`` engine-selection block, which
        compares against ``parser.get_default("route_engine")`` to detect "the
        user did not choose" -- a different question that must NOT be
        normalized away.
        """
        source = Path(route_cmd.__file__).read_text()
        reads = re.findall(r'getattr\(args, "route_engine"[^)]*\)', source)
        assert reads == [
            'getattr(args, "route_engine", "grid")',
            'getattr(args, "route_engine", engine_default)',
        ], reads
