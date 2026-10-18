"""Via-aware partner clearance in the coupled diff-pair search (Issue #6840).

Three defects, one per section:

1. The joint search's trail guard measured a partner VIA as a trace cell
   (``min_spacing_cells``, same layer only), so a rail could run past or
   head-on into its partner's barrel.  The post-search
   ``_pair_has_physical_overlap`` gate then threw the whole coupled route away
   (Board07 MIPI_DAT0/1, TMDS_D0/1/2; 06rf MIPI_CLK), and a near miss that did
   not overlap still committed a P/N segment-vs-via DRC shortfall.
2. That rejection printed ``centerline overlap (worst=+nanmm < 0)`` -- a NaN,
   and the name of a check that had not fired.
3. ``kct route`` never passed the committed coupled pairs to the trace
   optimizer's ``skip_nets``, so it rewrote one rail of a committed pair onto
   another row (06rf MIPI_D0: 0.40 mm uncoupled at commit, 20.1 mm final).
"""

from __future__ import annotations

import math
import re

import pytest

from kicad_tools.router.clearance_resolver import trace_via_clearance_mm
from kicad_tools.router.clearance_shapes import copper_gap, segment_shape, via_shape
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.diffpair import DifferentialPairConfig
from kicad_tools.router.diffpair_routing import CoupledPathfinder, DiffPairRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

BACKENDS = [
    "python",
    pytest.param(
        "cpp",
        marks=pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built"),
    ),
]

# ---------------------------------------------------------------------------
# Radii: the issue's recommended cell counts are a cross-check, not constants
# ---------------------------------------------------------------------------


def _radii_for(resolution: float, width: float) -> tuple[float, float, float]:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_clearance=0.2,
        via_diameter=0.6,
        via_drill=0.3,
        grid_resolution=resolution,
    )
    grid = RoutingGrid(width=4.0, height=4.0, rules=rules)
    nc = NetClassRouting(name="HS", trace_width=width, clearance=0.15)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        net_class_map={"D+": nc, "D-": nc},
    )
    return pf._partner_via_radii_cells("D+", "D-")


@pytest.mark.parametrize(
    ("resolution", "expected_cells"),
    [(0.127, 5), (0.1, 7)],  # Board07, 06rf (issue's R_via)
)
def test_trace_vs_via_radius_matches_the_issue_cross_check(resolution, expected_cells):
    r_p, r_n, r_vv = _radii_for(resolution, 0.225)
    # 0.3 (via radius) + 0.1125 (half of 0.225) + 0.2 (trace/via floor).
    assert r_p == pytest.approx(0.6125 / resolution)
    assert r_n == r_p
    assert math.ceil(r_p) == expected_cells
    assert r_vv == pytest.approx(0.8 / resolution)


# ---------------------------------------------------------------------------
# 1. The search never emits a partner-via clearance shortfall
# ---------------------------------------------------------------------------

#: Search rules for the synthetic fixture.  ``min_hole_to_hole`` is lowered
#: so the pair's own vias may sit at the start-pad pitch: the hole-to-hole
#: floor is not what this test is about.
_RULES = {
    "trace_width": 0.2,
    "trace_clearance": 0.15,
    "via_clearance": 0.2,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "grid_resolution": 0.1,
    "min_hole_to_hole": 0.25,
}


def _converging_pair(start_pitch: float, backend: str, **rule_overrides):
    """F.Cu start pads ``start_pitch`` apart, B.Cu goal pads 0.3 mm apart.

    The pair must change layer, and the cheapest place is at the wide start;
    from there the rails converge to the 3-cell target spacing and, without
    the #6840 guard, pass straight beside the partner's barrel.
    """
    rules = DesignRules(**{**_RULES, **rule_overrides})
    grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
    nc = NetClassRouting(
        name="HS", trace_width=0.2, clearance=0.15, intra_pair_clearance=0.1, coupled_routing=True
    )
    pads = (
        Pad(x=2.0, y=2.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.F_CU),
        Pad(x=2.3, y=6.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.B_CU),
        Pad(
            x=2.0 + start_pitch,
            y=2.0,
            width=0.2,
            height=0.2,
            net=2,
            net_name="DP-",
            layer=Layer.F_CU,
        ),
        Pad(x=2.6, y=6.0, width=0.2, height=0.2, net=2, net_name="DP-", layer=Layer.B_CU),
    )
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        net_class_map={"DP+": nc, "DP-": nc},
    )
    pf._use_cpp_coupled = backend == "cpp"
    for pad in pads:
        grid.add_pad(pad)
    result = pf.route_coupled(*pads, max_iterations_budget=200_000)
    if result is not None:
        assert pf.last_coupled_backend == backend
    return pf, rules, grid, result


