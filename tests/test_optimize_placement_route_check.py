"""``kct optimize-placement --route-check`` (issue #6234).

The calibration studies (#5948) found no pre-route score that reliably ranks
placements by routability, so ``--route-check`` confirms the optimizer's
result with bounded real routes and keeps the incumbent (the board as read)
when a candidate routes worse.

The pure parts are covered with a fake router: the acceptance rule
:func:`route_check_accepts`, the staged chain (skip, cache, snap-on-incumbent,
at most three routes, locked parts), the route argv and the deadline marker.
The CLI wiring is covered with the real optimizer and a fake router.

``test_route_check_rejects_wirelength_better_move`` is the acceptance test,
run with the real router on fleet board 01 (voltage divider: four parts,
three nets, two layers). On a board that small and open, every placement the
optimizer scores as *feasible* routes completely: there is room for any
detour, so no clearance-legal move can lose a net. A move that is better on
wirelength but less routable therefore has to give up the optimizer's own
clearance. ``_CRAMMED_MOVE`` drops R1, turned 180 degrees, onto connector
J1. That is the kind of cram a wirelength-only objective likes: the
optimizer's centre-anchored wirelength falls from 58.0 to 49.5 mm, but
R1's VOUT pad lands on J1's VIN pad. Measured at the default budget, the
board as read routes 3/3 nets with 0 DRC errors and the cram routes 1/3
with 5. The gate rejects the cram and keeps the incumbent.

Other measured moves on the same board (budget 100,000, seed 42):

* R1 and R2 each pulled 1.6 mm toward the middle: wirelength 58.0 -> 51.6,
  feasible, 3/3 nets and 0 DRC errors. Accepted, so the gate does not
  block a genuine improvement.
* R2 pushed onto J2's GND pad: wirelength 58.0 -> 51.0, 3/3 nets but 1 DRC
  error. Rejected on the DRC count.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.cli import optimize_placement_cmd as opc
from kicad_tools.placement import route_check as rc
from kicad_tools.placement.route_check import (
    DEFAULT_ROUTE_CHECK_BUDGET,
    RouteCheckOutcome,
    route_check_accepts,
    run_route_check_chain,
)
from kicad_tools.placement.vector import PlacementVector

REPO = Path(__file__).resolve().parents[1]
BOARD_01 = REPO / "boards" / "01-voltage-divider" / "output" / "voltage_divider.kicad_pcb"


def _outcome(routed: int, requested: int = 10, drc: int | None = 0, **kw) -> RouteCheckOutcome:
    return RouteCheckOutcome(routed, requested, drc, 0, kw.pop("wall", 1.0), **kw)


def _vec(*values: float) -> PlacementVector:
    return PlacementVector(data=np.asarray(values, dtype=np.float64))


# ---------------------------------------------------------------------------
# The acceptance rule
# ---------------------------------------------------------------------------


class TestRouteCheckAccepts:
    def test_equal_is_accepted(self):
        assert route_check_accepts(_outcome(9, drc=2), _outcome(9, drc=2))

    def test_better_completion_and_fewer_drc_is_accepted(self):
        assert route_check_accepts(_outcome(8, drc=3), _outcome(10, drc=0))

    def test_lower_completion_is_rejected(self):
        assert not route_check_accepts(_outcome(10), _outcome(9))

    def test_more_drc_errors_is_rejected_even_with_better_completion(self):
        assert not route_check_accepts(_outcome(8, drc=0), _outcome(10, drc=1))

    def test_unmeasured_candidate_is_rejected(self):
        assert not route_check_accepts(_outcome(5), _outcome(0, error="no route summary"))

    def test_unmeasured_incumbent_rejects_everything(self):
        assert not route_check_accepts(_outcome(0, error="crash"), _outcome(10))

    def test_drc_unknown_on_one_side_is_rejected(self):
        assert not route_check_accepts(_outcome(9, drc=0), _outcome(10, drc=None))
        assert not route_check_accepts(_outcome(9, drc=None), _outcome(10, drc=0))

    def test_drc_unknown_on_both_sides_compares_completion(self):
        assert route_check_accepts(_outcome(9, drc=None), _outcome(10, drc=None))
        assert not route_check_accepts(_outcome(10, drc=None), _outcome(9, drc=None))

    def test_nothing_to_route_counts_as_complete(self):
        assert _outcome(0, requested=0).completion == 1.0


# ---------------------------------------------------------------------------
# The staged chain, with a fake router
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Routes a vector by looking its first coordinate up in *table*."""

    def __init__(self, table: dict[float, RouteCheckOutcome]):
        self.table = table
        self.calls: list[float] = []

    def __call__(self, vec: PlacementVector) -> RouteCheckOutcome:
        key = float(vec.data[0])
        self.calls.append(key)
        return self.table[key]


