"""Obstacle-aware dogleg-variant selection for 45-degree quantization (#5333).

``quantize_pcb_file`` moves copper: the dogleg it substitutes for an
off-angle chord bulges up to ``min(|dx|, |dy|)`` perpendicular to that chord.
Board07's ``2c9bcb95`` full-recipe artifact shipped a GND pour-repair escape
whose chord cleared a foreign ``TMDS_D2_P`` via by 0.2075 mm and whose
diagonal-first dogleg cleared it by only 0.0822 mm -- under the 0.1016 mm
jlcpcb floor, and reported by KiCad's native refill as blocking.  The
axis-first variant of the same chord clears by 0.2437 mm.

These tests pin the decision procedure that picks it.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.quantize import quantize_pcb_file, segment_angle_census
from kicad_tools.router.quantize_planning import plan_quantization

# The measured Board07 geometry, in the recipe's (100, 100) routing frame:
# the GND chord, and the foreign TMDS_D2_P via its diagonal-first dogleg
# grazed.  Coordinates are the artifact's own, shifted by the recipe's
# +(6.5, 60) mm sheet-centering translation.
GND_CHORD = ((161.88, 155.92), (162.27, 156.85))
FOREIGN_VIA = (162.522, 155.88)


def _pcb_text(
    *,
    segments: list[tuple],
    vias: tuple[tuple[float, float, float, int], ...] = (),
    outline: tuple[float, float, float, float] = (100.0, 100.0, 210.0, 195.0),
    extra_edges: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = (),
) -> str:
    """Minimal but load-bearing 4-layer board.

    ``segments`` entries are ``(x1, y1, x2, y2, width, layer, net, uuid)``;
    ``vias`` entries are ``(x, y, size, net)``.  ``extra_edges`` adds
    ``Edge.Cuts`` lines INSIDE the rectangle -- the way a chamfer, slot or
    internal cutout appears to the outline scan.  They matter because a
    dogleg vertex always lies inside the chord's axis-aligned bounding box,
    so an axis-aligned rectangular edge can never be the binding constraint;
    only a non-axis-aligned edge can be.
    """
    layers = "\n".join(
        f'\t\t({index} "{name}" signal)'
        for index, name in enumerate(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    )
    lines = [
        "(kicad_pcb",
        "\t(version 20241229)",
        '\t(generator "test")',
        "\t(general",
        "\t\t(thickness 1.6)",
        "\t)",
        "\t(layers",
        layers,
        '\t\t(44 "Edge.Cuts" user)',
        "\t)",
        '\t(net 0 "")',
        '\t(net 1 "GND")',
        '\t(net 2 "SIG")',
        "\t(gr_rect",
        f"\t\t(start {outline[0]} {outline[1]})",
        f"\t\t(end {outline[2]} {outline[3]})",
        "\t\t(stroke (width 0.05) (type default))",
        '\t\t(layer "Edge.Cuts")',
        '\t\t(uuid "00000000-0000-4000-8000-0000000000ed")',
        "\t)",
    ]
    for index, ((ex1, ey1), (ex2, ey2)) in enumerate(extra_edges):
        lines += [
            "\t(gr_line",
            f"\t\t(start {ex1} {ey1})",
            f"\t\t(end {ex2} {ey2})",
            "\t\t(stroke (width 0.05) (type default))",
            '\t\t(layer "Edge.Cuts")',
            f'\t\t(uuid "00000000-0000-4000-8000-00000000e{index:03d}")',
            "\t)",
        ]
    for x1, y1, x2, y2, width, layer, net, seg_uuid in segments:
        lines += [
            "\t(segment",
            f"\t\t(start {x1} {y1})",
            f"\t\t(end {x2} {y2})",
            f"\t\t(width {width})",
            f'\t\t(layer "{layer}")',
            f'\t\t(uuid "{seg_uuid}")',
            f"\t\t(net {net})",
            "\t)",
        ]
    for index, (x, y, size, net) in enumerate(vias):
        lines += [
            "\t(via",
            f"\t\t(at {x} {y})",
            f"\t\t(size {size})",
            "\t\t(drill 0.3)",
            '\t\t(layers "F.Cu" "B.Cu")',
            f"\t\t(net {net})",
            f'\t\t(uuid "00000000-0000-4000-8000-{index:012d}")',
            "\t)",
        ]
    lines.append(")")
    return "\n".join(lines) + "\n"


def _board(tmp_path, **kwargs):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_pcb_text(**kwargs))
    return path


CHORD_UUID = "375b9e58-a32b-59e4-8a87-36b414299bc1"


def _chord_segment(width: float = 0.2, layer: str = "F.Cu", net: int = 1):
    (x1, y1), (x2, y2) = GND_CHORD
    return (x1, y1, x2, y2, width, layer, net, CHORD_UUID)


JLC_CLEARANCE = 0.1016
JLC_EDGE = 0.3

# A longer synthetic chord whose two dogleg variants separate cleanly: each
# vertex sits 0.6766 mm off the chord, on opposite sides.  The real Board07
# chord is only 1.0085 mm long, so its variants stay within ~0.2 mm of each
# other -- too tight to place a 0.6 mm via between them, which is why the
# "blocked on both sides" and edge cases need their own geometry.
LONG_CHORD = ((150.0, 150.0), (153.0, 151.4))
DIAGONAL_FIRST_MID = (151.4, 151.4)
AXIS_FIRST_MID = (151.6, 150.0)


def _long_chord(layer: str = "F.Cu", net: int = 1):
    (x1, y1), (x2, y2) = LONG_CHORD
    return (x1, y1, x2, y2, 0.2, layer, net, CHORD_UUID)


def _mid_blocker(layer: str, at: tuple[float, float], net: int = 2):
    """A short foreign trace straddling one dogleg vertex."""
    return (
        at[0],
        at[1] - 0.1,
        at[0],
        at[1] + 0.1,
        0.2,
        layer,
        net,
        "00000000-0000-4000-8000-00000000000b",
    )


class TestVariantSelection:
    def test_default_variant_kept_when_open(self, tmp_path):
        """No obstacle => no entry in either set (default diagonal-first)."""
        board = _board(tmp_path, segments=[_chord_segment()])
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset()
        assert plan.skip_uuids == frozenset()
        assert plan.notes == ()

    def test_axis_first_chosen_when_default_bulge_grazes_foreign_via(self, tmp_path):
        """The measured Board07 defect: 0.0822 mm default vs 0.2437 mm mirrored."""
        board = _board(
            tmp_path,
            segments=[_chord_segment()],
            vias=((FOREIGN_VIA[0], FOREIGN_VIA[1], 0.6, 2),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset({CHORD_UUID})
        assert plan.skip_uuids == frozenset()
        assert any("0.0822" in note for note in plan.notes), plan.notes

    def test_same_net_copper_is_not_an_obstacle(self, tmp_path):
        """A same-net via in the bulge must not mirror the dogleg."""
        board = _board(
            tmp_path,
            segments=[_chord_segment()],
            vias=((FOREIGN_VIA[0], FOREIGN_VIA[1], 0.6, 1),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset()
        assert plan.skip_uuids == frozenset()

    def test_obstacle_on_another_layer_is_not_an_obstacle(self, tmp_path):
        """A SEGMENT is copper on exactly one layer."""
        board = _board(
            tmp_path,
            segments=[_long_chord(), _mid_blocker("In1.Cu", DIAGONAL_FIRST_MID)],
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset()
        assert plan.skip_uuids == frozenset()

    def test_segment_blocks_only_its_own_layer(self, tmp_path):
        """Same geometry as above, moved onto the chord's layer, DOES mirror."""
        board = _board(
            tmp_path,
            segments=[_long_chord(), _mid_blocker("F.Cu", DIAGONAL_FIRST_MID)],
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset({CHORD_UUID})
        assert plan.skip_uuids == frozenset()

    def test_both_variants_blocked_skips_the_segment(self, tmp_path):
        """Copper on both sides of the chord leaves the segment off-angle.

        Both vias sit ON a dogleg vertex and 0.277 mm clear of the chord, so
        the chord's own clearance does not lower the floor: the decision is
        driven by the two variants alone.
        """
        board = _board(
            tmp_path,
            segments=[_long_chord()],
            vias=(
                (DIAGONAL_FIRST_MID[0], DIAGONAL_FIRST_MID[1], 0.6, 2),
                (AXIS_FIRST_MID[0], AXIS_FIRST_MID[1], 0.6, 2),
            ),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.skip_uuids == frozenset({CHORD_UUID})
        assert plan.axis_first_uuids == frozenset()
        assert any("left off-angle" in note for note in plan.notes), plan.notes

    def test_board_edge_vetoes_a_variant(self, tmp_path):
        """Copper-to-edge is enforced with the same no-worse-than-chord rule.

        A dogleg vertex never leaves the chord's axis-aligned bounding box,
        so only a non-axis-aligned edge (chamfer / slot / cutout) can bind.
        This edge runs PARALLEL to the chord, 0.95 mm off on the
        diagonal-first side: the chord and the axis-first variant clear it,
        the diagonal-first vertex comes within 0.1734 mm of it.
        """
        board = _board(
            tmp_path,
            segments=[_long_chord()],
            extra_edges=(((131.474, 142.403), (167.722, 159.319)),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset({CHORD_UUID})
        assert plan.skip_uuids == frozenset()

    def test_parallel_edge_on_the_other_side_leaves_the_default(self, tmp_path):
        """Control for the edge test: mirror the chamfer, keep the default."""
        board = _board(
            tmp_path,
            segments=[_long_chord()],
            extra_edges=(((132.278, 140.681), (168.526, 157.597)),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.axis_first_uuids == frozenset()
        assert plan.skip_uuids == frozenset()

    def test_preexisting_chord_violation_does_not_veto_quantization(self, tmp_path):
        """A board that already violates must still get 45-aligned.

        The quantizer is not a clearance-repair pass.  A variant is
        acceptable when it is no closer than the chord already was, so an
        unrelated pre-existing defect cannot silently stop quantization.
        """
        (x1, y1), _ = GND_CHORD
        board = _board(
            tmp_path,
            segments=[_chord_segment()],
            # Sitting on the chord's start: every variant violates.
            vias=((x1 + 0.05, y1, 0.6, 2),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan.skip_uuids == frozenset()

    def test_aligned_segments_are_never_candidates(self, tmp_path):
        aligned = (150.0, 150.0, 151.0, 151.0, 0.2, "F.Cu", 1, CHORD_UUID)
        board = _board(tmp_path, segments=[aligned])
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        assert plan == type(plan)()


class TestPlanFeedsQuantizer:
    def test_plan_keys_match_and_quantization_clears_the_via(self, tmp_path):
        """End-to-end: plan -> quantize leaves 0 off-angle and clears the via."""
        board = _board(
            tmp_path,
            segments=[_chord_segment()],
            vias=((FOREIGN_VIA[0], FOREIGN_VIA[1], 0.6, 2),),
        )
        plan = plan_quantization(board, clearance_mm=JLC_CLEARANCE, edge_clearance_mm=JLC_EDGE)
        replaced = quantize_pcb_file(
            board,
            axis_first_uuids=plan.axis_first_uuids,
            skip_uuids=plan.skip_uuids,
        )
        assert replaced == [CHORD_UUID]
        total, off_angle = segment_angle_census(board)
        assert off_angle == []
        # Axis-first puts the intermediate vertex on the START x, not the END x.
        assert "(start 161.88 155.92)\n\t\t(end 161.88 156.46)" in board.read_text()

    def test_unplanned_quantization_reproduces_the_defect(self, tmp_path):
        """Control: without the plan the shipped 0.0822 mm dogleg comes back."""
        board = _board(
            tmp_path,
            segments=[_chord_segment()],
            vias=((FOREIGN_VIA[0], FOREIGN_VIA[1], 0.6, 2),),
        )
        quantize_pcb_file(board)
        assert "(end 162.27 156.31)" in board.read_text()


class TestBoardArtifactRegression:
    """Guard the decision on the real Board07 copper, not just a fixture."""

    @pytest.mark.parametrize("axis_first", [False, True])
    def test_measured_gaps_bracket_the_jlcpcb_floor(self, axis_first):
        from shapely.geometry import LineString, Point

        from kicad_tools.router.quantize import dogleg_points

        (x1, y1), (x2, y2) = GND_CHORD
        points = dogleg_points(x1, y1, x2, y2, axis_first=axis_first)
        path = LineString(points).buffer(0.1)
        gap = path.distance(Point(*FOREIGN_VIA).buffer(0.3))
        if axis_first:
            assert gap > JLC_CLEARANCE
            assert gap == pytest.approx(0.2437, abs=5e-4)
        else:
            assert gap < JLC_CLEARANCE
            assert gap == pytest.approx(0.0822, abs=5e-4)
