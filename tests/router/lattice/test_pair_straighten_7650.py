"""Board 03 USB_D common-run and MCU-launch margin (issue #7650).

Two router changes recover what routing VBUS U2.5 cost board 03 (#7475):

* A gateway combination's matched-length tie now goes to the one whose body
  stays coupled longer (:func:`coupled_length`), not to the earlier try.
* A converged negotiation re-routes each planar coupled pair once without
  history (:meth:`LatticePathfinder._straighten_pairs`), so a pair no longer
  keeps a detour around demand that earlier passes left on its corridor.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.lattice.coupled import CoupledConnection
from kicad_tools.router.lattice.history import PhysicalHistory
from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairplan.gateway import WorkableCombo, best_workable, coupled_length
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules, NetClassRouting

PITCH = 0.35

# -- coupled_length ---------------------------------------------------------


def test_parallel_legs_at_the_pitch_are_coupled_end_to_end() -> None:
    p = [(0.0, 0.0), (5.0, 0.0)]
    n = [(0.0, PITCH), (5.0, PITCH)]
    assert coupled_length(p, n, pitch=PITCH) == pytest.approx(5.0)


def test_only_the_overlap_counts() -> None:
    p = [(0.0, 0.0), (5.0, 0.0)]
    n = [(2.0, PITCH), (9.0, PITCH)]
    assert coupled_length(p, n, pitch=PITCH) == pytest.approx(3.0)


def test_legs_off_the_pitch_or_not_parallel_are_not_coupled() -> None:
    p = [(0.0, 0.0), (5.0, 0.0)]
    assert coupled_length(p, [(0.0, 0.6), (5.0, 0.6)], pitch=PITCH) == 0.0
    assert coupled_length(p, [(0.0, PITCH), (5.0, 3.0)], pitch=PITCH) == 0.0


def test_a_partner_split_at_a_vertex_is_not_counted_twice() -> None:
    p = [(0.0, 0.0), (4.0, 0.0)]
    n = [(0.0, PITCH), (2.0, PITCH), (4.0, PITCH)]
    assert coupled_length(p, n, pitch=PITCH) == pytest.approx(4.0)


def test_board03_usb_d_r4_end_keeps_the_body_coupled_longer() -> None:
    # Board 03 arm C, In2.Cu body of USB_D U2.4->R4.1 (board coordinates).
    # Both combinations are 15.032 mm matched; the centred R4-end via pair
    # lets the body run 0.15 mm further before it turns in.
    parallel_p = [
        (37.05, 12.638), (37.213, 12.8), (37.825, 12.8), (37.825, 13.273),
        (39.825, 15.273), (39.825, 16.4), (39.825, 16.515), (39.45, 16.89),
    ]  # fmt: skip
    parallel_n = [
        (38.95, 12.638), (38.787, 12.8), (38.175, 12.8), (38.175, 13.127),
        (40.175, 15.127), (40.175, 16.4), (40.175, 16.515), (40.55, 16.89),
    ]  # fmt: skip
    centred_p = [*parallel_p[:5], (39.825, 16.0), (39.825, 16.665), (39.6, 16.89)]
    centred_n = [*parallel_n[:5], (40.175, 16.0), (40.175, 16.665), (40.4, 16.89)]
    run = coupled_length(parallel_p, parallel_n, pitch=PITCH)
    longer = coupled_length(centred_p, centred_n, pitch=PITCH)
    assert longer == pytest.approx(run + 0.15, abs=1e-3)


# -- the tie-break ----------------------------------------------------------


def test_a_matched_length_tie_goes_to_the_longer_coupled_body() -> None:
    first = WorkableCombo(0, 7.516, 7.516, coupled=4.4)
    second = WorkableCombo(1, 7.516, 7.516, coupled=4.55)
    assert best_workable([first, second]) == second
    # Length still leads: a shorter combination wins however little couples.
    shorter = WorkableCombo(2, 7.5, 7.5, coupled=1.0)
    assert best_workable([first, second, shorter]) == shorter
    # And an open escape site still leads length (#7463).
    assert best_workable([WorkableCombo(3, 7.4, 7.4, sealed=1, coupled=9.0), second]) == second


# -- CommittedCopper.remove_run ---------------------------------------------


def _committed() -> CommittedCopper:
    return CommittedCopper(
        2, trace_half=0.1, clearance=0.15, via_radius=0.3, via_via_gap=0.75, same_net_via_gap=0.6
    )


def test_remove_run_undoes_add_run() -> None:
    copper = _committed()
    run = [(0.0, 0.0), (3.0, 0.0), (3.0, 3.0)]
    copper.add_run(0, run, 7, 0.1, 0.15)
    assert not copper.seg_clear((1.0, 0.2), (2.0, 0.2), 0, 8)
    copper.remove_run(0, run, 7, 0.1, 0.15)
    assert copper.seg_clear((1.0, 0.2), (2.0, 0.2), 0, 8)
    assert all(not bucket for bucket in copper.copper[0].buckets.values())


def test_remove_run_refuses_copper_that_was_never_committed() -> None:
    copper = _committed()
    with pytest.raises(RuntimeError, match="not committed"):
        copper.remove_run(0, [(0.0, 0.0), (1.0, 0.0)], 7, 0.1, 0.15)


def test_remove_run_refuses_an_open_trial() -> None:
    copper = _committed()
    copper.add_run(0, [(0.0, 0.0), (1.0, 0.0)], 7, 0.1, 0.15)
    copper.begin_trial()
    with pytest.raises(RuntimeError, match="trial"):
        copper.remove_run(0, [(0.0, 0.0), (1.0, 0.0)], 7, 0.1, 0.15)


# -- _straighten_pairs ------------------------------------------------------

_RULES = DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb")
_HS = NetClassRouting(
    name="HS", trace_width=0.2, clearance=0.15, intra_pair_clearance=0.1, coupled_routing=True
)


def _pad(x: float, y: float, net: int, name: str, ref: str) -> Pad:
    return Pad(
        x=x, y=y, width=0.5, height=0.5, net=net, net_name=name, layer=Layer.F_CU, ref=ref, pin="1"
    )


def _pair() -> tuple[LatticePathfinder, CoupledConnection]:
    outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 12.0), (0.0, 12.0)]
    p_a, n_a = _pad(3.0, 5.0, 1, "D+", "J1"), _pad(3.0, 6.0, 2, "D-", "J1")
    p_b, n_b = _pad(27.0, 5.0, 1, "D+", "U1"), _pad(27.0, 6.0, 2, "D-", "U1")
    pf = LatticePathfinder(outline, [p_a, n_a, p_b, n_b], _RULES, LayerStack.two_layer())
    pc = CoupledConnection(
        key=("pair", 1, 2),
        pair_name="D",
        pad_p_a=p_a,
        pad_n_a=n_a,
        pad_p_b=p_b,
        pad_n_b=n_b,
        net_class=_HS,
        pitch=_HS.trace_width + _HS.effective_intra_pair_clearance(),
    )
    pf.build()
    return pf, pc


def _route_and_commit(pf, pc, history):
    committed = pf._fresh_committed()
    pres, why = pf._route_pair_impl(pc, committed=committed, history=history, present=1.0)
    assert pres is not None, why
    for net, layer, pts in pres.runs:
        half, clr = pf._conn_geometry(pc.net_class, net)
        committed.add_run(layer, pts, net, half, clr)
    routes = {(pc.key, "P"): pres.routes[0], (pc.key, "N"): pres.routes[1]}
    return committed, pres, routes


def _stored(committed: CommittedCopper) -> set:
    """Every committed segment as ``(layer, a, b, net)``."""
    return {
        (layer, a, b, net)
        for layer, index in enumerate(committed.copper)
        for bucket in index.buckets.values()
        for a, b, net, _hw, _clr in bucket
    }


def _segments(pres) -> set:
    return {
        (layer, a, b, net)
        for net, layer, pts in pres.runs
        for a, b in zip(pts, pts[1:], strict=False)
        if math.dist(a, b) > 1e-9
    }


def test_a_history_detour_is_straightened_once_negotiation_converged() -> None:
    pf, pc = _pair()
    # Demand an earlier pass left across the pair's straight corridor.
    history = PhysicalHistory()
    for layer in (0, 1):
        history.add_segment((15.0, 3.5), (15.0, 7.5), layer, 9, 0.1, 0.15, 50.0)
    committed, detour, routes = _route_and_commit(pf, pc, history)
    pf._straighten_pairs([(pc, detour)], committed, routes, 1.0)

    old, new = pf.pair_straightened[pc.key]
    assert new < old - 1.0
    assert routes[(pc.key, "P")] is not detour.routes[0]
    # The committed copper is now exactly the history-free run: the detour's
    # own segments are gone, not left behind under the new run.
    fresh, _why = pf._route_pair_impl(
        pc, committed=pf._fresh_committed(), history=PhysicalHistory(), present=1.0
    )
    assert fresh is not None
    assert _stored(committed) == _segments(fresh)
    assert _segments(detour) - _segments(fresh)
    # Its dip under the demand (y ~ 1.4 at x = 14.4) is free for another net.
    assert committed.seg_clear((14.0, 1.4), (14.8, 1.4), 0, 9)


def test_a_run_that_is_already_shortest_is_kept() -> None:
    pf, pc = _pair()
    committed, straight, routes = _route_and_commit(pf, pc, PhysicalHistory())
    pf._straighten_pairs([(pc, straight)], committed, routes, 1.0)
    assert pc.key not in pf.pair_straightened
    assert routes[(pc.key, "P")] is straight.routes[0]
    assert _stored(committed) == _segments(straight)
