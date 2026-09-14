"""Residual-angle pad bounds versus actual rectangular copper geometry."""

import math
from types import SimpleNamespace

import pytest

from kicad_tools.router.drc_nudge import _router_pad_bbox
from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.path import _path_violates
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.zones import ThermalRelief


def pad(angle=45, **kwargs):
    return Pad(x=0, y=0, width=4, height=1, net=2, net_name="other", rotation=angle, **kwargs)


def test_rotated_bbox():
    half = 2.5 / math.sqrt(2)
    assert _router_pad_bbox(pad()) == pytest.approx((-half, -half, half, half))


def test_crossing_rotated_pad_detected_by_escape_and_path():
    p = pad()
    segment = Segment(-2, 1.2, 2, 1.2, width=0.2, layer=Layer.F_CU, net=1)
    assert EscapeRouter._segment_to_pad_edge_gap(segment, p) < 0
    assert _path_violates([(-2, 1.2), (2, 1.2)], 0.2, Layer.F_CU, 0.2, [p])


def test_escape_rectangle_minimum_is_not_center_projection():
    p = pad(0)
    # Nearest rectangle corner is (2,.5), beyond the projection of its center.
    segment = Segment(1.5, 2, 3, 0.5, width=0.2, layer=Layer.F_CU, net=1)
    assert EscapeRouter._segment_to_pad_edge_gap(segment, p) == pytest.approx(
        1 / math.sqrt(2) - 0.1
    )


class Grid:
    resolution = 0.05
    rows = cols = 201

    def world_to_grid(self, x, y):
        return round(x / self.resolution) + 100, round(y / self.resolution) + 100

    def grid_to_world(self, x, y):
        return (x - 100) * self.resolution, (y - 100) * self.resolution


def thermal(p):
    return ThermalRelief(p, SimpleNamespace(), gap=0.3, spoke_width=0.2, spoke_angle=0)


def test_thermal_cardinal_corner_clearance():
    grid = Grid()
    ring = thermal(pad(0)).generate_antipad_cells(grid)
    assert grid.world_to_grid(2.2, 0.7) in ring


def test_thermal_rotated_copper_and_empty_bbox_corner():
    grid = Grid()
    ring = thermal(pad()).generate_antipad_cells(grid)
    # Actual clockwise copper near the long-axis end must remain intact.
    assert grid.world_to_grid(1.3, -1.3) not in ring
    # Empty bbox corner near the short-axis face needs clearance removal.
    assert grid.world_to_grid(0.5, 0.5) in ring


def world(angle, x, y, center=(0, 0)):
    a = math.radians(-angle)
    return center[0] + x * math.cos(a) - y * math.sin(a), center[1] + x * math.sin(
        a
    ) + y * math.cos(a)


