"""Instrumentation / ground-truth tests for the coupled diff-pair router.

Issue #4459 (Phase 1 of the #4409 epic): diagnostic-only wiring that makes the
0/9 coupled-routing failure triageable.  Three things are asserted here:

1. The ``[coupled-timing]`` diagnostic no longer prints a categorically-``None``
   ``best_state`` on the C++ path (the "best_state=None red herring") -- it
   reports ``backend=cpp`` and ``best_state=n/a (cpp)`` and leans on the real
   ``best_progress`` / dominant-rejection signal instead.
2. ``CoupledPathfinder.last_rejections`` is NON-EMPTY on the C++ path -- the
   per-reason rejection histogram is wired out of the C++ joint search (it was
   previously hard-emptied, so no frontier-pruning signal survived).
3. Every attempted pair is classified into the failure taxonomy and a
   structured ``[coupled-pair-report]`` line is emitted.

None of this changes routing behaviour or geometry -- these tests only assert
the diagnostics, never a different route.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair_routing import (
    COUPLED_OUTCOME_GUIDE_MISSING,
    COUPLED_OUTCOME_JOINT_PLATEAU,
    COUPLED_OUTCOME_LANDING_STALL,
    COUPLED_OUTCOME_SHADOW_BLOCKAGE,
    COUPLED_OUTCOME_SHADOW_OVERLAP,
    CoupledPairReport,
    CoupledPathfinder,
    _count_off_angle_segments,
    build_corridor_mask,
    classify_coupled_pair_outcome,
    dominant_rejection,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules

# The pure-Python coupled loop's rejection vocabulary (diffpair_routing.py); the
# C++ histogram must key on the SAME reasons (plus the via-guard superset).
_KNOWN_REJECTION_KEYS = {
    "sym_blocked_p",
    "sym_blocked_n",
    "sym_spacing",
    "sym_floor",
    "sym_trail",
    "asym_blocked_p",
    "asym_spacing_p",
    "asym_floor_p",
    "asym_trail_p",
    "asym_blocked_n",
    "asym_spacing_n",
    "asym_floor_n",
    "asym_trail_n",
    "via_blocked_p",
    "via_blocked_n",
    "via_trace_blocked_p",
    "via_trace_blocked_n",
    "corridor",
}


# ---------------------------------------------------------------------------
# Pure-unit taxonomy / helper tests (no backend required)
# ---------------------------------------------------------------------------


def test_dominant_rejection_picks_highest_count():
    assert dominant_rejection({"sym_floor": 3, "corridor": 9, "sym_spacing": 5}) == "corridor"


def test_dominant_rejection_ties_break_alphabetically():
    # corridor and sym_spacing both 5 -> alphabetically-first ("corridor").
    assert dominant_rejection({"sym_spacing": 5, "corridor": 5}) == "corridor"


def test_dominant_rejection_empty_is_none():
    assert dominant_rejection({}) is None
    assert dominant_rejection(None) is None


def test_classify_coupled_ok_when_coupled():
    assert (
        classify_coupled_pair_outcome(
            coupled=True,
            coupled_phase="corridor",
            guide_ok=True,
            best_progress=0.0,
            shadow_enabled=False,
            shadow_decline_reason=None,
        )
        == "coupled-ok"
    )


def test_classify_guide_missing():
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=False,
            best_progress=float("inf"),
            shadow_enabled=False,
            shadow_decline_reason=None,
        )
        == COUPLED_OUTCOME_GUIDE_MISSING
    )


def test_classify_joint_plateau_far_from_goal():
    # Far-from-goal stall (best_progress well above the near-miss threshold).
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=True,
            best_progress=398.0,
            shadow_enabled=False,
            shadow_decline_reason=None,
            near_miss_cells=60,
        )
        == COUPLED_OUTCOME_JOINT_PLATEAU
    )


def test_classify_landing_stall_near_goal():
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=True,
            best_progress=12.0,
            shadow_enabled=False,
            shadow_decline_reason=None,
            near_miss_cells=60,
        )
        == COUPLED_OUTCOME_LANDING_STALL
    )


def test_classify_shadow_decline_overlap_and_blockage():
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=True,
            best_progress=float("inf"),
            shadow_enabled=True,
            shadow_decline_reason="overlap",
        )
        == COUPLED_OUTCOME_SHADOW_OVERLAP
    )
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=True,
            best_progress=float("inf"),
            shadow_enabled=True,
            shadow_decline_reason="blockage",
        )
        == COUPLED_OUTCOME_SHADOW_BLOCKAGE
    )


def test_shadow_decline_reason_ignored_when_shadow_off():
    # A decline reason set from a prior pair must not leak into a flag-off
    # classification (shadow is not even attempted with the flag off).
    assert (
        classify_coupled_pair_outcome(
            coupled=False,
            coupled_phase="open",
            guide_ok=True,
            best_progress=400.0,
            shadow_enabled=False,
            shadow_decline_reason="overlap",
        )
        == COUPLED_OUTCOME_JOINT_PLATEAU
    )


def test_count_off_angle_segments():
    route = Route(net=1, net_name="G")
    # horizontal (on), vertical (on), 45 (on), off-angle (off).
    route.segments.append(Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU, net=1))
    route.segments.append(Segment(x1=5, y1=0, x2=5, y2=5, width=0.2, layer=Layer.F_CU, net=1))
    route.segments.append(Segment(x1=5, y1=5, x2=8, y2=8, width=0.2, layer=Layer.F_CU, net=1))
    route.segments.append(Segment(x1=8, y1=8, x2=12, y2=9, width=0.2, layer=Layer.F_CU, net=1))
    assert _count_off_angle_segments(route) == 1
    assert _count_off_angle_segments(None) == 0


def test_pair_report_format_line_has_all_fields():
    report = CoupledPairReport(
        pair_name="MIPI_CLK",
        classification=COUPLED_OUTCOME_JOINT_PLATEAU,
        coupled=False,
        backend="cpp",
        coupled_phase="open",
        guide_ok=True,
        best_progress=398.0,
        dominant_rejection="corridor",
        start_pitch_cells=10.0,
        end_pitch_cells=8.0,
        target_spacing_cells=5,
        off_angle_segments=0,
        shadow_enabled=False,
    )
    line = report.format_line()
    assert "[coupled-pair-report]" in line
    assert "pair=MIPI_CLK" in line
    assert f"class={COUPLED_OUTCOME_JOINT_PLATEAU}" in line
    assert "backend=cpp" in line
    assert "best_progress=398" in line
    assert "dominant_rejection=corridor" in line
    # inf renders as a stable token, never a Python float repr.
    inf_report = CoupledPairReport(
        pair_name="X",
        classification=COUPLED_OUTCOME_GUIDE_MISSING,
        coupled=False,
        backend="cpp",
        coupled_phase="open",
        guide_ok=False,
        best_progress=float("inf"),
        dominant_rejection=None,
        start_pitch_cells=1.0,
        end_pitch_cells=1.0,
        target_spacing_cells=2,
        off_angle_segments=0,
        shadow_enabled=False,
    )
    assert "best_progress=inf" in inf_report.format_line()


# ---------------------------------------------------------------------------
# C++ rejection-histogram wiring (acceptance criterion 2)
# ---------------------------------------------------------------------------


def _make_grid(width: float = 12.7, height: float = 12.7) -> RoutingGrid:
    return RoutingGrid(width=width, height=height, rules=DesignRules())


def _simple_pair_pads() -> tuple[Pad, Pad, Pad, Pad]:
    p_start = Pad(x=2.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    p_end = Pad(x=10.0, y=4.0, width=0.4, height=0.4, net=1, net_name="D+", layer=Layer.F_CU)
    n_start = Pad(x=2.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    n_end = Pad(x=10.0, y=6.0, width=0.4, height=0.4, net=2, net_name="D-", layer=Layer.F_CU)
    return p_start, p_end, n_start, n_end


def _make_pf(grid: RoutingGrid, use_cpp: bool) -> CoupledPathfinder:
    pf = CoupledPathfinder(
        grid=grid, rules=DesignRules(), target_spacing_cells=2, min_spacing_cells=2
    )
    pf._use_cpp_coupled = use_cpp
    return pf


@pytest.mark.skipif(
    not is_cpp_available(),
    reason="rejection-histogram wiring requires the router_cpp backend (kct build-native)",
)
def test_cpp_path_populates_rejection_histogram_on_budget_exit():
    """On a budget-exited C++ joint search ``last_rejections`` is non-empty.

    Previously ``_try_cpp_route_coupled`` hard-set ``last_rejections`` to an
    empty defaultdict on EVERY C++ return, so no signal survived about which
    guard pruned the frontier.  The histogram is now wired out of the C++
    search; a small joint search that exhausts its iteration budget must
    surface at least one rejection reason, all drawn from the known
    vocabulary.
    """
    pads = _simple_pair_pads()
    pf = _make_pf(_make_grid(), use_cpp=True)
    # A tiny iteration budget forces a budget-exit while the spacing/floor
    # guards are actively pruning off-target neighbours.
    res = pf.route_coupled(*pads, max_iterations_budget=16)
    assert res is None, "tiny budget must not converge (setup guard)"
    assert pf.last_coupled_backend == "cpp"
    assert len(pf.last_rejections) > 0, "C++ path must surface a rejection histogram"
    assert all(v > 0 for v in pf.last_rejections.values())
    unknown = set(pf.last_rejections) - _KNOWN_REJECTION_KEYS
    assert not unknown, f"unexpected rejection keys: {unknown}"
    # The dominant reason is a real, non-None signal now.
    assert dominant_rejection(pf.last_rejections) in _KNOWN_REJECTION_KEYS


@pytest.mark.skipif(
    not is_cpp_available(),
    reason="corridor rejection test requires the router_cpp backend (kct build-native)",
)
def test_cpp_path_reports_corridor_rejections_inside_tight_corridor():
    """A corridor-bounded C++ search records ``corridor`` frontier pruning."""
    grid = _make_grid()
    pads = _simple_pair_pads()
    p_start, p_end, n_start, n_end = pads
    guide = Route(net=1, net_name="GUIDE")
    guide.segments.append(
        Segment(x1=2.0, y1=5.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
    )
    # A DELIBERATELY tight corridor (radius 3) so many neighbours land outside
    # it and are pruned as ``corridor`` rejections; a tiny budget forces exit.
    corridor = build_corridor_mask(
        grid,
        guide,
        radius_cells=3,
        extra_cells=(
            grid.world_to_grid(p_start.x, p_start.y),
            grid.world_to_grid(p_end.x, p_end.y),
            grid.world_to_grid(n_start.x, n_start.y),
            grid.world_to_grid(n_end.x, n_end.y),
        ),
    )
    pf = _make_pf(grid, use_cpp=True)
    pf.route_coupled(*pads, max_iterations_budget=32, corridor=corridor)
    assert pf.last_rejections.get("corridor", 0) > 0, (
        f"tight corridor must prune neighbours; got {dict(pf.last_rejections)}"
    )


@pytest.mark.skipif(
    not is_cpp_available(),
    reason="backend-marker test requires the router_cpp backend (kct build-native)",
)
def test_last_coupled_backend_marker_tracks_backend():
    """``last_coupled_backend`` distinguishes the C++ vs pure-Python search."""
    pads = _simple_pair_pads()

    cpp_pf = _make_pf(_make_grid(), use_cpp=True)
    cpp_pf.route_coupled(*pads, max_iterations_budget=16)
    assert cpp_pf.last_coupled_backend == "cpp"

    py_pf = _make_pf(_make_grid(), use_cpp=False)
    py_pf.route_coupled(*pads, max_iterations_budget=16)
    assert py_pf.last_coupled_backend == "python"


# ---------------------------------------------------------------------------
# End-to-end diagnostic emission (acceptance criteria 1 & 3) via a stub
# pathfinder -- no backend required, and no real routing performed.
# ---------------------------------------------------------------------------


def _two_pad_router_and_pair():
    """A 2-pad diff pair + its router, ready for the coupled pre-phase."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair import (
        DifferentialPair,
        DifferentialPairType,
        DifferentialSignal,
    )

    router = Autorouter(width=30.0, height=10.0, rules=DesignRules())
    p_y, n_y = 4.8, 5.2
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 25.0,
                "y": p_y,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "USB_D+",
            },
            {
                "number": "2",
                "x": 25.0,
                "y": n_y,
                "width": 0.4,
                "height": 0.4,
                "net": 2,
                "net_name": "USB_D-",
            },
        ],
    )
    pair = DifferentialPair(
        name="USB_D",
        positive=DifferentialSignal(
            net_name="USB_D+", net_id=1, base_name="USB_D", polarity="P", notation="plus_minus"
        ),
        negative=DifferentialSignal(
            net_name="USB_D-", net_id=2, base_name="USB_D", polarity="N", notation="plus_minus"
        ),
        pair_type=DifferentialPairType.USB2,
    )
    return router, pair


