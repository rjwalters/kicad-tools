"""Board07 saved-fill topology and native name-only copper regressions."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB


@pytest.fixture
def generate_design_mod(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "boards" / "07-matchgroup-test"
    for name in ("generate_pcb", "generate_schematic", "generate_design"):
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    # This constructed copper lies within Board06-style sheet coordinates.
    monkeypatch.setattr(module.generate_pcb, "BOARD_ORIGIN_Y", 40)
    return module


def _two_fill_board(
    path,
    *,
    version=20260206,
    gap=0.0,
    point_touch=False,
    separate_zones=False,
    thickness_token="",
    bridge=False,
    via=False,
):
    """Two saved fill fragments, each anchored by one physical pad."""
    right_x = 112 + gap
    right_y = 62 if point_touch else 60
    rects = [(110, 60, 112, 62), (right_x, right_y, 114 + gap, right_y + 2)]
    lines = [
        f'(kicad_pcb (version {version}) (generator "test")',
        "  (general (thickness 1.6))",
        '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))',
        '  (net 0 "") (net 1 "GND") (net 2 "foreign")',
    ]
    for ref, x, y in (("R1", 111, 61), ("R2", right_x + 1, right_y + 1)):
        lines.append(
            f'  (footprint "test:pad" (layer "B.Cu") (at {x} {y}) '
            f'(property "Reference" "{ref}" (at 0 0) (layer "B.SilkS")) '
            '(pad "1" smd circle (at 0 0) (size 0.3 0.3) '
            '(layers "B.Cu") (net 1 "GND")))'
        )
    groups = [[rect] for rect in rects] if separate_zones else [rects]
    for group in groups:
        fills = []
        for x1, y1, x2, y2 in group:
            points = f"(pts (xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2}))"
            fills.append(f'(filled_polygon (layer "B.Cu") {points})')
        lines.append(
            '  (zone (net 1) (net_name "GND") (layer "B.Cu") '
            f"(min_thickness 0.25) {thickness_token} (fill yes) "
            "(polygon (pts (xy 110 60) (xy 115 60) (xy 115 65) (xy 110 65))) "
            + " ".join(fills)
            + ")"
        )
    if bridge:
        lines.append(
            '  (segment (start 111.5 61) (end 112.5 61) (width 0.2) (layer "B.Cu") (net 1))'
        )
    if via:
        lines.append(
            '  (via (at 113.5 61.5) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
        )
    # Real foreign copper near, but not crossing, the legal interior bridge.
    lines.append('  (segment (start 110 59.7) (end 114 59.7) (width 0.2) (layer "B.Cu") (net 2))')
    path.write_text("\n".join(lines) + "\n)\n")


def _physical_pad_groups(path):
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.connectivity import ConnectivityValidator

    return ConnectivityValidator(PCB.load(path)).extract_pad_partition()


@pytest.mark.parametrize(
    "options,connected",
    [
        ({}, False),
        ({"point_touch": True}, False),
        ({"gap": -0.2}, False),  # Canonical same-zone solid rule includes area overlap.
        ({"version": 20240108, "gap": 0.2}, True),
        ({"version": 20240108, "gap": 0.3}, False),
        ({"version": 20240108, "thickness_token": "(filled_areas_thickness no)"}, False),
        ({"thickness_token": "(filled_areas_thickness yes)"}, False),
        ({"separate_zones": True}, True),
        ({"separate_zones": True, "gap": -0.2}, True),
        ({"bridge": True}, True),
    ],
)
def test_pour_audit_matches_canonical_saved_fill_contacts(
    tmp_path, generate_design_mod, options, connected
):
    board = tmp_path / "contact.kicad_pcb"
    _two_fill_board(board, **options)
    canonical = _physical_pad_groups(board)
    assert (len(canonical) == 1) is connected
    audit = generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]
    assert audit["connected"] is connected
    assert sorted(sorted(name for name, _ in group) for group in audit["pad_groups"]) == sorted(
        sorted(group) for group in canonical
    )


def _name_only(path):
    text = path.read_text().replace('  (net 0 "") (net 1 "GND") (net 2 "foreign")', "")
    for old, new in [
        ('(net 1 "GND")', '(net "GND")'),
        ("(net 1)", '(net "GND")'),
        ("(net 2)", '(net "foreign")'),
    ]:
        text = text.replace(old, new)
    path.write_text(text)


@pytest.mark.parametrize("named", [False, True])
def test_repair_adds_real_bridge_preserving_existing_copper(tmp_path, generate_design_mod, named):
    from shapely.geometry import LineString, Point, box

    board = tmp_path / "repair.kicad_pcb"
    _two_fill_board(board, via=True)
    if named:
        _name_only(board)
    before = board.read_text()
    original = PCB.load(board)
    assert len(_physical_pad_groups(board)) == 2
    assert not generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert generate_design_mod._repair_pour_connectivity(board, ["GND"]) == (0, 1)
    after = board.read_text()
    repaired = PCB.load(board)
    assert len(repaired.vias) == len(original.vias)
    assert len(repaired.segments) == len(original.segments) + 1
    bridge = repaired.segments[-1]
    ox, oy = repaired.board_origin
    a, b = [(x + ox, y + oy) for x, y in (bridge.start, bridge.end)]
    assert math.dist(a, b) > 0.1
    assert bridge.net_name == "GND" and bridge.width == 0.2 and bridge.layer == "B.Cu"
    copper = LineString([a, b]).buffer(bridge.width / 2)
    for solid in [box(110, 60, 112, 62), box(112, 60, 114, 62)]:
        assert copper.intersection(solid).area > 0
        assert any(solid.contains(Point(p)) for p in (a, b))
    assert copper.distance(LineString([(110, 59.7), (114, 59.7)]).buffer(0.1)) >= 0.15
    assert len(_physical_pad_groups(board)) == 1
    assert generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert bridge.net_name_only is named
    assert repaired.nets == original.nets
    for kind in ("footprint", "via", "zone", "segment"):
        assert all(
            block in after for block in generate_design_mod._find_sexp_blocks(before, f"({kind}")
        )


def test_named_copper_bridge_is_counted_without_repair(tmp_path, generate_design_mod):
    board = tmp_path / "named.kicad_pcb"
    _two_fill_board(board, bridge=True, via=True)
    _name_only(board)
    before = board.read_bytes()
    assert generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert generate_design_mod._repair_pour_connectivity(board, ["GND"]) == (0, 0)
    assert board.read_bytes() == before


def test_single_false_fill_edge_changes_seven_plus_one_to_eight(
    tmp_path, generate_design_mod, monkeypatch
):
    board = tmp_path / "single-edge.kicad_pcb"
    _two_fill_board(board)
    text = board.read_text().replace('"GND"', '"+1V2"')
    # Synthetic seven-plus-one counterpart of the retained +1V2 partition.
    text = text.replace('"R1"', '"U1"').replace('"R2"', '"U4"')
    extras = []
    for i in range(6):
        extras.append(
            f'(footprint "test:pad" (layer "B.Cu") (at 111 61) '
            f'(property "Reference" "P{i}" (at 0 0) (layer "B.SilkS")) '
            '(pad "1" smd circle (at 0 0) (size 0.3 0.3) (layers "B.Cu") (net 1 "+1V2")))'
        )
    text = text.rstrip()[:-1] + "\n" + "\n".join(extras) + ")"
    board.write_text(text)
    audit = generate_design_mod._audit_pour_nets(board, ["+1V2"])["+1V2"]
    assert sorted(map(len, audit["pad_groups"])) == [1, 7]
    original_pairs = generate_design_mod._pour_connected_pairs

    def false_edge(elements, fills):
        yield from original_pairs(elements, fills)
        yield 0, 1

    monkeypatch.setattr(generate_design_mod, "_pour_connected_pairs", false_edge)
    faulty = generate_design_mod._audit_pour_nets(board, ["+1V2"])["+1V2"]
    assert faulty["connected"] and list(map(len, faulty["pad_groups"])) == [8]


@pytest.mark.parametrize("named", [False, True])
def test_emitted_via_and_stub_keep_net_dialect(tmp_path, generate_design_mod, named):
    board = tmp_path / "via.kicad_pcb"
    _two_fill_board(board, bridge=True)
    # R1 is on F.Cu; the joined fill and R2 are on B.Cu.
    text = board.read_text().replace(
        '(layers "B.Cu") (net 1 "GND")', '(layers "F.Cu") (net 1 "GND")', 1
    )
    board.write_text(text)
    if named:
        _name_only(board)
    before = PCB.load(board)
    assert not generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    vias, _ = generate_design_mod._repair_pour_connectivity(board, ["GND"])
    assert vias > 0
    after = PCB.load(board)
    assert after.nets == before.nets
    assert all(v.net_name == "GND" and v.net_name_only is named for v in after.vias)
    assert all(
        s.net_name == "GND" and s.net_name_only is named
        for s in after.segments[len(before.segments) :]
    )
    assert generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert len(_physical_pad_groups(board)) == 1


@pytest.mark.parametrize("span,connected", [('"F.Cu" "In1.Cu"', False), ('"F.Cu" "B.Cu"', True)])
def test_named_via_connectivity_respects_physical_span(
    tmp_path, generate_design_mod, span, connected
):
    board = tmp_path / "span.kicad_pcb"
    _two_fill_board(board, bridge=True)
    text = board.read_text().replace(
        '(layers "B.Cu") (net 1 "GND")', '(layers "F.Cu") (net 1 "GND")', 1
    )
    text = text.rstrip()[:-1] + f"(via (at 111 61) (size 0.6) (drill 0.3) (layers {span}) (net 1)))"
    board.write_text(text)
    _name_only(board)
    assert generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"] is connected
    assert (len(_physical_pad_groups(board)) == 1) is connected


@pytest.mark.parametrize("named", [False, True])
def test_foreign_copper_blocks_repair_in_both_dialects(tmp_path, generate_design_mod, named):
    board = tmp_path / "blocked.kicad_pcb"
    _two_fill_board(board, via=True)
    text = board.read_text().rstrip()[:-1]
    for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
        text += f'\n(segment (start 112 40) (end 112 140) (width 0.2) (layer "{layer}") (net 2))'
    board.write_text(text + "\n)")
    if named:
        _name_only(board)
    before = board.read_bytes()
    assert generate_design_mod._repair_pour_connectivity(board, ["GND"]) == (0, 0)
    assert board.read_bytes() == before
    assert not generate_design_mod._audit_pour_nets(board, ["GND"])["GND"]["connected"]
