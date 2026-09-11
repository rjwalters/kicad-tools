"""Physical contact paths missed by the label-free extractor (#5133)."""

from pathlib import Path

import pytest

from kicad_tools.validate.connectivity import ConnectivityValidator


def pad(ref, x, y, layer, size=(0.7, 0.3)):
    return f'''(footprint "test" (layer "F.Cu") (at {x} {y})
      (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))
      (pad "1" smd rect (at 0 0) (size {size[0]} {size[1]})
       (layers "{layer}") (net 1 "N")))'''


def fill(points, layer):
    return (
        f'(filled_polygon (layer "{layer}") (pts '
        + "".join(f"(xy {x} {y})" for x, y in points)
        + "))"
    )


def board(pads, fills, layer, extra=""):
    return (
        """(kicad_pcb (version 20240108) (generator test)
      (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "N") """
        + pads
        + extra
        + f'''
      (zone (net 1) (net_name "N") (layer "{layer}") (hatch edge 0.5)
       (connect_pads (clearance .2)) (min_thickness .2) (fill yes)
       (polygon (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))'''
        + "".join(fill(points, layer) for points in fills)
        + "))"
    )


def partition(tmp_path, text):
    path = tmp_path / "contact.kicad_pcb"
    path.write_text(text)
    return ConnectivityValidator(path).extract_pad_partition()


def bonded(groups):
    return any({"A.1", "B.1"} <= group for group in groups)


@pytest.mark.parametrize("gap", [False, True])
def test_pad_edge_via_to_inner_fill(tmp_path, gap):
    # Via centre is outside the .3-wide pad; its annulus overlaps the pad edge.
    x = 5.5 if gap else 5.2
    text = board(
        pad("A", x, 5, "F.Cu", (0.3, 0.7)) + pad("B", 9, 5, "In1.Cu"),
        [[(4, 4), (10, 4), (10, 6), (4, 6)]],
        "In1.Cu",
        '(via (at 5 5.15) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1))',
    )
    assert bonded(partition(tmp_path, text)) is not gap


@pytest.mark.parametrize("gap", [False, True])
def test_pad_trace_via_back_trace_fill(tmp_path, gap):
    # The via is outside the fill; only the back-layer trace contacts it.
    end = 7.7 if gap else 8.2
    extra = f"""(segment (start 2 5) (end 5 5) (width .15) (layer "F.Cu") (net 1))
      (via (at 5 5) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1))
      (segment (start 5.125 5) (end {end} 5) (width .2) (layer "B.Cu") (net 1))"""
    text = board(
        pad("A", 2, 5, "F.Cu") + pad("B", 9, 5, "B.Cu"),
        [[(8, 4), (10, 4), (10, 6), (8, 6)]],
        "B.Cu",
        extra,
    )
    assert bonded(partition(tmp_path, text)) is not gap


def test_same_zone_islands_are_not_a_connection(tmp_path):
    text = board(
        pad("A", 2, 5, "F.Cu") + pad("B", 9, 5, "F.Cu"),
        [[(1, 4), (3, 4), (3, 6), (1, 6)], [(8, 4), (10, 4), (10, 6), (8, 6)]],
        "F.Cu",
    )
    assert not bonded(partition(tmp_path, text))


FROZEN = (
    Path(__file__).resolve().parents[1]
    / "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
)


def test_frozen_existing_paths_join_partition():
    groups = ConnectivityValidator(FROZEN).extract_pad_partition()
    for pad_id in ("U1.15", "U1.17", "U1.32"):
        assert len(next(group for group in groups if pad_id in group)) > 1, pad_id
    assert any({"U1.15", "U1.32"} <= group for group in groups)


@pytest.mark.parametrize("span", [("F.Cu", "B.Cu"), ("In2.Cu", "B.Cu")])
@pytest.mark.parametrize("pad_layer", ["F.Cu", "B.Cu"])
def test_edge_via_respects_restricted_span_and_pad_layer(tmp_path, span, pad_layer):
    text = board(
        pad("A", 5.2, 5, pad_layer, (0.3, 0.7)) + pad("B", 9, 5, "In1.Cu"),
        [[(4, 4), (10, 4), (10, 6), (4, 6)]],
        "In1.Cu",
        f'(via (at 5 5.15) (size .45) (drill .25) (layers "{span[0]}" "{span[1]}") (net 1))',
    )
    assert bonded(partition(tmp_path, text)) is (span[0] == "F.Cu")


def test_fill_cutout_is_not_conductive(tmp_path):
    hole = [
        (0, 0),
        (10, 0),
        (10, 10),
        (0, 10),
        (0, 0),
        (4, 4),
        (4, 6),
        (6, 6),
        (6, 4),
        (4, 4),
        (0, 0),
    ]
    text = board(
        pad("A", 5.2, 5, "F.Cu", (0.3, 0.7)) + pad("B", 9, 5, "In1.Cu"),
        [hole],
        "In1.Cu",
        '(via (at 5 5.15) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1))',
    )
    assert not bonded(partition(tmp_path, text))


