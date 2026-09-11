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
    assert result[0].items == ("a", "b")
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
    assert result and "arc" in result[0].items
    zone = """(zone (net 1) (net_name "GND") (layer "F.Cu") (uuid "zone")
      (polygon (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1)))
      (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 1) (xy 0 1))))"""
    result = gaps(board(zone, track((0, 1.2), (4, 1.2), "track")))
    assert result and result[0].items == ("track", "zone")


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
    assert result and result[0].items == ("track", "via")
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