def _no_snap(vec: PlacementVector) -> PlacementVector:
    return vec


class TestRouteCheckChain:
    def test_regressing_optimizer_result_keeps_the_incumbent(self):
        incumbent, stage_a = _vec(0, 0, 0, 0), _vec(1, 0, 0, 0)
        router = _FakeRouter({0.0: _outcome(10), 1.0: _outcome(9)})
        vec, report = run_route_check_chain(incumbent, stage_a, _no_snap, router, budget=7)
        assert vec is incumbent
        assert report.accepted_stage == "incumbent"
        assert [s.accepted for s in report.stages] == [False, False]
        assert report.stages[1].skipped  # no snap move -> no route
        assert report.routes_run == 2
        assert report.as_dict()["budget"] == 7

    def test_snap_is_gated_against_the_accepted_stage_a(self):
        incumbent, stage_a, snapped = _vec(0, 0, 0, 0), _vec(1, 0, 0, 0), _vec(2, 0, 0, 0)
        router = _FakeRouter({0.0: _outcome(8), 1.0: _outcome(9), 2.0: _outcome(10)})

        def snap(vec):
            assert vec is stage_a  # B is built on the accepted A
            return snapped

        vec, report = run_route_check_chain(incumbent, stage_a, snap, router, budget=1)
        assert vec is snapped
        assert report.accepted_stage == rc.STAGE_DECOUPLING_SNAP
        # Incumbent, A and B each routed once; A's result is reused as B's
        # incumbent from the cache, so this is the 3-route ceiling.
        assert router.calls == [0.0, 1.0, 2.0]

    def test_snap_only_candidate_when_stage_a_is_rejected(self):
        incumbent, stage_a, snapped = _vec(0, 0, 0, 0), _vec(1, 0, 0, 0), _vec(2, 0, 0, 0)
        router = _FakeRouter({0.0: _outcome(9), 1.0: _outcome(5), 2.0: _outcome(9)})
        seen = []

        def snap(vec):
            seen.append(vec)
            return snapped

        vec, report = run_route_check_chain(incumbent, stage_a, snap, router, budget=1)
        assert seen == [incumbent]  # snap applied to the kept incumbent
        assert vec is snapped
        assert [s.accepted for s in report.stages] == [False, True]
        assert router.calls == [0.0, 1.0, 2.0]

    def test_rejected_snap_keeps_stage_a(self):
        incumbent, stage_a, snapped = _vec(0, 0, 0, 0), _vec(1, 0, 0, 0), _vec(2, 0, 0, 0)
        router = _FakeRouter({0.0: _outcome(9), 1.0: _outcome(9, drc=1), 2.0: _outcome(9, drc=2)})
        vec, report = run_route_check_chain(incumbent, stage_a, lambda v: snapped, router, budget=1)
        # A has more DRC errors than the incumbent: rejected. The snap of the
        # incumbent then also has more: rejected. Incumbent kept.
        assert vec is incumbent
        assert report.accepted_stage == "incumbent"

    def test_nothing_changed_routes_nothing(self):
        incumbent = _vec(0, 0, 0, 0)
        router = _FakeRouter({})
        vec, report = run_route_check_chain(incumbent, _vec(0, 0, 0, 0), _no_snap, router, budget=1)
        assert vec is incumbent
        assert router.calls == []
        assert report.routes_run == 0
        assert all(s.skipped for s in report.stages)

    def test_non_reproducible_route_is_warned(self):
        router = _FakeRouter({0.0: _outcome(9), 1.0: _outcome(9, reproducible=False)})
        _, report = run_route_check_chain(
            _vec(0, 0, 0, 0), _vec(1, 0, 0, 0), _no_snap, router, budget=1
        )
        assert report.warnings and "not reproducible" in report.warnings[0]

    def test_a_candidate_that_moves_a_locked_part_is_refused(self):
        # Component 1 (data[4:8]) is locked; stage A moved it.
        incumbent = _vec(0, 0, 0, 0, 5, 5, 0, 0)
        moved = _vec(1, 0, 0, 0, 6, 5, 0, 0)
        router = _FakeRouter({0.0: _outcome(9), 1.0: _outcome(10)})
        with pytest.raises(AssertionError, match="locked"):
            run_route_check_chain(incumbent, moved, _no_snap, router, budget=1, locked_indices=[1])


# ---------------------------------------------------------------------------
# Route invocation details
# ---------------------------------------------------------------------------


