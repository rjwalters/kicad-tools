"""Tests for Issue #4172: boundary stub-terminal detection (Phase 2b-0).

``detect_boundary_stub_terminals`` is a *pure* function (no grid/router/PCB
state). These tests feed hand-built clipped segments and pad locations
directly to the detector -- no chorus fixture, no full ``PCB`` round-trip --
which is the right level of isolation for a pure-function unit.

The detector implements the four-part spec from the #4170 design:

1. endpoint on the region-boundary line within ``EPSILON``
2. the segment's OTHER endpoint strictly OUTSIDE the region
3. endpoint NOT coincident with any pad/via center (within ``EPSILON``)
4. net has pad(s) OUTSIDE the region AND >=1 pad INSIDE

Each negative case below isolates the failure of exactly one part.
"""

from __future__ import annotations

from kicad_tools.core.types import CopperLayer
from kicad_tools.router.stub_terminals import (
    EPSILON,
    BoundaryEdge,
    PadLocation,
    RegionBox,
    StubSegment,
    StubTerminal,
    detect_boundary_stub_terminals,
)

# A simple 10x10 region at the origin used throughout.
REGION = RegionBox(0.0, 0.0, 10.0, 10.0)


def _straddling_pads(net_id: int) -> list[PadLocation]:
    """Pads for a net that owns copper both inside and outside REGION."""
    return [
        PadLocation(net_id=net_id, x=5.0, y=5.0),  # inside
        PadLocation(net_id=net_id, x=20.0, y=5.0),  # outside
    ]


# --------------------------------------------------------------------------- #
# Positive detection cases
# --------------------------------------------------------------------------- #


def test_detects_stub_on_right_edge():
    """A boundary->outside stub on a straddling net is detected."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0,  # on right edge (boundary)
        y1=5.0,
        x2=20.0,  # strictly outside
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-1",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)

    assert set(result.keys()) == {1}
    terminals = result[1]
    assert len(terminals) == 1
    t = terminals[0]
    assert isinstance(t, StubTerminal)
    assert t.net_id == 1
    assert t.net_name == "NET1"
    assert (t.x, t.y) == (10.0, 5.0)
    assert t.layer is CopperLayer.F_CU
    assert t.source_segment_uuid == "seg-1"
    assert t.boundary_edge is BoundaryEdge.RIGHT


def test_detects_stub_when_boundary_end_is_second_endpoint():
    """Detection is endpoint-order independent (boundary end is x2/y2)."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=-5.0,  # strictly outside (left)
        y1=5.0,
        x2=0.0,  # on left edge (boundary)
        y2=5.0,
        layer=CopperLayer.B_CU,
        uuid="seg-2",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)

    assert set(result.keys()) == {1}
    t = result[1][0]
    assert (t.x, t.y) == (0.0, 5.0)
    assert t.boundary_edge is BoundaryEdge.LEFT
    assert t.layer is CopperLayer.B_CU


def test_detects_stub_within_epsilon_of_boundary_line():
    """An endpoint within EPSILON of the edge line still counts as on-boundary."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0 + EPSILON / 2.0,  # just inside epsilon of the right edge
        y1=3.0,
        x2=25.0,  # strictly outside
        y2=3.0,
        layer=CopperLayer.F_CU,
        uuid="seg-eps",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)
    assert set(result.keys()) == {1}
    assert result[1][0].boundary_edge is BoundaryEdge.RIGHT


def test_detects_stub_on_top_and_bottom_edges():
    """Vertical stubs are classified onto TOP / BOTTOM edges."""
    top_seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=4.0,
        y1=0.0,  # top edge
        x2=4.0,
        y2=-8.0,  # strictly outside
        layer=CopperLayer.F_CU,
        uuid="top",
    )
    bottom_seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=6.0,
        y1=10.0,  # bottom edge
        x2=6.0,
        y2=18.0,  # strictly outside
        layer=CopperLayer.F_CU,
        uuid="bottom",
    )
    result = detect_boundary_stub_terminals([top_seg, bottom_seg], _straddling_pads(1), REGION)
    edges = {t.boundary_edge for t in result[1]}
    assert edges == {BoundaryEdge.TOP, BoundaryEdge.BOTTOM}


def test_multiple_nets_grouped_by_net_id():
    """Terminals are grouped per net; unrelated nets are independent."""
    seg1 = StubSegment(1, "NET1", 10.0, 2.0, 22.0, 2.0, CopperLayer.F_CU, "a")
    seg2 = StubSegment(2, "NET2", 10.0, 8.0, 22.0, 8.0, CopperLayer.F_CU, "b")
    pads = _straddling_pads(1) + _straddling_pads(2)
    result = detect_boundary_stub_terminals([seg1, seg2], pads, REGION)
    assert set(result.keys()) == {1, 2}
    assert result[1][0].boundary_edge is BoundaryEdge.RIGHT
    assert result[2][0].boundary_edge is BoundaryEdge.RIGHT


# --------------------------------------------------------------------------- #
# Negative cases -- each isolates the failure of exactly one detection part
# --------------------------------------------------------------------------- #


def test_rejects_pad_coincident_endpoint():
    """Part 3: a boundary endpoint sitting on a pad center is not a stub."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0,  # on right edge -- but a pad sits here
        y1=5.0,
        x2=20.0,  # strictly outside
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-1",
    )
    pads = _straddling_pads(1) + [
        PadLocation(net_id=1, x=10.0 + EPSILON / 2.0, y=5.0),  # coincident pad
    ]
    result = detect_boundary_stub_terminals([seg], pads, REGION)
    assert result == {}


