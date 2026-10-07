"""Physical gap controls: connected copper is not primitive clearance."""

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap


def board(*items):
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND") (net 2 "VCC")
    """
            + "\n".join(items)
            + ")"
        )
    )


def track(a, b, identity, net=1, width=0.2):
    return f'''(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]})
        (width {width}) (layer "F.Cu") (net {net}) (uuid "{identity}"))'''


def gaps(pcb, minimum=0.25):
    return [
        v
        for v in check_physical_copper_gap(pcb, minimum).violations
        if v.rule_id == "physical_copper_gap"
    ]


@pytest.mark.parametrize("net", [1, 2])
def test_parallel_gap_and_net_independence(net):
    result = gaps(board(track((0, 0), (4, 0), "a"), track((0, 0.3), (4, 0.3), "b", net)))
    assert result
    assert result[0].actual_value == pytest.approx(0.1, abs=0.001)
    assert result[0].items == ("Trace@F.Cu:w0.2:0/0.3~4/0.3", "Trace@F.Cu:w0.2:0/0~4/0")
    assert result[0].layer == "F.Cu"
    assert result[0].nets == (("GND",) if net == 1 else ("GND", "VCC"))


@pytest.mark.parametrize("join_x", [0, 4])
def test_hairpin_connected_elsewhere_retains_slit(join_x):
    assert gaps(
        board(
            track((0, 0), (4, 0), "a"),
            track((0, 0.3), (4, 0.3), "b"),
            track((join_x, 0), (join_x, 0.3), "join"),
        )
    )


@pytest.mark.parametrize(
    "segments",
    [
        [((0, 0), (2, 0)), ((2, 0), (2, 2))],
        [((0, 0), (3, 0)), ((1, 0), (4, 0))],
        [((0, 0), (4, 0)), ((0, 0), (4, 0))],
    ],
)
def test_valid_copper_unions_are_not_zero_gap_defects(segments):
    assert not gaps(board(*(track(a, b, str(i)) for i, (a, b) in enumerate(segments))))


def test_filled_third_primitive_hides_internal_boundaries():
    assert not gaps(
        board(
            track((0, 0), (4, 0), "a"),
            track((0, 0.3), (4, 0.3), "b"),
            track((0, 0.15), (4, 0.15), "fill", width=0.5),
        )
    )


@pytest.mark.parametrize(
    "gap,expected", [(0.2495, False), (0.25, False), (0.251, False), (0.245, True)]
)
def test_exact_limit_tolerance(gap, expected):
    assert (
        bool(gaps(board(track((0, 0), (4, 0), "a"), track((0, 0.2 + gap), (4, 0.2 + gap), "b"))))
        == expected
    )


@pytest.mark.parametrize("angle", [0, 15, 30, 45, 90])
def test_roundrect_pad_escape_and_rotation(angle):
    pad = f"""(footprint "test" (layer "F.Cu") (at 0 0)
      (property "Reference" "U1")
      (pad "1" smd roundrect (at 0 0 {angle}) (size 2 1) (layers "F.Cu")
      (roundrect_rratio .25) (net 1 "GND") (uuid "pad")))"""
    assert not gaps(board(pad, track((0, 0), (3, 0), "escape")))
    # A separate track beside the rotated pad has a narrow true copper gap.
    if angle == 45:
        assert gaps(board(pad, track((1.15, -2), (1.15, 2), "near")))


def test_arc_and_zone_provenance():
    arc = """(arc (start 0 0) (mid 1 1) (end 2 0) (width .2)
        (layer "F.Cu") (net 1) (uuid "arc"))"""
    result = gaps(board(arc, track((0, 1.3), (2, 1.3), "track")))
    assert result and "Arc@F.Cu:w0.2:0/0~1/1~2/0" in result[0].items
    zone = """(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone")
      (polygon (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1)))
      (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1))))"""
    result = gaps(board(zone, track((0, 1.2), (4, 1.2), "track")))
    assert result and result[0].items == ("Trace@F.Cu:w0.2:0/1.2~4/1.2", "zone")


def test_unknown_geometry_does_not_claim_complete():
    pcb = board('(gr_circle (center 0 0) (end 1 0) (width .2) (layer "F.Cu"))')
    assert (
        check_physical_copper_gap(pcb, 0.25).violations[0].rule_id
        == "physical_copper_gap_incomplete"
    )


@pytest.mark.parametrize("minimum", [0, -1, float("nan"), float("inf")])
def test_invalid_minimum(minimum):
    with pytest.raises(ValueError):
        gaps(board(), minimum)


def test_single_filled_zone_hairpin_and_other_layer():
    points = "(xy 0 0) (xy 4 0) (xy 4 .6) (xy 0 .6) (xy 0 .4) (xy 3.8 .4) (xy 3.8 .2) (xy 0 .2)"
    zone = f"""(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "hairpin")
      (polygon (pts {points})) (filled_polygon (layer "F.Cu") (pts {points})))"""
    result = gaps(board(zone))
    assert result and result[0].items == ("hairpin",)
    assert result[0].actual_value == pytest.approx(0.2)
    assert not gaps(
        board(
            track((0, 0), (4, 0), "a"), track((0, 0.3), (4, 0.3), "b").replace('"F.Cu"', '"B.Cu"')
        )
    )


def test_cli_and_checker_opt_in(tmp_path, capsys):
    import json

    from kicad_tools.cli import main
    from kicad_tools.sexp import serialize_sexp
    from kicad_tools.validate.checker import DRCChecker

    pcb = board(track((0, 0), (4, 0), "a"), track((0, 0.3), (4, 0.3), "b"))
    assert not DRCChecker(pcb).check_physical_copper_gap().violations
    path = tmp_path / "gap.kicad_pcb"
    original = serialize_sexp(pcb._sexp)
    path.write_text(original)
    assert (
        main(
            [
                "check",
                str(path),
                "--drc-only",
                "--only",
                "physical_copper_gap",
                "--physical-copper-gap",
                ".25",
                "--format",
                "json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["violations"][0]["rule_id"] == "physical_copper_gap"
    assert len(result["violations"][0]["closest_locations"]) == 2
    assert path.read_text() == original


def test_via_gap_and_unfilled_zone():
    via = """(via (at 2 .5) (size .6) (drill .3) (layers "F.Cu" "B.Cu")
      (net 1) (uuid "via"))"""
    result = gaps(board(via, track((0, 0), (4, 0), "track")))
    assert result and result[0].items == (
        "Trace@F.Cu:w0.2:0/0~4/0",
        "Via@2/0.5:F.Cu-B.Cu:d0.3/s0.6",
    )
    zone = """(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone")
      (polygon (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1))))"""
    assert (
        check_physical_copper_gap(board(zone), 0.25).violations[0].rule_id
        == "physical_copper_gap_incomplete"
    )


@pytest.mark.parametrize(
    "field,replacement",
    [("width", "banana"), ("width", "0"), ("start", "banana 0"), ("end", "0 banana")],
)
def test_malformed_track_geometry_is_incomplete_not_clean(field, replacement):
    text = track((0, 0), (4, 0), "bad")
    import re

    text = re.sub(r"\(" + field + r" [^)]*\)", "(" + field + " " + replacement + ")", text)
    result = check_physical_copper_gap(board(text), 0.25)
    assert any(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)


@pytest.mark.parametrize(
    "modifier",
    ["(chamfer_ratio 0.3) (chamfer top_left)", "(roundrect_rratio banana)", "(at banana 0)"],
)
def test_unsupported_or_recovered_pad_geometry_is_incomplete(modifier):
    pad_at = "" if modifier.startswith("(at") else "(at 0 0)"
    pcb = board(f"""(footprint "X" (layer "F.Cu") (at 0 0)
      (property "Reference" "U1")
      (pad "1" smd roundrect {pad_at} (size 1 1) (layers "F.Cu") (net 1 "GND") {modifier}))""")
    result = check_physical_copper_gap(pcb, 0.25)
    assert any(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)


def test_malformed_via_position_is_incomplete():
    pcb = board('(via (at banana 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))')
    result = check_physical_copper_gap(pcb, 0.25)
    assert any(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)


@pytest.mark.parametrize("angle", [-60, -30, 0, 30, 60, 90, 120, 150])
@pytest.mark.parametrize("gap", [-0.1, 0.2, 0.3])
def test_rotated_parallel_rect_pads_measure_physical_air(angle, gap):
    """KiCad positive angles rotate the long pad axis toward negative Y."""
    import math

    radians = math.radians(angle)
    pads = []
    for index in (0, 1):
        # Translate along the physical minor-axis normal. The 0.4 mm
        # combined half-heights leave exactly `gap` of air between pads.
        x = 10 + index * (0.4 + gap) * math.sin(radians)
        y = 10 + index * (0.4 + gap) * math.cos(radians)
        pads.append(f"""(footprint "X" (layer "F.Cu") (at {x} {y})
          (property "Reference" "U{index}")
          (pad "1" smd rect (at 0 0 {angle}) (size 4 .4) (layers "F.Cu")
            (net 1 "GND") (uuid "pad{index}")))""")
    result = check_physical_copper_gap(board(*pads), 0.25)
    assert not any(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)
    findings = [v for v in result.violations if v.rule_id == "physical_copper_gap"]
    if gap == 0.2:
        assert len(findings) == 1
        assert findings[0].actual_value == pytest.approx(gap, abs=0.001)
        assert findings[0].items == ("pad0", "pad1")
    else:
        assert not findings  # Overlapping copper and a wide air gap are valid.


@pytest.mark.parametrize("kind", ["pad", "via"])
def test_layer_specific_padstack_is_incomplete_through_cli(kind, tmp_path, capsys):
    import json

    from kicad_tools.cli import main
    from kicad_tools.sexp import serialize_sexp
    from kicad_tools.validate.checker import DRCChecker

    if kind == "pad":
        item = """(footprint "Stack" (layer "F.Cu") (at 10 10)
          (property "Reference" "U1")
          (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill .4)
            (layers "*.Cu" "*.Mask")
            (padstack (mode custom)
              (layer "F.Cu" (shape circle) (size 1 1))
              (layer "B.Cu" (shape rect) (size 3 1)))
            (net 1 "GND") (uuid "pad")))"""
    else:
        item = """(via (at 10 10) (size 1) (drill .4)
          (layers "F.Cu" "B.Cu") (net 1) (uuid "via")
          (padstack (mode custom) (layer "B.Cu" (size 3))))"""
    near = track((11.7, 8), (11.7, 12), "near").replace('"F.Cu"', '"B.Cu"')
    pcb = board(item, near)
    result = DRCChecker(pcb, physical_copper_gap_mm=0.25).check_physical_copper_gap()
    assert result.violations
    assert all(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)
    assert any(f"unsupported {kind} padstack" in v.message for v in result.violations)

    path = tmp_path / "padstack.kicad_pcb"
    original = serialize_sexp(pcb._sexp)
    path.write_text(original)
    assert (
        main(
            [
                "check",
                str(path),
                "--drc-only",
                "--only",
                "physical_copper_gap",
                "--physical-copper-gap",
                ".25",
                "--format",
                "json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert any(v["rule_id"] == "physical_copper_gap_incomplete" for v in payload["violations"])
    assert path.read_text() == original


def test_ordinary_rear_rectangle_reports_padstack_witness_gap():
    pad = """(footprint "Stack" (layer "F.Cu") (at 10 10)
      (property "Reference" "U1")
      (pad "1" thru_hole rect (at 0 0) (size 3 1) (drill .4)
       (layers "*.Cu" "*.Mask") (net 1 "GND") (uuid "pad")))"""
    near = track((11.7, 8), (11.7, 12), "near").replace('"F.Cu"', '"B.Cu"')
    result = check_physical_copper_gap(board(pad, near), 0.25)
    assert not any(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)
    assert any(
        v.layer == "B.Cu" and v.actual_value == pytest.approx(0.1) for v in result.violations
    )


@pytest.mark.parametrize(
    "kind", ["gr_text", "gr_text_box", "gr_curve", "fp_text", "fp_text_box", "fp_curve", "property"]
)
@pytest.mark.parametrize("layer", ["F.Cu", "B.Cu", "F.SilkS"])
def test_raw_graphic_inventory_api_and_cli(kind, layer, tmp_path, capsys):
    import json

    from kicad_tools.cli import main
    from kicad_tools.sexp import serialize_sexp

    content = (
        'user "label"'
        if kind == "fp_text"
        else '"Reference" "U1"'
        if kind == "property"
        else '"label"'
        if "text" in kind
        else ""
    )
    item = f'({kind} {content} (at 1 1) (start 1 1) (end 3 3) (pts (xy 1 1) (xy 2 2) (xy 3 2) (xy 4 1)) (layer "{layer}") (stroke (width .2) (type default)) (effects (font (size 1 1) (thickness .15))))'
    if kind.startswith("fp_") or kind == "property":
        item = f'(footprint "test" (layer "F.Cu") (at 0 0) {item})'
    pcb = board(item)
    result = check_physical_copper_gap(pcb, 0.25)
    expected = layer.endswith(".Cu")
    assert bool(result.violations) is expected
    assert all(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)
    path = tmp_path / "graphic.kicad_pcb"
    original = serialize_sexp(pcb._sexp)
    path.write_text(original)
    status = main(
        [
            "check",
            str(path),
            "--drc-only",
            "--only",
            "physical_copper_gap",
            "--physical-copper-gap",
            ".25",
            "--format",
            "json",
        ]
    )
    assert status == (2 if expected else 0)
    data = json.loads(capsys.readouterr().out)
    assert bool(data["violations"]) is expected
    assert path.read_text() == original


@pytest.mark.parametrize("kind", ["gr_text", "fp_text", "property", "gr_text_box", "fp_text_box"])
@pytest.mark.parametrize(
    "hide", ["hide", "(hide yes)", "(effects (hide yes))", "(effects hide)", "(hide no)"]
)
def test_hidden_copper_text_inventory(kind, hide):
    content = (
        'user "label"'
        if kind == "fp_text"
        else '"Reference" "U1"'
        if kind == "property"
        else '"label"'
    )
    item = f'({kind} {content} (at 1 1) (start 1 1) (end 3 3) (layer "F.Cu") {hide})'
    if kind.startswith("fp_") or kind == "property":
        item = f'(footprint "test" (layer "F.Cu") (at 0 0) {item})'
    result = check_physical_copper_gap(board(item), 0.25)
    assert bool(result.violations) is (hide == "(hide no)")


def test_unrecognized_raw_copper_object_is_incomplete():
    pcb = board('(gr_future_shape (layer "F.Cu") (at 1 1))')
    assert any(
        v.rule_id == "physical_copper_gap_incomplete"
        for v in check_physical_copper_gap(pcb, 0.25).violations
    )


@pytest.mark.parametrize("kind", ["gr_text", "fp_text", "property", "gr_text_box", "fp_text_box"])
def test_visible_literal_hide_is_not_a_visibility_marker(kind):
    content = (
        'user "hide"'
        if kind == "fp_text"
        else '"Reference" "hide"'
        if kind == "property"
        else '"hide"'
    )
    item = f'({kind} {content} (at 1 1) (start 1 1) (end 3 3) (layer "F.Cu"))'
    if kind.startswith("fp_") or kind == "property":
        item = f'(footprint "test" (layer "F.Cu") (at 0 0) {item})'
    assert check_physical_copper_gap(board(item), 0.25).violations


# --- Footprint copper polygons (fp_poly), Issue #5817 ----------------------
#
# A standard ``NetTie-2_SMD_Pad0.5mm`` joins its two pads with a filled
# ``F.Cu`` ``fp_poly``.  Before #5817 that primitive was inventoried as
# "unsupported copper graphic" and the whole assessment came back
# incomplete.

# Verbatim reduced fixture from Issue #5817 (one net tie plus a board
# outline; no routing, no zone fill).
NET_TIE_FIXTURE = """(kicad_pcb (version 20260115) (generator "pcbnew")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)
        (44 "Edge.Cuts" user)) (setup (pad_to_mask_clearance 0))
      (gr_rect (start 100 70) (end 190 160)
        (stroke (width 0.05) (type default)) (fill none)
        (layer "Edge.Cuts") (uuid "e4af335a-9b4d-5c17-90e3-7320110fa3bc"))
      (footprint "NetTie-2_SMD_Pad0.5mm"
        (layer "F.Cu")
        (uuid "80f92f63-7d6e-418b-978d-20000011cde0")
        (at 140.852007 85.488825 -90)
        (descr "Net tie, 2 pin, 0.5mm square SMD pads")
        (tags "net tie")
        (property "Reference" "NT3" (at 0 -1.2 0) (layer "F.SilkS") (hide yes)
          (uuid "ed86465d-16b2-4087-88d6-25aa25885dfd")
          (effects (font (size 1 1) (thickness 0.15))))
        (property "Value" "BOOT0_TIE" (at 0 1.2 0) (layer "F.Fab") (hide yes)
          (uuid "55decc6c-ccba-49a9-9c58-e53b55d22ed2")
          (effects (font (size 1 1) (thickness 0.15))))
        (attr exclude_from_pos_files exclude_from_bom allow_missing_courtyard)
        (net_tie_pad_groups "1, 2")
        (duplicate_pad_numbers_are_jumpers no)
        (fp_poly (pts (xy -0.5 -0.25) (xy 0.5 -0.25) (xy 0.5 0.25) (xy -0.5 0.25))
          (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu")
          (uuid "4c587801-35de-4fc4-895b-9942aca7f89f"))
        (pad "1" smd circle (at -0.5 0 270) (size 0.5 0.5) (layers "F.Cu")
          (net "SWCLK") (uuid "1745e8e7-cb05-4e0c-be57-60ff31465fb9"))
        (pad "2" smd circle (at 0.5 0 270) (size 0.5 0.5) (layers "F.Cu")
          (net "BOOT0") (uuid "b531e0f4-5d47-4a72-8a54-a3eaafb85b86"))
        (embedded_fonts no)))"""


def poly_footprint(
    identity="tie",
    at="10 10 0",
    layer="F.Cu",
    fill="yes",
    stroke="0",
    pts="(xy -0.5 -0.4) (xy 0.5 -0.4) (xy 0.5 0.4) (xy -0.5 0.4)",
    reference="NT1",
    pads="",
    uuid=True,
):
    """A footprint whose only copper is one ``fp_poly`` (plus optional pads)."""
    tag = f'(uuid "{identity}")' if uuid else ""
    return f'''(footprint "NetTie-2_SMD_Pad0.5mm" (layer "{layer}") (at {at})
      (property "Reference" "{reference}")
      (fp_poly (pts {pts}) (stroke (width {stroke}) (type solid))
        (fill {fill}) (layer "{layer}") {tag})
      {pads})'''


def tie_pads(layer="F.Cu", prefix="tie"):
    return f'''(pad "1" smd circle (at -0.5 0 270) (size .5 .5) (layers "{layer}")
        (net 1 "GND") (uuid "{prefix}-p1"))
      (pad "2" smd circle (at 0.5 0 270) (size .5 .5) (layers "{layer}")
        (net 2 "VCC") (uuid "{prefix}-p2"))'''


def test_net_tie_filled_polygon_is_modeled_not_incomplete():
    """The issue's own fixture: complete coverage and no phantom slit."""
    pcb = PCB(parse_string(NET_TIE_FIXTURE))
    result = check_physical_copper_gap(pcb, 0.1)
    assert result.violations == []


