"""Regression tests for Issue #8591: foreign inner-plane fill is not a via obstacle."""

from __future__ import annotations

from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    KeepoutPolygon,
    PadInfo,
    _check_point_filled_polygon_clearance,
    _check_segment_filled_polygon_clearance,
    identify_nearest_obstacle,
)

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def _fill(layer: str) -> FilledPolygon:
    return FilledPolygon(net_number=5, net_name="+3V3", layer=layer, points=list(SQUARE))


def test_inner_foreign_fill_does_not_block_via_site():
    assert _check_point_filled_polygon_clearance(5.0, 5.0, 0.25, [_fill("In2.Cu")], 0.2)


def test_outer_foreign_fill_still_blocks_via_site():
    for layer in ("F.Cu", "B.Cu"):
        assert not _check_point_filled_polygon_clearance(5.0, 5.0, 0.25, [_fill(layer)], 0.2)


def test_inner_fill_does_not_block_stub_segment():
    assert _check_segment_filled_polygon_clearance(
        4.0, 5.0, 6.0, 5.0, 0.1, [_fill("In1.Cu")], 0.2, stub_layer="F.Cu"
    )
    assert not _check_segment_filled_polygon_clearance(
        4.0, 5.0, 6.0, 5.0, 0.1, [_fill("F.Cu")], 0.2, stub_layer="F.Cu"
    )


def test_via_blocking_keepout_still_blocks_on_inner_layer_name():
    ko = KeepoutPolygon(
        net_number=-1,
        net_name="ko",
        layer="In2.Cu",
        points=list(SQUARE),
        layers=("In2.Cu",),
        blocks_vias=True,
    )
    assert not _check_point_filled_polygon_clearance(5.0, 5.0, 0.25, [ko], 0.2)


def test_obstacle_report_ignores_inner_fill_but_names_outer():
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(PadInfo)}
    kwargs = {
        n: (0.0 if f.type in ("float", float) else 0)
        for n, f in fields.items()
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    kwargs.update({"x": 5.0, "y": 5.0})
    pad = PadInfo(**kwargs)
    inner = identify_nearest_obstacle(
        pad, 0.45, 0.2, [], other_net_filled_polygons=[_fill("In2.Cu")]
    )
    assert inner.obstacle_type != "zone_fill"
    outer = identify_nearest_obstacle(pad, 0.45, 0.2, [], other_net_filled_polygons=[_fill("F.Cu")])
    assert outer.obstacle_type == "zone_fill"
