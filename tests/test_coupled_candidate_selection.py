"""Compare-and-keep between the joint and pose-centreline coupled candidates (#6858).

A successful joint search used to displace a better pose-centreline trunk
(06rf MIPI_CLK at a 700k budget).  Both candidates now go through the same
copper gates and the better one is committed; the loser leaves no trace.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_routing import (
    CoupledPathfinder,
    DiffPairRouter,
    _select_coupled_candidate,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

P_NET, N_NET = 1, 2
RES = 0.1


def _rail(net: int, name: str, pts, vias=()) -> Route:
    route = Route(net=net, net_name=name)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        route.segments.append(
            Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net, net_name=name)
        )
    route.vias.extend(vias)
    return route


def _tight(end_x: float = 25.0) -> tuple[Route, Route]:
    """Fully coupled straight pair between the fixture pads."""
    return (
        _rail(P_NET, "USB_D+", [(5.0, 4.6), (end_x, 4.6)]),
        _rail(N_NET, "USB_D-", [(5.0, 5.4), (end_x, 5.4)]),
    )


def _loose() -> tuple[Route, Route]:
    """N detours 3.6 mm away from P mid-route: long uncoupled stretch."""
    return (
        _rail(P_NET, "USB_D+", [(5.0, 4.6), (25.0, 4.6)]),
        _rail(
            N_NET,
            "USB_D-",
            [(5.0, 5.4), (8.0, 5.4), (10.0, 9.0), (20.0, 9.0), (22.0, 5.4), (25.0, 5.4)],
        ),
    )


def _overlapping() -> tuple[Route, Route]:
    """Gate-rejected: an N via sits on P copper (via-aware overlap, #6840)."""
    p, n = _tight()
    n.vias.append(
        Via(
            x=10.0, y=4.9, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU),
            net=N_NET, net_name="USB_D-",
        )
    )  # fmt: skip
    return p, n


def _router() -> Autorouter:
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    nc = NetClassRouting(name="HS", coupled_routing=True)
    router = Autorouter(
        width=30.0, height=10.0, rules=rules, net_class_map={"USB_D+": nc, "USB_D-": nc}
    )
    for ref, x in (("U1", 5.0), ("J1", 25.0)):
        router.add_component(
            ref,
            [
                {"number": "1", "x": x, "y": 4.6, "width": 0.4, "height": 0.4,
                 "net": P_NET, "net_name": "USB_D+"},
                {"number": "2", "x": x, "y": 5.4, "width": 0.4, "height": 0.4,
                 "net": N_NET, "net_name": "USB_D-"},
            ],
        )  # fmt: skip
    return router


def _run(monkeypatch, capsys, joint, pose):
    """Route the pair with stubbed joint / pose searches.

    Returns ``(router, marked, independent_calls, stdout)``; ``marked`` lists
    every route handed to ``_mark_route``.
    """
    monkeypatch.setattr(DiffPairRouter, "_shadow_route_pair", lambda self, *a, **k: None)
    monkeypatch.setattr(DiffPairRouter, "_pose_copper_rejection", lambda self, *a, **k: None)
    router = _router()
    router._diffpair.enable_pose_centerline = True
    router._diffpair.enable_pose_corridor_guard = False
    [pair] = router._diffpair.detect_differential_pairs()

    def fake_route_coupled(self, *args, **kwargs):
        self.last_timeout_exceeded = False
        self.last_iterations = 1
        return joint

    monkeypatch.setattr(CoupledPathfinder, "route_coupled", fake_route_coupled)

    def fake_pose(pathfinder, *args, **kwargs):
        pathfinder.last_pose_report = (
            {"applied": True, "centerline_poses": 3, "iterations": 7}
            if pose is not None
            else {"applied": False, "reason": "stubbed-decline"}
        )
        return pose

    monkeypatch.setattr("kicad_tools.router.diffpair_pose.route_centerline_pose", fake_pose)

    marked: list[Route] = []
    real_mark = router._mark_route
    monkeypatch.setattr(router, "_mark_route", lambda r: (marked.append(r), real_mark(r))[1])
    independent: list[object] = []

    def fake_independent(self, pair, spacing=None):
        independent.append(pair)
        return [], None

    monkeypatch.setattr(DiffPairRouter, "route_differential_pair_independent", fake_independent)
    router._diffpair.route_differential_pair_coupled(
        pair, coupled_only=False, per_pair_timeout=30.0, per_pair_max_iterations=1000
    )
    return router, marked, independent, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Selection rule (pure)
# ---------------------------------------------------------------------------


def _cand(routes):
    return (routes[0], routes[1], None)


def test_lower_uncoupled_length_wins_both_ways_round():
    assert _select_coupled_candidate(_cand(_loose()), _cand(_tight()), RES) == "pose"
    assert _select_coupled_candidate(_cand(_tight()), _cand(_loose()), RES) == "joint"