@pytest.mark.parametrize("minimum", [0.1, 0.25, 0.6])
def test_net_tie_joined_copper_is_never_a_zero_width_slit(minimum):
    """Pad-to-pad copper is an intentional join, not facing boundaries."""
    item = poly_footprint(
        pts="(xy -0.5 -0.25) (xy 0.5 -0.25) (xy 0.5 0.25) (xy -0.5 0.25)",
        pads=tie_pads(),
    )
    assert check_physical_copper_gap(board(item), minimum).violations == []


@pytest.mark.parametrize(
    "near,expected_layer",
    [
        (((10.6, 9), (10.6, 11)), "F.Cu"),  # vertical neighbour, x gap 0.1
        (((9, 10.7), (11, 10.7)), "F.Cu"),  # horizontal neighbour, y gap 0.1
    ],
)
def test_rotated_polygon_gap_to_separate_boundary(near, expected_layer):
    """A -90 deg footprint turns the 1.0 x 0.8 local rect on its side.

    Board extent becomes x 9.6..10.4 / y 9.5..10.5, so BOTH neighbours sit
    0.1 mm off the copper.  An untransformed polygon would put the vertical
    neighbour inside the copper and the horizontal one 0.2 mm away, so the
    measured value discriminates the rotation in each axis.
    """
    result = gaps(board(poly_footprint(at="10 10 -90"), track(*near, "near")))
    assert len(result) == 1
    assert result[0].actual_value == pytest.approx(0.1, abs=0.001)
    assert result[0].layer == expected_layer
    assert "tie" in result[0].items
    # Footprint copper carries no net of its own; the finding is geometric.
    assert result[0].nets == ("", "GND")