def test_route_argv_is_iteration_bounded_and_seeded(tmp_path):
    argv = rc.route_check_argv(
        tmp_path / "a.kicad_pcb",
        tmp_path / "b.kicad_pcb",
        budget=1234,
        seed=7,
        timeout_s=900,
        manufacturer="jlcpcb",
        layers=2,
    )
    joined = " ".join(argv)
    assert "--deterministic-budget" in argv
    assert "--deterministic-rescue" in argv
    assert "--per-net-iterations 1234" in joined
    assert "--seed 7" in joined
    assert "--no-placement-feedback" in argv  # the router never moves parts
    assert "--starting-layers 2 --max-layers 2" in joined
    assert "--mfr jlcpcb" in joined


def test_deadline_marker_matches_only_the_router_log_line():
    advisory = (
        "[deterministic-budget] WARNING: --timeout 900s is set. ... a "
        "'[deterministic-budget] stage deadline fired' log line will mark ..."
    )
    assert not rc.deadline_fired(advisory)
    fired = advisory + "\n  [deterministic-budget] stage deadline fired -- run is not reproducible"
    assert rc.deadline_fired(fired)


def test_parser_flags_and_default_budget():
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(["optimize-placement", "board.kicad_pcb"])
    assert args.route_check is False
    # parser.py carries the default as a literal to stay import-light.
    assert args.route_check_budget == DEFAULT_ROUTE_CHECK_BUDGET
    args = parser.parse_args(
        ["optimize-placement", "board.kicad_pcb", "--route-check", "--route-check-budget", "500"]
    )
    assert args.route_check is True and args.route_check_budget == 500


# ---------------------------------------------------------------------------
# CLI wiring (real optimizer, fake router)
# ---------------------------------------------------------------------------


@pytest.fixture
def board01(tmp_path) -> Path:
    dst = tmp_path / "voltage_divider.kicad_pcb"
    shutil.copyfile(BOARD_01, dst)
    return dst


def _lock(pcb: Path, ref: str) -> None:
    """Add KiCad's ``(locked yes)`` flag to footprint *ref*."""
    from kicad_tools.schema.pcb import PCB

    text = pcb.read_text()
    marker = f'(fp_text reference "{ref}"'
    head, tail = text.split(marker, 1)
    at = head.rfind("(at ")
    head = head[:at] + "(locked yes)\n    " + head[at:]
    pcb.write_text(head + marker + tail)
    assert ref in {fp.reference for fp in PCB.load(str(pcb)).footprints if fp.locked}


def _positions(pcb: Path) -> dict[str, tuple[float, float, float]]:
    from kicad_tools.schema.pcb import PCB

    return {
        fp.reference: (round(fp.position[0], 4), round(fp.position[1], 4), fp.rotation)
        for fp in PCB.load(str(pcb)).footprints
    }


def _run_json(capsys, pcb: Path, out: Path, **kwargs) -> tuple[int, dict]:
    rc_code = opc.run_optimize_placement(
        str(pcb),
        output_path=str(out),
        max_iterations=3,
        as_json=True,
        allow_infeasible=True,
        **kwargs,
    )
    return rc_code, json.loads(capsys.readouterr().out)


