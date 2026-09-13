"""Length matching must avoid actual pad copper, not just other routes."""

from dataclasses import replace

import pytest
from shapely.geometry import LineString
from shapely.ops import unary_union

from kicad_tools.router.diffpair import DifferentialPair, DifferentialPairType, DifferentialSignal
from kicad_tools.router.diffpair_detection import DetectedPair, DetectionSource
from kicad_tools.router.diffpair_length_tuning import tune_diff_pair_skew
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules


def _fixture(*, pad_net=3, pad_layer=Layer.F_CU, block_all=False):
    rules = DesignRules(trace_width=0.2, trace_clearance=0.15)
    grid = RoutingGrid(width=25, height=20, rules=rules)
    p = Route(
        net=1,
        net_name="D_P",
        segments=[
            Segment(x1=5, y1=8, x2=17, y2=8, width=0.2, layer=Layer.F_CU, net=1, net_name="D_P")
        ],
    )
    n = Route(
        net=2,
        net_name="D_N",
        segments=[
            Segment(x1=5, y1=9, x2=15, y2=9, width=0.2, layer=Layer.F_CU, net=2, net_name="D_N")
        ],
    )
    pair = DetectedPair(
        pair=DifferentialPair(
            name="D",
            positive=DifferentialSignal(
                net_name="D_P", net_id=1, base_name="D", polarity="P", notation="suffix"
            ),
            negative=DifferentialSignal(
                net_name="D_N", net_id=2, base_name="D", polarity="N", notation="suffix"
            ),
            pair_type=DifferentialPairType.CUSTOM,
        ),
        source=DetectionSource.EXPLICIT,
    )
    pad = Pad(
        x=10 if block_all else 5.6,
        y=9.65,
        width=10.4 if block_all else 0.35,
        height=0.5,
        net=pad_net,
        net_name=f"PAD_{pad_net}",
        layer=pad_layer,
        ref="J_FOREIGN",
        pin="1",
        shape="rect",
    )
    grid.add_pad(pad)
    # The untuned routes are legal; only the proposed trombone is obstructed.
    assert all(
        grid.worst_segment_pad_deficit(s, exclude_net=r.net)[0] <= 0
        for r in (p, n)
        for s in r.segments
    )
    grid.mark_route(p)
    grid.mark_route(n)
    return grid, pair, p, n


def _tune(grid, pair, p, n, **kwargs):
    return tune_diff_pair_skew(
        pair,
        {p.net: p, n.net: n},
        tolerance_mm=0.05,
        intra_pair_clearance_mm=0.1,
        grid=grid,
        **kwargs,
    )


def _worst(grid, route, *, exclude_net=None):
    return max(
        grid.worst_segment_pad_deficit(
            s, exclude_net=route.net if exclude_net is None else exclude_net
        )[0]
        for s in route.segments
    )


def test_foreign_pad_blocks_first_bulge_but_interior_host_is_legal():
    grid, pair, p, n = _fixture()
    # The original geometric-only proposal really enters the foreign pad.
    _, unchecked, initial = _tune(None, pair, p, n)
    assert initial.success
    assert _worst(grid, unchecked) > 0.1

    p_out, n_out, result = _tune(grid, pair, p, n)
    assert result.success
    assert result.skew_after_mm <= 0.05
    assert p_out is p
    assert _worst(grid, n_out) <= 0
    # Moving the insertion host must keep the original lead's copper.
    copper = unary_union([LineString([(s.x1, s.y1), (s.x2, s.y2)]) for s in n_out.segments])
    lead = LineString([(5, 9), (7.5, 9)])
    assert lead.difference(copper).length < 1e-9
    assert min(s.x1 for s in n_out.segments if abs(s.y2 - s.y1) > 0.01) >= 7.5


def test_all_insertion_positions_obstructed_roll_back_original_objects():
    grid, pair, p, n = _fixture(block_all=True)
    p_segments, n_segments = p.segments, n.segments
    p_out, n_out, result = _tune(grid, pair, p, n)
    assert not result.success
    assert result.inserts_applied == 0
    assert p_out is p and n_out is n
    assert p_out.segments is p_segments and n_out.segments is n_segments
    assert result.skew_after_mm == pytest.approx(2.0)
    assert _worst(grid, n_out) == 0


