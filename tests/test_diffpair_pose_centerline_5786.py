"""Pose-based centerline search for coupled diff pairs (Issue #5786).

Epic #5784 Phase 2.  Covers

* the ported Dubins calculator (``cpp/include/dubins.hpp``, from KRT's
  ``rust_router/src/dubins.rs``) against closed-form Dubins path lengths;
* the pose search + derived rails on small synthetic boards: open field,
  a forced detour, a closed corridor, and the C++-only contract.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend not built")

PI = math.pi


def _dubins():
    from kicad_tools.router import router_cpp

    return router_cpp.dubins_path_length, router_cpp.dubins_path_length_scaled


class TestDubinsLengths:
    """Known Dubins lengths, unit radius unless stated."""

    def test_straight_line_is_euclidean(self):
        f, _ = _dubins()
        assert f(0, 0, 0, 5, 0, 0, 1.0) == pytest.approx(5.0)
        assert f(0, 0, PI / 4, 3, 3, PI / 4, 1.0) == pytest.approx(3 * math.sqrt(2))

    def test_quarter_turn_is_a_quarter_circle(self):
        f, _ = _dubins()
        # (0,0) heading +x to (1,1) heading +y: one left arc of radius 1.
        assert f(0, 0, 0, 1, 1, PI / 2, 1.0) == pytest.approx(PI / 2)

    def test_half_circle_u_turn(self):
        f, _ = _dubins()
        assert f(0, 0, 0, 0, 2, PI, 1.0) == pytest.approx(PI)

    def test_u_turn_with_straight_between_arcs(self):
        f, _ = _dubins()
        # Two quarter arcs joined by a straight of length 2: pi + 2.
        assert f(0, 0, 0, 0, 4, PI, 1.0) == pytest.approx(PI + 2)

    def test_length_scales_with_radius(self):
        f, _ = _dubins()
        assert f(0, 0, 0, 0, 8, PI, 2.0) == pytest.approx(2 * (PI + 2))

    def test_turn_in_place_is_an_arc(self):
        f, _ = _dubins()
        # Coincident positions: the calculator approximates by dtheta * r.
        assert f(0, 0, 0, 0, 0, PI / 2, 1.0) == pytest.approx(PI / 2)

    def test_never_shorter_than_euclidean(self):
        f, _ = _dubins()
        for k1 in range(8):
            for k2 in range(8):
                got = f(0, 0, k1 * PI / 4, 7, 3, k2 * PI / 4, 1.5)
                assert got >= math.hypot(7, 3) - 1e-9

    def test_right_turn_mirrors_left_turn(self):
        f, _ = _dubins()
        left = f(0, 0, 0, 1, 1, PI / 2, 1.0)
        right = f(0, 0, 0, 1, -1, -PI / 2, 1.0)
        assert left == pytest.approx(right)

    def test_krt_scaled_contract(self):
        """KRT returns ``(len * 1000) as i32`` (truncation); the port keeps that."""
        _, scaled = _dubins()
        assert scaled(0, 0, 0, 5, 0, 0, 1.0) == 5000
        assert scaled(0, 0, 0, 1, 1, PI / 2, 1.0) == int(PI / 2 * 1000)

    def test_radius_floor(self):
        """KRT clamps the radius to >= 0.1 (``min_radius.max(0.1)``)."""
        f, _ = _dubins()
        assert f(0, 0, 0, 0, 0, PI, 0.0) == pytest.approx(PI * 0.1)


def test_dubins_port_keeps_krt_license_notice():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    header = (root / "src/kicad_tools/router/cpp/include/dubins.hpp").read_text()
    assert "Copyright (c) 2026 drandyhaas" in header
    assert "Permission is hereby granted, free of charge" in header
    assert "KiCadRoutingTools" in header
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text()
    assert "Copyright (c) 2026 drandyhaas" in notices
    assert "dubins.hpp" in notices


# ---------------------------------------------------------------------------
# Pose search on synthetic boards
# ---------------------------------------------------------------------------


def _setup(obstacle: bool = False, closed_corridor: bool = False):
    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.grid import RoutingGrid
    from kicad_tools.router.layers import Layer, LayerStack
    from kicad_tools.router.primitives import Pad, Route, Segment
    from kicad_tools.router.rules import DesignRules

    rules = DesignRules(
        trace_width=0.15,
        trace_clearance=0.15,
        via_diameter=0.45,
        via_drill=0.2,
        via_clearance=0.15,
        min_hole_to_hole=0.3,
        grid_resolution=0.05,
    )
    grid = RoutingGrid(
        width=30, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )

    def pad(x, y, net, name):
        return Pad(x=x, y=y, width=0.4, height=0.3, net=net, net_name=name, layer=Layer.F_CU)

    ps, ns = pad(5, 10.5, 1, "P"), pad(5, 9.5, 2, "N")
    pe, ne = pad(25, 10.5, 1, "P"), pad(25, 9.5, 2, "N")
    for p in (ps, ns, pe, ne):
        grid.add_pad(p)
    if obstacle:
        top = 20 if closed_corridor else 15
        wall = Route(net=9, net_name="X")
        wall.segments.append(
            Segment(
                x1=15, y1=0 if closed_corridor else 5, x2=15, y2=top,
                width=0.2, layer=Layer.F_CU, net=9, net_name="X",
            )
        )  # fmt: skip
        grid.mark_route(wall)
    pf = CoupledPathfinder(grid, rules, target_spacing_cells=5, min_spacing_cells=5)
    pf.set_net_name_to_id({"P": 1, "N": 2, "X": 9})
    return pf, (ps, pe, ns, ne)


def _coupled_fraction(p_route, n_route, pitch, tol=1.5):
    """Share of P length with a same-layer, parallel N segment within tol*pitch."""
    total = 0.0
    coupled = 0.0
    for s in p_route.segments:
        length = math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        total += length
        mx, my = (s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2
        for t in n_route.segments:
            if t.layer != s.layer:
                continue
            a1 = math.atan2(s.y2 - s.y1, s.x2 - s.x1)
            a2 = math.atan2(t.y2 - t.y1, t.x2 - t.x1)
            d = abs((a1 - a2 + PI) % (2 * PI) - PI)
            if d > 0.05 and abs(d - PI) > 0.05:
                continue
            # distance from P midpoint to N segment
            dx, dy = t.x2 - t.x1, t.y2 - t.y1
            ll = dx * dx + dy * dy
            u = max(0.0, min(1.0, ((mx - t.x1) * dx + (my - t.y1) * dy) / ll)) if ll else 0.0
            dist = math.hypot(mx - (t.x1 + u * dx), my - (t.y1 + u * dy))
            if dist <= tol * pitch:
                coupled += length
                break
    return coupled / total if total else 0.0


def test_open_field_pair_is_coupled_trunk_with_end_legs():
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup()
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=20)
    assert res is not None, pf.last_pose_report
    p_route, n_route = res
    assert pf.last_pose_report["applied"] is True
    assert not p_route.vias and not n_route.vias
    # Pad to pad, and every segment is on a 45-degree multiple.
    for route, pad_s, pad_e in ((p_route, ps, pe), (n_route, ns, ne)):
        assert (route.segments[0].x1, route.segments[0].y1) == (pad_s.x, pad_s.y)
        assert (route.segments[-1].x2, route.segments[-1].y2) == (pad_e.x, pad_e.y)
        for s in route.segments:
            ang = math.degrees(math.atan2(s.y2 - s.y1, s.x2 - s.x1)) % 45
            assert min(ang, 45 - ang) < 0.5
    # Same pitch the joint search targets, along the trunk.
    pitch = 0.305  # width + clearance floor + margin
    assert _coupled_fraction(p_route, n_route, pitch) >= 0.85


def test_detour_keeps_pair_coupled_and_clear_of_foreign_copper():
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup(obstacle=True)
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=30)
    assert res is not None, pf.last_pose_report
    p_route, n_route = res
    # The wall is a 0.2 mm track at x=15, y in [5, 15].  Both rails must stay
    # >= half-width + clearance + wall half-width from it.
    for route in (p_route, n_route):
        for s in route.segments:
            for t in (i / 20 for i in range(21)):
                x, y = s.x1 + (s.x2 - s.x1) * t, s.y1 + (s.y2 - s.y1) * t
                cy = min(max(y, 5.0), 15.0)
                d = math.hypot(x - 15.0, y - cy)
                assert d >= 0.1 + 0.15 + 0.075 - 1e-6
    assert _coupled_fraction(p_route, n_route, 0.305) >= 0.85


@pytest.mark.parametrize("obstacle", [False, True])
def test_every_emitted_segment_passes_the_clearance_kernel(obstacle):
    """No derived P/N segment may be one the shared kernel would refuse."""
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup(obstacle=obstacle)
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=30)
    assert res is not None, pf.last_pose_report
    impl = pf._get_cpp_coupled_impl()
    layer = pf.grid.layer_to_index(ps.layer.value)
    for route, partner in ((res[0], ns.net), (res[1], ps.net)):
        for s in route.segments:
            assert impl.rail_segment_clear(
                s.x1, s.y1, s.x2, s.y2, layer, route.net, partner, s.width / 2.0, 0.15
            ), (route.net_name, s)


def test_dubins_is_symmetric_for_reversed_straight_poses():
    f, _ = _dubins()
    # A straight run reversed (both headings flipped) has the same length.
    assert f(0, 0, 0, 6, 0, 0, 1.0) == pytest.approx(f(6, 0, PI, 0, 0, PI, 1.0))
    # Mirror image across the x axis: left and right families swap.
    a = f(0, 0, 0, 4, 3, PI / 2, 1.0)
    b = f(0, 0, 0, 4, -3, -PI / 2, 1.0)
    assert a == pytest.approx(b)


def test_dubins_lsl_closed_form():
    """Left arc then straight: r*pi/2 + d (an LS path, the LSL family's degenerate case)."""
    f, _ = _dubins()
    # (0,0) heading +x: a unit left quarter arc ends at (1,1) heading +y, then
    # 3 straight along +y reaches (1,4).
    assert f(0, 0, 0, 1, 4, PI / 2, 1.0) == pytest.approx(PI / 2 + 3)


def test_sealed_corridor_declines_and_reports_a_reason():
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup(obstacle=True, closed_corridor=True)
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=20, max_iterations=20_000)
    assert res is None
    assert pf.last_pose_report["applied"] is False
    assert pf.last_pose_report["reason"] in {"search-failed", "no-legal-setback"}