class TestCliRouteCheck:
    def test_off_by_default_no_route_check_key(self, board01, tmp_path, capsys, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("route check must not run when off")

        monkeypatch.setattr(rc, "bounded_route", boom)
        _, doc = _run_json(capsys, board01, tmp_path / "out.kicad_pcb")
        assert "route_check" not in doc

    def test_reject_keeps_board_as_read_and_locked_parts(
        self, board01, tmp_path, capsys, monkeypatch
    ):
        _lock(board01, "J1")
        before = _positions(board01)
        calls: list[Path] = []

        def fake_route(pcb, **kwargs):
            calls.append(Path(pcb))
            # Each candidate is a private copy, never the user's board/output.
            assert Path(pcb).parent != board01.parent
            assert kwargs["budget"] == 321
            moved = _positions(Path(pcb)) != before
            return _outcome(2 if moved else 3, requested=3)

        monkeypatch.setattr(rc, "bounded_route", fake_route)
        out = tmp_path / "out.kicad_pcb"
        code, doc = _run_json(
            capsys, board01, out, route_check=True, route_check_budget=321, seed_method="random"
        )
        report = doc["route_check"]
        assert report["accepted_stage"] == "incumbent"
        assert report["budget"] == 321
        assert report["stages"][0]["stage"] == "optimize"
        assert report["stages"][0]["accepted"] is False
        assert report["stages"][0]["incumbent"]["completion"] == 1.0
        assert report["routes_run"] == len(calls) <= 3
        # The incumbent is written untouched, byte for byte.
        assert out.read_bytes() == board01.read_bytes()
        assert _positions(out)["J1"] == before["J1"]
        assert code == 0

    def test_accept_writes_candidate_and_never_moves_locked(
        self, board01, tmp_path, capsys, monkeypatch
    ):
        _lock(board01, "J1")
        before = _positions(board01)
        monkeypatch.setattr(rc, "bounded_route", lambda pcb, **kw: _outcome(3, requested=3))
        out = tmp_path / "out.kicad_pcb"
        _, doc = _run_json(capsys, board01, out, route_check=True, seed_method="random")
        report = doc["route_check"]
        assert report["accepted_stage"] != "incumbent"
        after = _positions(out)
        assert after != before  # the optimizer result was written
        assert after["J1"] == before["J1"]  # ... but the locked part did not move

    def test_rejects_non_positive_budget(self, board01, tmp_path, capsys):
        code, doc = _run_json(
            capsys, board01, tmp_path / "o.kicad_pcb", route_check=True, route_check_budget=0
        )
        assert code == 1 and doc["success"] is False


def test_mcp_tool_exposes_route_check(board01, tmp_path, monkeypatch):
    from kicad_tools.mcp.tools.optimize_placement import optimize_placement

    monkeypatch.setattr(rc, "bounded_route", lambda pcb, **kw: _outcome(2, requested=3))
    calls = []
    real = opc.route_check_placement

    def spy(*args, **kwargs):
        calls.append(kwargs["budget"])
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "kicad_tools.mcp.tools.optimize_placement.route_check_placement", spy, raising=True
    )
    out = tmp_path / "mcp.kicad_pcb"
    result = optimize_placement(
        str(board01),
        max_iterations=3,
        output_path=str(out),
        route_check=True,
        route_check_budget=99,
    )
    assert calls == [99]
    assert result["route_check"]["budget"] == 99
    # Every candidate "routes" equally, so stage A is accepted (or skipped).
    assert result["route_check"]["accepted_stage"] in ("optimize", "decoupling_snap", "incumbent")


# ---------------------------------------------------------------------------
# Acceptance: a wirelength-better, less-routable move, real router
# ---------------------------------------------------------------------------

# R1 turned 180 degrees (rotation index 2) and dropped onto connector J1. Its
# VIN pad lands on J1's VIN pad, which the optimizer likes, and its VOUT pad
# lands on J1's VIN pad too, a short.
_CRAMMED_MOVE = {"R1": (-10.0, 4.23, 2.0)}


def _board01_context(pcb: Path):
    comps, nets, board, rules, origin = opc._read_board_data(str(pcb))
    pins = opc._board_pins(opc._read_current_vector(str(pcb), comps), comps, set())
    incumbent = opc._with_sides(opc._read_current_vector(str(pcb), comps), pins)
    data = incumbent.data.copy()
    for i, comp in enumerate(comps):
        if comp.reference in _CRAMMED_MOVE:
            dx, dy, rot = _CRAMMED_MOVE[comp.reference]
            data[4 * i] += dx
            data[4 * i + 1] += dy
            data[4 * i + 2] = rot
    return comps, nets, board, rules, origin, pins, incumbent, PlacementVector(data=data)


def test_crammed_move_is_better_on_the_optimizers_wirelength(board01):
    comps, nets, board, rules, _o, pins, incumbent, crammed = _board01_context(board01)
    cfg = opc._parse_weights(None)
    sizes = opc._build_footprint_sizes(comps)
    before = opc._evaluate(incumbent, comps, nets, rules, board, cfg, sizes, fixed_sides=pins)
    after = opc._evaluate(crammed, comps, nets, rules, board, cfg, sizes, fixed_sides=pins)
    assert after.breakdown.wirelength < before.breakdown.wirelength


# Two real routes plus two post-route DRCs: a few seconds unloaded, so it
# lives in tests/ci_extended.txt and carries its own timeout headroom.
@pytest.mark.timeout(300)
def test_route_check_rejects_wirelength_better_move(board01):
    """Real router, default budget: the cram loses nets, the incumbent is kept."""
    comps, _n, _b, _r, origin, pins, incumbent, crammed = _board01_context(board01)
    vec, report = opc.route_check_placement(
        str(board01),
        comps,
        origin,
        crammed,
        lambda v: v,  # no snap: one gated stage
        fixed_sides=pins,
        locked_indices=[],
        budget=DEFAULT_ROUTE_CHECK_BUDGET,
        seed=42,
    )
    print("\n".join(report.lines()))
    stage = report.stages[0]
    assert stage.incumbent is not None and stage.candidate is not None
    assert stage.incumbent.completion == 1.0
    assert stage.candidate.completion < stage.incumbent.completion
    assert not stage.accepted
    assert np.array_equal(vec.data, incumbent.data)
    assert report.accepted_stage == "incumbent"
    assert report.routes_run == 2
