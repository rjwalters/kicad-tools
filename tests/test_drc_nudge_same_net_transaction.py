"""Same-net via exits must retain copper connectivity without creating shorts."""

import pytest
from shapely.geometry import LineString, Point, box

from kicad_tools.router.core import Autorouter
from kicad_tools.router.drc_nudge import drc_verify_and_nudge
from kicad_tools.router.io import validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _scene(foreign=False, connector_layer=Layer.F_CU):
    router = Autorouter(
        width=20,
        height=20,
        origin_x=130,
        origin_y=110,
        rules=DesignRules(trace_clearance=0.15, manufacturer="jlcpcb"),
        force_python=True,
    )
    router.add_component(
        "R5",
        [
            {
                "number": "1",
                "x": 139.5875,
                "y": 117.5,
                "width": 1.025,
                "height": 1.4,
                "net": 8,
                "net_name": "VCC",
                "layer": Layer.F_CU,
            },
        ]
        + (
            [
                {
                    "number": "2",
                    "x": 141.4125,
                    "y": 117.5,
                    "width": 1.025,
                    "height": 1.4,
                    "net": 1,
                    "net_name": "RESET",
                    "layer": Layer.F_CU,
                }
            ]
            if foreign
            else []
        ),
    )
    via = Via(
        x=139.89999389648438,
        y=117.5,
        diameter=0.8,
        drill=0.3,
        layers=(Layer.B_CU, Layer.F_CU),
        net=8,
        net_name="VCC",
    )
    segment = Segment(
        via.x, via.y, 139.58749389648438, 117.5, 0.5, connector_layer, net=8, net_name="VCC"
    )
    router.routes = [Route(net=8, net_name="VCC", segments=[segment], vias=[via])]
    return router, via, segment


def test_public_same_net_exit_rejects_exact_r5_reset_overlap():
    router, via, segment = _scene(foreign=True)
    before = (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2)
    assert validate_routes(router) == []
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2) == before
    assert validate_routes(router) == []
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_public_same_net_exit_allows_pad_contact_replaced_by_trace():
    router, via, segment = _scene()
    result = drc_verify_and_nudge(router, max_passes=1)
    assert via.x == pytest.approx(140.665)
    assert segment.x1 == via.x
    pad = box(139.075, 116.8, 140.1, 118.2)
    assert Point(via.x, via.y).distance(pad) > via.diameter / 2
    trace = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)])
    assert trace.intersects(pad)
    assert trace.distance(Point(via.x, via.y)) == 0
    assert result.vias_nudged == 1
    assert validate_routes(router) == []


def test_same_net_exit_cannot_replace_front_pad_contact_with_back_trace():
    router, via, segment = _scene(connector_layer=Layer.B_CU)
    before = (via.x, segment.x1)
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, segment.x1) == before
    assert result.skipped.get("via_pad_contact_blocked") == 1


@pytest.mark.parametrize("endpoint,expected_moves", [((11.0, 11.0), 0), ((10.0, 10.0), 1)])
def test_circular_pad_exit_requires_real_copper_contact(endpoint, expected_moves):
    router = Autorouter(
        width=20,
        height=20,
        rules=DesignRules(trace_clearance=0.15, manufacturer="jlcpcb"),
        force_python=True,
    )
    router.add_component(
        "R1",
        [
            {
                "number": "1",
                "x": 10.0,
                "y": 10.0,
                "width": 2.0,
                "height": 2.0,
                "shape": "circle",
                "net": 1,
                "net_name": "VCC",
                "layer": Layer.F_CU,
            }
        ],
    )
    via = Via(
        x=10.8,
        y=10.7,
        diameter=0.8,
        drill=0.3,
        layers=(Layer.F_CU, Layer.B_CU),
        net=1,
        net_name="VCC",
    )
    segment = Segment(via.x, via.y, *endpoint, 0.1, Layer.F_CU, net=1, net_name="VCC")
    router.routes = [Route(net=1, net_name="VCC", segments=[segment], vias=[via])]
    before = (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2)
    pad_copper = Point(10, 10).buffer(1, quad_segs=256)
    assert Point(via.x, via.y).buffer(0.4).intersection(pad_copper).area > 0

    result = drc_verify_and_nudge(router, max_passes=1)

    assert result.vias_nudged == expected_moves
    if not expected_moves:
        assert (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2) == before
        assert result.skipped.get("via_pad_contact_blocked") == 1
    else:
        assert (via.x, via.y) != before[:2]
        assert Point(via.x, via.y).buffer(0.4).intersection(pad_copper).area == 0
        trace = LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).buffer(0.05)
        assert trace.intersection(pad_copper).area > 0
        assert trace.intersection(Point(via.x, via.y).buffer(0.4)).area > 0


