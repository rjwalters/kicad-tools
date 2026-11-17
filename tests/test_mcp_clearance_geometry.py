"""File-backed physical and shared-model checks for MCP clearance measurement."""

import pytest

from kicad_tools.mcp.tools.analysis import measure_clearance


def board(tmp_path, elements):
    path = tmp_path / "clearance.kicad_pcb"
    path.write_text(f"""(kicad_pcb (version 20240108) (generator test)
        (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
        (net 0 "") (net 1 "A") (net 2 "B") (net 3 "C")
        {elements})""")
    return path


def pad(
    ref="U1",
    x=0,
    y=0,
    shape="rect",
    width=4,
    height=1,
    angle=0,
    fp_angle=0,
    local=(0, 0),
    net=1,
    layer="F.Cu",
    ratio=0.25,
):
    return f'''(footprint "P" (layer "{layer}") (at {x} {y} {fp_angle})
        (property "Reference" "{ref}")
        (pad "1" smd {shape} (at {local[0]} {local[1]} {angle}) (size {width} {height})
          (layers "{layer}") (roundrect_rratio {ratio}) (net {net} "{chr(64 + net) if net else ""}")))'''


def segment(a, b, width=0.2, net=2, layer="F.Cu", uuid="trace1"):
    return f'''(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]})
        (width {width}) (layer "{layer}") (net {net}) (uuid "{uuid}"))'''


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "a,b,width,expected", [((-3, 1), (3, 1), 0.2, 0.4), ((1.99, 0.49), (2.5, 0.49), 0.02, -0.01)]
)
def test_rectangle_physical_numbers(tmp_path, reverse, a, b, width, expected):
    path = board(tmp_path, pad() + segment(a, b, width))
    items = ("B", "U1") if reverse else ("U1", "B")
    result = measure_clearance(str(path), *items, layer="F.Cu")
    assert result.min_clearance_mm == pytest.approx(expected)
    assert result.location == (0, 0)
    assert len(result.clearances) == 1
    measurement = result.clearances[0]
    assert {measurement.from_type, measurement.to_type} == {"pad", "track"}
    assert {measurement.from_item, measurement.to_item} == {"U1-1", "Track-trace1"}


def via(x=0, y=2, size=0.5, net=2, uuid="via1"):
    return f'''(via (at {x} {y}) (size {size}) (drill .2) (layers "F.Cu" "B.Cu")
        (net {net}) (uuid "{uuid}"))'''


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("pad_pad", 1.25),
        ("pad_via", 1.25),
        ("track_track", 0.8),
        ("track_via", 0.65),
        ("circle_via", 1.25),
        ("via_via", 1.5),
    ],
)
def test_pair_dispatches_numeric_and_shared_parity(tmp_path, kind, expected, reverse):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.clearance import CopperElement, _calculate_clearance

    first = pad()
    second = via()
    if kind == "pad_pad":
        second = pad("U2", 0, 2, "circle", 0.5, 0.5, net=2)
    elif kind == "track_track":
        first = segment((-2, 0), (2, 0), net=1, uuid="trackA")
        second = segment((-2, 1), (2, 1), net=2, uuid="trackB")
    elif kind == "track_via":
        first = segment((-2, 0), (2, 0), net=1)
        second = via(y=1)
    elif kind == "circle_via":
        first = pad(shape="circle", width=1, height=1)
    elif kind == "via_via":
        first = via(y=0, net=1, uuid="viaA")
    path = board(tmp_path, first + second)
    result = measure_clearance(str(path), *(("B", "A") if reverse else ("A", "B")), layer="F.Cu")
    assert result.min_clearance_mm == pytest.approx(expected, abs=1e-4)
    pcb = PCB.load(path)
    elements = [CopperElement.from_pad(p, f) for f in pcb.footprints for p in f.pads]
    elements += [CopperElement.from_segment(s) for s in pcb.segments]
    elements += [CopperElement.from_via(v) for v in pcb.vias]
    a, b = sorted(elements, key=lambda e: e.net_number)
    direct = _calculate_clearance(b, a) if reverse else _calculate_clearance(a, b)
    assert result.min_clearance_mm == round(direct[0], 4)
    assert result.location == tuple(round(x, 3) for x in direct[1:])
    assert len(result.clearances) == 1


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("trace_y,expected,passes", [(98.6, 0.1846, True), (98.45, 0.0904, False)])
def test_c19_rotated_roundrect_actual_control(
    tmp_path, monkeypatch, reverse, trace_y, expected, passes
):
    from kicad_tools.mcp.tools import analysis

    path = board(
        tmp_path,
        pad(
            "C19",
            147.086449,
            97.046534,
            "roundrect",
            1,
            1.45,
            angle=270,
            fp_angle=-90,
            local=(0.95, 0),
        )
        + segment((147.9, trace_y), (154.4, trace_y), width=0.11),
    )
    monkeypatch.setattr(analysis, "_get_design_rules_clearance", lambda pcb: 0.1016)
    result = measure_clearance(
        str(path), *(("B", "C19") if reverse else ("C19", "B")), layer="F.Cu"
    )
    assert result.min_clearance_mm == pytest.approx(expected, abs=0.005)
    assert result.required_clearance_mm == 0.1016
    assert result.passes_rules is passes
    assert result.location == (147.086, 97.997)


