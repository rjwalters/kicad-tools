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
