"""Gateway combination selection by whole-route length (issue #7362).

Until #7362 the lattice gateway retry returned the FIRST combination that
routed, in fan-length order, and never compared the coupled body.  On board
03 (after #7324 narrowed the USB pair to 0.20 mm) the only pass-under
combination that routed put D+ right of the segment at J1 and on the left
via at U2: the body ran past the vias to y~9 on In2.Cu and doubled back, a
polarity twist in disguise worth about +3 mm per net (USB_D 18.3/19.8 mm
against the 16 mm bound).

Now each group keeps its shortest workable combination (P+N route length,
ties to the earlier try), at most ``MAX_WORKABLE_GATEWAY_COMBOS`` of them are
routed, and a polarity-crossing combination is rejected before routing.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.lattice.pathfinder import _PairResult
from kicad_tools.router.pairplan import gateway as pp_gateway
from kicad_tools.router.pairplan.gateway import (
    MAX_WORKABLE_GATEWAY_COMBOS,
    WorkableCombo,
    best_workable,
    combo_length_floor,
    polarity_crosses,
)
from kicad_tools.router.primitives import Route
from tests.router.lattice.test_pair_gateway_pass_under_6836 import _dual_row_board

# -- pure model ---------------------------------------------------------------


def test_board03_usb_d_pass_under_is_a_polarity_crossing() -> None:
    # Board 03 fresh route on main (seed 42): J1.A6 (D+) / J1.A7 (D-) pads,
    # then the accepted U2 gateway vias, D+ on the LEFT one.
    j1 = ((38.425, 5.8), (37.575, 5.8))
    u2_vias = ((37.6, 8.1625), (38.4, 8.1625))
    assert polarity_crosses(j1, u2_vias)
    assert polarity_crosses(u2_vias, j1)  # symmetric in the ends
    # The same vias with D+ on the right would not cross.
    assert not polarity_crosses(j1, (u2_vias[1], u2_vias[0]))


def test_06rf_pass_under_keeps_its_side() -> None:
    # 06rf USB2 (#6836): D+ is the smaller x at both ends -- pass-under,
    # but no crossing.
    u1 = ((111.5, 65.3), (112.3, 65.3))
    j1 = ((113.0, 54.9), (114.0, 54.9))
    assert not polarity_crosses(u1, j1)


def test_an_end_along_the_chord_has_no_side() -> None:
    # P/N stacked along the travel direction: no left/right to disagree on.
    a = ((0.0, 0.0), (0.0, -0.8))
    b = ((0.5, 10.0), (-0.5, 10.0))
    assert not polarity_crosses(a, b)


def test_shorter_combo_beats_the_first_and_ties_keep_try_order() -> None:
    first = WorkableCombo(0, 9.0, 9.5)
    shorter = WorkableCombo(3, 7.0, 7.2)
    assert best_workable([first, shorter]) == shorter
    assert best_workable([shorter, first]) == shorter
    tie = WorkableCombo(5, 7.2, 7.0)
    assert best_workable([tie, shorter]) == shorter  # equal length: earlier try
    assert best_workable([]) is None


def test_length_floor_is_a_lower_bound() -> None:
    a = ((0.0, 0.0), (0.0, 1.0))
    b = ((10.0, 0.0), (10.0, 1.0))
    assert combo_length_floor(a, b, 2.0) == pytest.approx(22.0)


# -- the pathfinder loop ------------------------------------------------------


def _fake_result(pc, length: float) -> _PairResult:
    net_p, net_n = pc.pad_p_a.net, pc.pad_n_a.net
    return _PairResult(
        routes=[Route(net=net_p, net_name="D+"), Route(net=net_n, net_name="D-")],
        runs=[(net_p, 1, [(0.0, 0.0), (length, 0.0)]), (net_n, 1, [(0.0, 1.0), (length, 1.0)])],
        resources=set(),
    )


def _run_gateway(monkeypatch, lengths: list[float | None]):
    """``_route_pair_gateway`` with each tried combination's length scripted.

    The n-th routed combination returns a P and an N run of ``lengths[n]``
    mm (``None``: declines).  Lengths sit far above the geometric floor so
    the prune never skips one.
    """
    pf, pc = _dual_row_board()
    pf.build()
    calls: list[tuple[object, object]] = []

    def fake(pc_, layer, combo, **_kw):
        idx = len(calls)
        calls.append(combo)
        length = lengths[idx] if idx < len(lengths) else None
        if length is None:
            return None, "pair-no-path"
        return _fake_result(pc_, length), "ok"

    monkeypatch.setattr(pf, "_try_gateway_combo", fake)
    pres, reason = pf._route_pair_gateway(
        pc, committed=pf._fresh_committed(), history={}, present=1.0
    )
    return pf, pc, pres, reason, calls


def test_gateway_keeps_the_shortest_workable_combo_not_the_first(monkeypatch) -> None:
    pf, pc, pres, reason, calls = _run_gateway(monkeypatch, [300.0, None, 200.0, 250.0])
    assert reason == "ok"
    assert pres is not None
    # The third try (200 mm per net) wins over the first workable (300 mm).
    assert [run[2][-1][0] for run in pres.runs] == [200.0, 200.0]
    assert "picked #3 of 3 workable" in pf.pair_gateway_log[pc.key]
    assert len(calls) >= 4  # the rest decline (scripted None)


def test_gateway_routes_at_most_the_workable_cap(monkeypatch) -> None:
    pf, pc, pres, _reason, calls = _run_gateway(monkeypatch, [500.0 - i for i in range(20)])
    assert pres is not None
    assert len(calls) == MAX_WORKABLE_GATEWAY_COMBOS
    assert f"of {MAX_WORKABLE_GATEWAY_COMBOS} workable" in pf.pair_gateway_log[pc.key]


def test_gateway_selection_is_deterministic(monkeypatch) -> None:
    logs = []
    for _ in range(2):
        pf, pc, pres, _reason, _calls = _run_gateway(monkeypatch, [300.0, 300.0, 300.0])
        assert pres is not None
        logs.append(pf.pair_gateway_log[pc.key])
    assert logs[0] == logs[1]
    assert "picked #1 of 3 workable" in logs[0]  # equal lengths: first try wins


def test_gateway_rejects_a_polarity_crossing_unrouted(monkeypatch) -> None:
    monkeypatch.setattr(pp_gateway, "polarity_crosses", lambda a, b: True)
    pf, pc, pres, reason, calls = _run_gateway(monkeypatch, [100.0] * 20)
    assert pres is None
    assert calls == []  # never routed
    assert reason.endswith("polarity-crossing")
    log = pf.pair_gateway_log[pc.key]
    assert log.startswith("gateway declined") and "polarity-crossing" in log


def test_real_dual_row_pair_still_couples_through_pass_under() -> None:
    # The #6836 pair keeps coupling: its pass-under is not a crossing.
    pf, pc = _dual_row_board()
    pf.route_netset([], coupled=[pc], uncoupled_fallback=True, pair_gateways=True)
    assert pf.pair_outcomes[pc.key] == "coupled", pf.pair_gateway_log
    assert "workable" in pf.pair_gateway_log[pc.key]
