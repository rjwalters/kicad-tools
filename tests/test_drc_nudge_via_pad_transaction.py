"""A foreign-pad repair must not trade its violation for new copper shorts."""

from kicad_tools.router.core import Autorouter
from kicad_tools.router.drc_nudge import drc_verify_and_nudge
from kicad_tools.router.io import validate_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _scene():
    router = Autorouter(
        width=12,
        height=12,
        rules=DesignRules(trace_clearance=0.2, manufacturer="pcbway"),
    )
    _pad(router, "A", 5.0, 5.0, 1.0, 2)
    via = Via(x=4.5, y=5.0, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router.routes = [Route(net=1, net_name="SIG", vias=[via])]
    return router, via


def _pad(router, ref, x, y, size, net):
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
                "layer": Layer.F_CU,
            }
        ],
    )


def test_foreign_pad_destination_is_rejected_without_mutation():
    router, via = _scene()
    _pad(router, "B", 3.8, 5.0, 0.2, 3)
    before = validate_routes(router)
    assert {v.obstacle_net for v in before} == {2}
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert validate_routes(router) == before
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_legal_move_preserves_unrelated_existing_violation():
    router, via = _scene()
    _pad(router, "B", 9.0, 9.0, 1.0, 3)
    # A pre-existing unrelated violation on a protected net must not veto
    # the valid repair or be silently removed by its transaction.
    other = Via(x=9.0, y=9.0, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=4)
    router.routes.append(Route(net=4, net_name="OTHER", vias=[other]))
    before = [v for v in validate_routes(router) if v.net == 4]
    result = drc_verify_and_nudge(router, max_passes=1, skip_nets={4})
    assert (via.x, via.y) == (3.98, 5.0)
    assert validate_routes(router) == before
    assert result.segments_nudged == 1


def test_snapped_chain_short_rolls_back_via_and_all_endpoints():
    router, via = _scene()
    # The original vertical segment clears B. Sliding its upper endpoint
    # left to the otherwise-clear via destination cuts through B.
    seg = Segment(x1=4.5, y1=5.0, x2=4.5, y2=2.0, width=0.2, layer=Layer.B_CU, net=1)
    router.routes[0].segments.append(seg)
    router.add_component(
        "B",
        [
            {
                "number": "1",
                "x": 4.05,
                "y": 4.0,
                "width": 0.2,
                "height": 0.2,
                "net": 3,
                "net_name": "B",
                "layer": Layer.B_CU,
            }
        ],
    )
    before = validate_routes(router)
    assert {v.obstacle_net for v in before} == {2}
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert (seg.x1, seg.y1, seg.x2, seg.y2) == (4.5, 5.0, 4.5, 2.0)
    assert validate_routes(router) == before
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_destination_foreign_via_is_rejected():
    router, via = _scene()
    other = Via(x=3.7, y=5.0, diameter=0.2, drill=0.1, layers=(Layer.F_CU, Layer.B_CU), net=3)
    router.routes.append(Route(net=3, net_name="OTHER", vias=[other]))
    before = validate_routes(router)
    assert {v.obstacle_type for v in before} == {"pad"}
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert validate_routes(router) == before
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_destination_foreign_track_is_rejected():
    router, via = _scene()
    router.routes.append(
        Route(
            net=3,
            net_name="OTHER",
            segments=[Segment(x1=3.8, y1=4.0, x2=3.8, y2=6.0, width=0.2, layer=Layer.B_CU, net=3)],
        )
    )
    before = validate_routes(router)
    assert {v.obstacle_type for v in before} == {"pad"}
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert validate_routes(router) == before
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_same_net_hole_spacing_is_not_exempt():
    router, via = _scene()
    router.rules.min_drill_clearance = 0.3
    other = Via(x=3.5, y=5.0, diameter=0.6, drill=0.3, layers=(Layer.F_CU, Layer.B_CU), net=1)
    router.routes[0].vias.append(other)
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_move_into_unsupported_same_net_smd_pad_is_rejected():
    router, via = _scene()
    router.rules.manufacturer = "jlcpcb"
    _pad(router, "B", 3.98, 5.0, 0.4, 1)
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_move_cannot_cross_board_edge():
    router, via = _scene()
    router._edge_clearance = 0.05
    router._edge_segments = [((4.1, 0.0), (4.1, 10.0))]
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert result.skipped.get("via_pad_destination_blocked") == 1