class _CppLikeStubPathfinder:
    """Stub emulating a C++ joint search that made progress but did not couple.

    Mirrors the ``_try_cpp_route_coupled`` state: ``last_best_state`` is ``None``
    (the C++ path carries no Python ``CoupledState``) but ``last_best_progress``
    and ``last_rejections`` hold the REAL signal, and ``last_coupled_backend``
    is ``"cpp"``.
    """

    def __init__(self):
        self.last_timeout_exceeded = False
        self.last_iteration_limited = False
        self.last_iterations = 1000
        self.last_best_progress = 398.0
        self.last_best_state = None  # the historic red herring
        self.last_best_node = None
        self.last_coupled_backend = "cpp"
        self.last_rejections = {"corridor": 120, "sym_spacing": 40}

    def route_coupled(self, *_a, **_k):
        return None  # deferred (did not couple)


def _install_stub(monkeypatch):
    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "CoupledPathfinder", lambda *a, **k: _CppLikeStubPathfinder())


def test_coupled_timing_kills_best_state_none_red_herring(monkeypatch, capsys):
    """The ``[coupled-timing]`` line must NOT print ``best_state=None`` on the
    C++ path -- it reports ``backend=cpp`` / ``best_state=n/a (cpp)`` and the
    real ``best_progress`` + dominant-rejection instead."""
    router, pair = _two_pad_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    # A guide route exists so the pair is classified as a joint-A* stall, not
    # guide-missing.
    guide = Route(net=1, net_name="USB_D+")
    guide.segments.append(
        Segment(x1=5.0, y1=5.0, x2=25.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
    )
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: guide)
    _install_stub(monkeypatch)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)
    out = capsys.readouterr().out

    assert "[coupled-timing]" in out
    assert "backend=cpp" in out
    assert "best_state=n/a (cpp)" in out
    assert "best_state=None" not in out, (
        f"the best_state=None red herring must be gone; got: {out!r}"
    )
    assert "best_progress=398.0" in out
    assert "dominant_rejection=corridor" in out