def test_physical_trace_can_join_separate_fill_islands(tmp_path):
    text = board(
        pad("A", 2, 5, "F.Cu") + pad("B", 9, 5, "F.Cu"),
        [[(1, 4), (3, 4), (3, 6), (1, 6)], [(8, 4), (10, 4), (10, 6), (8, 6)]],
        "F.Cu",
        '(segment (start 2.8 5) (end 8.2 5) (width .2) (layer "F.Cu") (net 1))',
    )
    assert bonded(partition(tmp_path, text))


@pytest.mark.parametrize("angle", [-45, -30, 0, 30, 45, 90, 180, 270])
@pytest.mark.parametrize("overlap", [True, False])
def test_rotated_raw_pad_annulus_contact_and_gap(tmp_path, angle, overlap):
    import math

    from kicad_tools.lvs.copper_lvs import compare_partitions

    distance = 0.15 + 0.225 + (-0.01 if overlap else 0.02)
    x, y = (
        5 + math.sin(math.radians(angle)) * distance,
        5 + math.cos(math.radians(angle)) * distance,
    )
    a = pad("A", 5, 5, "F.Cu", (1, 0.3)).replace("(at 0 0) (size", f"(at 0 0 {angle}) (size")
    # Different declared labels must neither prevent physical shorts nor
    # create a bond across a real gap.
    a = a.replace('(net 1 "N")', '(net 2 "OTHER")')
    text = board(
        a + pad("B", 9, 5, "In1.Cu"),
        [[(4, 4), (10, 4), (10, 6), (4, 6)]],
        "In1.Cu",
        f'(net 2 "OTHER") (via (at {x} {y}) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 2))',
    )
    groups = partition(tmp_path, text)
    assert bonded(groups) is overlap
    result = compare_partitions({("A", "1"): "OTHER", ("B", "1"): "N"}, groups)
    assert bool(result.shorts) is overlap


def test_frozen_raw_positive_area_paths_and_unchanged_bytes():
    import hashlib
    from collections import deque

    from shapely.geometry import LineString, Point

    from kicad_tools.validate.rules.clearance import _pad_polygon

    before = FROZEN.read_bytes()
    assert (
        hashlib.sha256(before).hexdigest()
        == "e6ab8780fa72e727342544e1063b0f3e86054862848c6c8bc4cd15ae83e62178"
    )
    validator = ConnectivityValidator(FROZEN)
    pcb = validator.pcb
    fp = pcb.get_footprint("U1")
    copper_layers = {layer.name for layer in pcb.copper_layers}
    for number in ("15", "17", "32"):
        source = next(p for p in fp.pads if p.number == number)
        nodes = [("pad", _pad_polygon(source, fp), set(source.layers) & copper_layers)]
        for segment in pcb.segments_in_net(source.net_number):
            nodes.append(
                (
                    "segment",
                    LineString([segment.start, segment.end]).buffer(segment.width / 2),
                    {segment.layer},
                )
            )
        for via in pcb.vias_in_net(source.net_number):
            annulus = (
                Point(via.position)
                .buffer(via.size / 2, quad_segs=64)
                .difference(Point(via.position).buffer(via.drill / 2, quad_segs=64))
            )
            # Independently expand the explicit endpoints in physical stack order.
            order = [layer.name for layer in pcb.copper_layers]
            a, b = sorted(order.index(layer) for layer in via.layers)
            nodes.append(("via", annulus, set(order[a : b + 1])))
        for zone in pcb.zones:
            if zone.net_number != source.net_number:
                continue
            for index, points in enumerate(zone.filled_polygons):
                solid = validator._fill_solid_region(points)
                if solid is not None:
                    nodes.append(("fill", solid, {zone.filled_polygon_layer(index)}))
        queue, seen, found = deque([0]), {0}, False
        while queue:
            left = queue.popleft()
            if nodes[left][0] == "fill":
                found = True
                break
            for right in range(1, len(nodes)):
                if right in seen or not nodes[left][2] & nodes[right][2]:
                    continue
                if nodes[left][1].intersection(nodes[right][1]).area > 0:
                    seen.add(right)
                    queue.append(right)
        assert found, f"U1.{number} lacks independently measured positive-area path"
    assert FROZEN.read_bytes() == before


@pytest.mark.parametrize("with_fill", [False, True])
@pytest.mark.parametrize("depth,bond", [(0.0005, False), (0.002, True)])
def test_pour_graph_preserves_existing_via_trace_contact_depth(tmp_path, with_fill, depth, bond):
    via_x = 5 + 0.225 + 0.1 - depth
    extra = f"""(segment (start 2 5) (end 5 5) (width .2) (layer "F.Cu") (net 1))
      (via (at {via_x} 5) (size .45) (drill .25) (layers "F.Cu" "B.Cu") (net 1))"""
    fills = [[(5, 4), (6, 4), (6, 6), (5, 6)]] if with_fill else []
    text = board(pad("A", 2, 5, "F.Cu") + pad("B", via_x, 5, "B.Cu"), fills, "B.Cu", extra)
    assert bonded(partition(tmp_path, text)) is bond
