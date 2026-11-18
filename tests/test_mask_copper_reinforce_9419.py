"""``mask_to_copper`` on a reinforced board whose far side carries the same net (#9419).

After #9315 a recorded run excuses its own side's copper only. ``pcb reinforce``
also opens the mask on the *opposite* side at each anchor and end hole, and a
net doubled on both outer layers exposes its own copper there; a branch can tee
off a run inside the strip width, and two wires of one net can meet end to end.
All of that is same-net copper the wire is soldered to. Different-net copper in
the same openings stays an error.
"""

import pytest
from shapely.geometry import Point, box

from kicad_tools.validate.mask_copper import MaskCopperPolicy, assess_attributed_geometry

POLICY = MaskCopperPolicy(0.1, "order", "process", "rev")

A, B = (12.0, 10.0), (28.0, 10.0)


def _disk(x, y, r):
    return Point(x, y).buffer(r)


def _record(layer="B.Cu", anchors=(A, B), **overrides):
    record = {
        "net": "PWR",
        "layer": layer,
        "kind": "track",
        "path": "HS_POWER",
        "paths": ["HS_POWER"],
        "leg_start": {"x": anchors[0][0], "y": anchors[0][1]},
        "leg_end": {"x": anchors[-1][0], "y": anchors[-1][1]},
        "anchors": [{"x": x, "y": y} for x, y in anchors],
        "length_mm": 16.0,
        "straight_line_mm": 16.0,
        "wire_gauge_awg": 16,
        "bare_diameter_mm": 1.29,
        "mask_opening": True,
        "status": "full",
        "reason": "",
    }
    record.update(overrides)
    return record


def _geometry(pieces):
    """AttributedMaskGeometry from ``(kind, net, layers_geometry)`` tuples."""
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    objects = {}
    unions = {}
    for index, (kind, net, layers) in enumerate(pieces, start=1):
        identity = f"00000000-0000-0000-0000-{index:012d}"
        objects[identity] = {
            "kind": kind,
            "net": net,
            "layers_geometry": layers,
            "margin_mm": {side: 0.0 for side in layers if side.endswith(".Mask")},
        }
        for side, shape in layers.items():
            unions[side] = shape if side not in unions else unions[side].union(shape)
    exported = ExportedMaskGeometry("a" * 64, None, None, "10.0.5", {}, layers=unions)
    return AttributedMaskGeometry(exported, objects)


def _both_sides_leg(far_copper=True, different_net_far=False):
    """B.Cu leg A-B; anchors are through vias with an end-hole circle on each mask."""
    strip = box(A[0], 9.75, B[0], 10.25)
    pieces = [
        ("segment", "PWR", {"B.Cu": box(10.0, 9.75, 30.0, 10.25)}),
        ("gr_line", "", {"B.Mask": strip}),
    ]
    for x, y in (A, B):
        pieces.append(
            (
                "via",
                "PWR",
                {
                    "F.Cu": _disk(x, y, 0.4),
                    "B.Cu": _disk(x, y, 0.4),
                    "F.Mask": _disk(x, y, 0.4),
                    "B.Mask": _disk(x, y, 0.4),
                },
            )
        )
        pieces.append(("gr_circle", "", {"F.Mask": _disk(x, y, 0.7), "B.Mask": _disk(x, y, 0.7)}))
    if far_copper:
        pieces.append(("segment", "PWR", {"F.Cu": box(10.0, 9.75, 30.0, 10.25)}))
    if different_net_far:
        pieces.append(("segment", "OTHER", {"F.Cu": box(11.0, 10.62, 13.0, 10.72)}))
    return _geometry(pieces)


def test_far_side_same_net_copper_at_anchors_is_not_an_error():
    result = assess_attributed_geometry(_both_sides_leg(), POLICY, (), [_record()])
    assert result.measurements
    far = [m for m in result.measurements if m.mask_side == "F.Mask"]
    assert far, "the far-side openings exposed nothing to measure"
    assert {m.disposition for m in far} == {"within_reinforce_run"}
    assert {m.owner_run for m in far} == {"HS_POWER"}
    assert result.passed
    assert {m.disposition for m in result.measurements} == {"within_reinforce_run"}


