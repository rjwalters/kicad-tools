"""``mask_to_copper`` and a footprint's own land pattern (issue #9420).

``RF_Module:ESP32-C3-WROOM-02`` carries a rectangular ground pad with circular
ground pads on it: each circular opening exposes the rectangular pad. That is
the library land pattern, so same-net pads of **one** footprint measure as
``within_own_footprint`` at info; a different footprint, a different net, or a
track keeps the measured verdict.
"""

from shapely.geometry import LineString, Point, box

from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    assess_attributed_geometry,
    assessment_results,
)

POLICY = MaskCopperPolicy(
    clearance_mm=0.09,
    source="order",
    process="process",
    revision="rev",
    warning_clearance_mm=0.15,
)
OWNER = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
RECT = box(0, 0, 3, 3)
CIRCLE = Point(1.5, 1.5).buffer(0.5, quad_segs=64)


def _geometry(
    *,
    owner_parent="fp-1",
    conductor_parent="fp-1",
    owner_net="GND",
    net="GND",
    conductor_kind="pad:rect",
    conductor=RECT,
):
    from shapely.ops import unary_union

    from kicad_tools.validate.mask_copper_geometry import AttributedMaskGeometry
    from kicad_tools.validate.mask_export_geometry import ExportedMaskGeometry

    exported = ExportedMaskGeometry(
        "a" * 64,
        None,
        None,
        "10.0.6",
        {},
        layers={"F.Mask": CIRCLE, "F.Cu": unary_union([CIRCLE, conductor])},
    )
    return AttributedMaskGeometry(
        exported,
        {
            OWNER: {
                "kind": "pad:circle",
                "net": owner_net,
                "pad_ref": "U1/2",
                "parent_uuid": owner_parent,
                "layers_geometry": {"F.Mask": CIRCLE, "F.Cu": CIRCLE},
                "margin_mm": {"F.Mask": 0.0},
            },
            OTHER: {
                "kind": conductor_kind,
                "net": net,
                "pad_ref": "U1/1",
                "parent_uuid": conductor_parent,
                "layers_geometry": {"F.Cu": conductor},
                "margin_mm": {},
            },
        },
    )


def _measure(geometry):
    result = assess_attributed_geometry(geometry, POLICY, ())
    assert result.coverage == "complete"
    found = [m for m in result.measurements if m.conductor_uuid == OTHER]
    assert len(found) == 1
    severity = [
        v.severity
        for v in assessment_results(result).violations
        if v.rule_id == "mask_to_copper" and OTHER in v.items
    ]
    return found[0], severity[0]


def test_same_net_pads_of_one_footprint_are_info_and_listed():
    m, severity = _measure(_geometry())
    assert m.disposition == "within_own_footprint"
    assert severity == "info"


def test_same_net_pads_of_different_footprints_still_error():
    m, severity = _measure(_geometry(conductor_parent="fp-2"))
    assert m.disposition == "violation"
    assert severity == "error"


def test_different_net_pads_of_one_footprint_still_error():
    m, severity = _measure(_geometry(net="VCC"))
    assert m.disposition == "violation"
    assert severity == "error"


def test_pads_without_a_footprint_identity_are_not_exempt():
    m, severity = _measure(_geometry(owner_parent="", conductor_parent=""))
    assert m.disposition == "violation"
    assert severity == "error"


def test_same_footprint_track_is_not_a_pad_pair():
    """Only pad-to-pad is the land pattern; a segment keeps its verdict."""
    track = LineString([(1.5, 1.5), (4, 1.5)]).buffer(0.1, quad_segs=64)
    m, severity = _measure(_geometry(conductor_kind="segment", conductor=track))
    assert m.disposition != "within_own_footprint"
