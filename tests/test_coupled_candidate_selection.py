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


# ---------------------------------------------------------------------------
# Issue #5333: the pair's enforced skew limit outranks uncoupled length
# ---------------------------------------------------------------------------


def _split_balanced() -> tuple[Route, Route]:
    """P and N bow apart symmetrically: long uncoupled stretch, zero skew."""
    return (
        _rail(
            P_NET,
            "USB_D+",
            [(5.0, 4.6), (8.0, 4.6), (10.0, 1.0), (20.0, 1.0), (22.0, 4.6), (25.0, 4.6)],
        ),
        _rail(
            N_NET,
            "USB_D-",
            [(5.0, 5.4), (8.0, 5.4), (10.0, 9.0), (20.0, 9.0), (22.0, 5.4), (25.0, 5.4)],
        ),
    )


def _tight_skewed() -> tuple[Route, Route]:
    """Fully coupled, but N carries a 0.5 mm stub: little uncoupled, big skew."""
    p, n = _tight()
    n.segments.append(
        Segment(x1=25.0, y1=5.4, x2=25.0, y2=5.9, width=0.2, layer=Layer.F_CU,
                net=N_NET, net_name="USB_D-")
    )  # fmt: skip
    return p, n


def _metrics(routes):
    from kicad_tools.router.diffpair_routing import _coupled_candidate_metrics

    return _coupled_candidate_metrics(*routes)


def test_fixture_preconditions_for_the_skew_limit_rule():
    """The two fixtures disagree: one wins on uncoupled, the other on skew."""
    bal_unc, bal_skew, _ = _metrics(_split_balanced())
    sk_unc, sk_skew, _ = _metrics(_tight_skewed())
    assert sk_unc + RES < bal_unc  # the skewed pair is the better-coupled one
    assert bal_skew < 0.05 < sk_skew


def test_candidate_meeting_the_skew_limit_beats_a_better_coupled_one():
    # Old rule (no limit): the lower-uncoupled, out-of-limit candidate wins.
    assert _select_coupled_candidate(_cand(_tight_skewed()), _cand(_split_balanced()), RES) == (
        "joint"
    )
    # Issue #5333: with the enforced limit, the in-limit candidate wins either way round.
    assert (
        _select_coupled_candidate(
            _cand(_tight_skewed()), _cand(_split_balanced()), RES, skew_limit_mm=0.05
        )
        == "pose"
    )
    assert (
        _select_coupled_candidate(
            _cand(_split_balanced()), _cand(_tight_skewed()), RES, skew_limit_mm=0.05
        )
        == "joint"
    )


def test_skew_limit_is_ignored_when_both_or_neither_candidate_meets_it():
    # Both meet a loose limit: the #6858 ordering (uncoupled first) decides.
    assert (
        _select_coupled_candidate(
            _cand(_tight_skewed()), _cand(_split_balanced()), RES, skew_limit_mm=5.0
        )
        == "joint"
    )
    # Neither meets an impossible limit: same ordering.
    assert (
        _select_coupled_candidate(
            _cand(_tight_skewed()), _cand(_split_balanced()), RES, skew_limit_mm=-1.0
        )
        == "joint"
    )


def test_pair_skew_target_carries_the_class_cap_and_the_derived_budget():
    """Issue #7893: the class's error cap and the name-identified ps budget."""
    router = _router()
    [pair] = router._diffpair.detect_differential_pairs()
    nc = NetClassRouting(name="HS", coupled_routing=True, skew_tolerance_mm=0.05)
    router.net_class_map = {"USB_D+": nc, "USB_D-": nc}
    target = router._diffpair._pair_skew_target(pair)
    assert target.cap_mm == pytest.approx(0.05)
    assert target.budget_ps == pytest.approx(14.39, abs=0.01)  # USB 2.0 HS, from the name


def _cap_only(cap_mm):
    from kicad_tools.router.pair_skew_target import PairSkewTarget

    return lambda self, pair: PairSkewTarget("USB_D+", "USB_D-", cap_mm=cap_mm)


def test_router_commits_the_in_limit_pose_over_a_better_coupled_skewed_joint(monkeypatch, capsys):
    """End to end through ``route_differential_pair_coupled`` (board 07 TMDS_D1 shape)."""
    monkeypatch.setattr(DiffPairRouter, "_pair_skew_target", _cap_only(0.05))
    _router_, marked, independent, out = _run(
        monkeypatch, capsys, _tight_skewed(), _split_balanced()
    )
    assert "[coupled-select] kept=pose" in out
    assert not independent