@pytest.mark.parametrize("reverse", [False, True])
def test_oval_true_corner_clearance(tmp_path, reverse):
    path = board(tmp_path, pad(shape="oval") + segment((1.95, 0.8), (3, 0.8)))
    result = measure_clearance(str(path), *(("B", "A") if reverse else ("A", "B")), layer="F.Cu")
    assert result.min_clearance_mm == pytest.approx(
        (0.45**2 + 0.8**2) ** 0.5 - 0.5 - 0.1, abs=0.001
    )


def test_missing_polygon_fails_closed(tmp_path, monkeypatch):
    from kicad_tools.validate.rules import clearance

    path = board(tmp_path, pad() + segment((-3, 1), (3, 1)))
    monkeypatch.setattr(clearance, "_pad_polygon", lambda *args: None)
    with pytest.raises(ValueError, match="Copper polygon unavailable for U1-1"):
        measure_clearance(str(path), "A", "B")


def test_same_net_exempt_but_net_zero_included(tmp_path):
    path = board(tmp_path, pad() + pad("U2", 0, 2, net=1))
    with pytest.raises(ValueError, match="No clearance measurements possible"):
        measure_clearance(str(path), "U1", "U2")
    path = board(tmp_path, pad(net=0) + pad("U2", 0, 2, net=0))
    assert measure_clearance(str(path), "U1", "U2").min_clearance_mm == 1


def test_layers_and_neighbor_minimum(tmp_path):
    path = board(
        tmp_path,
        pad()
        + pad("U2", 0, 3, net=2)
        + pad("U3", 0, 2, net=3)
        + pad("U4", 0, 0, net=2, layer="B.Cu"),
    )
    result = measure_clearance(str(path), "U1")
    assert result.item2 == "U3"
    assert result.min_clearance_mm == 1
    assert result.layer == "F.Cu"
    assert result.location == (2, 1)
    assert len(result.clearances) == 2
    assert measure_clearance(str(path), "U1", layer="F.Cu") == result
    with pytest.raises(ValueError, match="No copper elements found"):
        measure_clearance(str(path), "U1", layer="B.Cu")


def test_unrounded_minimum_compared_to_rule_floor(tmp_path, monkeypatch):
    from kicad_tools.mcp.tools import analysis

    path = board(tmp_path, pad() + segment((-3, 0.70159), (3, 0.70159)))
    monkeypatch.setattr(analysis, "_get_design_rules_clearance", lambda pcb: 0.1016)
    result = measure_clearance(str(path), "A", "B")
    assert result.min_clearance_mm == 0.1016
    assert result.passes_rules is False
    assert result.required_clearance_mm == 0.1016


@pytest.mark.parametrize("angle", [30, -30, 45])
@pytest.mark.parametrize("other", ["track", "pad", "via"])
@pytest.mark.parametrize("reverse", [False, True])
def test_noncardinal_physical_overlap_and_mirrored_clearance(tmp_path, angle, other, reverse):
    import math

    def world(angle, x, y, center):
        a = math.radians(-angle)
        return center[0] + x * math.cos(a) - y * math.sin(a), center[1] + x * math.sin(
            a
        ) + y * math.cos(a)

    fp = (10, 10)
    center = world(37, 2, 1, fp)
    first = pad(x=10, y=10, shape="roundrect", angle=angle, fp_angle=37, local=(2, 1), ratio=0.1)
    for physical in (True, False):
        target_angle = angle if physical else -angle
        pos = world(target_angle, 1.8, 0.2, center)
        if other == "track":
            second = segment(world(target_angle, 1.6, 0.2, center), pos, width=0.1)
        elif other == "pad":
            second = pad("U2", *pos, shape="circle", width=0.1, height=0.1, net=2)
        else:
            second = via(*pos, size=0.1)
        path = board(tmp_path, first + second)
        result = measure_clearance(
            str(path), *(("B", "A") if reverse else ("A", "B")), layer="F.Cu"
        )
        assert (result.min_clearance_mm < 0) is physical
        if not physical:
            assert result.min_clearance_mm > 0.2