def _partner_via_gaps(p_route: Route, n_route: Route) -> tuple[float, float]:
    """(worst P/N segment-vs-via gap, worst P/N via-vs-via gap)."""
    seg_via = via_via = math.inf
    for a, b in ((p_route, n_route), (n_route, p_route)):
        for v in a.vias:
            for s in b.segments:
                seg_via = min(seg_via, copper_gap(via_shape(v), segment_shape(s)))
    for v in p_route.vias:
        for w in n_route.vias:
            via_via = min(via_via, copper_gap(via_shape(v), via_shape(w)))
    return seg_via, via_via


def _rail_cells(route: Route, grid: RoutingGrid) -> set[tuple[int, int]]:
    cells = set()
    for s in route.segments:
        cells.add(grid.world_to_grid(s.x1, s.y1))
        cells.add(grid.world_to_grid(s.x2, s.y2))
    return cells


@pytest.mark.parametrize("backend", BACKENDS)
def test_converging_pair_keeps_trace_via_floor_from_partner_barrels(backend):
    """0.7 mm start pitch: main laid P 0.4 mm from N's via (gap 0.0 < 0.2)."""
    pf, rules, grid, result = _converging_pair(0.7, backend)
    assert result is not None, "the pair is routable with legal via placement"
    p_route, n_route = result
    assert p_route.vias and n_route.vias, "the fixture must exercise a layer change"

    floor = trace_via_clearance_mm(rules.trace_clearance, rules.via_clearance)
    seg_via, via_via = _partner_via_gaps(p_route, n_route)
    assert seg_via >= floor - 1e-9, f"P/N segment-vs-via gap {seg_via:.4f} < {floor}"
    assert via_via >= floor - 1e-9, f"P/N via-vs-via gap {via_via:.4f} < {floor}"
    assert DiffPairRouter._pair_physical_overlap_report(p_route, n_route) is None

    # Every partner rail cell (segment ends are lattice points, and a unit
    # orthogonal step is nearest a lattice via at one of its ends) is at least
    # the radius from each via of the other rail, on every layer.
    r_p, r_n, _ = pf._partner_via_radii
    for rail, partner, radius in ((p_route, n_route, r_p), (n_route, p_route, r_n)):
        for v in partner.vias:
            vx, vy = grid.world_to_grid(v.x, v.y)
            for cx, cy in _rail_cells(rail, grid):
                assert math.hypot(cx - vx, cy - vy) >= radius - 1e-9, (cx, cy, vx, vy)


@pytest.mark.parametrize("backend", BACKENDS)
def test_tight_start_pitch_never_yields_a_partner_via_overlap(backend):
    """0.6 mm start pitch: main returned an N via overlapping P by 0.1 mm.

    With vias only legal 0.8 mm apart and the start zone unable to widen that
    far, there is no clean coupled route -- the search must say so rather
    than hand back copper the commit gate would reject.
    """
    _pf, rules, _grid, result = _converging_pair(0.6, backend)
    if result is None:
        return
    p_route, n_route = result
    floor = trace_via_clearance_mm(rules.trace_clearance, rules.via_clearance)
    seg_via, via_via = _partner_via_gaps(p_route, n_route)
    assert seg_via >= floor - 1e-9, f"P/N segment-vs-via gap {seg_via:.4f} < {floor}"
    assert via_via >= floor - 1e-9, f"P/N via-vs-via gap {via_via:.4f} < {floor}"
    assert DiffPairRouter._pair_physical_overlap_report(p_route, n_route) is None


def _ffc_pair(manufacturer: str | None, backend: str):
    """06rf J4-like start: 0.3 x 0.8 mm SMD pads at 0.8 mm pitch on F.Cu."""
    kw = dict(_RULES)
    if manufacturer:
        kw["manufacturer"] = manufacturer
    rules = DesignRules(**kw)
    grid = RoutingGrid(width=8.0, height=8.0, rules=rules)
    nc = NetClassRouting(
        name="HS", trace_width=0.2, clearance=0.15, intra_pair_clearance=0.1, coupled_routing=True
    )
    pads = (
        Pad(x=2.0, y=2.0, width=0.3, height=0.8, net=1, net_name="DP+", layer=Layer.F_CU),
        Pad(x=2.3, y=6.0, width=0.2, height=0.2, net=1, net_name="DP+", layer=Layer.B_CU),
        Pad(x=2.8, y=2.0, width=0.3, height=0.8, net=2, net_name="DP-", layer=Layer.F_CU),
        Pad(x=2.6, y=6.0, width=0.2, height=0.2, net=2, net_name="DP-", layer=Layer.B_CU),
    )
    for pad in pads:
        grid.add_pad(pad)
    pf = CoupledPathfinder(
        grid=grid,
        rules=rules,
        target_spacing_cells=3,
        min_spacing_cells=3,
        net_class_map={"DP+": nc, "DP-": nc},
    )
    pf._use_cpp_coupled = backend == "cpp"
    return rules, grid, pf.route_coupled(*pads, max_iterations_budget=200_000)


