"""Imported copper arcs retain their geometry, identity and measurement population."""

import math
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.analysis.routing_quality import compute_routing_quality
from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
from kicad_tools.benchmark.external.metrics import measure_copper
from kicad_tools.schema import PCB, Arc
from kicad_tools.sexp import parse_string

ARC = """(arc  (start 10.000 10)\n  (mid 15 5.0000) (end 20 10)
 (width .25) (layer "F.Cu")
 (uuid "22ea5bc2-b75d-400a-964f-ff4bf9222c3d") (net 1))"""
HEADER = """(kicad_pcb (version 20240108) (generator "test")
 (general (thickness 1.6))
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (net 0 "") (net 1 "SIG") (net 2 "OTHER")"""


def pad(ref, x, y, layer="F.Cu", net=1, name="SIG"):
    return f'''(footprint "test" (layer "F.Cu") (at {x} {y})
    (property "Reference" "{ref}")
    (pad "1" smd circle (at 0 0) (size .2 .2) (layers "{layer}") (net {net} "{name}")))'''


def board(*items):
    return PCB(parse_string(HEADER + "\n".join(items) + ")"))


def status(pcb, strict):
    result = NetStatusAnalyzer(pcb, strict=strict).analyze().get_net("SIG")
    assert result is not None
    return result


@pytest.mark.parametrize("name_only", [False, True])
def test_load_save_preserves_exact_arc_node(tmp_path: Path, name_only):
    arc = ARC.replace("(net 1)", '(net "SIG")') if name_only else ARC
    header = HEADER.replace('(net 0 "") (net 1 "SIG") (net 2 "OTHER")', "") if name_only else HEADER
    source = tmp_path / "in.kicad_pcb"
    source.write_text(header + arc + ")")
    pcb = PCB.load(source)
    (item,) = pcb.arcs
    assert item.start == (10, 10)
    assert item.mid == (15, 5)
    assert item.end == (20, 10)
    assert item.width == 0.25
    assert item.net_name == "SIG"
    assert item.net_name_only == name_only
    assert item.uuid == "22ea5bc2-b75d-400a-964f-ff4bf9222c3d"
    assert list(pcb.arcs_on_layer("F.Cu")) == [item]
    assert list(pcb.arcs_in_net(item.net_number)) == [item]
    assert not list(pcb.arcs_on_layer("B.Cu"))
    assert pcb.segments == []
    target = tmp_path / "out.kicad_pcb"
    pcb.save(target)
    assert arc.encode() in target.read_bytes()
    again = PCB.load(target)
    assert again.arcs == pcb.arcs
    # A real tree edit must invalidate the retained text rather than hide it.
    width = pcb._sexp.find("arc").find("width")
    width.children[0].value = 0.4
    width.children[0]._original_str = None
    pcb.save(target)
    assert PCB.load(target).arcs[0].width == 0.4
    assert arc.encode() not in target.read_bytes()


@pytest.mark.parametrize("strict", [False, True])
def test_arc_only_connectivity_and_measurement(tmp_path, strict):
    pcb = board(ARC, pad("J1", 10, 10), pad("J2", 20, 10))
    result = status(pcb, strict)
    assert (result.status, result.island_count, result.has_routing) == ("complete", 1, True)
    report = TraceLengthAnalyzer().analyze_net(pcb, "SIG")
    assert report.total_length_mm == pytest.approx(5 * math.pi)
    assert report.segment_count == 0
    assert report.arc_count == 1
    assert report.layers_used == {"F.Cu"}
    path = tmp_path / "arc.kicad_pcb"
    pcb.save(path)
    metrics = measure_copper(path)
    assert metrics.wirelength_mm == pytest.approx(report.total_length_mm)
    assert (metrics.segment_count, metrics.arc_count) == (0, 1)
    quality = compute_routing_quality(pcb)
    assert quality.off_axis_count == quality.total_segments == 0
    assert quality.to_dict()["copper_arc_count"] == 1
    assert quality.copper_arc_length_mm == pytest.approx(5 * math.pi)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "x,y,layer,expected",
    [
        (15, 5, "F.Cu", "complete"),  # Interior of the actual arc.
        (15, 10, "F.Cu", "incomplete"),  # Chord, not copper.
        (15, 7, "F.Cu", "incomplete"),  # Inside bounding box, not copper.
        (15, 4.77, "F.Cu", "incomplete"),  # 0.005 mm air gap.
        (15, 5, "B.Cu", "incomplete"),
    ],
)
def test_interior_contact_requires_real_same_layer_copper(strict, x, y, layer, expected):
    pcb = board(ARC, pad("J1", 10, 10), pad("J2", x, y, layer))
    assert status(pcb, strict).status == expected


@pytest.mark.parametrize("strict", [False, True])
def test_wrong_net_arc_does_not_connect(strict):
    pcb = board(ARC.replace("(net 1)", "(net 2)"), pad("J1", 10, 10), pad("J2", 20, 10))
    assert status(pcb, strict).status == "incomplete"


@pytest.mark.parametrize("strict", [False, True])
def test_mixed_arc_segment_via_path(strict):
    pcb = board(
        ARC,
        pad("J1", 10, 10),
        pad("J2", 15, 2, "B.Cu"),
        '(via (at 15 5) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net 1))',
        '(segment (start 15 5) (end 15 2) (width .25) (layer "B.Cu") (net 1))',
    )
    assert status(pcb, strict).status == "complete"
    report = TraceLengthAnalyzer().analyze_net(pcb, "SIG")
    assert report.total_length_mm == pytest.approx(5 * math.pi + 3)
    assert (report.segment_count, report.arc_count, report.via_count) == (1, 1, 1)
    assert report.layers_used == {"F.Cu", "B.Cu"}


