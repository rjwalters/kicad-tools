"""Footprint arc schema and reflection must preserve true on-arc points."""

import math

import pytest

from kicad_tools.recovery.applicator import StrategyApplicator
from kicad_tools.schema.pcb import PCB, Footprint, FootprintGraphic, GraphicArc
from kicad_tools.sexp import parse_string


@pytest.mark.parametrize("tag", ["footprint", "module"])
@pytest.mark.parametrize("angle", [-270, -90, 90, 270])
def test_legacy_arc_points_local_and_signed(tag, angle):
    fp = Footprint.from_sexp(
        parse_string(
            f'({tag} "X" (layer "F.Cu") (at 50 60 30) '
            f'(fp_arc (start 3 4) (end 5 4) (angle {angle}) (layer "F.SilkS") (width .1)))'
        )
    )
    arc = fp.graphics[0]
    assert arc.start == (5, 4)
    assert arc.mid == pytest.approx(
        (3 + 2 * math.cos(math.radians(angle / 2)), 4 + 2 * math.sin(math.radians(angle / 2)))
    )
    assert arc.end == pytest.approx(
        (3 + 2 * math.cos(math.radians(angle)), 4 + 2 * math.sin(math.radians(angle)))
    )


def test_modern_mid_preserved_and_wins_over_legacy_angle():
    arc = FootprintGraphic.from_sexp(
        parse_string(
            "(fp_arc (start 1 2) (mid 3 4) (end 5 6) (angle 90) "
            '(stroke (width .2) (type default)) (layer "F.SilkS") (uuid "arc-id"))'
        ),
        "arc",
    )
    assert (arc.start, arc.mid, arc.end) == ((1, 2), (3, 4), (5, 6))
    assert arc.stroke_width == 0.2 and arc.uuid == "arc-id"


def test_old_positional_constructor_and_nonarc_defaults():
    graphic = FootprintGraphic("line", "F.SilkS", 0.1, (1, 2), (3, 4), None, None, [], "old-id")
    assert graphic.uuid == "old-id" and graphic.mid is None
    parsed = FootprintGraphic.from_sexp(
        parse_string('(fp_line (start 1 2) (end 3 4) (layer "F.SilkS"))'), "line"
    )
    assert parsed.mid is None and parsed.start == (1, 2) and parsed.end == (3, 4)


@pytest.mark.parametrize(
    "encoding",
    [
        "(start 3 4) (end 5 4) (angle -90)",
        "(start 5 4) (mid 3 6) (end 1 4)",
    ],
)
def test_mirror_and_save_reload_keep_all_arc_points_consistent(tmp_path, encoding):
    path = tmp_path / "arc.kicad_pcb"
    path.write_text(
        "(kicad_pcb (version 20240108) "
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)) '
        '(footprint "X" (layer "F.Cu") (at 50 60 30) '
        '(property "Reference" "J1") '
        f'(fp_arc {encoding} (layer "F.SilkS") (stroke (width .1)))))'
    )
    original = path.read_bytes()
    pcb = PCB.load(str(path))
    fp = pcb.footprints[0]
    arc = fp.graphics[0]
    before = [arc.start, arc.mid, arc.end]
    StrategyApplicator()._mirror_footprint_cosmetics(fp)
    mirrored = [(x, -y) for x, y in before]
    assert [arc.start, arc.mid, arc.end] == mirrored
    output = tmp_path / "mirrored.kicad_pcb"
    pcb.save(str(output))
    reloaded = PCB.load(str(output)).footprints[0].graphics[0]
    for actual, expected in zip(
        [reloaded.start, reloaded.mid, reloaded.end], mirrored, strict=True
    ):
        assert actual == pytest.approx(expected)
    assert reloaded.layer == "B.SilkS"
    assert path.read_bytes() == original
    StrategyApplicator()._mirror_footprint_cosmetics(fp)
    for actual, expected in zip([arc.start, arc.mid, arc.end], before, strict=True):
        assert actual == pytest.approx(expected)


def test_graphic_arc_missing_tokens_keeps_existing_defaults():
    arc = GraphicArc.from_sexp(parse_string("(gr_arc (start 1 2) (end 3 4))"))
    assert arc.mid == (0, 0)
