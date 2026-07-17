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
"""

from unittest.mock import patch

from kicad_tools.cli import route_cmd
from kicad_tools.router.io import GridAutoSelection, PadPosition


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
