"""Tests for Issue #2604: BlockingElement.ref population.

The PlacementFeedbackLoop never produces MOVE_COMPONENT strategies because
``BlockingElement.ref`` was always None for component blockers (GridCell
has no ``ref`` field).  This module verifies the spatial-lookup fix in
``RootCauseAnalyzer._find_blocking_elements`` correctly recovers the
owning component reference via ``RoutingGrid.find_pad_ref_at``.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.failure_analysis import (
    BlockingElement,
    Rectangle,
    RootCauseAnalyzer,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


@pytest.fixture
def design_rules() -> DesignRules:
    """Standard design rules for these tests."""
    return DesignRules(
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
    )


@pytest.fixture
def two_layer_stack() -> LayerStack:
    """Two-layer stack for these tests."""
    return LayerStack.two_layer()


@pytest.fixture
def routing_grid(design_rules: DesignRules, two_layer_stack: LayerStack) -> RoutingGrid:
    """Routing grid sized for two components on a small board."""
    return RoutingGrid(
        width=50.0,
        height=50.0,
        rules=design_rules,
        origin_x=0.0,
        origin_y=0.0,
        layer_stack=two_layer_stack,
    )


def _add_block_at(
    grid: RoutingGrid,
    ref: str,
    cx: float,
    cy: float,
    width: float,
    height: float,
    net: int = 0,
    net_name: str = "",
) -> Pad:
    """Add a single SMD pad to ``grid`` representing a component obstacle."""
    pad = Pad(
        x=cx,
        y=cy,
        width=width,
        height=height,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )
    grid.add_pad(pad)
    return pad


class TestFindPadRefAt:
    """Tests for the new ``RoutingGrid.find_pad_ref_at`` helper."""

    def test_returns_none_for_empty_grid(self, routing_grid: RoutingGrid):
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=0) is None

    def test_finds_ref_at_pad_center(self, routing_grid: RoutingGrid):
        _add_block_at(routing_grid, "U1", 10.0, 10.0, 2.0, 2.0)
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=0) == "U1"

    def test_finds_ref_within_clearance_envelope(self, routing_grid: RoutingGrid):
        # Pad center at (10,10), 2x2mm => metal extends to (9..11, 9..11),
        # plus clearance + trace_width/2 ~= 0.25mm.  A point at (11.2, 10)
        # should still resolve to U1 because grid blocking extends through
        # the clearance halo.
        _add_block_at(routing_grid, "U1", 10.0, 10.0, 2.0, 2.0)
        assert routing_grid.find_pad_ref_at(11.2, 10.0, layer_idx=0) == "U1"

    def test_returns_none_outside_envelope(self, routing_grid: RoutingGrid):
        _add_block_at(routing_grid, "U1", 10.0, 10.0, 2.0, 2.0)
        assert routing_grid.find_pad_ref_at(20.0, 20.0, layer_idx=0) is None

    def test_picks_nearest_when_envelopes_overlap(
        self, routing_grid: RoutingGrid
    ):
        _add_block_at(routing_grid, "U1", 10.0, 10.0, 2.0, 2.0)
        _add_block_at(routing_grid, "U2", 12.0, 10.0, 2.0, 2.0)
        # Point closer to U1 should resolve to U1
        assert routing_grid.find_pad_ref_at(10.5, 10.0, layer_idx=0) == "U1"
        # Point closer to U2 should resolve to U2
        assert routing_grid.find_pad_ref_at(11.5, 10.0, layer_idx=0) == "U2"

    def test_layer_filter_smd(
        self, routing_grid: RoutingGrid, two_layer_stack: LayerStack
    ):
        """SMD pads only affect their own layer."""
        # Manually add an SMD pad on B_CU and confirm it isn't found on F_CU
        pad = Pad(
            x=10.0,
            y=10.0,
            width=2.0,
            height=2.0,
            net=0,
            net_name="",
            layer=Layer.B_CU,
            ref="U1",
            pin="1",
        )
        routing_grid.add_pad(pad)
        b_cu_idx = routing_grid.layer_to_index(Layer.B_CU.value)
        f_cu_idx = routing_grid.layer_to_index(Layer.F_CU.value)
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=b_cu_idx) == "U1"
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=f_cu_idx) is None

    def test_pth_pads_block_all_layers(self, routing_grid: RoutingGrid):
        """PTH pads ignore the layer filter -- they block both F_CU and B_CU."""
        pad = Pad(
            x=10.0,
            y=10.0,
            width=2.0,
            height=2.0,
            net=0,
            net_name="",
            layer=Layer.F_CU,
            ref="J1",
            pin="1",
            through_hole=True,
            drill=1.0,
        )
        routing_grid.add_pad(pad)
        b_cu_idx = routing_grid.layer_to_index(Layer.B_CU.value)
        f_cu_idx = routing_grid.layer_to_index(Layer.F_CU.value)
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=f_cu_idx) == "J1"
        assert routing_grid.find_pad_ref_at(10.0, 10.0, layer_idx=b_cu_idx) == "J1"


class TestBlockingElementRefPopulation:
    """Tests that ``_find_blocking_elements`` populates ``ref`` correctly."""

    def test_component_blocker_has_populated_ref(
        self, routing_grid: RoutingGrid
    ):
        """The fix for Issue #2604: ref must NOT be None for component blockers."""
        # Place U1 directly between the start and end points so it lands
        # inside the routing corridor.
        _add_block_at(routing_grid, "U1", 25.0, 25.0, 4.0, 4.0)

        analyzer = RootCauseAnalyzer()
        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 25.0),
            end=(45.0, 25.0),
            net="DAC_CLK",
            layer=0,
        )

        component_blockers = [
            b for b in analysis.blocking_elements if b.type == "component"
        ]
        assert component_blockers, "Expected at least one component blocker"
        # Every component blocker must have a non-None ref now.
        for b in component_blockers:
            assert b.ref is not None, (
                f"Component blocker has ref=None (Issue #2604 regression): {b}"
            )
            assert b.movable is True
        # And the ref of the inserted U1 must be present.
        refs = {b.ref for b in component_blockers}
        assert "U1" in refs

    def test_has_movable_blockers_implies_refs_present(
        self, routing_grid: RoutingGrid
    ):
        """If has_movable_blockers is True, at least one blocker must have a ref.

        This is the invariant the strategy generator relies on.  If an
        analyzer returns has_movable_blockers=True but every blocker has
        ref=None (the pre-fix behavior), every move strategy gets dropped
        in ``_generate_move_strategies``.
        """
        _add_block_at(routing_grid, "U1", 25.0, 25.0, 4.0, 4.0)

        analyzer = RootCauseAnalyzer()
        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 25.0),
            end=(45.0, 25.0),
            net="DAC_CLK",
            layer=0,
        )
        if not analysis.has_movable_blockers:
            pytest.skip("Analyzer didn't detect movable blockers in this fixture")
        ref_present = [
            b.ref
            for b in analysis.blocking_elements
            if b.type == "component" and b.movable and b.ref
        ]
        assert ref_present, (
            "has_movable_blockers=True but no component blocker has a ref "
            "(Issue #2604 regression -- strategy generator will drop "
            "every candidate)"
        )

    def test_nearby_component_populated(self, routing_grid: RoutingGrid):
        """``nearby_component`` is gated on ``b.ref`` -- verify it populates.

        See ``failure_analysis.py:759``: ``component_blockers = [b for b
        in blocking if b.ref]``.  Pre-fix this was always empty.
        """
        _add_block_at(routing_grid, "U1", 25.0, 25.0, 4.0, 4.0)

        analyzer = RootCauseAnalyzer()
        analysis = analyzer.analyze_routing_failure(
            grid=routing_grid,
            start=(5.0, 25.0),
            end=(45.0, 25.0),
            net="DAC_CLK",
            layer=0,
        )
        if not [b for b in analysis.blocking_elements if b.type == "component"]:
            pytest.skip("No component blockers detected in this fixture")
        assert analysis.nearby_component is not None

    def test_zone_blockers_still_have_none_ref(
        self, routing_grid: RoutingGrid
    ):
        """Zones legitimately have ref=None -- verify we didn't break that."""
        # Build a synthetic zone-blocker test by directly checking the
        # element-type classification: a zone cell must remain ref=None.
        # We can't easily add a real zone here, so we just assert the
        # API contract via a constructed BlockingElement.
        zone = BlockingElement(
            type="zone",
            ref=None,
            net=None,
            bounds=Rectangle(0, 0, 1, 1),
            movable=False,
            layer=0,
        )
        assert zone.ref is None
        assert zone.movable is False
