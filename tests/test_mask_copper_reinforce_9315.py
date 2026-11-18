"""``mask_to_copper`` vs the openings ``pcb reinforce`` writes on purpose (#9315).

A reinforced run's strip, end holes and anchor flashes are the design, not
defects: with ``<board>.reinforcement.json`` present, the run's own copper
measures ``within_reinforce_run`` (info) and the opening names the owning
net and run. Different-net copper inside a strip's clearance band stays an
error; same-net copper off the run stays at its current severity. Without a
sidecar (or with no matching record) the assessment is unchanged.
"""

import json

import pytest
from shapely.geometry import Point, box

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    assess_attributed_geometry,
    assessment_results,
    check_mask_to_copper,
)

POLICY = MaskCopperPolicy(0.1, "order", "process", "rev")

A, B = (12.0, 10.0), (28.0, 10.0)


def _disk(x, y, r):
    return Point(x, y).buffer(r)


def reinforce_record(**overrides):
    record = {
        "net": "PWR",
        "layer": "F.Cu",
        "kind": "track",
        "path": "HS_POWER",
        "paths": ["HS_POWER"],
        "leg_start": {"x": 10.0, "y": 10.0},
        "leg_end": {"x": 30.0, "y": 10.0},
        "anchors": [{"x": A[0], "y": A[1]}, {"x": B[0], "y": B[1]}],
        "length_mm": 20.0,
        "straight_line_mm": 20.0,
        "wire_gauge_awg": 16,
        "bare_diameter_mm": 1.29,
        "mask_opening": True,
        "status": "full",
        "reason": "",
    }
    record.update(overrides)
    return record


def _geometry(mask_pieces, copper_pieces):
    """AttributedMaskGeometry from (kind, net, layers_geometry) tuples."""
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    objects = {}
    mask_union = None
    copper_union = None
    for index, (kind, net, layers) in enumerate(mask_pieces + copper_pieces, start=1):
        identity = f"00000000-0000-0000-0000-{index:012d}"
        margin = {side: 0.0 for side in layers if side.endswith(".Mask")}
        objects[identity] = {
            "kind": kind,
            "net": net,
            "layers_geometry": layers,
            "margin_mm": margin,
        }
        for side, shape in layers.items():
            if side.endswith(".Mask"):
                mask_union = shape if mask_union is None else mask_union.union(shape)
            else:
                copper_union = shape if copper_union is None else copper_union.union(shape)
    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.5",
        {},
        layers={"F.Mask": mask_union, "F.Cu": copper_union},
    )
    return AttributedMaskGeometry(exported, objects)


def straight_geometry(
    neighbour=None, offrun=None, pad_on_run=False, stray_via=False, stray_strip=False
):
    """One straight reinforced leg: strip from anchor to anchor, untented anchors."""
    track = box(10.0, 9.75, 30.0, 10.25)
    strip = box(A[0], 9.75, B[0], 10.25)
    copper = [("segment", "PWR", {"F.Cu": track})]
    mask = [
        ("gr_line", "", {"F.Mask": strip}),
        ("via", "PWR", {"F.Cu": _disk(*A, 0.4), "F.Mask": _disk(*A, 0.4)}),
        ("via", "PWR", {"F.Cu": _disk(*B, 0.4), "F.Mask": _disk(*B, 0.4)}),
    ]
    if pad_on_run:
        copper.append(
            (
                "pad:circle",
                "PWR",
                {"F.Cu": _disk(20.0, 10.0, 0.6).difference(_disk(20.0, 10.0, 0.3))},
            )
        )
    if stray_via:
        copper.append(
            ("via", "PWR", {"F.Cu": _disk(22.0, 10.6, 0.4), "F.Mask": _disk(22.0, 10.6, 0.4)})
        )
    if stray_strip:
        copper.append(("segment", "PWR", {"F.Cu": box(40.0, 39.75, 50.0, 40.25)}))
        mask.append(("gr_line", "", {"F.Mask": box(41.0, 39.75, 47.0, 40.25)}))
    if neighbour is not None:
        copper.append(("segment", "OTHER", {"F.Cu": box(15.0, 10.32, 18.0, 10.42)}))
    if offrun is not None:
        copper.append(("segment", "PWR", {"F.Cu": box(20.0, 9.5, 20.2, 10.5)}))
    return _geometry(mask, copper)