def test_uncoupled_tie_within_resolution_falls_to_lower_skew():
    # 0.05 mm of extra N copper: same uncoupled figure within RES, skew 0.05 mm.
    skewed = _tight(end_x=25.0)
    skewed[1].segments[-1].x2 = 25.05
    assert _select_coupled_candidate(_cand(skewed), _cand(_tight()), RES) == "pose"
    assert _select_coupled_candidate(_cand(_tight()), _cand(skewed), RES) == "joint"


def test_equal_length_and_skew_falls_to_fewer_vias():
    with_via = _tight()
    with_via[1].vias.append(
        Via(x=15.0, y=5.4, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU),
            net=N_NET, net_name="USB_D-")
    )  # fmt: skip
    assert _select_coupled_candidate(_cand(with_via), _cand(_tight()), RES) == "pose"
    assert _select_coupled_candidate(_cand(_tight()), _cand(with_via), RES) == "joint"


def test_full_tie_keeps_the_joint_result():
    assert _select_coupled_candidate(_cand(_tight()), _cand(_tight()), RES) == "joint"


def test_single_candidate_and_none():
    assert _select_coupled_candidate(_cand(_tight()), None, RES) == "joint"
    assert _select_coupled_candidate(None, _cand(_tight()), RES) == "pose"
    assert _select_coupled_candidate(None, None, RES) is None


# ---------------------------------------------------------------------------
# Through route_differential_pair_coupled
# ---------------------------------------------------------------------------


def test_better_pose_trunk_displaces_a_worse_joint_result(monkeypatch, capsys):
    joint, pose = _loose(), _tight()
    router, marked, independent, out = _run(monkeypatch, capsys, joint, pose)
    assert not independent
    assert "[coupled-select] kept=pose" in out
    assert router.routes[-2:] == list(pose)


def test_gate_rejected_joint_commits_the_pose_trunk_not_the_independent_fallback(
    monkeypatch, capsys
):
    pose = _tight()
    router, _, independent, out = _run(monkeypatch, capsys, _overlapping(), pose)
    assert not independent
    assert "[coupled-select] kept=pose joint=rejected:via-overlap" in out
    assert router.routes[-2:] == list(pose)


def test_declined_pose_commits_the_joint_result_unchanged(monkeypatch, capsys):
    joint = _loose()
    router, marked, independent, out = _run(monkeypatch, capsys, joint, None)
    assert not independent
    assert "[coupled-select] kept=joint" in out
    assert "declined:stubbed-decline" in out
    assert router.routes[-2:] == list(joint)
    assert marked == list(joint)
    assert not router._diffpair._pose_claims()


def test_full_tie_commits_the_joint_result(monkeypatch, capsys):
    joint, pose = _tight(), _tight()
    router, _, _, out = _run(monkeypatch, capsys, joint, pose)
    assert "[coupled-select] kept=joint" in out
    assert router.routes[-2:] == list(joint)


def test_losing_candidate_leaves_no_grid_marks_or_route_entry(monkeypatch, capsys):
    joint, pose = _tight(), _loose()
    router, marked, _, _ = _run(monkeypatch, capsys, joint, pose)
    assert marked == list(joint)
    assert all(r is not pose[0] and r is not pose[1] for r in router.routes)


def test_winning_pose_trunk_is_claimed(monkeypatch, capsys):
    router, _, _, _ = _run(monkeypatch, capsys, _loose(), _tight())
    assert {P_NET, N_NET} <= router._diffpair._pose_claims()
    assert {P_NET, N_NET} <= router._diffpair._committed_coupled_claims()


def test_pose_not_run_when_disabled(monkeypatch, capsys):
    monkeypatch.setattr(DiffPairRouter, "_shadow_route_pair", lambda self, *a, **k: None)
    router = _router()
    router._diffpair.enable_pose_centerline = False
    [pair] = router._diffpair.detect_differential_pairs()
    joint = _loose()
    monkeypatch.setattr(CoupledPathfinder, "route_coupled", lambda self, *a, **k: joint)
    router._diffpair.route_differential_pair_coupled(
        pair, coupled_only=True, per_pair_timeout=30.0, per_pair_max_iterations=1000
    )
    assert router.routes[-2:] == list(joint)
    assert "[coupled-pose]" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["joint", "pose"])
def test_select_line_reports_metrics(monkeypatch, capsys, kind):
    joint, pose = (_tight(), _loose()) if kind == "joint" else (_loose(), _tight())
    _, _, _, out = _run(monkeypatch, capsys, joint, pose)
    assert f"kept={kind} joint=(" in out and "pose=(" in out and "pose_s=" in out