def test_pose_search_declines_across_layers():
    from kicad_tools.router.diffpair_pose import route_centerline_pose
    from kicad_tools.router.layers import Layer

    pf, (ps, pe, ns, ne) = _setup()
    pe.layer = Layer.B_CU
    ne.layer = Layer.B_CU
    assert route_centerline_pose(pf, ps, pe, ns, ne) is None
    assert pf.last_pose_report["reason"] == "multi-layer"


def test_polarity_flip_between_ends_is_not_coupled():
    """P above N at the start but below N at the end needs a swap via: decline."""
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup()
    pe.y, ne.y = ne.y, pe.y
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=10, max_iterations=20_000)
    assert res is None


def test_derived_rails_hold_the_pitch():
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup()
    res = route_centerline_pose(pf, ps, pe, ns, ne, timeout_seconds=20)
    assert res is not None
    p_route, n_route = res
    # Mid-trunk, the two rails are exactly one pitch apart.
    p_mid = max(p_route.segments, key=lambda s: math.hypot(s.x2 - s.x1, s.y2 - s.y1))
    n_mid = max(n_route.segments, key=lambda s: math.hypot(s.x2 - s.x1, s.y2 - s.y1))
    assert abs(p_mid.y1 - n_mid.y1) == pytest.approx(0.305, abs=1e-3)