def test_run_own_copper_is_info_and_opening_names_the_run():
    geometry = straight_geometry()
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    assert result.passed
    assert result.measurements
    for measurement in result.measurements:
        assert measurement.disposition == "within_reinforce_run"
        assert measurement.owner_net == "PWR"
        assert measurement.owner_run == "HS_POWER"
        assert measurement.same_net is True
    severity = {v.severity for v in assessment_results(result).violations}
    assert "error" not in severity
    assert "warning" not in severity


def test_opening_label_falls_back_to_leg_endpoints():
    geometry = straight_geometry()
    record = reinforce_record(path=None, paths=[])
    result = assess_attributed_geometry(geometry, POLICY, (), [record])
    assert result.measurements
    assert {m.owner_run for m in result.measurements} == {"PWR leg (10.00,10.00)-(30.00,10.00)"}


def test_different_net_neighbour_inside_clearance_band_still_error():
    geometry = straight_geometry(neighbour=True)
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    neighbour = [m for m in result.measurements if m.conductor_net == "OTHER"]
    assert neighbour, "different-net neighbour was not measured at all"
    assert {m.disposition for m in neighbour} == {"violation"}
    assert {m.same_net for m in neighbour} == {False}
    # The opening is attributed even for the gated finding: it names the run.
    assert {m.owner_net for m in neighbour} == {"PWR"}
    assert {m.owner_run for m in neighbour} == {"HS_POWER"}
    own = [m for m in result.measurements if m.disposition == "within_reinforce_run"]
    assert own
    violation = assessment_results(result)
    severities = {
        m.disposition: v.severity
        for m, v in zip(
            result.measurements,
            _measurement_severities(result),
            strict=True,
        )
    }
    assert severities["violation"] == "error"
    assert severities["within_reinforce_run"] == "info"
    assert violation.violations


def _measurement_severities(result):
    from kicad_tools.validate.violations import DRCViolation

    return [
        v
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and isinstance(v, DRCViolation)
    ]


def test_finding_message_names_run_and_net_relation():
    geometry = straight_geometry(neighbour=True)
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    messages = [v.message for v in assessment_results(result).violations]
    excused = [m for m in messages if "within_reinforce_run" in m]
    assert excused and all("reinforce run HS_POWER" in m and "same-net" in m for m in excused)
    gated = [m for m in messages if "different-net" in m]
    assert gated and all("reinforce run HS_POWER" in m for m in gated)


def test_same_net_off_run_neighbour_still_reported():
    geometry = straight_geometry(offrun=True)
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    gated = [m for m in result.measurements if m.disposition == "violation"]
    assert gated, "same-net off-run copper was excused"
    assert {m.conductor_net for m in gated} == {"PWR"}
    assert {m.same_net for m in gated} == {True}
    # ...while the run's own copper on the same strip stays excused.
    assert {m.disposition for m in result.measurements} == {
        "violation",
        "within_reinforce_run",
    }
    assert not result.passed


def test_strip_over_same_net_pad_on_the_run_is_excused():
    geometry = straight_geometry(pad_on_run=True)
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    pad = [m for m in result.measurements if m.conductor_kind == "pad:circle"]
    assert pad
    assert {m.disposition for m in pad} == {"within_reinforce_run"}
    assert result.passed