def test_same_net_exit_preserves_fixed_track_interior_contact():
    router, via, segment = _scene()
    fixed = Segment(139.7, 116.0, 139.7, 119.0, 0.1, Layer.B_CU, net=8)
    router.existing_routes = [Route(net=8, net_name="VCC", segments=[fixed])]
    original = (via.x, segment.x1, fixed.x1, fixed.y1, fixed.x2, fixed.y2)
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, segment.x1, fixed.x1, fixed.y1, fixed.x2, fixed.y2) == original
    assert result.skipped.get("via_pad_contact_blocked") == 1


def test_same_net_exit_can_use_preserved_via_to_reach_front_pad():
    router, via, segment = _scene(connector_layer=Layer.B_CU)
    bridge = Via(
        x=138.5,
        y=117.5,
        diameter=0.3,
        drill=0.1,
        layers=(Layer.F_CU, Layer.B_CU),
        net=8,
        net_name="VCC",
    )
    fixed = [
        Segment(138.5, 117.5, 139.5875, 117.5, 0.2, layer, net=8)
        for layer in (Layer.F_CU, Layer.B_CU)
    ]
    router.existing_routes = [Route(net=8, net_name="VCC", vias=[bridge], segments=fixed)]
    before = (bridge.x, bridge.y, bridge.layers)
    result = drc_verify_and_nudge(router, max_passes=1)
    assert via.x == pytest.approx(140.665)
    assert segment.x1 == via.x
    assert (bridge.x, bridge.y, bridge.layers) == before
    assert result.vias_nudged == 1
    assert validate_routes(router) == []


def test_same_net_exit_rejects_foreign_back_layer_snapped_trace():
    """The #5189 contained-drill witness must not gain a foreign-pad short."""
    router = Autorouter(
        width=12,
        height=12,
        rules=DesignRules(trace_clearance=0.2, manufacturer="jlcpcb"),
        force_python=True,
    )
    for ref, x, y, size, net, layer in (
        ("A", 5, 5, 1, 1, Layer.F_CU),
        ("B", 4.05, 4, 0.2, 3, Layer.B_CU),
    ):
        router.add_component(
            ref,
            [
                {
                    "number": "1",
                    "x": x,
                    "y": y,
                    "width": size,
                    "height": size,
                    "net": net,
                    "net_name": ref,
                    "layer": layer,
                }
            ],
        )
    via = Via(x=4.7, y=5, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    segment = Segment(x1=4.7, y1=5, x2=4.7, y2=2, width=0.2, layer=Layer.B_CU, net=1)
    router.routes = [Route(net=1, net_name="A", vias=[via], segments=[segment])]
    before = (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2)
    assert validate_routes(router) == []
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y, segment.x1, segment.y1, segment.x2, segment.y2) == before
    assert validate_routes(router) == []
    assert result.skipped.get("via_pad_destination_blocked") == 1


@pytest.mark.parametrize(
    "layers,census,eligible", [(2, [], False), (4, None, False), (4, [], True)]
)
def test_transaction_process_findings_require_candidate_eligibility(layers, census, eligible):
    from kicad_tools.router.drc_nudge import _via_pad_process_findings
    from tests.test_drc_nudge import TestViaInPadComponentHoleCensus, _StubLayerStack

    pad, via, router = TestViaInPadComponentHoleCensus()._make_fixture(all_pads=census)
    router.layer_stack = _StubLayerStack(num_layers=layers)
    via.x = 10.3
    findings = _via_pad_process_findings(via, router)
    assert bool(findings) is not eligible


@pytest.mark.parametrize("hole_x", [11.3, 15.0])
def test_repair_destination_respects_loaded_anonymous_npth(tmp_path, hole_x):
    import math

    from kicad_tools.router.drc_nudge import DRCNudgeResult, _scan_and_repair_via_in_pad
    from tests.router.test_via_in_pad_component_hole_census import _loaded_holes
    from tests.test_drc_nudge import TestViaInPadComponentHoleCensus, _StubLayerStack

    loaded = _loaded_holes(tmp_path, "np_thru_hole", "", x=hole_x, y=9.7)
    _, via, router = TestViaInPadComponentHoleCensus()._make_fixture(all_pads=[])
    router.layer_stack = _StubLayerStack(num_layers=2)
    router._loaded_component_holes = loaded._loaded_component_holes
    before = math.hypot(via.x - hole_x, via.y - 9.7) - (via.drill + 0.3) / 2
    floor = max(router.rules.min_drill_clearance, router.rules.min_hole_to_hole)
    assert before >= floor
    _scan_and_repair_via_in_pad(router, 2, DRCNudgeResult())
    gap = math.hypot(via.x - hole_x, via.y - 9.7) - (via.drill + 0.3) / 2
    assert gap >= floor
    assert validate_routes(router) == []