@pytest.mark.parametrize("angle", [30, -30, 45, 89.5, 90.5, 179.5, 270.5, 0, 90, 180, 270])
@pytest.mark.parametrize("parser", ["routing", "analysis"])
def test_file_loaded_local_membership_and_clearance(tmp_path, angle, parser):
    from kicad_tools.router.io import load_pads_for_analysis, load_pcb_for_routing, validate_routes
    from kicad_tools.router.pad_geometry import pad_contains_point, pad_point_distance
    from kicad_tools.router.primitives import Route, Via
    from kicad_tools.router.rules import DesignRules

    text = f"""(kicad_pcb (version 20240108) (generator test)
      (net 0 "") (net 1 "SIGNAL") (net 2 "OTHER")
      (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts") (width .1))
      (footprint "P" (layer "F.Cu") (at 10 10)
        (property "Reference" "U1")
        (pad "1" smd rect (at 0 0 {angle}) (size 4 1) (layers "F.Cu") (net 2 "OTHER"))))"""
    path = tmp_path / "rotated.kicad_pcb"
    path.write_text(text)
    if parser == "routing":
        loaded, _ = load_pcb_for_routing(str(path), validate_drc=False)
        p = loaded.pads[("U1", "1")]
    else:
        p = load_pads_for_analysis(path.read_text())[0]
    inside = world(angle, 1.8, 0.2, (10, 10))
    outside = world(angle, 1.8, 0.8, (10, 10))
    assert pad_contains_point(p, *inside)
    assert not pad_contains_point(p, *outside)
    assert pad_point_distance(p, *outside) == pytest.approx(0.3)
    # Independent polygon oracle from the file-loaded schema's raw dimensions
    # and absolute angle, without relying on the router's cardinal swap.
    from shapely.affinity import rotate, translate
    from shapely.geometry import Point, box

    from kicad_tools.schema.pcb import PCB

    schema_board = PCB.load(path)
    footprint = schema_board.footprints[0]
    schema_pad = footprint.pads[0]
    w, h = schema_pad.size
    copper = translate(
        rotate(box(-w / 2, -h / 2, w / 2, h / 2), -schema_pad.rotation, origin=(0, 0)),
        xoff=footprint.position[0],
        yoff=footprint.position[1],
    )
    assert copper.contains(Point(*inside))
    assert not copper.contains(Point(*outside))
    assert copper.distance(Point(*outside)) == pytest.approx(pad_point_distance(p, *outside))

    # Physical local-frame traces with exactly required gap and an overlap.
    for local_y, violation in ((0.8, False), (0.79, True), (0.2, True)):
        a, b = world(angle, -1, local_y, (10, 10)), world(angle, 1, local_y, (10, 10))
        segment = Segment(*a, *b, width=0.2, layer=Layer.F_CU, net=1)
        via_pos = world(angle, 0, local_y, (10, 10))
        route = Route(
            net=1,
            net_name="SIGNAL",
            segments=[segment],
            vias=[Via(*via_pos, diameter=0.2, drill=0.1, net=1, layers=(Layer.F_CU, Layer.B_CU))],
        )
        router = SimpleNamespace(
            rules=DesignRules(trace_clearance=0.2, via_clearance=0.2),
            routes=[route],
            pads={("U1", "1"): p},
            nets={},
            net_names={1: "SIGNAL", 2: "OTHER"},
        )
        result = validate_routes(router)
        pad_hits = [v for v in result if v.obstacle_type == "pad"]
        assert bool(pad_hits) == violation
        assert bool([v for v in pad_hits if v.segment_index == -1]) == violation
        assert bool([v for v in pad_hits if v.segment_index != -1]) == violation


@pytest.mark.parametrize("angle", [30, -30, 45, 89.5, 270.5])
def test_directional_extent_matches_transformed_corner_projection(angle):
    from kicad_tools.router.pad_geometry import pad_directional_extent

    p = pad(angle)
    corners = [world(angle, x, y) for x in (-2, 2) for y in (-0.5, 0.5)]
    for dx, dy in ((1, 0), (0, 1), (2**-0.5, 2**-0.5)):
        assert pad_directional_extent(p, dx, dy) == pytest.approx(
            max(x * dx + y * dy for x, y in corners)
        )


@pytest.mark.parametrize(
    "pth,dimensions,drill,expected",
    [
        (False, (4, 1), 0, None),
        (True, (4, 1), 0.4, None),
        (True, (0, 0), 0.4, 0.55),
        (True, (0, 0), 0, 0.85),
    ],
)
def test_sparse_bounds_and_contours(pth, dimensions, drill, expected):
    from kicad_tools.router.primitives import pad_half_extents
    from kicad_tools.router.rules import DesignRules
    from kicad_tools.router.sparse import SparseRoutingGraph

    p = Pad(10, 10, *dimensions, net=2, net_name="N", rotation=45, through_hole=pth, drill=drill)
    graph = SparseRoutingGraph(20, 20, DesignRules())
    graph.add_pad(p)
    half = (expected, expected) if expected else pad_half_extents(p)
    for layer in range(2) if pth else [0]:
        assert graph.obstacles[layer][0][2:4] == pytest.approx(half)
        contours = [w for w in graph.waypoints[layer] if w.waypoint_type == "contour"]
        assert len(contours) >= 8
        assert all(not graph._point_blocked(w.x, w.y, layer) for w in contours)


@pytest.mark.parametrize("angle", [0, 30, -30, 45, 89.5])
def test_thermal_ring_and_spokes_connect_copper_to_pour(angle):
    from kicad_tools.router.pad_geometry import pad_contains_point, pad_point_distance

    p, grid = pad(angle), Grid()
    relief = thermal(p)
    ring, spokes = relief.generate_antipad_cells(grid), relief.generate_spoke_cells(grid)
    assert spokes and spokes <= ring
    assert all(not pad_contains_point(p, *grid.grid_to_world(*cell)) for cell in ring)
    assert all(pad_point_distance(p, *grid.grid_to_world(*cell)) <= 0.3 + 1e-12 for cell in ring)
    # Each contiguous spoke component must touch both pad copper and exterior.
    unseen = set(spokes)
    components = []
    while unseen:
        component = {unseen.pop()}
        todo = list(component)
        while todo:
            x, y = todo.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = (x + dx, y + dy)
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.add(neighbor)
                    todo.append(neighbor)
        components.append(component)
    assert len(components) == 4
    for component in components:
        neighbors = {
            (x + dx, y + dy) for x, y in component for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        }
        assert any(pad_contains_point(p, *grid.grid_to_world(*n)) for n in neighbors)
        assert any(pad_point_distance(p, *grid.grid_to_world(*n)) > 0.3 for n in neighbors)