def test_same_net_pad_does_not_block_trombone():
    grid, pair, p, n = _fixture(pad_net=2)
    _, n_out, result = _tune(grid, pair, p, n)
    assert result.success and result.skew_after_mm <= 0.05
    assert _worst(grid, n_out) == 0
    # Exemption is net-specific; the same geometry would hit a foreign pad.
    assert _worst(grid, n_out, exclude_net=0) > 0.1


def test_pad_on_other_copper_layer_does_not_block_trombone():
    grid, pair, p, n = _fixture(pad_layer=Layer.B_CU)
    _, n_out, result = _tune(grid, pair, p, n)
    assert result.success and result.skew_after_mm <= 0.05
    assert _worst(grid, n_out) == 0
    assert (
        max(
            grid.worst_segment_pad_deficit(replace(s, layer=Layer.B_CU), exclude_net=n.net)[0]
            for s in n_out.segments
        )
        > 0.1
    )


def test_fixed_host_is_not_split_to_evade_exclusion():
    grid, pair, p, n = _fixture()
    p_out, n_out, result = _tune(grid, pair, p, n, fixed_segment_ids={id(s) for s in n.segments})
    assert not result.success
    assert result.reason == "no_suitable_segment"
    assert p_out is p and n_out is n


def test_interior_retry_preserves_fixed_escape_segment_identity():
    grid, pair, p, n = _fixture()
    host = n.segments[0]
    fixed_escape = replace(host, x1=14)
    n.segments = [replace(host, x2=14), fixed_escape]
    p_out, n_out, result = _tune(grid, pair, p, n, fixed_segment_ids={id(fixed_escape)})
    assert result.success and result.skew_after_mm <= 0.05
    assert p_out is p
    assert any(segment is fixed_escape for segment in n_out.segments)
    assert (fixed_escape.x1, fixed_escape.y1, fixed_escape.x2, fixed_escape.y2) == (14, 9, 15, 9)
    assert _worst(grid, n_out) == 0


def _fragment(route):
    host = route.segments[0]
    route.segments = [
        replace(host, x1=host.x1 + i * 0.1, x2=host.x1 + (i + 1) * 0.1)
        for i in range(round((host.x2 - host.x1) / 0.1))
    ]


def test_fragmented_straight_host_can_be_tuned_without_manual_compaction():
    grid, pair, p, n = _fixture()
    _fragment(n)
    original_segments = n.segments
    p_out, n_out, result = _tune(grid, pair, p, n)
    assert result.success and result.skew_after_mm <= 0.05
    assert p_out is p
    assert n.segments is original_segments
    assert _worst(grid, n_out) == 0
    assert len(n_out.segments) < len(n.segments)


def test_obstructed_fragmented_host_rolls_back_without_publishing_compaction():
    grid, pair, p, n = _fixture(block_all=True)
    _fragment(n)
    original_segments = n.segments
    p_out, n_out, result = _tune(grid, pair, p, n)
    assert not result.success
    assert p_out is p and n_out is n
    assert n_out.segments is original_segments


def test_compaction_retains_fixed_escape_at_end_of_fragmented_host():
    grid, pair, p, n = _fixture()
    _fragment(n)
    fixed = n.segments[-1]
    p_out, n_out, result = _tune(grid, pair, p, n, fixed_segment_ids={id(fixed)})
    assert result.success and result.skew_after_mm <= 0.05
    assert p_out is p
    assert any(segment is fixed for segment in n_out.segments)
    assert _worst(grid, n_out) == 0


def test_compaction_retains_same_net_pad_junction():
    grid, pair, p, n = _fixture()
    _fragment(n)
    pad = Pad(
        x=10,
        y=9,
        width=0.3,
        height=0.3,
        net=n.net,
        net_name=n.net_name,
        layer=Layer.F_CU,
        ref="TP",
        pin="1",
        shape="rect",
    )
    grid.add_pad(pad)
    _, n_out, result = _tune(grid, pair, p, n)
    assert result.success and result.skew_after_mm <= 0.05
    assert any(segment.start == (10, 9) or segment.end == (10, 9) for segment in n_out.segments)
    assert _worst(grid, n_out) == 0