def test_rejects_outside_outside_segment():
    """Part 2: a segment with both endpoints outside is not a boundary stub.

    This mirrors ``strip_traces``'s both-endpoints-outside branch, which is
    skipped rather than clipped -- so it never yields a boundary endpoint.
    """
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=-5.0,  # outside (left)
        y1=5.0,
        x2=20.0,  # outside (right)
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-oo",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)
    assert result == {}


def test_rejects_all_inside_net():
    """Part 4: a net whose pads are all inside the region is ineligible.

    Even if a segment presents a boundary endpoint, a net that does not
    straddle the region needs no reconnection.
    """
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0,  # on right edge
        y1=5.0,
        x2=20.0,  # strictly outside
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-1",
    )
    inside_only_pads = [
        PadLocation(net_id=1, x=3.0, y=3.0),
        PadLocation(net_id=1, x=7.0, y=7.0),
    ]
    result = detect_boundary_stub_terminals([seg], inside_only_pads, REGION)
    assert result == {}


def test_rejects_all_outside_net():
    """Part 4 (other half): a net with no pad inside is also ineligible."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0,
        y1=5.0,
        x2=20.0,
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-1",
    )
    outside_only_pads = [
        PadLocation(net_id=1, x=20.0, y=5.0),
        PadLocation(net_id=1, x=25.0, y=5.0),
    ]
    result = detect_boundary_stub_terminals([seg], outside_only_pads, REGION)
    assert result == {}


def test_rejects_near_boundary_but_not_on_line_endpoint():
    """Part 1: an endpoint near the region but not on any edge line is rejected.

    The inside endpoint here is well inside the box (not on a boundary line),
    and the outside endpoint is outside -- but neither endpoint lies on the
    boundary line, so this is a normal boundary-crossing trace, not a clipped
    stub. (In practice ``strip_traces`` would have clipped this; the detector
    must not fabricate a terminal from an un-clipped interior endpoint.)
    """
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=5.0,  # strictly inside, not on any edge line
        y1=5.0,
        x2=20.0,  # strictly outside
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-nb",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)
    assert result == {}


def test_rejects_endpoint_just_beyond_epsilon_of_line():
    """Part 1 boundary: an endpoint just beyond EPSILON of the edge is rejected."""
    seg = StubSegment(
        net_id=1,
        net_name="NET1",
        x1=10.0 - 2.0 * EPSILON,  # inside, more than EPSILON from the right edge
        y1=5.0,
        x2=20.0,  # strictly outside
        y2=5.0,
        layer=CopperLayer.F_CU,
        uuid="seg-far",
    )
    result = detect_boundary_stub_terminals([seg], _straddling_pads(1), REGION)
    assert result == {}


# --------------------------------------------------------------------------- #
# Purity / signature guarantees
# --------------------------------------------------------------------------- #


def test_region_box_normalizes_corner_order():
    """RegionBox normalizes so callers may pass corners in any order."""
    a = RegionBox(0.0, 0.0, 10.0, 10.0)
    b = RegionBox(10.0, 10.0, 0.0, 0.0)
    assert (a.x1, a.y1, a.x2, a.y2) == (b.x1, b.y1, b.x2, b.y2)


def test_pad_exactly_on_boundary_counts_as_inside():
    """Inclusive box: a pad on the boundary line is inside (Phase 2a parity).

    A net with one pad on the boundary and one strictly outside owns NO pad
    strictly inside... but the boundary pad counts as inside under the
    inclusive test, so the net is eligible and its stub is detected.
    """
    seg = StubSegment(1, "NET1", 10.0, 4.0, 20.0, 4.0, CopperLayer.F_CU, "s")
    pads = [
        PadLocation(net_id=1, x=0.0, y=5.0),  # on left boundary -> counts inside
        PadLocation(net_id=1, x=20.0, y=5.0),  # outside
    ]
    result = detect_boundary_stub_terminals([seg], pads, REGION)
    assert set(result.keys()) == {1}


def test_empty_inputs_return_empty_mapping():
    result = detect_boundary_stub_terminals([], [], REGION)
    assert result == {}