def test_nearby_broad_phase_does_not_drop_rotated_neighbor(monkeypatch):
    from kicad_tools.router import path as paths
    from kicad_tools.router.rules import DesignRules

    start = Pad(0, 0, 0.5, 0.5, net=1, net_name="N", ref="U1", pin="1")
    end = Pad(2, 0, 0.5, 0.5, net=1, net_name="N", ref="U1", pin="2")
    foreign = Pad(1, 3, 8, 0.1, net=2, net_name="other", ref="U2", pin="1", rotation=45)
    lookup = {("U1", "1"): start, ("U1", "2"): end, ("U2", "1"): foreign}
    seen = []

    def check(points, width, layer, clearance, pads, **kwargs):
        seen.append(pads)
        return len(seen) == 1  # Force wrap so its broad-phase filter is exercised.

    monkeypatch.setattr(paths, "_path_violates", check)
    monkeypatch.setattr(
        paths, "_perimeter_wrap_candidates", lambda *a: [[(0, 0), (1, 0.2), (2, 0)]]
    )
    routes, _ = paths.create_intra_ic_routes(1, [("U1", "1"), ("U1", "2")], lookup, DesignRules())
    assert routes
    assert foreign in seen[1]


def test_perimeter_wrap_encloses_rotated_pad_field():
    from kicad_tools.router.path import _perimeter_wrap_candidates

    p = pad(45)
    start = Pad(-2, 0, 0.5, 0.5, net=1, net_name="N")
    end = Pad(2, 0, 0.5, 0.5, net=1, net_name="N")
    paths = _perimeter_wrap_candidates(start, end, [start, end, p], 0.2, 0.2)
    assert paths
    required_y = 2.5 / math.sqrt(2) + 0.2 + 0.1 + 0.05
    assert all(abs(y) == pytest.approx(required_y) for route in paths for x, y in route if y != 0)
    assert any(not _path_violates(route, 0.2, Layer.F_CU, 0.2, [p]) for route in paths)


def test_lateral_search_budget_reaches_rotated_long_axis(monkeypatch):
    from kicad_tools.router.escape import EscapeDirection
    from kicad_tools.router.rules import DesignRules

    router = object.__new__(EscapeRouter)
    router.via_in_pad_supported = True
    router._mfr_limits = None
    router.rules = DesignRules(via_diameter=0.6, via_drill=0.3)
    probed = []

    def reject(**kwargs):
        probed.append((kwargs["x"], kwargs["y"]))
        return False

    monkeypatch.setattr(router, "_can_place_via", reject)
    p = pad(45)
    assert router._try_lateral_via_escape(p, EscapeDirection.NORTH, 0.2, 0.2) is None
    assert probed
    assert max(abs(y) for x, y in probed) > 2.25  # Naive half-height budget stopped at1.05mm.


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_cardinal_bbox_exact_and_same_net_layer_exemptions(angle):
    from kicad_tools.router.io import _resolve_pad_dims_and_rotation, validate_routes
    from kicad_tools.router.primitives import Route, Via
    from kicad_tools.router.rules import DesignRules

    w, h, r = _resolve_pad_dims_and_rotation(angle, 4, 1)
    p = Pad(0, 0, w, h, net=1, net_name="N", rotation=r)
    assert _router_pad_bbox(p) == (-w / 2, -h / 2, w / 2, h / 2)
    seg = Segment(-1, 0, 1, 0, width=0.2, layer=Layer.F_CU, net=1)
    route = Route(
        net=1,
        net_name="N",
        segments=[seg],
        vias=[Via(0, 0, 0.6, 0.3, (Layer.F_CU, Layer.B_CU), net=1)],
    )
    router = SimpleNamespace(rules=DesignRules(), routes=[route], pads={("U", "1"): p}, nets={})
    assert not [v for v in validate_routes(router) if v.obstacle_type == "pad"]
    p.net = 2
    p.layer = Layer.B_CU
    route.vias = []
    assert not [v for v in validate_routes(router) if v.obstacle_type == "pad"]
    p.through_hole = True
    assert [v for v in validate_routes(router) if v.obstacle_type == "pad"]
