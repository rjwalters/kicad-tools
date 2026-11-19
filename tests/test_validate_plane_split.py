"""Tests for the ``plane_split`` advisory (Issue #9591).

A foreign track that crosses a zone on its own layer can cut it into several
pad-bearing pieces.  The rule judges the zone *outline* geometrically, so an
unfilled zone is judged like a filled one.

Fixture ``tests/fixtures/drc/plane_split.kicad_pcb``: a ``+3V3`` zone on F.Cu
with pad ``C20.1`` on its left and ``U1.62`` on its right, and a ``SIG`` track
running across the whole zone between them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.plane_split import PlaneSplitRule

_FIXTURE = Path(__file__).parent / "fixtures" / "drc" / "plane_split.kicad_pcb"
_TRACK = re.compile(r"^  \(segment .*\n", re.M)


def _board(tmp_path, text: str) -> PCB:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    return PCB.load(str(path))


def _run(pcb: PCB):
    return PlaneSplitRule().check(pcb, None).violations


@pytest.fixture
def text() -> str:
    return _FIXTURE.read_text()


def test_track_across_zone_splits_it_and_names_pads(tmp_path, text):
    found = _run(_board(tmp_path, text))
    assert [v.rule_id for v in found] == ["plane_split"]
    v = found[0]
    assert v.severity == "warning"
    assert v.layer == "F.Cu"
    assert "zone [+3V3] on F.Cu" in v.message
    assert "[SIG]" in v.message
    assert "piece 1: C20.1" in v.message
    assert "piece 2: U1.62" in v.message
    assert "+3V3" in v.nets and "SIG" in v.nets
    assert 18.0 <= v.location[0] <= 22.0


def test_same_board_without_track_is_clean(tmp_path, text):
    assert _run(_board(tmp_path, _TRACK.sub("", text))) == []


def test_track_that_only_notches_the_zone_is_clean(tmp_path, text):
    # Stops short of the far edge: the plane still connects round the end.
    notch = text.replace("(end 20 19.5)", "(end 20 12)")
    assert _run(_board(tmp_path, notch)) == []


def test_cut_whose_pieces_hold_no_extra_pad_is_clean(tmp_path, text):
    # Both pads on the left of the cut: the right piece carries nothing.
    moved = text.replace("(at 34 10)", "(at 12 10)")
    assert _run(_board(tmp_path, moved)) == []


def test_foreign_vias_alone_are_legitimate_obstacles(tmp_path, text):
    fence = "".join(
        f'  (via (at 20 {y / 2}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))\n'
        for y in range(3, 37)
    )
    board = _TRACK.sub(fence, text)
    assert _run(_board(tmp_path, board)) == []


def test_track_on_another_layer_is_ignored(tmp_path, text):
    other = text.replace('(layer "F.Cu") (net 2)', '(layer "B.Cu") (net 2)')
    assert _run(_board(tmp_path, other)) == []


def test_same_net_stitch_rejoins_pieces(tmp_path, text):
    stitch = '  (segment (start 8 10) (end 32 10) (width 0.3) (layer "F.Cu") (net 1))\n'
    board = text.replace("  (zone ", stitch + "  (zone ", 1)
    assert _run(_board(tmp_path, board)) == []


def test_unfilled_zone_is_still_judged(tmp_path, text):
    pcb = _board(tmp_path, text)
    assert all(not z.filled_polygons for z in pcb.zones)
    assert len(_run(pcb)) == 1


def test_zone_without_pads_is_clean(tmp_path, text):
    no_pads = re.sub(r"  \(footprint .*?\n  \)\n", "", text, flags=re.S)
    assert "(footprint" not in no_pads
    assert _run(_board(tmp_path, no_pads)) == []


def test_two_zones_on_one_layer_are_each_judged(tmp_path, text):
    second = (
        '  (zone (net 2) (net_name "SIG") (layer "F.Cu") (hatch edge 0.5)\n'
        "    (connect_pads (clearance 0.2)) (min_thickness 0.2)\n"
        "    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))\n"
        "    (polygon (pts (xy 2 19) (xy 4 19) (xy 4 19.5) (xy 2 19.5)))\n"
        "  )\n"
    )
    board = text.rstrip().removesuffix(")") + second + ")\n"
    found = _run(_board(tmp_path, board))
    assert len(found) == 1 and "zone [+3V3]" in found[0].message


def test_keepout_rule_area_is_a_legitimate_obstacle(tmp_path, text):
    keepout = (
        '  (zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5)\n'
        "    (keepout (tracks allowed) (vias allowed) (pads allowed) "
        "(copperpour not_allowed) (footprints allowed))\n"
        "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        "    (polygon (pts (xy 19 1) (xy 21 1) (xy 21 19) (xy 19 19)))\n"
        "  )\n"
    )
    board = _TRACK.sub("", text).rstrip().removesuffix(")") + keepout + ")\n"
    assert _run(_board(tmp_path, board)) == []


def test_registered_as_advisory_category(tmp_path, text):
    from kicad_tools.cli.check_categories import CHECK_CATEGORIES
    from kicad_tools.validate.known_warnings import NON_GATING_RULE_PREFIXES

    assert "plane_split" in CHECK_CATEGORIES
    assert "plane_split" in NON_GATING_RULE_PREFIXES
    checker = DRCChecker(_board(tmp_path, text), manufacturer="jlcpcb", layers=2)
    results = checker.check_plane_split()
    assert [v.rule_id for v in results.violations] == ["plane_split"]