@pytest.mark.parametrize("angles", [(0, 45, 90), (90, 45, 0), (0, 180, 270), (270, 180, 0)])
def test_swept_lengths_and_tessellation_error(angles):
    points = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in angles]
    arc = Arc(start=points[0], mid=points[1], end=points[2], width=0.25, layer="F.Cu", net_number=1)
    assert arc.length == pytest.approx(math.radians(abs(angles[2] - angles[0])))
    samples = arc.centerline_points()
    assert samples[0] == arc.start and samples[-1] == arc.end
    for a, b in zip(samples, samples[1:], strict=False):
        chord_mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert abs(1 - math.hypot(*chord_mid)) <= 0.00001 + 1e-14


@pytest.mark.parametrize(
    "bad_arc",
    [
        ARC.replace("(mid 15 5.0000)", "(mid 15 10)"),
        ARC.replace("(mid 15 5.0000)", ""),
        ARC.replace("(mid 15 5.0000)", "(mid nan 5)"),
        ARC.replace("(width .25)", "(width 0)"),
        ARC.replace("(width .25)", "(width inf)"),
    ],
)
def test_invalid_arc_fails_instead_of_fake_copper(bad_arc):
    with pytest.raises(ValueError, match="Copper arc"):
        board(bad_arc)


def test_differential_pair_skew_includes_arcs():
    pcb = board(ARC, '(segment (start 10 20) (end 20 20) (width .25) (layer "F.Cu") (net 2))')
    pair = TraceLengthAnalyzer().analyze_diff_pair(pcb, "SIG", "OTHER")
    assert pair.skew_mm == pytest.approx(5 * math.pi - 10)


def test_origin_and_page_fit_keep_all_three_arc_points(tmp_path):
    pcb = board(ARC, '(gr_rect (start 5 2) (end 25 15) (stroke (width .1)) (layer "Edge.Cuts"))')
    (arc,) = pcb.arcs
    assert (arc.start, arc.mid, arc.end) == ((5, 8), (10, 3), (15, 8))
    pcb.page_fit(margin=1)
    assert len(pcb.arcs) == 1
    assert pcb.arcs[0] == arc
    path = tmp_path / "moved.kicad_pcb"
    pcb.save(path)
    assert PCB.load(path).arcs == pcb.arcs


def test_committed_arc_fixture_completion_and_measurement():
    from kicad_tools.benchmark.external.metrics import measure_completion

    path = Path(__file__).parent / "fixtures" / "copper_arc_routed.kicad_pcb"
    completion = measure_completion(path)
    assert completion.connections_routed == completion.connections_total == 1
    assert completion.nets_incomplete == completion.nets_unrouted == 0
    assert measure_copper(path).wirelength_mm == pytest.approx(5 * math.pi)


@pytest.mark.parametrize("name_only", [False, True])
def test_arc_emitter_preserves_net_dialect_and_three_point_order(name_only):
    item = Arc.from_sexp(parse_string(ARC.replace("(net 1)", '(net "SIG")') if name_only else ARC))
    emitted = item.to_sexp(offset=(100, 200))
    assert [child.tag for child in emitted.iter_children()] == [
        "start",
        "mid",
        "end",
        "width",
        "layer",
        "uuid",
        "net",
    ]
    again = Arc.from_sexp(emitted)
    assert again.start == (110, 210)
    assert again.mid == (115, 205)
    assert again.end == (120, 210)
    assert again.net_name_only == name_only
    assert again.uuid == item.uuid
    assert again.length == pytest.approx(item.length)


@pytest.mark.parametrize("strict", [False, True])
def test_arc_to_arc_chain(strict):
    second = (
        ARC.replace("(start 10.000 10)", "(start 20 10)")
        .replace("(mid 15 5.0000)", "(mid 25 15)")
        .replace("(end 20 10)", "(end 30 10)")
        .replace("22ea5bc2-b75d-400a-964f-ff4bf9222c3d", "another-arc")
    )
    pcb = board(ARC, second, pad("J1", 10, 10), pad("J2", 30, 10))
    assert status(pcb, strict).status == "complete"
    assert TraceLengthAnalyzer().analyze_net(pcb, "SIG").total_length_mm == pytest.approx(
        10 * math.pi
    )


@pytest.mark.parametrize("filled,expected", [(True, "complete"), (False, "incomplete")])
def test_arc_interior_to_filled_zone(filled, expected):
    points = "(pts (xy 14 3) (xy 16 3) (xy 16 5.1) (xy 14 5.1))"
    fill = f'(filled_polygon (layer "F.Cu") {points})' if filled else ""
    zone = f"""(zone (net 1) (net_name "SIG") (layer "F.Cu")
      (hatch edge .5) (connect_pads (clearance .2)) (min_thickness .1)
      (fill yes (thermal_gap .3) (thermal_bridge_width .3))
      (polygon {points}) {fill})"""
    pcb = board(ARC, zone, pad("J1", 10, 10), pad("J2", 15, 3.5))
    assert status(pcb, True).status == expected
