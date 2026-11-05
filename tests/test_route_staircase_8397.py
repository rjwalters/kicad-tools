"""Staircase collapse survives the ``--timeout`` budget skip (issue #8397)."""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.cli import route_cmd
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
from kicad_tools.router.primitives import Route, Segment

PITCH = 0.05


def _staircase(steps: int = 12) -> list[Segment]:
    """Alternating horizontal / 45-degree one-cell steps on a 0.05 mm grid."""
    pts = [(0.0, 0.0)]
    for i in range(steps):
        x, y = pts[-1]
        pts.append((x + PITCH, y) if i % 2 == 0 else (x + PITCH, y + PITCH))
    return [
        Segment(
            x1=a[0], y1=a[1], x2=b[0], y2=b[1], width=0.2, layer=Layer.F_CU, net=1, net_name="N"
        )
        for a, b in zip(pts, pts[1:], strict=False)
    ]


def _short_per_mm(route: Route) -> float:
    length = sum(((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for s in route.segments)
    short = sum(
        1 for s in route.segments if ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 < 2 * PITCH
    )
    return short / length


class _Grid:
    def __init__(self):
        self.resyncs = []
        self.routes = []

    def get_total_overflow(self):
        return 0

    def unmark_route(self, r):
        pass

    def mark_route(self, r):
        pass

    def _mark_route_on_cpp_cells(self, r):
        pass

    def resync_route_occupancy(self, pairs):
        self.resyncs.append(pairs)


def test_staircase_only_pass_collapses_staircase(monkeypatch):
    route = Route(net=1, net_name="N", segments=_staircase(), vias=[])
    before = _short_per_mm(route)
    router = SimpleNamespace(routes=[route], grid=_Grid())

    monkeypatch.setattr(route_cmd, "_detour_net_anchors", lambda r: None)
    monkeypatch.setattr(route_cmd, "_provide_via_connection_context", lambda r, o: None)
    monkeypatch.setattr(route_cmd, "_committed_coupled_skip_nets", lambda r: set())
    monkeypatch.setattr(route_cmd, "_connectivity_snapshot", lambda r: None)
    monkeypatch.setattr(route_cmd, "_enforce_connectivity_invariant_or_exit", lambda *a, **k: None)
    monkeypatch.setattr("kicad_tools.router.optimizer.make_collision_checker", lambda *a, **k: None)

    route_cmd._compress_staircases_only(router, SimpleNamespace(), quiet=True)

    out = router.routes[0]
    assert len(out.segments) < len(route.segments)
    assert _short_per_mm(out) < before / 2
    # endpoints unchanged
    assert (out.segments[0].x1, out.segments[0].y1) == (0.0, 0.0)
    last_in, last_out = route.segments[-1], out.segments[-1]
    assert (last_out.x2, last_out.y2) == (last_in.x2, last_in.y2)


def test_staircase_only_config_does_not_run_other_passes():
    cfg = OptimizationConfig(
        merge_collinear=False,
        eliminate_zigzags=False,
        compress_staircase=True,
        convert_45_corners=False,
        pull_tight=False,
        remove_jogs=False,
        shorten_detours=False,
        minimize_vias=False,
    )
    segs = _staircase()
    out = TraceOptimizer(config=cfg).optimize_segments(segs)
    assert 0 < len(out) < len(segs)


def _stub_staircase_helpers(monkeypatch):
    monkeypatch.setattr(route_cmd, "_detour_net_anchors", lambda r: None)
    monkeypatch.setattr(route_cmd, "_provide_via_connection_context", lambda r, o: None)
    monkeypatch.setattr(route_cmd, "_committed_coupled_skip_nets", lambda r: set())
    monkeypatch.setattr(route_cmd, "_connectivity_snapshot", lambda r: None)
    monkeypatch.setattr(route_cmd, "_enforce_connectivity_invariant_or_exit", lambda *a, **k: None)
    monkeypatch.setattr("kicad_tools.router.optimizer.make_collision_checker", lambda *a, **k: None)


class _VetoGate:
    """Stands in for the HV-pairwise / Kelvin accept gate: rejects every rewrite."""

    def __init__(self):
        self.calls = 0
        self.vetoes = 0

    def __call__(self, original, optimized):
        self.calls += 1
        self.vetoes += 1
        return False

    def report(self):
        pass


def test_staircase_only_pass_runs_the_veto_gates(monkeypatch):
    """The ``--timeout`` path must honour the HV-pairwise / Kelvin gates (#4766, #5398)."""
    route = Route(net=1, net_name="N", segments=_staircase(), vias=[])
    original_segments = list(route.segments)
    router = SimpleNamespace(routes=[route], grid=_Grid())
    gate = _VetoGate()
    _stub_staircase_helpers(monkeypatch)
    monkeypatch.setattr("kicad_tools.router.optimizer.trace.pairwise_route_gate", lambda r: gate)
    monkeypatch.setattr("kicad_tools.router.optimizer.trace.kelvin_branch_gate", lambda r: None)

    route_cmd._compress_staircases_only(router, SimpleNamespace(), quiet=True)

    assert gate.calls == 1
    assert router._pairwise_optimize_vetoes == 1
    # vetoed rewrite: original copper is kept
    assert router.routes[0].segments == original_segments


def test_staircase_only_pass_goes_through_grid_synced_wrapper(monkeypatch):
    route = Route(net=1, net_name="N", segments=_staircase(), vias=[])
    router = SimpleNamespace(routes=[route], grid=_Grid())
    seen = {}

    def fake(r, optimizer, skip_nets=None):
        seen["skip"] = skip_nets
        seen["cfg"] = optimizer.config
        return r.routes

    _stub_staircase_helpers(monkeypatch)
    monkeypatch.setattr("kicad_tools.router.optimizer.optimize_routes_grid_synced", fake)

    route_cmd._compress_staircases_only(router, SimpleNamespace(), quiet=True)

    assert seen["skip"] == set()
    assert seen["cfg"].compress_staircase is True
    assert seen["cfg"].merge_collinear is False


def _drive_pipeline_to_optimizer(monkeypatch, *, fits_budget, no_optimize):
    calls = []
    monkeypatch.setattr(
        route_cmd, "_stage_boundary", lambda name: 99 if name == "drc-nudge" else None
    )
    monkeypatch.setattr(route_cmd, "_make_stage_quality_recorder", lambda a: None)
    monkeypatch.setattr(route_cmd, "_record_stage_quality", lambda *a, **k: None)
    monkeypatch.setattr(route_cmd, "_engine_post_passes_enabled", lambda a, quiet: True)
    monkeypatch.setattr(route_cmd, "_mark_nongrid_routes_for_post_pass", lambda *a, **k: None)
    monkeypatch.setattr(route_cmd, "_resolve_route_engine", lambda a: "grid")
    monkeypatch.setattr(route_cmd, "_optimization_fits_budget", lambda a, quiet: fits_budget)
    monkeypatch.setattr(route_cmd, "_optimize_traces", lambda *a, **k: calls.append("full"))
    monkeypatch.setattr(
        route_cmd, "_compress_staircases_only", lambda *a, **k: calls.append("staircase")
    )
    route = Route(net=1, net_name="N", segments=_staircase(), vias=[])
    router = SimpleNamespace(routes=[route], grid=_Grid())
    rc = route_cmd._run_post_route_pipeline(
        router, SimpleNamespace(no_optimize=no_optimize), quiet=True
    )
    assert rc == 99
    return calls


def test_pipeline_budget_skip_runs_staircase_pass(monkeypatch):
    assert _drive_pipeline_to_optimizer(monkeypatch, fits_budget=False, no_optimize=False) == [
        "staircase"
    ]


def test_pipeline_within_budget_runs_full_optimizer_only(monkeypatch):
    assert _drive_pipeline_to_optimizer(monkeypatch, fits_budget=True, no_optimize=False) == [
        "full"
    ]


def test_pipeline_no_optimize_runs_neither(monkeypatch):
    assert _drive_pipeline_to_optimizer(monkeypatch, fits_budget=False, no_optimize=True) == []


def _drive_complete_pipeline(monkeypatch, *, complete, engine, no_optimize=False):
    calls = []
    monkeypatch.setattr(
        route_cmd, "_stage_boundary", lambda name: 99 if name == "drc-nudge" else None
    )
    monkeypatch.setattr(route_cmd, "_make_stage_quality_recorder", lambda a: None)
    monkeypatch.setattr(route_cmd, "_record_stage_quality", lambda *a, **k: None)
    monkeypatch.setattr(route_cmd, "_engine_post_passes_enabled", lambda a, quiet: False)
    monkeypatch.setattr(route_cmd, "_resolve_route_engine", lambda a: engine)
    monkeypatch.setattr(route_cmd, "_optimize_traces", lambda *a, **k: calls.append("full"))
    monkeypatch.setattr(
        route_cmd, "_compress_staircases_only", lambda *a, **k: calls.append("staircase")
    )
    marked = []
    route = Route(net=1, net_name="N", segments=_staircase(), vias=[])
    fixed = Route(net=2, net_name="F", segments=[], vias=[])
    router = SimpleNamespace(
        routes=[route], existing_routes=[fixed], grid=_Grid(), _mark_route=marked.append
    )
    rc = route_cmd._run_post_route_pipeline(
        router,
        SimpleNamespace(complete=complete, no_optimize=no_optimize),
        quiet=True,
    )
    assert rc == 99
    return calls, marked, route, fixed


def test_complete_lattice_runs_staircase_pass_with_nonblind_grid(monkeypatch):
    calls, marked, route, fixed = _drive_complete_pipeline(
        monkeypatch, complete=True, engine="lattice"
    )
    assert calls == ["staircase"]
    assert marked == [route, fixed]


def test_plain_lattice_run_stays_untouched(monkeypatch):
    calls, marked, _r, _f = _drive_complete_pipeline(monkeypatch, complete=False, engine="lattice")
    assert calls == [] and marked == []


def test_complete_no_optimize_runs_nothing(monkeypatch):
    calls, marked, _r, _f = _drive_complete_pipeline(
        monkeypatch, complete=True, engine="lattice", no_optimize=True
    )
    assert calls == [] and marked == []


def test_mark_complete_copper_skips_already_registered_routes_real_grid():
    """Issue #8397 review: ``Grid.mark_route`` is not idempotent -- no duplicates."""
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.rules import DesignRules

    router = Autorouter(width=10.0, height=10.0, rules=DesignRules())
    seg = lambda net, y: Segment(  # noqa: E731
        x1=1.0, y1=y, x2=3.0, y2=y, width=0.2, layer=Layer.F_CU, net=net, net_name=f"N{net}"
    )
    preserved = Route(net=2, net_name="N2", segments=[seg(2, 5.0)], vias=[])
    new = Route(net=1, net_name="N1", segments=[seg(1, 2.0)], vias=[])
    router._mark_route(preserved)  # as load_pcb_for_routing does
    router.existing_routes = [preserved]
    router.routes = [new]

    route_cmd._mark_complete_copper_for_staircase_pass(router, quiet=True)
    route_cmd._mark_complete_copper_for_staircase_pass(router, quiet=True)

    ids = [id(r) for r in router.grid.routes]
    assert len(ids) == len(set(ids)) == 2