def test_warning_severity_limit_leaves_candidate_selection_unchanged(monkeypatch, capsys):
    """No skew target (only an advisory limit): plain #6858 ordering decides."""
    from kicad_tools.router.pair_skew_target import PairSkewTarget

    monkeypatch.setattr(
        DiffPairRouter,
        "_pair_skew_target",
        lambda self, pair: PairSkewTarget("USB_D+", "USB_D-", warning_cap_mm=0.05),
    )
    router = _router()
    [pair] = router._diffpair.detect_differential_pairs()
    assert router._diffpair._pair_skew_target(pair).enforced is False

    # Same fixtures as the error-limit end-to-end test, opposite winner.
    _router_, _marked, independent, out = _run(
        monkeypatch, capsys, _tight_skewed(), _split_balanced()
    )
    assert "[coupled-select] kept=joint" in out
    assert not independent


# ---------------------------------------------------------------------------
# Issue #7913: the budget's length form (kicad-cli's .kicad_dru skew limit)
# ---------------------------------------------------------------------------


def _within_mm(limit):
    """``(p, n) -> bool``: the pair's length skew meets ``limit`` mm."""
    from kicad_tools.router.diffpair_routing import calculate_route_length

    return lambda p, n: abs(calculate_route_length([p]) - calculate_route_length([n])) <= limit


def test_candidate_meeting_the_length_form_beats_a_better_coupled_one():
    """Board 07 TMDS_D1 shape: both meet the ps budget, only one its length form.

    Before #7913 the better-coupled joint won although its skew was over the
    0.484 mm limit kicad-cli and the router's own post-route pair tuners
    hold it to.
    """
    both_meet_ps = _within_mm(5.0)
    joint, pose = _cand(_tight_skewed()), _cand(_split_balanced())
    # The ps tier alone ties, so uncoupled length decides (the old outcome).
    assert _select_coupled_candidate(joint, pose, RES, skew_ok=both_meet_ps) == "joint"
    # The length form breaks the tie for the in-limit candidate, either way round.
    assert (
        _select_coupled_candidate(
            joint, pose, RES, skew_ok=both_meet_ps, skew_ok_length=_within_mm(0.05)
        )
        == "pose"
    )
    assert (
        _select_coupled_candidate(
            pose, joint, RES, skew_ok=both_meet_ps, skew_ok_length=_within_mm(0.05)
        )
        == "joint"
    )


def test_length_form_ranks_below_the_ps_budget():
    """A candidate that meets the ps budget still beats one that only meets the length form."""
    joint, pose = _cand(_tight_skewed()), _cand(_split_balanced())
    joint_only_ps = lambda p, n: p is joint[0]  # noqa: E731
    assert (
        _select_coupled_candidate(
            joint, pose, RES, skew_ok=joint_only_ps, skew_ok_length=_within_mm(0.05)
        )
        == "joint"
    )


def test_length_form_is_ignored_when_both_or_neither_meets_it():
    joint, pose = _cand(_tight_skewed()), _cand(_split_balanced())
    for limit in (5.0, -1.0):
        assert _select_coupled_candidate(joint, pose, RES, skew_ok_length=_within_mm(limit)) == (
            "joint"
        )


def test_length_form_predicate_uses_the_budget_at_the_slowest_layer():
    """``_length_form_ok`` is ``PairSkewTarget.tolerance_mm`` on the meter's length."""
    from kicad_tools.physics.skew_budget import LayerDelays
    from kicad_tools.router.diffpair_routing import _length_form_ok
    from kicad_tools.router.pair_skew_target import (
        PairSkewMeter,
        PairSkewTarget,
        segment_length_parts,
    )

    delays = LayerDelays(ps_per_mm={"F.Cu": 6.0, "B.Cu": 6.0}, via_ps_per_mm=6.0)
    target = PairSkewTarget("USB_D+", "USB_D-", budget_ps=3.0, delays=delays)
    assert target.tolerance_mm == pytest.approx(0.5)
    ok = _length_form_ok(PairSkewMeter(target, segment_length_parts))
    assert ok is not None
    p, n = _tight()
    assert ok(p, n)
    p, n = _tight_skewed()  # N carries 0.5 mm more copper: at the limit
    assert ok(p, n)
    n.segments[-1].y2 += 0.1
    assert not ok(p, n)
    # No ps budget: nothing to add beside the caps ``within`` already checks.
    assert (
        _length_form_ok(PairSkewMeter(PairSkewTarget("a", "b", cap_mm=0.05), segment_length_parts))
        is None
    )
