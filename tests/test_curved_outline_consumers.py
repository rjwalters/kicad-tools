"""Real routing consumers retain conservative arc/Bezier approximation bounds."""

from __future__ import annotations

import math

import numpy as np
import pytest

from kicad_tools.router import Autorouter, DesignRules
from kicad_tools.router.io import _extract_edge_segments, validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _cubic(t):
    return np.array([5 + 30 * t * t - 20 * t * t * t, 10 + 15 * t - 15 * t * t])


def _cubic_distance(point):
    # Independently minimize squared distance: its derivative is a quintic.
    # Include every real stationary point and both endpoints, not just a local
    # optimizer started near the tested chord.
    x = np.polynomial.Polynomial([5 - point[0], 0, 30, -20])
    y = np.polynomial.Polynomial([10 - point[1], 15, -15])
    roots = (x * x.deriv() + y * y.deriv()).roots()
    times = [0.0, 1.0] + [r.real for r in roots if abs(r.imag) < 1e-9 and 0 <= r.real <= 1]
    return min(math.dist(point, _cubic(t)) for t in times)


def _geometry(kind):
    if kind == "arc":
        graphic = '(gr_arc (start 15 10) (mid 10 15) (end 5 10) (layer "Edge.Cuts"))'
    else:
        graphic = '(gr_curve (pts (xy 5 10) (xy 5 15) (xy 15 15) (xy 15 10)) (layer "Edge.Cuts"))'
    edges = _extract_edge_segments(f"(kicad_pcb {graphic})")
    a, b = edges[len(edges) // 2]
    midpoint = np.array([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])
    if kind == "arc":
        normal = midpoint - [10, 10]
        normal /= np.linalg.norm(normal)
        point = np.array([10, 10]) + 5 * normal

        def distance(p):
            return abs(math.dist(p, (10, 10)) - 5)
    else:
        low, high = 0.0, 1.0
        for _ in range(60):
            t = (low + high) / 2
            if _cubic(t)[0] < midpoint[0]:
                low = t
            else:
                high = t
        t = (low + high) / 2
        point = _cubic(t)
        normal = np.array([-(15 - 30 * t), 60 * t - 60 * t * t])
        normal /= np.linalg.norm(normal)
        distance = _cubic_distance
    return edges, point, normal, distance


@pytest.mark.parametrize("kind", ["arc", "cubic"])
@pytest.mark.parametrize("side", [-1, 1], ids=["outer-boundary", "cutout"])
@pytest.mark.parametrize("unsafe", [True, False])
def test_real_clearance_and_grid_at_curve_chord_midpoint(kind, side, unsafe):
    edges, point, normal, distance = _geometry(kind)
    error = edges.max_error_mm
    assert 0 < error <= 1e-4
    threshold = 0.3 - 1e-4  # Preserve the production comparator's existing policy.
    gap = threshold - error / 8 if unsafe else threshold + 4 * error
    start = point + side * (gap + 0.1) * normal
    end = start + side * 1e-7 * normal
    measured = min(distance(start), distance(end)) - 0.1
    assert (measured < threshold) == unsafe
    router = Autorouter(
        width=22,
        height=22,
        rules=DesignRules(grid_resolution=0.05, trace_width=0.2, trace_clearance=0.2),
    )
    router._edge_segments = edges
    router._edge_clearance = 0.3
    router.routes = [
        Route(
            net=1,
            net_name="SIG",
            segments=[
                Segment(x1=start[0], y1=start[1], x2=end[0], y2=end[1], layer=Layer.F_CU, width=0.2)
            ],
            vias=[],
        )
    ]
    violations = [v for v in validate_routes(router) if v.obstacle_type == "edge"]
    assert bool(violations) == unsafe
    # Use the same retained container in the actual production grid. Samples
    # near either side of the curved boundary must remain blocked on all layers.
    router.grid.add_edge_keepout(edges, 0.3)
    probe = point + side * (0.3 - error / 8) * normal
    gx, gy = router.grid.world_to_grid(*probe)
    assert router.grid.cell_at(0, gy, gx).blocked
    assert router.grid.cell_at(1, gy, gx).blocked


def test_real_loader_preserves_mixed_disconnected_paths_without_source_rewrite(tmp_path):
    from kicad_tools.router.io import load_pcb_for_routing

    text = """(kicad_pcb (version 20241229) (generator "test")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "SIG")
      (gr_rect (start 0 0) (end 22 22) (layer "Edge.Cuts"))
      (gr_circle (center 17 15) (end 18 15) (layer "Edge.Cuts"))
      (gr_arc (start 12 10) (mid 10 12) (end 8 10) (layer "Edge.Cuts"))
      (gr_arc (start 8 10) (mid 10 8) (end 12 10) (layer "Edge.Cuts"))
      (gr_curve (pts (xy 3 3) (xy 3 5) (xy 5 5) (xy 5 3)) (layer "Edge.Cuts"))
      (gr_line (start 5 3) (end 3 3) (layer "Edge.Cuts"))
      (gr_line (start 19 17) (end 20 18) (layer "Edge.Cuts"))
      (gr_curve (pts (xy -500 -500)) (layer "F.SilkS"))
      (footprint "R" (layer "F.Cu") (at 17 5)
        (property "Reference" "R1" (at 0 -2) (layer "F.SilkS"))
        (gr_curve (pts (xy -900 -900)) (layer "Edge.Cuts"))
        (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
        (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))))"""
    path = tmp_path / "mixed.kicad_pcb"
    path.write_text(text)
    original = path.read_bytes()
    router, _ = load_pcb_for_routing(
        path, rules=DesignRules(grid_resolution=0.2), edge_clearance=0.2, validate_drc=False
    )
    edges = router._edge_segments
    assert edges == _extract_edge_segments(text)
    assert edges.max_error_mm > 0
    assert ((19, 17), (20, 18)) in edges
    assert ((5, 3), (3, 3)) in edges
    assert all(math.dist(a, b) < 3 or (a, b) in edges[:4] for a, b in edges)
    assert (router.grid.width, router.grid.height) == (22, 22)
    assert path.read_bytes() == original
    for x, y in [(12, 10), (10, 12), (8, 10), (10, 8), (3, 3), (5, 3), (19, 17), (18, 15)]:
        gx, gy = router.grid.world_to_grid(x, y)
        assert router.grid.cell_at(0, gy, gx).blocked