def test_python_fallback_is_documented_cpp_only():
    from kicad_tools.router import diffpair_pose

    assert "C++-only" in (diffpair_pose.__doc__ or "")


def test_stub_ends_snap_onto_exact_pad_centres():
    """float32 stub ends must land on the pad centre (x.xx5 snap-tie hazard)."""
    from kicad_tools.router.diffpair_routing import _snap_route_ends_to_pads
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad, Route, Segment

    def pad(x, y):
        return Pad(x=x, y=y, width=0.4, height=0.3, net=1, net_name="P", layer=Layer.F_CU)

    start, end = pad(65.525, 23.365), pad(63.0, 23.555)
    route = Route(net=1, net_name="P")
    route.segments.append(
        Segment(
            x1=65.5250015258789, y1=23.364999771118164, x2=64.0, y2=23.555,
            width=0.2, layer=Layer.F_CU, net=1, net_name="P",
        )
    )  # fmt: skip
    route.segments.append(
        Segment(
            x1=64.0, y1=23.555, x2=63.0, y2=23.55500030517578,
            width=0.2, layer=Layer.F_CU, net=1, net_name="P",
        )
    )  # fmt: skip
    _snap_route_ends_to_pads(route, start, end)
    assert (route.segments[0].x1, route.segments[0].y1) == (65.525, 23.365)
    assert (route.segments[-1].x2, route.segments[-1].y2) == (63.0, 23.555)


