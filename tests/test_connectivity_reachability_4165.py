"""Unit tests for the per-pad copper-reachability check (Issue #4165).

``kicad_tools.router.connectivity.check_net_pad_connectivity`` is the real
geometric-adjacency oracle used by the orchestrator to detect a multi-pad net
that a single two-terminal corridor left partially connected.  Unlike
``NetStatusAnalyzer`` (Issue #4176) it does NOT union copper on a clearance
tolerance -- two elements are joined only when they geometrically touch within a
tight numeric epsilon, so it errs toward reporting *incomplete* rather than
over-connecting.
"""

from __future__ import annotations

from kicad_tools.router.connectivity import check_net_pad_connectivity
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment, Via


def _seg(x1, y1, x2, y2, layer=Layer.F_CU):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer)


def test_fewer_than_two_pads_is_trivially_complete():
    assert check_net_pad_connectivity([(0.0, 0.0)], segments=[]) == (1, 1)
    assert check_net_pad_connectivity([], segments=[]) == (0, 0)


def test_two_pads_joined_by_one_segment():
    pads = [(0.0, 0.0), (10.0, 0.0)]
    segs = [_seg(0.0, 0.0, 10.0, 0.0)]
    assert check_net_pad_connectivity(pads, segments=segs) == (2, 2)


def test_three_pads_but_corridor_strands_one():
    """The core repro: a two-terminal corridor connects 2 of 3 pads."""
    pads = [(0.0, 0.0), (10.0, 0.0), (5.0, 20.0)]
    # Single corridor between the two most-distant collinear pads; the third
    # pad at (5, 20) is nowhere on it.
    segs = [_seg(0.0, 0.0, 10.0, 0.0)]
    connected, total = check_net_pad_connectivity(pads, segments=segs)
    assert total == 3
    assert connected == 2


def test_intermediate_pad_lying_on_corridor_counts():
    """A third pad that happens to sit on the corridor IS connected."""
    pads = [(0.0, 0.0), (10.0, 0.0), (5.0, 0.0)]
    segs = [_seg(0.0, 0.0, 10.0, 0.0)]
    assert check_net_pad_connectivity(pads, segments=segs) == (3, 3)


def test_chained_segments_connect_all_three():
    pads = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    segs = [
        _seg(0.0, 0.0, 10.0, 0.0),
        _seg(10.0, 0.0, 10.0, 10.0),
    ]
    assert check_net_pad_connectivity(pads, segments=segs) == (3, 3)


def test_existing_copper_completes_a_partial_new_route():
    """Pre-existing same-net copper counts toward completion."""
    pads = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    new_segs = [_seg(0.0, 0.0, 10.0, 0.0)]
    existing = [_seg(10.0, 0.0, 10.0, 10.0)]
    assert check_net_pad_connectivity(pads, segments=new_segs, existing_segments=existing) == (3, 3)


def test_via_bridges_layers():
    """A via joins segments on different layers at its drill point."""
    pads = [(0.0, 0.0), (10.0, 10.0)]
    segs = [
        _seg(0.0, 0.0, 5.0, 0.0, layer=Layer.F_CU),
        _seg(5.0, 0.0, 10.0, 10.0, layer=Layer.B_CU),
    ]
    via = Via(x=5.0, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU))
    # Without the via the two segments are on different layers -> disjoint.
    assert check_net_pad_connectivity(pads, segments=segs) == (1, 2)
    # With the via they are one net.
    assert check_net_pad_connectivity(pads, segments=segs, vias=[via]) == (2, 2)


def test_near_miss_is_not_over_connected():
    """A corridor that lands *near* (not touching) a pad must NOT count it.

    This is the anti-#4176 guarantee: a 50 micron gap is far below any
    clearance/tolerance a proximity-union model would bridge, but it is well
    above the 1 micron coincidence epsilon, so the pad stays unconnected.
    """
    pads = [(0.0, 0.0), (10.0, 0.0), (5.0, 0.05)]
    segs = [_seg(0.0, 0.0, 10.0, 0.0)]
    connected, total = check_net_pad_connectivity(pads, segments=segs)
    assert total == 3
    assert connected == 2  # the near-miss pad is honestly NOT connected


def test_pcb_schema_segment_shape_via_start_end():
    """Segments exposing start/end tuples (PCB schema) are also accepted."""

    class _PcbSeg:
        def __init__(self, start, end, layer="F.Cu"):
            self.start = start
            self.end = end
            self.layer = layer

    pads = [(0.0, 0.0), (10.0, 0.0)]
    segs = [_PcbSeg((0.0, 0.0), (10.0, 0.0))]
    assert check_net_pad_connectivity(pads, segments=segs) == (2, 2)


def test_through_via_inner_layer_chain_complete():
    """Issue #4429 (confirming): the router oracle already handles this.

    ``pad -> In2.Cu trace -> through-via -> F.Cu trace -> pad`` with the vias
    at the segment endpoints: the layer-agnostic via union joins the two
    different-layer segments into one component, so both pads are connected.
    This confirms the router accounting is NOT affected by the per-component
    layer bug that #4429 fixes in ``NetStatusAnalyzer``.
    """
    pads = [(20.0, 15.0), (35.0, 15.0)]
    segs = [
        _seg(20.0, 15.0, 30.0, 15.0, layer=Layer.IN2_CU),
        _seg(30.0, 15.0, 35.0, 15.0, layer=Layer.F_CU),
    ]
    # Without vias the two segments are on different layers -> disjoint.
    assert check_net_pad_connectivity(pads, segments=segs) == (1, 2)
    vias = [
        Via(x=20.0, y=15.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU)),
        Via(x=30.0, y=15.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU)),
    ]
    assert check_net_pad_connectivity(pads, segments=segs, vias=vias) == (2, 2)
