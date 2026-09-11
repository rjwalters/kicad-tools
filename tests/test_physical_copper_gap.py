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