def test_far_side_opening_without_a_record_is_unchanged():
    result = assess_attributed_geometry(_both_sides_leg(), POLICY, (), [])
    assert any(m.disposition == "violation" for m in result.measurements)
    assert not result.passed


def test_far_side_different_net_copper_is_still_an_error():
    geometry = _both_sides_leg(different_net_far=True)
    result = assess_attributed_geometry(geometry, POLICY, (), [_record()])
    errors = [m for m in result.measurements if m.disposition == "violation"]
    assert errors
    assert {m.conductor_net for m in errors} == {"OTHER"}
    assert {m.mask_side for m in errors} == {"F.Mask"}
    assert not result.passed


def test_far_side_opening_needs_a_confirmed_anchor_on_the_run_layer():
    """A record whose anchors stand on no same-net copper claims nothing (#9315 gate)."""
    far_anchors = ((40.0, 40.0), (50.0, 40.0))
    result = assess_attributed_geometry(
        _both_sides_leg(), POLICY, (), [_record(anchors=far_anchors)]
    )
    assert not result.passed
    assert all(m.owner_run is None for m in result.measurements if m.mask_side == "F.Mask")


def _tee_geometry():
    """Run on B.Cu with a same-net branch leaving it inside the strip width."""
    strip = box(A[0], 9.75, B[0], 10.25)
    pieces = [
        ("segment", "PWR", {"B.Cu": box(10.0, 9.75, 30.0, 10.25)}),
        ("segment", "PWR", {"B.Cu": box(19.85, 9.9, 20.15, 14.0)}),
        ("gr_line", "", {"B.Mask": strip}),
    ]
    for x, y in (A, B):
        pieces.append(("via", "PWR", {"B.Cu": _disk(x, y, 0.4), "B.Mask": _disk(x, y, 0.4)}))
    return _geometry(pieces)


def test_branch_teeing_off_the_run_inside_the_strip_is_not_an_error():
    result = assess_attributed_geometry(_tee_geometry(), POLICY, (), [_record()])
    branch = [m for m in result.measurements if m.conductor_kind == "segment"]
    assert branch
    assert {m.disposition for m in branch} == {"within_reinforce_run"}
    assert result.passed


def _end_to_end_geometry():
    """Two wires of one net meeting end to end: second wire's anchor 2.3 mm from the first's."""
    a2, b2 = (30.3, 10.0), (45.0, 10.0)
    pieces = [
        ("segment", "PWR", {"B.Cu": box(10.0, 9.75, 47.0, 10.25)}),
        ("gr_line", "", {"B.Mask": box(A[0], 9.75, B[0], 10.25)}),
        ("gr_line", "", {"B.Mask": box(a2[0], 9.75, b2[0], 10.25)}),
    ]
    for x, y in (A, B, a2, b2):
        pieces.append(("via", "PWR", {"B.Cu": _disk(x, y, 0.4), "B.Mask": _disk(x, y, 0.4)}))
    return _geometry(pieces), a2, b2


def test_two_wires_of_one_net_meeting_end_to_end_have_no_junction_error():
    geometry, a2, b2 = _end_to_end_geometry()
    records = [_record(), _record(anchors=(a2, b2), path="SECOND", paths=["SECOND"])]
    result = assess_attributed_geometry(geometry, POLICY, (), records)
    assert result.measurements
    assert {m.disposition for m in result.measurements} == {"within_reinforce_run"}
    assert result.passed


@pytest.mark.parametrize("net", ["OTHER"])
def test_different_net_copper_under_the_strip_stays_an_error(net):
    geometry = _tee_geometry()
    # A different-net conductor under the strip is a genuine short-in-waiting.
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry

    objects = dict(geometry.objects)
    objects["00000000-0000-0000-0000-0000000000ee"] = {
        "kind": "segment",
        "net": net,
        "layers_geometry": {"B.Cu": box(16.0, 10.3, 17.0, 10.35)},
        "margin_mm": {},
    }
    exported = geometry.exported
    exported.layers["B.Cu"] = exported.layers["B.Cu"].union(box(16.0, 10.3, 17.0, 10.35))
    result = assess_attributed_geometry(
        AttributedMaskGeometry(exported, objects), POLICY, (), [_record()]
    )
    errors = [m for m in result.measurements if m.disposition == "violation"]
    assert errors
    assert {m.conductor_net for m in errors} == {net}
    assert not result.passed
