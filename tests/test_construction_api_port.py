"""Partial construction interfaces preserve endpoints and current routing contracts."""

import pytest

from tests.test_diffpair_coupled_cpp_parity import _make_grid, _make_pf, _make_simple_pair_pads
from tests.test_diffpair_routing_integration import _two_pad_diffpair_router


def test_partial_reconstruction_does_not_add_unchecked_goal_tail():
    finder = _make_pf(_make_grid(), use_cpp=False)
    pads = _make_simple_pair_pads()
    finder._cpp_reconstruct_pads = pads
    grid = finder.grid
    path = []
    for x in (2.0, 3.0, 4.0):
        px, py = grid.world_to_grid(x, 4.0)
        nx, ny = grid.world_to_grid(x, 6.0)
        path.append((px, py, 0, nx, ny, 0, False))
    routes = finder._reconstruct_coupled_routes_from_cpp_path(path, partial=True)
    for route, offset in zip(routes, (0, 3), strict=True):
        assert route.segments[-1].end == pytest.approx(
            grid.grid_to_world(*path[-1][offset : offset + 2])
        )
        assert route.segments[-1].end[0] < pads[1].x
    assert finder._cpp_reconstruct_pads is pads
    complete = finder._reconstruct_coupled_routes_from_cpp_path(path)
    assert complete[0].segments[-1].end == (pads[1].x, pads[1].y)


def test_partial_recovery_uses_remaining_pair_budget_and_normal_commit(monkeypatch):
    import time

    import kicad_tools.router.partial_recovery as recovery
    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.primitives import Route, Segment

    auto = _two_pad_diffpair_router()
    router = auto._diffpair
    router.enable_shadow_construction = False
    monkeypatch.setattr(router, "_single_ended_guide_route", lambda *a, **kw: None)
    monkeypatch.setattr(router, "_polarity_swap_between", lambda *a: True)
    native_calls = []

    def exhausted(finder, *pads, **kwargs):
        native_calls.append(kwargs)
        finder.last_coupled_backend = "cpp"
        finder.last_best_cpp_path = [(1, 1, 0, 2, 1, 0, False)]
        finder.last_iterations = kwargs["max_iterations_budget"]
        finder.last_timeout_exceeded = True
        finder.last_iteration_limited = True
        return None

    monkeypatch.setattr(CoupledPathfinder, "route_coupled", exhausted)
    seen = []
    started = time.monotonic()

    def recover(dpr, finder, pair, pads, **kwargs):
        seen.append(kwargs)
        assert dpr is router and not auto.routes
        return tuple(
            Route(
                net=a.net,
                net_name=a.net_name,
                segments=[
                    Segment(
                        x1=a.x,
                        y1=a.y,
                        x2=b.x,
                        y2=b.y,
                        width=0.2,
                        layer=a.layer,
                        net=a.net,
                        net_name=a.net_name,
                    )
                ],
            )
            for a, b in ((pads[0], pads[1]), (pads[2], pads[3]))
        )

    monkeypatch.setattr(recovery, "recover_partial_pair", recover)
    pair = router.detect_differential_pairs()[0]
    routes, _ = router.route_differential_pair_coupled(
        pair, per_pair_timeout=5, per_pair_max_iterations=20
    )
    assert len(native_calls) == 1 and native_calls[0]["max_iterations_budget"] == 20
    assert len(seen) == 1
    assert started < seen[0]["deadline"] <= time.monotonic() + 5
    assert seen[0]["board_thickness_mm"] > 0
    assert len(routes) == 2
    assert all(any(r is candidate for candidate in auto.routes) for r in routes)


def test_partial_recovery_rejects_expired_or_stale_seed():
    import time

    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.partial_recovery import recover_partial_pair

    auto = _two_pad_diffpair_router()
    pair = auto._diffpair.detect_differential_pairs()[0]
    p, n = auto._diffpair._get_pair_pads(pair)
    pads = (p[0], p[1], n[0], n[1])
    finder = CoupledPathfinder(
        auto.grid, auto.rules, target_spacing_cells=4, net_class_map=auto.net_class_map
    )
    finder.last_best_cpp_path = [(1, 1, 0, 2, 1, 0, False)]
    finder._cpp_reconstruct_pads = pads
    for deadline in (0, time.monotonic() + 5):
        assert (
            recover_partial_pair(
                auto._diffpair,
                finder,
                pair,
                pads,
                deadline=deadline,
                board_thickness_mm=1.6,
                num_copper_layers=2,
            )
            is None
        )
    assert finder._cpp_reconstruct_pads is pads
    assert not auto.routes and not auto.grid.routes
