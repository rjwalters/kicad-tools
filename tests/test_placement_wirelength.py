"""Tests for placement wirelength module.

Tests both the existing HPWL functions and the new per-footprint ratsnest
distance computation.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.placement.cost import Net
from kicad_tools.placement.vector import PlacedComponent, TransformedPad
from kicad_tools.placement.wirelength import (
    FootprintRatsnest,
    HPWLResult,
    compute_hpwl,
    compute_hpwl_breakdown,
    compute_per_footprint_ratsnest,
)

# ---------------------------------------------------------------------------
# Fixtures: synthetic 3-component, 2-net layout
# ---------------------------------------------------------------------------


def _make_placed_component(
    reference: str,
    x: float,
    y: float,
    pads: list[tuple[str, float, float]],
) -> PlacedComponent:
    """Helper to create a PlacedComponent with transformed pads at given positions."""
    transformed = tuple(
        TransformedPad(name=name, x=px, y=py, size_x=0.5, size_y=0.5) for name, px, py in pads
    )
    return PlacedComponent(
        reference=reference,
        x=x,
        y=y,
        rotation=0.0,
        side=0,
        pads=transformed,
    )


@pytest.fixture
def three_component_layout():
    """Three-component layout with analytically known ratsnest distances.

    Layout (all on front side, no rotation):

        U1 at (0, 0) with pads:
            pad "1" at (0, 0)   -- on Net_A
            pad "2" at (1, 0)   -- on Net_B

        R1 at (5, 0) with pads:
            pad "1" at (5, 0)   -- on Net_A
            pad "2" at (6, 0)   -- on Net_B

        C1 at (10, 0) with pads:
            pad "1" at (10, 0)  -- on Net_B

    Net_A: U1.1, R1.1  ->  distance = 5.0
    Net_B: U1.2, R1.2, C1.1
        U1 nearest on Net_B: min(dist(U1.2, R1.2), dist(U1.2, C1.1)) = min(5, 9) = 5.0
        R1 nearest on Net_B: min(dist(R1.2, U1.2), dist(R1.2, C1.1)) = min(5, 4) = 4.0
        C1 nearest on Net_B: min(dist(C1.1, U1.2), dist(C1.1, R1.2)) = min(9, 4) = 4.0

    Expected ratsnest per footprint:
        U1: Net_A contribution = 5.0, Net_B contribution = 5.0  -> total = 10.0
        R1: Net_A contribution = 5.0, Net_B contribution = 4.0  -> total = 9.0
        C1: Net_B contribution = 4.0                             -> total = 4.0
    """
    placements = [
        _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0), ("2", 1.0, 0.0)]),
        _make_placed_component("R1", 5, 0, [("1", 5.0, 0.0), ("2", 6.0, 0.0)]),
        _make_placed_component("C1", 10, 0, [("1", 10.0, 0.0)]),
    ]
    nets = [
        Net(name="Net_A", pins=[("U1", "1"), ("R1", "1")]),
        Net(name="Net_B", pins=[("U1", "2"), ("R1", "2"), ("C1", "1")]),
    ]
    return placements, nets


# ---------------------------------------------------------------------------
# Tests for compute_per_footprint_ratsnest
# ---------------------------------------------------------------------------


class TestPerFootprintRatsnest:
    """Tests for compute_per_footprint_ratsnest."""

    def test_three_component_layout(self, three_component_layout):
        """Ratsnest distances match analytical expectations for 3-component layout."""
        placements, nets = three_component_layout
        result = compute_per_footprint_ratsnest(placements, nets)

        # Should return one entry per component
        assert len(result) == 3

        # Convert to dict for easier assertions
        by_ref = {fr.reference: fr.ratsnest_mm for fr in result}

        assert by_ref["U1"] == pytest.approx(10.0, abs=0.001)
        assert by_ref["R1"] == pytest.approx(9.0, abs=0.001)
        assert by_ref["C1"] == pytest.approx(4.0, abs=0.001)

    def test_sorted_descending(self, three_component_layout):
        """Result is sorted by ratsnest_mm in descending order."""
        placements, nets = three_component_layout
        result = compute_per_footprint_ratsnest(placements, nets)

        distances = [fr.ratsnest_mm for fr in result]
        assert distances == sorted(distances, reverse=True)

        # U1 (10.0) should be first, C1 (4.0) should be last
        assert result[0].reference == "U1"
        assert result[-1].reference == "C1"

    def test_no_nets(self):
        """All ratsnest distances are 0.0 when there are no nets."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)]),
            _make_placed_component("R1", 5, 0, [("1", 5.0, 0.0)]),
        ]
        result = compute_per_footprint_ratsnest(placements, nets=[])

        assert len(result) == 2
        for fr in result:
            assert fr.ratsnest_mm == 0.0

    def test_single_component_no_airwires(self):
        """Single component with a net has 0.0 ratsnest (no other footprint)."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)]),
        ]
        nets = [
            Net(name="Net_A", pins=[("U1", "1")]),
        ]
        result = compute_per_footprint_ratsnest(placements, nets)

        assert len(result) == 1
        assert result[0].reference == "U1"
        assert result[0].ratsnest_mm == 0.0

    def test_unconnected_footprint_has_zero_ratsnest(self):
        """A footprint with no net connections has ratsnest_mm == 0.0."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)]),
            _make_placed_component("R1", 5, 0, [("1", 5.0, 0.0)]),
            _make_placed_component("TP1", 20, 0, [("1", 20.0, 0.0)]),  # test point, unconnected
        ]
        nets = [
            Net(name="Net_A", pins=[("U1", "1"), ("R1", "1")]),
        ]
        result = compute_per_footprint_ratsnest(placements, nets)

        by_ref = {fr.reference: fr.ratsnest_mm for fr in result}
        assert by_ref["TP1"] == 0.0
        assert by_ref["U1"] == pytest.approx(5.0, abs=0.001)
        assert by_ref["R1"] == pytest.approx(5.0, abs=0.001)

    def test_diagonal_distance(self):
        """Ratsnest correctly uses Euclidean distance for non-axis-aligned pads."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)]),
            _make_placed_component("R1", 3, 4, [("1", 3.0, 4.0)]),
        ]
        nets = [
            Net(name="Net_A", pins=[("U1", "1"), ("R1", "1")]),
        ]
        result = compute_per_footprint_ratsnest(placements, nets)

        by_ref = {fr.reference: fr.ratsnest_mm for fr in result}
        expected = math.sqrt(3**2 + 4**2)  # 5.0
        assert by_ref["U1"] == pytest.approx(expected, abs=0.001)
        assert by_ref["R1"] == pytest.approx(expected, abs=0.001)

    def test_multiple_pads_same_net_picks_nearest(self):
        """When a footprint has multiple pads on the same net, the nearest pair is used."""
        # U1 has two pads on Net_A: one at (0,0) and one at (2,0)
        # R1 has one pad on Net_A at (1,0)
        # Nearest pair is (2,0)->(1,0) distance=1, not (0,0)->(1,0) distance=1
        # Actually both are distance 1. Let me make it clearer.
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0), ("2", 4.0, 0.0)]),
            _make_placed_component("R1", 5, 0, [("1", 5.0, 0.0)]),
        ]
        nets = [
            Net(name="Net_A", pins=[("U1", "1"), ("U1", "2"), ("R1", "1")]),
        ]
        result = compute_per_footprint_ratsnest(placements, nets)

        by_ref = {fr.reference: fr.ratsnest_mm for fr in result}
        # U1 nearest to R1 on Net_A: min(dist((0,0),(5,0)), dist((4,0),(5,0))) = min(5, 1) = 1.0
        assert by_ref["U1"] == pytest.approx(1.0, abs=0.001)
        # R1 nearest to U1 on Net_A: min(dist((5,0),(0,0)), dist((5,0),(4,0))) = min(5, 1) = 1.0
        assert by_ref["R1"] == pytest.approx(1.0, abs=0.001)

    def test_empty_placements(self):
        """Empty placements return empty list."""
        result = compute_per_footprint_ratsnest([], [])
        assert result == []

    def test_footprint_ratsnest_dataclass(self):
        """FootprintRatsnest is a frozen dataclass with expected fields."""
        fr = FootprintRatsnest(reference="U1", ratsnest_mm=5.0)
        assert fr.reference == "U1"
        assert fr.ratsnest_mm == 5.0


# ---------------------------------------------------------------------------
# Tests for existing HPWL functions (ensure no regressions)
# ---------------------------------------------------------------------------


class TestComputeHPWL:
    """Regression tests for existing HPWL functions."""

    def test_hpwl_two_component_net(self):
        """HPWL for a 2-pad net is the Manhattan distance between pads."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)]),
            _make_placed_component("R1", 3, 4, [("1", 3.0, 4.0)]),
        ]
        nets = [Net(name="Net_A", pins=[("U1", "1"), ("R1", "1")])]

        total = compute_hpwl(placements, nets)
        # HPWL = (3-0) + (4-0) = 7.0
        assert total == pytest.approx(7.0)

    def test_hpwl_empty_nets(self):
        """HPWL is 0.0 with no nets."""
        placements = [_make_placed_component("U1", 0, 0, [("1", 0.0, 0.0)])]
        assert compute_hpwl(placements, []) == 0.0

    def test_hpwl_breakdown_returns_per_net(self):
        """HPWL breakdown returns per-net data."""
        placements = [
            _make_placed_component("U1", 0, 0, [("1", 0.0, 0.0), ("2", 1.0, 0.0)]),
            _make_placed_component("R1", 5, 0, [("1", 5.0, 0.0)]),
        ]
        nets = [
            Net(name="Net_A", pins=[("U1", "1"), ("R1", "1")]),
            Net(name="Net_B", pins=[("U1", "2")]),  # single-pad net
        ]

        result = compute_hpwl_breakdown(placements, nets)
        assert isinstance(result, HPWLResult)
        assert len(result.per_net) == 2
        assert result.per_net[0].name == "Net_A"
        assert result.per_net[0].hpwl == pytest.approx(5.0)
        assert result.per_net[1].name == "Net_B"
        assert result.per_net[1].hpwl == 0.0  # single-pad net