def test_stray_untented_via_off_the_run_is_not_excused():
    geometry = straight_geometry(stray_via=True)
    stray_uuid = next(
        identity
        for identity, item in geometry.objects.items()
        if item["kind"] == "via" and abs(item["layers_geometry"]["F.Cu"].centroid.x - 22.0) < 1e-9
    )
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    stray = [
        m
        for m in result.measurements
        if m.conductor_uuid == stray_uuid or stray_uuid in m.opening_uuids
    ]
    assert stray
    assert {m.disposition for m in stray} == {"violation"}
    # Its opening is not attributed either: no owner net, no run. The stray
    # conductor measured against the attributed strip still names that run.
    own_opening = [m for m in stray if stray_uuid in m.opening_uuids]
    assert own_opening
    assert {m.owner_run for m in own_opening} == {None}
    over_strip = [m for m in stray if m.conductor_uuid == stray_uuid]
    assert over_strip
    assert {m.owner_run for m in over_strip} == {"HS_POWER"}


def test_same_net_trace_under_unrelated_opening_not_excused():
    """A same-net trace wholly under its own cutout far from the run stays an error.

    The run's strip was never net-scoped: an opening 30 mm away is not
    continuous with either anchor flash, so it attributes to nothing and the
    trace under it keeps its measured verdict (#9315 review blocker).
    """
    geometry = straight_geometry(stray_strip=True)
    stray_uuid = next(
        identity
        for identity, item in geometry.objects.items()
        if item["kind"] == "segment"
        and abs(item["layers_geometry"]["F.Cu"].centroid.y - 40.0) < 1e-9
    )
    result = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    stray = [m for m in result.measurements if m.conductor_uuid == stray_uuid]
    assert stray, "same-net trace under its own unrelated opening was not measured"
    assert {m.disposition for m in stray} == {"violation"}
    # The cutout it sits under is a bare board graphic: unattributed, so no
    # owner net and no run name it.
    assert {m.owner_run for m in stray} == {None}
    assert {m.owner_net for m in stray} == {None}
    assert {m.same_net for m in stray} == {None}
    assert not result.passed


def test_bent_leg_with_several_strip_pieces():
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    seg1 = box(10.0, 9.75, 30.0, 10.25)
    seg2 = box(29.75, 10.0, 30.25, 25.0)
    piece1 = box(A[0], 9.75, 30.0, 10.25)
    piece2 = box(29.75, 10.0, 30.25, 23.0)
    anchor_far = (30.0, 23.0)
    via_a = _disk(*A, 0.4)
    via_far = _disk(*anchor_far, 0.4)
    objects = {}
    layers = {
        "F.Mask": piece1.union(piece2).union(via_a).union(via_far),
        "F.Cu": seg1.union(seg2).union(via_a).union(via_far),
    }
    for index, (kind, net, geoms) in enumerate(
        [
            ("segment", "PWR", {"F.Cu": seg1}),
            ("segment", "PWR", {"F.Cu": seg2}),
            ("gr_line", "", {"F.Mask": piece1}),
            ("gr_line", "", {"F.Mask": piece2}),
            ("via", "PWR", {"F.Cu": via_a, "F.Mask": via_a}),
            ("via", "PWR", {"F.Cu": via_far, "F.Mask": via_far}),
        ],
        start=1,
    ):
        objects[f"00000000-0000-0000-0000-{index:012d}"] = {
            "kind": kind,
            "net": net,
            "layers_geometry": geoms,
            "margin_mm": {side: 0.0 for side in geoms if side.endswith(".Mask")},
        }
    geometry = AttributedMaskGeometry(
        ExportedMaskGeometry("a" * 64, None, None, "10.0.5", {}, layers=layers), objects
    )
    record = reinforce_record(
        leg_start={"x": 10.0, "y": 10.0},
        leg_end={"x": 30.0, "y": 25.0},
        anchors=[{"x": A[0], "y": A[1]}, {"x": anchor_far[0], "y": anchor_far[1]}],
    )
    result = assess_attributed_geometry(geometry, POLICY, (), [record])
    assert result.measurements
    for measurement in result.measurements:
        assert measurement.disposition == "within_reinforce_run", measurement
    assert result.passed


