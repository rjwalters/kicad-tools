"""Tests for kicad_tools.optim.fom_features.

Issue #3186.
"""

from __future__ import annotations

import pytest

from kicad_tools.optim.fom_features import (
    BoardFeatures,
    FootprintFeature,
    PadFeature,
    _is_fixed_footprint,
    _mst_cost_manhattan,
    _pad_absolute_position,
    euclidean,
    extract_features,
    manhattan,
    routed_net_length,
    segment_length,
    steiner_lower_bound,
)
from kicad_tools.schema.pcb import PCB, Footprint, Pad, Segment

# --------------------------------------------------------------------
# Geometric primitives
# --------------------------------------------------------------------


def test_manhattan_basic():
    assert manhattan((0, 0), (3, 4)) == 7
    assert manhattan((1.5, 2.5), (1.5, 2.5)) == 0.0
    assert manhattan((-1, -2), (1, 2)) == 6


def test_euclidean_basic():
    assert euclidean((0, 0), (3, 4)) == 5.0
    assert euclidean((1, 1), (1, 1)) == 0.0


def test_segment_length():
    seg = Segment(start=(0.0, 0.0), end=(3.0, 4.0), width=0.2, layer="F.Cu", net_number=1)
    assert segment_length(seg) == pytest.approx(5.0)


def test_mst_cost_manhattan_empty_or_single():
    assert _mst_cost_manhattan([]) == 0.0
    assert _mst_cost_manhattan([(0, 0)]) == 0.0


def test_mst_cost_manhattan_two_points():
    assert _mst_cost_manhattan([(0, 0), (3, 4)]) == 7.0


def test_mst_cost_manhattan_three_collinear_points():
    # Three points on a line: MST cost = sum of consecutive distances.
    assert _mst_cost_manhattan([(0, 0), (1, 0), (2, 0)]) == 2.0


def test_mst_cost_manhattan_square():
    # Square corners: MST is a tree with 3 unit edges = 3.
    pts = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert _mst_cost_manhattan(pts) == 3.0


# --------------------------------------------------------------------
# Convex hull fallback
# --------------------------------------------------------------------


def test_convex_hull_area_degenerate():
    from kicad_tools.optim.fom_geometry import _andrew_monotone_chain_area

    assert _andrew_monotone_chain_area([]) == 0.0
    assert _andrew_monotone_chain_area([(0, 0)]) == 0.0
    assert _andrew_monotone_chain_area([(0, 0), (1, 0)]) == 0.0


def test_convex_hull_area_unit_square():
    from kicad_tools.optim.fom_geometry import _andrew_monotone_chain_area

    pts = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert _andrew_monotone_chain_area(pts) == pytest.approx(1.0)


def test_convex_hull_area_collinear_returns_zero():
    from kicad_tools.optim.fom_geometry import _andrew_monotone_chain_area

    pts = [(0, 0), (1, 0), (2, 0), (3, 0)]
    assert _andrew_monotone_chain_area(pts) == 0.0


# --------------------------------------------------------------------
# Footprint helpers
# --------------------------------------------------------------------


def _make_fp(ref="R1", locked=False, rotation=0.0, position=(0.0, 0.0)):
    return Footprint(
        name="Resistor",
        layer="F.Cu",
        position=position,
        rotation=rotation,
        reference=ref,
        value="10k",
        locked=locked,
    )


def test_is_fixed_footprint_locked():
    fp = _make_fp(ref="R1", locked=True)
    assert _is_fixed_footprint(fp) is True


def test_is_fixed_footprint_connector():
    fp = _make_fp(ref="J1")
    assert _is_fixed_footprint(fp) is True


def test_is_fixed_footprint_mounting_hole():
    fp = _make_fp(ref="MK1")
    assert _is_fixed_footprint(fp) is True
    fp = _make_fp(ref="MH3")
    assert _is_fixed_footprint(fp) is True


def test_is_fixed_footprint_test_point():
    fp = _make_fp(ref="TP1")
    assert _is_fixed_footprint(fp) is True


def test_is_fixed_footprint_regular_part():
    fp = _make_fp(ref="R1")
    assert _is_fixed_footprint(fp) is False
    fp = _make_fp(ref="C42")
    assert _is_fixed_footprint(fp) is False
    fp = _make_fp(ref="U7")
    assert _is_fixed_footprint(fp) is False


