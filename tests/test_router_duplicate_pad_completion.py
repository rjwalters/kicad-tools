"""Physical duplicate lands must remain visible in completion accounting."""

from dataclasses import replace

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Pad, Route, Segment, Via


def pad(x=10, y=10, *, terminal="land1", layer=Layer.F_CU, **kwargs):
    return Pad(
        x=x,
        y=y,
        width=1,
        height=1,
        net=1,
        net_name="SIGNAL",
        ref="J1",
        pin="SH",
        terminal_id=terminal,
        layer=layer,
        **kwargs,
    )


def status(pads, segments=(), vias=()):
    routes = [Route(1, "SIGNAL", list(segments), list(vias))] if segments or vias else []
    return validate_net_connectivity(routes, {1: pads})[1]


def segment(x1, y1, x2, y2, layer=Layer.F_CU, width=0.2):
    return Segment(x1, y1, x2, y2, width, layer, 1)


def via(layers=(Layer.F_CU, Layer.B_CU)):
    return Via(10, 10, 0.3, 0.6, layers, 1)


def test_disconnected_lands_without_routes_keep_authored_report_names():
    result = status([pad(), pad(20, terminal="land2")])
    assert result == {
        "total_pads": 2,
        "connected_pads": 1,
        "connected": False,
        "stranded_pads": ["J1.SH"],
    }


@pytest.mark.parametrize("bridge", ["none", "via", "pth"])
def test_coincident_opposite_layer_lands_need_a_barrel(bridge):
    pads = [pad(), pad(terminal="land2", layer=Layer.B_CU)]
    if bridge == "pth":
        pads[0] = replace(pads[0], through_hole=True, drill=0.3)
    assert status(pads, vias=[via()] if bridge == "via" else [])["connected"] == (bridge != "none")


def test_blind_via_does_not_bridge_to_the_back_layer():
    pads = [pad(), pad(terminal="land2", layer=Layer.B_CU)]
    assert not status(pads, vias=[via((Layer.F_CU, Layer.IN1_CU))])["connected"]
    pads[1] = replace(pads[1], layer=Layer.IN1_CU)
    assert status(pads, vias=[via((Layer.F_CU, Layer.IN2_CU))])["connected"]


@pytest.mark.parametrize("layer", [Layer.F_CU, Layer.B_CU])
def test_near_pad_does_not_attach_through_old_two_mm_proximity(layer):
    pads = [pad(), pad(12, terminal="land2", layer=layer)]
    # The endpoint at x=10.8 is only 1.2mm from the second land's centre,
    # but neither its trace cap nor pad copper reaches that land.
    result = status(pads, [segment(10, 10, 10.8, 10)])
    assert not result["connected"]
    assert result["connected_pads"] == 1
    assert result["stranded_pads"] == ["J1.SH"]


@pytest.mark.parametrize("layer,connected", [(Layer.F_CU, True), (Layer.B_CU, False)])
def test_overlapping_pad_copper_requires_a_shared_layer(layer, connected):
    assert status([pad(), pad(10.8, terminal="land2", layer=layer)])["connected"] == connected


def test_track_width_contact_and_mid_segment_contacts_connect():
    pads = [pad(), pad(20, terminal="land2")]
    # Trace centres outside the pads still contact their top edges.
    assert status(pads, [segment(9, 10.55, 21, 10.55)])["connected"]
    assert not status(pads, [segment(9, 10.7, 21, 10.7)])["connected"]


def test_crossing_copper_on_different_layers_needs_via():
    pads = [pad(5, 10), pad(10, 15, terminal="land2", layer=Layer.B_CU)]
    segments = [segment(5, 10, 15, 10), segment(10, 5, 10, 15, Layer.B_CU)]
    assert not status(pads, segments)["connected"]
    assert status(pads, segments, [via()])["connected"]


def test_circular_pad_corners_are_not_copper():
    pads = [pad(shape="circle"), pad(10.9, 10.9, terminal="land2", shape="circle")]
    assert not status(pads)["connected"]


def test_rotated_pad_contacts_use_rotation():
    first = replace(pad(rotation=45), width=4, height=0.4)
    assert status([first, pad(11, 9, terminal="land2")])["connected"]
    assert not status([first, pad(11, 11, terminal="land2")])["connected"]


def test_distinct_legacy_net_retains_original_accounting():
    # Only the new physical-terminal marker opts into strict contact accounting.
    result = status([pad(terminal=""), pad(20, terminal="")])
    assert result["connected_pads"] == 0
    assert result["stranded_pads"] == ["J1.SH", "J1.SH"]