def test_polygon_provenance_falls_back_to_reference_when_uuid_absent():
    result = gaps(
        board(poly_footprint(at="10 10 -90", uuid=False), track((10.6, 9), (10.6, 11), "near"))
    )
    assert result and "NT1:fp_poly:0" in result[0].items


def test_polygon_connected_elsewhere_still_reports_the_slit():
    """A track leaving pad 1 and doubling back over the tie is a hairpin."""
    item = poly_footprint(
        pts="(xy -0.5 -0.25) (xy 0.5 -0.25) (xy 0.5 0.25) (xy -0.5 0.25)",
        pads=tie_pads(),
    )
    result = gaps(
        board(
            item,
            track((9.5, 10), (9.5, 10.45), "riser"),
            track((9.5, 10.45), (11.5, 10.45), "return"),
        )
    )
    assert result
    assert min(v.actual_value or 0 for v in result) == pytest.approx(0.1, abs=0.001)
    assert any("tie" in v.items for v in result)


def test_unfilled_polygon_is_a_stroked_ring_not_solid_copper():
    """``(fill no)`` prints only the outline, so the interior stays air."""
    item = poly_footprint(
        fill="no",
        stroke="0.2",
        pts="(xy -0.6 -0.35) (xy 0.6 -0.35) (xy 0.6 0.35) (xy -0.6 0.35)",
    )
    assert not gaps(board(item), 0.25)
    interior = gaps(board(item), 0.6)
    assert interior
    assert interior[0].items == ("tie",)
    assert interior[0].actual_value == pytest.approx(0.5, abs=0.001)