def test_is_fixed_footprint_prefix_collision():
    # JEDEC etc. shouldn't match J prefix; we require digit after.
    fp = _make_fp(ref="JX")
    assert _is_fixed_footprint(fp) is False


def test_pad_absolute_position_no_rotation():
    fp = _make_fp(position=(10.0, 20.0), rotation=0.0)
    pad = Pad(number="1", type="smd", shape="rect", position=(1.0, 2.0), size=(0.5, 0.5), layers=[])
    x, y = _pad_absolute_position(fp, pad)
    assert (x, y) == (11.0, 22.0)


def test_pad_absolute_position_with_rotation_90():
    fp = _make_fp(position=(0.0, 0.0), rotation=90.0)
    pad = Pad(number="1", type="smd", shape="rect", position=(1.0, 0.0), size=(0.5, 0.5), layers=[])
    x, y = _pad_absolute_position(fp, pad)
    # KiCad negated-angle convention (#3739): a +90° footprint maps the pad
    # offset (1, 0) -> (0, -1), not (0, +1).
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(-1.0, abs=1e-9)


# --------------------------------------------------------------------
# Steiner lower bound
# --------------------------------------------------------------------


def _pad_feature(x, y, net=1, name="N1", ref="R1", pin="1"):
    return PadFeature(
        x=x,
        y=y,
        net_number=net,
        net_name=name,
        reference=ref,
        pad_number=pin,
        layers=("F.Cu",),
        pad_type="smd",
    )


def test_steiner_lower_bound_zero_pads():
    assert steiner_lower_bound([]) == 0.0


def test_steiner_lower_bound_one_pad():
    assert steiner_lower_bound([_pad_feature(0, 0)]) == 0.0


def test_steiner_lower_bound_two_pads_is_manhattan():
    bound = steiner_lower_bound([_pad_feature(0, 0), _pad_feature(3, 4)])
    assert bound == pytest.approx(7.0)


def test_steiner_lower_bound_three_pads_is_at_most_mst():
    # Steiner tree always shorter than or equal to MST.
    pads = [_pad_feature(0, 0), _pad_feature(2, 0), _pad_feature(1, 2)]
    bound = steiner_lower_bound(pads)
    mst_cost = _mst_cost_manhattan([(p.x, p.y) for p in pads])
    assert bound <= mst_cost + 1e-9


# --------------------------------------------------------------------
# Synthesized PCB integration
# --------------------------------------------------------------------


def _build_minimal_pcb_sexp(footprints_data=None):
    """Build a minimal valid PCB S-expression for testing.

    footprints_data: list of dicts with optional 'ref', 'pos', 'pads' keys.
    """
    from kicad_tools.sexp.parser import SExp

    pcb_sexp = SExp.list("kicad_pcb")
    pcb_sexp.append(SExp.list("version", 20240108))
    pcb_sexp.append(SExp.list("generator", '"test"'))
    pcb_sexp.append(SExp.list("paper", '"A4"'))
    pcb_sexp.append(SExp.list("layers"))
    pcb_sexp.append(SExp.list("setup"))
    pcb_sexp.append(SExp.list("net", 0, '""'))
    return pcb_sexp


def _empty_pcb() -> PCB:
    """Create the most minimal PCB possible (no real footprints)."""
    pcb = PCB.create(width=100, height=100)
    return pcb


def test_extract_features_empty_pcb():
    pcb = _empty_pcb()
    features = extract_features(pcb)
    assert isinstance(features, BoardFeatures)
    assert features.footprints == []
    assert features.nets_to_pads == {}


def test_extract_features_records_segments_and_vias():
    pcb = _empty_pcb()
    # Manually add some segments and vias via the private list (PCB's
    # public setters guard against direct assignment that wouldn't
    # persist, but our test only consumes the in-memory snapshot).
    seg = Segment(start=(0.0, 0.0), end=(5.0, 0.0), width=0.2, layer="F.Cu", net_number=1)
    seg2 = Segment(start=(5.0, 0.0), end=(5.0, 5.0), width=0.2, layer="F.Cu", net_number=1)
    pcb._segments = [seg, seg2]
    from kicad_tools.schema.pcb import Via

    via = Via(position=(2.0, 2.0), size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_number=1)
    pcb._vias = [via]

    features = extract_features(pcb)
    assert features.segments_by_net.get(1) == [seg, seg2]
    assert features.vias_by_net.get(1) == [via]