def test_b_side_strip_and_anchors():
    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    track = box(10.0, 9.75, 30.0, 10.25)
    strip = box(A[0], 9.75, B[0], 10.25)
    via_a = _disk(*A, 0.4)
    via_b = _disk(*B, 0.4)
    objects = {}
    layers = {
        "B.Mask": strip.union(via_a).union(via_b),
        "B.Cu": track.union(via_a).union(via_b),
    }
    for index, (kind, net, geoms) in enumerate(
        [
            ("segment", "PWR", {"B.Cu": track}),
            ("gr_line", "", {"B.Mask": strip}),
            ("via", "PWR", {"B.Cu": via_a, "B.Mask": via_a}),
            ("via", "PWR", {"B.Cu": via_b, "B.Mask": via_b}),
        ],
        start=1,
    ):
        objects[f"00000000-0000-0000-0000-{index:012d}"] = {
            "kind": kind,
            "net": net,
            "layers_geometry": geoms,
            "margin_mm": {side: 0.0 for side in geoms if side.endswith(".Mask")},
        }
    geometry = AttributedMaskGeometry(
        ExportedMaskGeometry("a" * 64, None, None, "10.0.5", {}, layers=layers), objects
    )
    record = reinforce_record(
        layer="B.Cu",
        leg_start={"x": 10.0, "y": 10.0},
        leg_end={"x": 30.0, "y": 10.0},
    )
    result = assess_attributed_geometry(geometry, POLICY, (), [record])
    assert result.measurements
    for measurement in result.measurements:
        assert measurement.copper_layer == "B.Cu"
        assert measurement.disposition == "within_reinforce_run"


@pytest.mark.parametrize(
    "wires",
    [
        (),
        [reinforce_record(net="OTHER")],
        [reinforce_record(layer="B.Cu")],
        [{"net": "PWR", "layer": "F.Cu"}],
    ],
)
def test_no_matching_record_keeps_today_verdict(wires):
    geometry = straight_geometry()
    result = assess_attributed_geometry(geometry, POLICY, (), wires)
    assert result.measurements
    assert {m.disposition for m in result.measurements} == {"violation"}
    assert {m.owner_run for m in result.measurements} == {None}
    # Board-level graphics carry no native net: those openings stay null
    # (an unattributed via opening still names its own net).
    graphic = [m for m in result.measurements if m.opening_kind == "gr_line"]
    assert graphic
    assert {m.owner_net for m in graphic} == {None}
    assert not result.passed


def test_check_mask_to_copper_loads_the_sidecar(tmp_path, monkeypatch):
    import kicad_tools.validate.mask_copper_geometry as geometry_module

    geometry = straight_geometry()
    monkeypatch.setattr(
        geometry_module, "inspect_attributed_mask_geometry", lambda path, **kw: geometry
    )
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    result = check_mask_to_copper(board, POLICY)
    assert {m.disposition for m in result.measurements} == {"violation"}
    sidecar = tmp_path / "board.kicad_pcb.reinforcement.json"
    sidecar.write_text(json.dumps({"version": 1, "wires": [reinforce_record()]}))
    result = check_mask_to_copper(board, POLICY)
    assert result.measurements
    assert {m.disposition for m in result.measurements} == {"within_reinforce_run"}
    assert result.passed
    # An explicit empty list means exactly that: no attribution.
    result = check_mask_to_copper(board, POLICY, reinforce_wires=[])
    assert {m.disposition for m in result.measurements} == {"violation"}


def test_assessment_round_trip_keeps_run_metadata():
    geometry = straight_geometry(neighbour=True)
    assessment = assess_attributed_geometry(geometry, POLICY, (), [reinforce_record()])
    restored = type(assessment).from_dict(assessment.to_dict())
    assert restored.measurements == assessment.measurements