def test_coupled_pair_report_emitted_with_classification(monkeypatch, capsys):
    """A structured ``[coupled-pair-report]`` line is emitted, classifying the
    pair into the failure taxonomy (here a far-from-goal joint-A* plateau)."""
    router, pair = _two_pad_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    guide = Route(net=1, net_name="USB_D+")
    guide.segments.append(
        Segment(x1=5.0, y1=5.0, x2=25.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=1)
    )
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: guide)
    _install_stub(monkeypatch)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)
    out = capsys.readouterr().out

    assert "[coupled-pair-report]" in out
    assert "pair=USB_D" in out
    assert f"class={COUPLED_OUTCOME_JOINT_PLATEAU}" in out
    assert "coupled=False" in out
    assert "guide_ok=True" in out
    # The report is retained on the router for programmatic consumption.
    report = dpr._last_coupled_pair_report
    assert report is not None
    assert report.classification == COUPLED_OUTCOME_JOINT_PLATEAU
    assert report.dominant_rejection == "corridor"


def test_coupled_pair_report_classifies_guide_missing(monkeypatch, capsys):
    """With no single-ended guide the pair is classified ``guide-missing``."""
    router, pair = _two_pad_router_and_pair()
    dpr = router._diffpair
    dpr.enable_shadow_construction = False
    monkeypatch.setattr(dpr, "_single_ended_guide_route", lambda *a, **k: None)
    _install_stub(monkeypatch)

    dpr.route_differential_pair_coupled(pair, coupled_only=True)
    out = capsys.readouterr().out

    assert f"class={COUPLED_OUTCOME_GUIDE_MISSING}" in out
    assert dpr._last_coupled_pair_report is not None
    assert dpr._last_coupled_pair_report.classification == COUPLED_OUTCOME_GUIDE_MISSING