def test_stub_end_far_from_pad_is_left_alone():
    from kicad_tools.router.diffpair_routing import _snap_route_ends_to_pads
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad, Route, Segment

    pad = Pad(x=10.0, y=10.0, width=0.4, height=0.3, net=1, net_name="P", layer=Layer.F_CU)
    route = Route(net=1, net_name="P")
    route.segments.append(
        Segment(
            x1=10.5, y1=10.0, x2=12.0, y2=10.0,
            width=0.2, layer=Layer.F_CU, net=1, net_name="P",
        )
    )  # fmt: skip
    _snap_route_ends_to_pads(route, pad, pad)
    assert (route.segments[0].x1, route.segments[0].y1) == (10.5, 10.0)


def test_claimed_nets_are_skipped_by_the_two_phase_main_pass():
    """Nets a pose-coupled pair committed must not be re-routed (Issue #5786)."""
    import inspect

    from kicad_tools.router.algorithms.two_phase import TwoPhaseRouter

    sig = inspect.signature(TwoPhaseRouter.__init__)
    assert "get_claimed_nets" in sig.parameters
    src = inspect.getsource(TwoPhaseRouter.route_all)
    assert (
        "_get_claimed_nets" in src
        and "net_order = [n for n in net_order if n not in claimed" in src
    )


def test_pose_rescue_flag_defaults_on_and_has_an_env_opt_out(monkeypatch):
    """Issue #5895 flipped the default ON; ``KCT_POSE_CENTERLINE=0`` opts out."""
    from kicad_tools.router.diffpair_routing import DiffPairRouter

    class _StubAutorouter:
        net_class_map: dict = {}

        def __getattr__(self, name):  # tolerate whatever __init__ touches
            raise AttributeError(name)

    monkeypatch.delenv("KCT_POSE_CENTERLINE", raising=False)
    try:
        assert DiffPairRouter(_StubAutorouter()).enable_pose_centerline is True
        monkeypatch.setenv("KCT_POSE_CENTERLINE", "0")
        assert DiffPairRouter(_StubAutorouter()).enable_pose_centerline is False
        monkeypatch.setenv("KCT_POSE_CENTERLINE", "1")
        assert DiffPairRouter(_StubAutorouter()).enable_pose_centerline is True
    except AttributeError:
        pytest.skip("DiffPairRouter.__init__ needs a fuller autorouter stub")


def test_span_check_is_applied_to_every_candidate_leg():
    """The caller's exact pad gate vetoes legs the kernel (partner-waived) allows."""
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    pf, (ps, pe, ns, ne) = _setup()
    seen: list[tuple] = []

    def _reject_all(*args):
        seen.append(args)
        return False

    assert route_centerline_pose(pf, ps, pe, ns, ne, span_check=_reject_all) is None
    assert pf.last_pose_report["reason"] == "no-legal-setback"
    assert seen, "span_check was never consulted"
    # (x1, y1, x2, y2, layer_index, net, width)
    assert {a[5] for a in seen} <= {ps.net, ns.net}
    assert all(a[6] == pytest.approx(0.15) for a in seen)

    res = route_centerline_pose(pf, ps, pe, ns, ne, span_check=lambda *a: True)
    assert res is not None, pf.last_pose_report


def test_pathfinder_double_without_cpp_surface_declines():
    """Joint-state-only test doubles must not crash the pose rescue."""
    from kicad_tools.router.diffpair_pose import route_centerline_pose

    class _Double:
        pass

    pf = _Double()
    assert route_centerline_pose(pf, None, None, None, None) is None  # type: ignore[arg-type]
    assert pf.last_pose_report["reason"] == "cpp-unavailable"