def test_extract_features_skips_zero_net():
    pcb = _empty_pcb()
    seg_zero = Segment(start=(0.0, 0.0), end=(1.0, 1.0), width=0.2, layer="F.Cu", net_number=0)
    pcb._segments = [seg_zero]
    features = extract_features(pcb)
    assert 0 not in features.segments_by_net


def test_routed_net_length_sums_segments():
    pcb = _empty_pcb()
    pcb._segments = [
        Segment(start=(0.0, 0.0), end=(3.0, 4.0), width=0.2, layer="F.Cu", net_number=1),
        Segment(start=(0.0, 0.0), end=(1.0, 0.0), width=0.2, layer="F.Cu", net_number=2),
    ]
    features = extract_features(pcb)
    assert routed_net_length(features, 1) == pytest.approx(5.0)
    assert routed_net_length(features, 2) == pytest.approx(1.0)
    assert routed_net_length(features, 999) == 0.0


def test_board_features_total_pad_count():
    f = BoardFeatures()
    fp = FootprintFeature(
        reference="R1",
        value="10k",
        name="R",
        x=0.0,
        y=0.0,
        rotation=0.0,
        layer="F.Cu",
        locked=False,
        is_fixed=False,
        pad_features=[_pad_feature(0, 0), _pad_feature(1, 1)],
    )
    f.footprints.append(fp)
    assert f.total_pad_count == 2


def test_board_features_fixed_footprints_filter():
    f = BoardFeatures()
    f.footprints.append(FootprintFeature("R1", "10k", "R", 0, 0, 0, "F.Cu", False, False))
    f.footprints.append(FootprintFeature("J1", "USB", "J", 0, 0, 0, "F.Cu", False, True))
    fixed = f.fixed_footprints
    assert len(fixed) == 1
    assert fixed[0].reference == "J1"


def test_footprint_feature_bbox_no_pads():
    fp = FootprintFeature("R1", "10k", "R", 5.0, 7.0, 0, "F.Cu", False, False)
    # No pads -> bbox is the point itself.
    assert fp.bbox == (5.0, 7.0, 5.0, 7.0)


def test_footprint_feature_bbox_with_pads():
    fp = FootprintFeature(
        "R1",
        "10k",
        "R",
        5.0,
        7.0,
        0,
        "F.Cu",
        False,
        False,
        pad_features=[_pad_feature(0, 0), _pad_feature(10, 20)],
    )
    assert fp.bbox == (0, 0, 10, 20)


# ----------------------------------------------------------------------
# Phase 0 numeric feature vector  (issue #3187)
# ----------------------------------------------------------------------


def _bf_with_footprints(footprints, board_bbox=(0.0, 0.0, 20.0, 20.0)):
    bf = BoardFeatures()
    bf.footprints = list(footprints)
    bf.board_bbox = board_bbox
    # Re-populate nets_to_pads from the footprints' pads.
    for fp in footprints:
        for pad in fp.pad_features:
            if pad.net_number > 0:
                bf.nets_to_pads.setdefault(pad.net_number, []).append(pad)
                bf.net_names.setdefault(pad.net_number, pad.net_name)
    return bf


def _ff(ref, x, y, pads=(), layer="F.Cu", fixed=False):
    return FootprintFeature(
        reference=ref,
        value="",
        name="",
        x=x,
        y=y,
        rotation=0.0,
        layer=layer,
        locked=False,
        is_fixed=fixed,
        pad_features=list(pads),
    )


def test_phase0_feature_count_matches_names():
    from kicad_tools.optim.fom_features import (
        PHASE0_FEATURE_NAMES,
        extract_phase0_features,
    )

    # Build a trivial board with one footprint + one pad.
    bf = _bf_with_footprints([_ff("R1", 5, 5, [_pad_feature(5, 5, net=1)])])
    feats = extract_phase0_features(bf)
    assert set(feats) == set(PHASE0_FEATURE_NAMES)
    assert len(feats) == 20


