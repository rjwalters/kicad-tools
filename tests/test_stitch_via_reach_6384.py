"""Stitch's via reach bounds the pour weld neighbourhood (Issue #6384).

``PourLinkCloser.weld_clearance`` picks the one clearance ``run_stitch``
welds a round's pads at: the largest ``c(P, N)`` over every neighbour net N a
weld could land next to.  It used a fixed 5 mm radius from the pad polygon,
which missed a stricter class just beyond 5 mm (the clearance term was never
added) and large pads (whose direct / dog-leg vias scale with the pad).

The neighbourhood is now derived from stitch's own geometry:
``max_via_reach_mm(pad) + max(via_size, trace_width) / 2 + c(P, N)`` from the
pad centre.  These tests pin the bound (fuzzed against the placement
strategies) and the decision it drives.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    calculate_dogleg_via_position,
    calculate_extended_escape_position,
    calculate_via_position,
    max_via_reach_mm,
)

PAD_SHAPES = [(0.3, 0.3), (1.0, 0.4), (5.0, 5.0), (7.0, 0.5)]
OFFSET = 0.5
ESCAPE = 4.0


# ---------------------------------------------------------------------------
# The bound itself
# ---------------------------------------------------------------------------


class TestMaxViaReach:
    def test_small_pad_is_bounded_by_the_escape_distance(self):
        assert max_via_reach_mm(0.3, 0.3, 0.5, 4.0) == pytest.approx(4.0)

    def test_long_pad_is_bounded_by_the_dogleg(self):
        assert max_via_reach_mm(7.0, 0.5, 0.5, 4.0) == pytest.approx(3.5 + 1.0 + 1.3)
        # Orientation does not matter: R is half the longer side.
        assert max_via_reach_mm(0.5, 7.0, 0.5, 4.0) == max_via_reach_mm(7.0, 0.5, 0.5, 4.0)

    def test_parameters_move_the_bound(self):
        assert max_via_reach_mm(0.3, 0.3, 0.5, 6.0) == pytest.approx(6.0)
        assert max_via_reach_mm(5.0, 5.0, 1.0, 4.0) == pytest.approx(2.5 + 2.0 + 1.3)


def _reaches(w: float, h: float, escape: float, trials: int, seed: int) -> list[float]:
    """Distances (from the pad centre) of every via and waypoint any strategy returned."""
    rng = random.Random(seed)
    out: list[float] = []
    for _ in range(trials):
        pad = PadInfo("U1", "1", 1, "GND", 0.0, 0.0, "F.Cu", w, h)
        n = rng.randint(5, 120)
        vias = [(rng.uniform(-7, 7), rng.uniform(-7, 7), 0.6, 2) for _ in range(n)]
        kw: dict = {
            "offset": OFFSET,
            "via_size": 0.6,
            "existing_vias": [],
            "clearance": 0.2,
            "other_net_vias": vias,
            "trace_width": 0.2,
        }
        p = calculate_via_position(pad, **kw)
        if p is not None:
            out.append(math.hypot(p[0], p[1]))
        d = calculate_dogleg_via_position(pad, **kw)
        if d is not None:
            out += [math.hypot(d[0], d[1]), math.hypot(d[2], d[3])]
        e = calculate_extended_escape_position(pad, escape_distance=escape, **kw)
        if e is not None:
            out.append(math.hypot(e[0], e[1]))
            out += [math.hypot(x, y) for x, y in e[2]]
    return out


class TestReachBoundHolds:
    """Every via / waypoint the strategies return lies inside the bound."""

    @pytest.mark.parametrize("w,h", PAD_SHAPES)
    @pytest.mark.parametrize("escape", [ESCAPE, 2.5])
    def test_fuzzed_placements_stay_within_the_bound(self, w, h, escape):
        reaches = _reaches(w, h, escape, trials=200, seed=6384)
        assert reaches, "the fuzz placed nothing; it no longer exercises the strategies"
        bound = max_via_reach_mm(w, h, OFFSET, escape)
        assert max(reaches) <= bound + 1e-9

    def test_the_bound_is_needed_on_a_long_pad(self):
        # The old fixed 5 mm radius is beaten by real placements on a 7 x 0.5 pad.
        assert max(_reaches(7.0, 0.5, ESCAPE, trials=200, seed=6384)) > 5.0


# ---------------------------------------------------------------------------
# The weld clearance decision
# ---------------------------------------------------------------------------

shapely = pytest.importorskip("shapely")

from kicad_tools.router.oracle_completion import (  # noqa: E402
    WELD_ESCAPE_DISTANCE_MM,
    WELD_OFFSET_MM,
    ClosureAttempt,
    PourLinkCloser,
)
from kicad_tools.router.rules import NetClassRouting  # noqa: E402

VIA, TRACE = 0.6, 0.2
PAD_X, PAD_Y = 10.0, 10.0


def _fp(ref: str, x: float, y: float, net_num: int, net: str, w: float, h: float) -> str:
    return (
        f'  (footprint "T:{ref}" (layer "F.Cu")\n'
        f"    (at {x:g} {y:g})\n"
        f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {w:g} {h:g}) (layers "F.Cu") '
        f'(net {net_num} "{net}"))\n'
        f"  )\n"
    )


def _board(tmp_path: Path, pad: tuple[float, float], neighbour: str) -> Path:
    """A GND pour pad ``R1.1`` of size ``pad`` at (10, 10) plus ``neighbour``."""
    text = (
        '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
        '  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "")\n  (net 1 "GND")\n  (net 2 "HV")\n'
        + _fp("R1", PAD_X, PAD_Y, 1, "GND", *pad)
        + neighbour
        + '  (gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))\n)\n'
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return path


def _right_of(dist: float) -> str:
    """A 1 x 1 mm HV pad whose nearest edge is ``dist`` mm right of the pad centre."""
    return _fp("J1", PAD_X + dist + 0.5, PAD_Y, 2, "HV", 1.0, 1.0)


def _above(dist: float) -> str:
    """A 1 x 1 mm HV pad whose nearest edge is ``dist`` mm off the pad centre in y."""
    return _fp("J1", PAD_X, PAD_Y + dist + 0.5, 2, "HV", 1.0, 1.0)


def _closer(hv: float, **kw) -> PourLinkCloser:
    ncm = {
        "GND": NetClassRouting(name="Power", clearance=0.2),
        "HV": NetClassRouting(name="HV", clearance=hv),
    }
    return PourLinkCloser(
        via_size=VIA, via_drill=0.3, clearance=0.15, trace_width=TRACE, net_class_map=ncm, **kw
    )


@pytest.fixture
def stitch_calls(monkeypatch):
    from kicad_tools.cli import stitch_cmd

    calls: list[dict] = []

    def fake_run_stitch(pcb_path, nets, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(vias_added=[], already_connected=0, pads_skipped=[], traces_added=[])

    monkeypatch.setattr(stitch_cmd, "run_stitch", fake_run_stitch)
    return calls


def _weld(closer: PourLinkCloser, board: Path) -> None:
    closer._weld_pads(board, {"GND": {"R1.1"}}, ClosureAttempt())


class TestWeldNeighbourhood:
    def test_a_strict_class_just_beyond_5mm_is_kept(self, tmp_path, stitch_calls):
        # 0.3 mm pad: HV copper 5.2 mm from the centre (5.05 from the pad
        # polygon, so the old 5 mm radius missed it) but within
        # 4.0 + 0.3 + 1.0 = 5.3 of where a weld can reach.
        board = _board(tmp_path, (0.3, 0.3), _right_of(5.2))
        _weld(_closer(1.0), board)
        assert stitch_calls[0]["clearance"] == pytest.approx(1.0)

    def test_a_large_pad_reaches_farther(self, tmp_path, stitch_calls):
        # 7 x 0.5 pad: its dog-leg vias reach 3.5 + 1.0 + 1.3 = 5.8 mm.  HV
        # copper 6.0 mm off the short axis (5.75 from the pad edge) is within
        # 5.8 + 0.3 + 0.5.
        board = _board(tmp_path, (7.0, 0.5), _above(6.0))
        _weld(_closer(0.5), board)
        assert stitch_calls[0]["clearance"] == pytest.approx(0.5)

    def test_just_beyond_the_reach_keeps_the_floor(self, tmp_path, stitch_calls):
        reach = max_via_reach_mm(0.3, 0.3, WELD_OFFSET_MM, WELD_ESCAPE_DISTANCE_MM)
        reach += max(VIA, TRACE) / 2 + 1.0
        board = _board(tmp_path, (0.3, 0.3), _right_of(reach + 0.1))
        _weld(_closer(1.0), board)
        assert stitch_calls[0]["clearance"] == pytest.approx(0.2)

    def test_the_large_pad_case_beyond_its_reach_keeps_the_floor(self, tmp_path, stitch_calls):
        board = _board(tmp_path, (7.0, 0.5), _above(5.8 + 0.3 + 0.5 + 0.1))
        _weld(_closer(0.5), board)
        assert stitch_calls[0]["clearance"] == pytest.approx(0.2)


class TestExplicitStitchParameters:
    def test_offset_and_escape_distance_are_passed_explicitly(self, tmp_path, stitch_calls):
        board = _board(tmp_path, (0.3, 0.3), "")
        closer = _closer(1.0)
        _weld(closer, board)
        assert stitch_calls
        for call in stitch_calls:
            assert call["offset"] == closer.weld_offset == WELD_OFFSET_MM
            assert call["escape_distance"] == closer.weld_escape_distance
            assert call["escape_distance"] == WELD_ESCAPE_DISTANCE_MM

    def test_the_reach_follows_the_values_passed_to_stitch(self, tmp_path, stitch_calls):
        # Shrink the escape distance: the 5.2 mm neighbour of the first test is
        # now out of reach (3.0 + 0.3 + 1.0 = 4.3), and stitch gets the same 3.0.
        board = _board(tmp_path, (0.3, 0.3), _right_of(5.2))
        _weld(_closer(1.0, weld_escape_distance=3.0), board)
        assert stitch_calls[0]["escape_distance"] == 3.0
        assert stitch_calls[0]["clearance"] == pytest.approx(0.2)
        # ... and widen it: a neighbour at 6.5 mm is in reach of a 6 mm escape.
        board = _board(tmp_path, (0.3, 0.3), _right_of(6.5))
        _weld(_closer(1.0, weld_escape_distance=6.0), board)
        assert stitch_calls[-1]["escape_distance"] == 6.0
        assert stitch_calls[-1]["clearance"] == pytest.approx(1.0)