def _vias_in_smd_pads(grid: RoutingGrid, routes, drill: float) -> list[tuple[float, float]]:
    from kicad_tools.router.pad_geometry import pad_point_distance

    return [
        (via.x, via.y)
        for route in routes
        for via in route.vias
        for pad in grid._pads
        if not pad.through_hole and pad_point_distance(pad, via.x, via.y) < drill / 2
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_via_in_an_smd_pad_when_the_fab_cannot_fill_it(backend):
    """jlcpcb has no via-in-pad process; the endpoint waiver must not bypass it.

    The coupled via move skips ``_is_via_blocked`` at the pair's own pads, so
    before #6840 the cheapest route drilled both vias straight into the F.Cu
    start pads (06rf MIPI_CLK at 700k: two ``via_in_pad`` errors in
    ``kct check --mfr jlcpcb``).  Without a manufacturer the in-pad vias are
    still allowed (the control).
    """
    rules, grid, result = _ffc_pair(None, backend)
    assert result is not None
    assert _vias_in_smd_pads(grid, result, rules.via_drill), "control: in-pad vias allowed"

    rules, grid, result = _ffc_pair("jlcpcb", backend)
    assert result is not None
    assert all(route.vias for route in result)
    assert _vias_in_smd_pads(grid, result, rules.via_drill) == []


def test_backends_agree_on_the_converging_pair():
    if not is_cpp_available():
        pytest.skip("C++ backend not built")
    _, _, _, py = _converging_pair(0.7, "python")
    _, _, _, cpp = _converging_pair(0.7, "cpp")
    assert py is not None and cpp is not None
    for a, b in zip(py, cpp, strict=True):
        assert [(v.x, v.y) for v in a.vias] == [(v.x, v.y) for v in b.vias]
        assert [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in a.segments] == [
            (s.x1, s.y1, s.x2, s.y2, s.layer) for s in b.segments
        ]


# ---------------------------------------------------------------------------
# 2. The overlap rejection reports a finite gap and the real check
# ---------------------------------------------------------------------------


def _via_vs_segment_pair() -> tuple[Route, Route]:
    """N via (d=0.6) whose centre is 0.381 mm from a 0.225 mm P trace on B.Cu.

    The Board07 MIPI_DAT0 shape: 3 cells x 0.127 mm, copper gap
    0.381 - 0.3 - 0.1125 = -0.0315 mm.
    """
    p = Route(net=1, net_name="USB_D+")
    p.segments.append(
        Segment(
            x1=5.0,
            y1=4.619,
            x2=8.0,
            y2=4.619,
            width=0.225,
            layer=Layer.B_CU,
            net=1,
            net_name="USB_D+",
        )
    )
    n = Route(net=2, net_name="USB_D-")
    n.vias.append(
        Via(
            x=6.0,
            y=5.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=2,
            net_name="USB_D-",
        )
    )
    return p, n


def test_overlap_report_gives_the_worst_gap_and_element_kinds():
    p, n = _via_vs_segment_pair()
    report = DiffPairRouter._pair_physical_overlap_report(p, n)
    assert report is not None
    assert math.isfinite(report.gap_mm)
    assert report.gap_mm == pytest.approx(-0.0315, abs=1e-6)
    assert report.description == "N via vs P segment on B.Cu"
    # Moving the via clear removes the finding.
    n.vias[0] = Via(x=6.0, y=5.3, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
    assert DiffPairRouter._pair_physical_overlap_report(p, n) is None


def _two_pad_router() -> Autorouter:
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15, grid_resolution=0.1)
    nc = NetClassRouting(name="HS", coupled_routing=True)
    router = Autorouter(
        width=30.0, height=10.0, rules=rules, net_class_map={"USB_D+": nc, "USB_D-": nc}
    )
    for ref, x in (("U1", 5.0), ("J1", 25.0)):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": 4.6,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 1,
                    "net_name": "USB_D+",
                },
                {
                    "number": "2",
                    "x": x,
                    "y": 5.4,
                    "width": 0.4,
                    "height": 0.4,
                    "net": 2,
                    "net_name": "USB_D-",
                },
            ],
        )
    return router