def test_phase0_features_finite_on_empty_board():
    from kicad_tools.optim.fom_features import (
        PHASE0_FEATURE_NAMES,
        extract_phase0_features,
    )

    feats = extract_phase0_features(BoardFeatures())
    assert set(feats) == set(PHASE0_FEATURE_NAMES)
    import math as _m

    for n, v in feats.items():
        assert _m.isfinite(v), f"feature {n} = {v} is not finite"


def test_phase0_component_density_per_quadrant():
    from kicad_tools.optim.fom_features import _component_density_per_quadrant

    # Board 0..10 x 0..10.  Put one comp in each quadrant.
    fps = [
        _ff("R1", 7.5, 7.5, [_pad_feature(7.5, 7.5)]),  # Q1
        _ff("R2", 2.5, 7.5, [_pad_feature(2.5, 7.5)]),  # Q2
        _ff("R3", 2.5, 2.5, [_pad_feature(2.5, 2.5)]),  # Q3
        _ff("R4", 7.5, 2.5, [_pad_feature(7.5, 2.5)]),  # Q4
    ]
    bf = _bf_with_footprints(fps, board_bbox=(0.0, 0.0, 10.0, 10.0))
    q1, q2, q3, q4 = _component_density_per_quadrant(bf)
    # Each quadrant is 5x5 = 25 mm^2 with 1 component each.
    assert q1 == pytest.approx(1 / 25)
    assert q2 == pytest.approx(1 / 25)
    assert q3 == pytest.approx(1 / 25)
    assert q4 == pytest.approx(1 / 25)


def test_phase0_dense_package_count_threshold():
    from kicad_tools.optim.fom_features import _dense_package_count

    # 8 pads -> not dense; 16 pads -> dense.
    fps = [
        _ff("U1", 5, 5, [_pad_feature(5, 5) for _ in range(8)]),
        _ff("U2", 5, 5, [_pad_feature(5, 5) for _ in range(20)]),
    ]
    bf = _bf_with_footprints(fps)
    assert _dense_package_count(bf) == 1


def test_phase0_isolated_pad_count_picks_up_unconnected():
    from kicad_tools.optim.fom_features import _isolated_pad_count

    # 3 pads on net 0 (isolated) + 2 pads on net 1 (connected) = 3 isolated.
    fps = [
        _ff(
            "R1",
            0,
            0,
            [
                _pad_feature(0, 0, net=0),
                _pad_feature(1, 0, net=0),
                _pad_feature(2, 0, net=0),
                _pad_feature(3, 0, net=1),
                _pad_feature(4, 0, net=1),
            ],
        )
    ]
    bf = _bf_with_footprints(fps)
    assert _isolated_pad_count(bf) == 3


def test_phase0_pour_pad_coverage_picks_power_nets():
    from kicad_tools.optim.fom_features import _pour_pad_coverage

    fps = [
        _ff(
            "U1",
            0,
            0,
            [
                _pad_feature(0, 0, net=1, name="GND"),
                _pad_feature(1, 0, net=2, name="VCC"),
                _pad_feature(2, 0, net=3, name="DATA"),
                _pad_feature(3, 0, net=4, name="CLK"),
            ],
        )
    ]
    bf = _bf_with_footprints(fps)
    # 2 of 4 pads are power-looking.
    assert _pour_pad_coverage(bf) == pytest.approx(0.5)


def test_phase0_bbox_aspect_ratio_square_is_one():
    from kicad_tools.optim.fom_features import _bbox_aspect_ratio

    bf = BoardFeatures(board_bbox=(0, 0, 10, 10))
    assert _bbox_aspect_ratio(bf) == 1.0


def test_phase0_bbox_aspect_ratio_2_to_1():
    from kicad_tools.optim.fom_features import _bbox_aspect_ratio

    bf = BoardFeatures(board_bbox=(0, 0, 20, 10))
    assert _bbox_aspect_ratio(bf) == 2.0


def test_phase0_convex_hull_area_unit_square():
    from kicad_tools.optim.fom_features import _convex_hull_area

    bf = _bf_with_footprints(
        [
            _ff("F1", 0, 0),
            _ff("F2", 1, 0),
            _ff("F3", 1, 1),
            _ff("F4", 0, 1),
        ]
    )
    assert _convex_hull_area(bf) == pytest.approx(1.0)
