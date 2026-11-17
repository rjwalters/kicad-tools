"""Native KiCad pad geometry must bound the Kelvin contact exemption."""

from pathlib import Path

import pytest
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import get_backend_info
from kicad_tools.router.io import load_pads_for_analysis
from kicad_tools.router.kelvin_obstacles import _pad_outline
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# KiCad 10.0.6 PAD.GetEffectivePolygon(F_Cu), from the adjacent saved fixture.
# Its authored +30 degree angle points clockwise in board coordinates.
NATIVE_POLYGON = Polygon(
    [(8.217949, 10.666987), (11.682051, 8.666987), (12.182051, 9.533013), (8.717949, 11.533013)]
)
FIXTURE = Path(__file__).parents[1] / "fixtures/kelvin_native/rotated-shunt.kicad_pcb"


def test_imported_rotated_pad_matches_native_polygon():
    pad = load_pads_for_analysis(FIXTURE)[0]
    assert (pad.x, pad.y, pad.width, pad.height, pad.rotation) == (10.2, 10.1, 4, 1, 30)
    # Native polygon coordinates are rounded to 1 nm.
    assert _pad_outline(pad).hausdorff_distance(NATIVE_POLYGON) < 1e-6


@pytest.mark.parametrize("force_python", [True, False])
@pytest.mark.parametrize("angle", [0, 30])
@pytest.mark.parametrize("trace_width,resolution", [(0.2, 0.1), (0.3, 0.1), (0.2, 0.05)])
def test_preserved_force_track_not_shared_outside_native_shunt(
    force_python, angle, trace_width, resolution
):
    if not force_python and not get_backend_info()["available"]:
        pytest.skip("C++ extension unavailable")
    pad = load_pads_for_analysis(FIXTURE)[0]
    router = Autorouter(
        22,
        22,
        force_python=force_python,
        physics_enabled=False,
        per_net_iterations=20000,
        rules=DesignRules(trace_width=trace_width, grid_resolution=resolution),
    )
    for ref, x, y in [("R1", pad.x, pad.y), ("Q1", 14, 12), ("U1", 16, 12), ("U2", 16, 14)]:
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": pad.width if ref == "R1" else 0.8,
                    "height": pad.height if ref == "R1" else 0.8,
                    "rotation": angle if ref == "R1" else 0,
                    "net": 1,
                    "net_name": "ISENSE_TEST",
                }
            ],
        )
    preserved = Route(1, "ISENSE_TEST", [Segment(pad.x, pad.y, 14, 12, trace_width, Layer.F_CU, 1)])
    original = preserved.copy_geometry()
    router._mark_route(preserved)
    router.routes.append(preserved)
    routes = router.route_net(1)
    assert len(routes) == 3
    assert preserved == original
    if not force_python:
        assert router.router.fallback_stats["fallback_count"] == 0
    contact = NATIVE_POLYGON if angle else box(pad.x - 2, pad.y - 0.5, pad.x + 2, pad.y + 0.5)

    def metal(route):
        return unary_union(
            [
                LineString([segment.start, segment.end]).buffer(segment.width / 2)
                for segment in route.segments
                if segment.layer == Layer.F_CU
            ]
        )

    senses = [route for route in routes if abs(route.segments[-1].end[0] - 16) < 1e-5]
    assert len(senses) == 2
    for sense in senses:
        assert metal(sense).intersection(metal(preserved)).difference(contact).area < 1e-9