def test_rejection_line_names_the_overlap_with_a_finite_gap(monkeypatch, capsys):
    monkeypatch.setattr(DiffPairRouter, "_shadow_route_pair", lambda self, *a, **k: None)
    router = _two_pad_router()
    router._diffpair.enable_pose_centerline = False
    [pair] = router._diffpair.detect_differential_pairs()

    def fake_route_coupled(self, *args, **kwargs):
        self.last_timeout_exceeded = False
        self.last_iterations = 1
        return _via_vs_segment_pair()

    monkeypatch.setattr(CoupledPathfinder, "route_coupled", fake_route_coupled)
    routes, _ = router._diffpair.route_differential_pair_coupled(
        pair, coupled_only=True, per_pair_timeout=30.0, per_pair_max_iterations=1000
    )
    out = capsys.readouterr().out

    assert routes == []
    # "+nanmm" was the old line; "dominant" must not count.
    assert not re.search(r"(?<![a-z])nan", out, re.IGNORECASE)
    assert "centerline overlap" not in out
    assert "via-aware P/N overlap: N via vs P segment on B.Cu, gap -0.0315mm" in out
    # A rejected pair is not a committed coupled pair.
    assert not router._committed_coupled_nets


# ---------------------------------------------------------------------------
# 3. Committed coupled pairs are kept out of the trace optimizer
# ---------------------------------------------------------------------------


def test_committed_coupled_pair_is_recorded_and_skipped_by_the_optimizer():
    from kicad_tools.cli.route_cmd import _committed_coupled_skip_nets

    router = _two_pad_router()
    _routes, _warnings, routed = router.route_diffpair_prepass(
        DifferentialPairConfig(enabled=True, spacing=0.8)
    )
    assert routed == {1, 2}
    assert router._committed_coupled_nets == {1, 2}
    assert _committed_coupled_skip_nets(router) == {1, 2}


def test_no_committed_pairs_means_no_skip_set():
    from kicad_tools.cli.route_cmd import _committed_coupled_skip_nets

    router = _two_pad_router()
    assert _committed_coupled_skip_nets(router) is None


def _split_rail(net: int, name: str, y: float) -> Route:
    """A straight 5 -> 25 mm rail in three collinear pieces.

    ``merge_collinear`` always rewrites it into one segment, so an untouched
    rail after the optimizer can only mean the optimizer skipped the net.
    """
    route = Route(net=net, net_name=name)
    for x1, x2 in ((5.0, 10.0), (10.0, 15.0), (15.0, 25.0)):
        route.segments.append(
            Segment(x1=x1, y1=y, x2=x2, y2=y, width=0.2, layer=Layer.F_CU, net=net, net_name=name)
        )
    return route


def _optimizer_fixture(committed: set[int]) -> Autorouter:
    """Pair 1/2 plus an unrelated net 3, all routed as split straight rails."""
    router = _two_pad_router()
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 5.0, "y": 8.0, "width": 0.4, "height": 0.4, "net": 3,
             "net_name": "SIG"},
            {"number": "2", "x": 25.0, "y": 8.0, "width": 0.4, "height": 0.4, "net": 3,
             "net_name": "SIG"},
        ],
    )  # fmt: skip
    for route in (
        _split_rail(1, "USB_D+", 4.6),
        _split_rail(2, "USB_D-", 5.4),
        _split_rail(3, "SIG", 8.0),
    ):
        router.routes.append(route)
        router.grid.mark_route(route)
    router._committed_coupled_nets = set(committed)
    return router


def _copper(router: Autorouter, net: int) -> list[tuple]:
    return [
        (s.x1, s.y1, s.x2, s.y2, s.width, s.layer)
        for r in router.routes
        if r.net == net
        for s in r.segments
    ]


def test_optimizer_pass_leaves_a_committed_coupled_pairs_copper_unchanged():
    """``kct route``'s optimizer pass skips the committed pair, not the rest.

    Drives ``_optimize_traces`` -- the one optimizer pass every ``kct route``
    pipeline calls (#6847) -- on real routes: the committed pair's copper is
    exactly what the coupled pre-pass laid, while an ordinary net in the same
    pass is still optimized.
    """
    import argparse

    from kicad_tools.cli.route_cmd import _optimize_traces

    router = _optimizer_fixture({1, 2})
    before = {net: _copper(router, net) for net in (1, 2, 3)}
    _optimize_traces(router, argparse.Namespace(strict=False, verbose=False), quiet=True)

    assert _copper(router, 1) == before[1]
    assert _copper(router, 2) == before[2]
    assert len(_copper(router, 3)) == 1, "control: the optimizer ran on the other net"


def test_optimizer_pass_rewrites_the_same_rails_when_nothing_is_committed():
    """Control: the pair's split rails are optimizable, so the skip is real."""
    import argparse

    from kicad_tools.cli.route_cmd import _optimize_traces

    router = _optimizer_fixture(set())
    _optimize_traces(router, argparse.Namespace(strict=False, verbose=False), quiet=True)
    assert len(_copper(router, 1)) == 1
    assert len(_copper(router, 2)) == 1