def test_filled_polygon_is_dilated_by_its_stroke_width():
    item = poly_footprint(
        stroke="0.2", pts="(xy -0.5 -0.25) (xy 0.5 -0.25) (xy 0.5 0.25) (xy -0.5 0.25)"
    )
    result = gaps(board(item, track((9, 10.55), (11, 10.55), "near")))
    assert result and result[0].actual_value == pytest.approx(0.1, abs=0.001)


def test_back_side_polygon_stays_on_the_back_layer():
    item = poly_footprint(layer="B.Cu", at="10 10 -90")
    back = track((10.6, 9), (10.6, 11), "near").replace('"F.Cu"', '"B.Cu"')
    result = gaps(board(item, back))
    assert len(result) == 1
    assert result[0].layer == "B.Cu"
    assert "tie" in result[0].items
    assert not gaps(board(item, track((10.6, 9), (10.6, 11), "near")))


def test_non_copper_polygon_is_out_of_scope():
    item = poly_footprint(layer="F.SilkS")
    assert check_physical_copper_gap(board(item), 0.25).violations == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pts": "(xy banana 0) (xy 1 0) (xy 1 1)"},
        {"pts": "(xy 0 0) (xy 1 0)"},
        {"pts": "(xy 0 0 0) (xy 1 0) (xy 1 1)"},
        {"stroke": "banana"},
        {"stroke": "-0.2"},
        {"fill": "no", "stroke": "0"},
        {"layer": "In1.Cu"},
    ],
)
def test_invalid_polygon_geometry_is_incomplete_not_clean(kwargs):
    pcb = board(poly_footprint(**kwargs))
    result = check_physical_copper_gap(pcb, 0.25)
    assert result.violations
    assert all(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)


def test_missing_polygon_points_is_incomplete_not_clean():
    item = """(footprint "NT" (layer "F.Cu") (at 10 10)
      (property "Reference" "NT1")
      (fp_poly (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu")
        (uuid "tie")))"""
    result = check_physical_copper_gap(board(item), 0.25)
    assert result.violations
    assert all(v.rule_id == "physical_copper_gap_incomplete" for v in result.violations)