def test_legal_move_reconnects_chain_across_same_net_routes():
    router, via = _scene()
    first = Segment(x1=4.5, y1=5.0, x2=2.0, y2=4.0, width=0.2, layer=Layer.B_CU, net=1)
    second = Segment(x1=2.0, y1=6.0, x2=4.5, y2=5.0, width=0.2, layer=Layer.B_CU, net=1)
    router.routes[0].segments.append(first)
    router.routes.append(Route(net=1, net_name="SIG", segments=[second]))
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (3.98, 5.0)
    assert (first.x1, first.y1) == (via.x, via.y)
    assert (second.x2, second.y2) == (via.x, via.y)
    assert validate_routes(router) == []
    assert result.remaining_violations == 0


def test_preserved_endpoint_contact_cannot_be_disconnected():
    router, via = _scene()
    preserved = Segment(x1=4.5, y1=5.0, x2=4.5, y2=2.0, width=0.2, layer=Layer.B_CU, net=1)
    router.existing_routes = [Route(net=1, net_name="SIG", segments=[preserved])]
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert (preserved.x1, preserved.y1, preserved.x2, preserved.y2) == (4.5, 5.0, 4.5, 2.0)
    assert result.remaining_violations == 1
    assert result.skipped.get("via_pad_contact_blocked") == 1


def test_preserved_interior_contact_cannot_be_disconnected():
    router, via = _scene()
    preserved = Segment(x1=4.5, y1=6.0, x2=4.5, y2=2.0, width=0.2, layer=Layer.B_CU, net=1)
    router.existing_routes = [Route(net=1, net_name="SIG", segments=[preserved])]
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert result.skipped.get("via_pad_contact_blocked") == 1


def test_move_along_preserved_copper_can_retain_contact():
    router, via = _scene()
    preserved = Segment(x1=4.5, y1=5.0, x2=2.0, y2=5.0, width=0.2, layer=Layer.B_CU, net=1)
    router.existing_routes = [Route(net=1, net_name="SIG", segments=[preserved])]
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (3.98, 5.0)
    assert (preserved.x1, preserved.y1, preserved.x2, preserved.y2) == (4.5, 5.0, 2.0, 5.0)
    assert result.remaining_violations == 0


def test_off_center_same_net_pad_contact_is_preserved():
    router, via = _scene()
    router.add_component(
        "B",
        [
            {
                "number": "1",
                "x": 4.8,
                "y": 5.0,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "SIG",
                "layer": Layer.B_CU,
            }
        ],
    )
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert result.skipped.get("via_pad_contact_blocked") == 1


def test_contact_rejection_restores_snapped_endpoints():
    router, via = _scene()
    moved = Segment(x1=4.5, y1=5.0, x2=2.0, y2=2.0, width=0.2, layer=Layer.B_CU, net=1)
    preserved = Segment(x1=4.5, y1=5.0, x2=4.5, y2=2.0, width=0.2, layer=Layer.B_CU, net=1)
    router.routes[0].segments.append(moved)
    router.existing_routes = [Route(net=1, net_name="SIG", segments=[preserved])]
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (4.5, 5.0)
    assert (moved.x1, moved.y1, moved.x2, moved.y2) == (4.5, 5.0, 2.0, 2.0)
    assert result.skipped.get("via_pad_contact_blocked") == 1


def test_legal_move_retains_off_center_pad_contact():
    router, via = _scene()
    router.add_component(
        "B",
        [
            {
                "number": "1",
                "x": 4.2,
                "y": 5.0,
                "width": 2.0,
                "height": 0.4,
                "net": 1,
                "net_name": "SIG",
                "layer": Layer.B_CU,
            }
        ],
    )
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (3.98, 5.0)
    assert result.remaining_violations == 0


def test_residual_rotation_does_not_create_false_rectangular_contact():
    router, via = _scene()
    router.add_component(
        "B",
        [
            {
                "number": "1",
                "x": 5.4,
                "y": 5.1,
                "width": 2.0,
                "height": 0.2,
                "net": 1,
                "net_name": "SIG",
                "layer": Layer.B_CU,
            }
        ],
    )
    # Current primitives have no rotation field. Exercise a richer caller's
    # optional residual angle without changing the router metadata model.
    router.pads[("B", "1")].rotation = 45.0
    result = drc_verify_and_nudge(router, max_passes=1)
    assert (via.x, via.y) == (3.98, 5.0)
    assert result.remaining_violations == 0
