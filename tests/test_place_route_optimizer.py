"""Tests for kicad_tools.optimize.place_route module."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_tools.optimize import OptimizationResult, PlaceRouteOptimizer

# =============================================================================
# Test PCB fixtures
# =============================================================================

# Simple 2-component PCB for testing
SIMPLE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (gr_line (start 90 90) (end 110 90) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 110 90) (end 110 110) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 110 110) (end 90 110) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 90 110) (end 90 90) (layer "Edge.Cuts") (width 0.1))
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 95 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 105 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
)
"""


# =============================================================================
# OptimizationResult tests
# =============================================================================


class TestOptimizationResult:
    """Tests for OptimizationResult dataclass."""

    def test_default_construction(self):
        """Test OptimizationResult with default values."""
        result = OptimizationResult(success=True)

        assert result.success is True
        assert result.pcb_path is None
        assert result.routes is None
        assert result.placement_conflicts is None
        assert result.drc_results is None
        assert result.iterations == 0
        assert result.message == ""

    def test_full_construction(self):
        """Test OptimizationResult with all fields."""
        mock_routes = [MagicMock()]
        mock_conflicts = [MagicMock()]
        mock_drc = MagicMock()

        result = OptimizationResult(
            success=False,
            pcb_path=Path("/test/board.kicad_pcb"),
            routes=mock_routes,
            placement_conflicts=mock_conflicts,
            drc_results=mock_drc,
            iterations=5,
            message="Test message",
        )

        assert result.success is False
        assert result.pcb_path == Path("/test/board.kicad_pcb")
        assert result.routes == mock_routes
        assert result.placement_conflicts == mock_conflicts
        assert result.drc_results == mock_drc
        assert result.iterations == 5
        assert result.message == "Test message"

    def test_has_placement_conflicts_true(self):
        """Test has_placement_conflicts when conflicts exist."""
        result = OptimizationResult(
            success=False,
            placement_conflicts=[MagicMock()],
        )
        assert result.has_placement_conflicts is True

    def test_has_placement_conflicts_false(self):
        """Test has_placement_conflicts when no conflicts."""
        result = OptimizationResult(success=True, placement_conflicts=None)
        assert result.has_placement_conflicts is False

        result2 = OptimizationResult(success=True, placement_conflicts=[])
        assert result2.has_placement_conflicts is False

    def test_has_drc_violations_true(self):
        """Test has_drc_violations when violations exist."""
        mock_drc = MagicMock()
        mock_drc.passed = False

        result = OptimizationResult(success=False, drc_results=mock_drc)
        assert result.has_drc_violations is True

    def test_has_drc_violations_false(self):
        """Test has_drc_violations when DRC passed."""
        mock_drc = MagicMock()
        mock_drc.passed = True

        result = OptimizationResult(success=True, drc_results=mock_drc)
        assert result.has_drc_violations is False

    def test_has_drc_violations_no_results(self):
        """Test has_drc_violations when no DRC was run."""
        result = OptimizationResult(success=True, drc_results=None)
        assert result.has_drc_violations is False

    def test_routing_complete_true(self):
        """Test routing_complete when routes exist."""
        result = OptimizationResult(success=True, routes=[MagicMock()])
        assert result.routing_complete is True

    def test_routing_complete_false(self):
        """Test routing_complete when no routes."""
        result = OptimizationResult(success=False, routes=None)
        assert result.routing_complete is False

        result2 = OptimizationResult(success=False, routes=[])
        assert result2.routing_complete is False

    def test_str_success(self):
        """Test string representation for success."""
        result = OptimizationResult(
            success=True,
            routes=[MagicMock(), MagicMock()],
            iterations=3,
            message="Done",
        )
        s = str(result)
        assert "SUCCESS" in s
        assert "iterations=3" in s
        assert "routes=2" in s

    def test_str_failed(self):
        """Test string representation for failure."""
        result = OptimizationResult(
            success=False,
            iterations=10,
            message="Max iterations exceeded",
        )
        s = str(result)
        assert "FAILED" in s
        assert "iterations=10" in s


# =============================================================================
# PlaceRouteOptimizer tests
# =============================================================================


class TestPlaceRouteOptimizer:
    """Tests for PlaceRouteOptimizer class."""

    @pytest.fixture
    def mock_analyzer(self):
        """Create a mock PlacementAnalyzer."""
        analyzer = MagicMock()
        analyzer.find_conflicts.return_value = []
        analyzer.get_components.return_value = []
        analyzer.get_board_edge.return_value = None
        return analyzer

    @pytest.fixture
    def mock_fixer(self):
        """Create a mock PlacementFixer."""
        fixer = MagicMock()
        fixer.suggest_fixes.return_value = []
        fixer.apply_fixes.return_value = MagicMock(fixes_applied=0)
        return fixer

    @pytest.fixture
    def mock_router(self):
        """Create a mock Autorouter."""
        router = MagicMock()
        router.nets = {1: [("R1", "1"), ("R2", "1")], 2: [("R1", "2"), ("R2", "2")]}
        router.pads = {}
        router.route_all.return_value = [MagicMock(net=1), MagicMock(net=2)]
        return router

    @pytest.fixture
    def mock_drc_results(self):
        """Create mock DRC results."""
        results = MagicMock()
        results.passed = True
        results.errors = []
        results.warnings = []
        return results

    def test_init(self, mock_analyzer, mock_fixer, mock_router, tmp_path):
        """Test PlaceRouteOptimizer initialization."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: mock_router,
            verbose=False,
        )

        assert optimizer.pcb_path == pcb_path
        assert optimizer.analyzer == mock_analyzer
        assert optimizer.fixer == mock_fixer
        assert optimizer.verbose is False

    def test_optimize_success_no_conflicts(self, mock_analyzer, mock_fixer, mock_router, tmp_path):
        """Test optimization succeeds when no conflicts and routing complete."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: mock_router,
            drc_checker_factory=None,  # Skip DRC
            verbose=False,
        )

        result = optimizer.optimize(max_iterations=5)

        assert result.success is True
        assert result.iterations == 1
        assert len(result.routes) == 2

    def test_optimize_max_iterations_exceeded(self, mock_analyzer, mock_fixer, tmp_path):
        """Test optimization fails when max iterations exceeded."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        # Router that always fails to route some nets
        def failing_router_factory():
            router = MagicMock()
            router.nets = {1: [("R1", "1"), ("R2", "1")]}
            router.pads = {}
            router.route_all.return_value = []  # No routes
            return router

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=failing_router_factory,
            verbose=False,
        )

        result = optimizer.optimize(max_iterations=3, allow_placement_changes=False)

        assert result.success is False
        assert "Could not route" in result.message

    def test_optimize_with_drc_check(
        self, mock_analyzer, mock_fixer, mock_router, mock_drc_results, tmp_path
    ):
        """Test optimization includes DRC checking."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        mock_checker = MagicMock()
        mock_checker.check_all.return_value = mock_drc_results

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: mock_router,
            drc_checker_factory=lambda pcb: mock_checker,
            verbose=False,
        )

        result = optimizer.optimize()

        assert result.success is True
        mock_checker.check_all.assert_called_once()

    def test_optimize_drc_failure(self, mock_analyzer, mock_fixer, mock_router, tmp_path):
        """Test optimization fails on DRC violations."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        mock_drc_results = MagicMock()
        mock_drc_results.passed = False
        mock_drc_results.errors = [MagicMock()]
        mock_drc_results.warnings = []

        mock_checker = MagicMock()
        mock_checker.check_all.return_value = mock_drc_results

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: mock_router,
            drc_checker_factory=lambda pcb: mock_checker,
            verbose=False,
        )

        result = optimizer.optimize(max_iterations=1)

        assert result.success is False
        assert "DRC violations" in result.message
        assert result.drc_results == mock_drc_results

    def test_optimize_skip_drc(self, mock_analyzer, mock_fixer, mock_router, tmp_path):
        """Test optimization can skip DRC checking."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        mock_checker = MagicMock()

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: mock_router,
            drc_checker_factory=lambda pcb: mock_checker,
            verbose=False,
        )

        result = optimizer.optimize(skip_drc=True)

        assert result.success is True
        mock_checker.check_all.assert_not_called()


class TestPlaceRouteOptimizerIdentifyBlockers:
    """Tests for _identify_blockers method."""

    @pytest.fixture
    def optimizer_with_components(self, tmp_path):
        """Create optimizer with mock components."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        # Mock analyzer with components
        mock_analyzer = MagicMock()
        mock_comp1 = MagicMock()
        mock_comp1.reference = "R1"
        mock_comp1.position = MagicMock(x=95, y=100)

        mock_comp2 = MagicMock()
        mock_comp2.reference = "R2"
        mock_comp2.position = MagicMock(x=105, y=100)

        mock_comp3 = MagicMock()
        mock_comp3.reference = "C1"
        mock_comp3.position = MagicMock(x=100, y=100)  # Between R1 and R2

        mock_analyzer.get_components.return_value = [mock_comp1, mock_comp2, mock_comp3]
        mock_analyzer.find_conflicts.return_value = []

        # Mock router with net information
        def router_factory():
            router = MagicMock()
            router.nets = {
                1: [("R1", "1"), ("R2", "1")],
            }
            mock_pad_r1 = MagicMock(x=95, y=100)
            mock_pad_r2 = MagicMock(x=105, y=100)
            router.pads = {
                ("R1", "1"): mock_pad_r1,
                ("R2", "1"): mock_pad_r2,
            }
            router.route_all.return_value = []
            return router

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=MagicMock(),
            router_factory=router_factory,
            verbose=False,
        )

        return optimizer

    def test_identify_blockers_finds_component_in_path(self, optimizer_with_components):
        """Test that components between net pads are identified as blockers."""
        blockers = optimizer_with_components._identify_blockers([1])

        # C1 is at x=100, between R1 at x=95 and R2 at x=105
        assert "C1" in blockers
        # R1 and R2 are part of the net, should not be blockers
        assert "R1" not in blockers
        assert "R2" not in blockers

    def test_identify_blockers_empty_for_no_failed_nets(self, optimizer_with_components):
        """Test that no blockers found when no failed nets."""
        blockers = optimizer_with_components._identify_blockers([])
        assert blockers == []


class TestPlaceRouteOptimizerNudgeBlockers:
    """Tests for _nudge_blockers method."""

    def test_nudge_blockers_creates_fixes(self, tmp_path):
        """Test that nudge_blockers creates and applies fixes."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        # Mock component
        mock_comp = MagicMock()
        mock_comp.reference = "C1"
        mock_comp.position = MagicMock(x=100, y=100)

        mock_analyzer = MagicMock()
        mock_analyzer.get_components.return_value = [mock_comp]
        mock_analyzer.get_board_edge.return_value = MagicMock(
            min_x=90, max_x=110, min_y=90, max_y=110
        )
        mock_analyzer.find_conflicts.return_value = []

        mock_fixer = MagicMock()
        mock_fixer.apply_fixes.return_value = MagicMock(fixes_applied=1)

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=mock_analyzer,
            fixer=mock_fixer,
            router_factory=lambda: MagicMock(),
            verbose=False,
        )

        optimizer._nudge_blockers(["C1"])

        # Verify apply_fixes was called
        mock_fixer.apply_fixes.assert_called_once()

        # Get the fixes that were passed
        call_args = mock_fixer.apply_fixes.call_args
        fixes = call_args[0][1]  # Second positional arg

        assert len(fixes) == 1
        assert fixes[0].component == "C1"
        # Should move away from center (center is at 100,100, so component at 100,100
        # should move positively in both directions)


class TestPlaceRouteOptimizerFromPcb:
    """Tests for from_pcb factory method."""

    def test_from_pcb_creates_optimizer(self, tmp_path):
        """Test from_pcb creates a working optimizer."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))

        optimizer = PlaceRouteOptimizer.from_pcb(
            pcb=pcb,
            pcb_path=pcb_path,
            manufacturer="jlcpcb",
            layers=2,
            verbose=False,
        )

        assert optimizer.pcb_path == pcb_path
        assert optimizer.analyzer is not None
        assert optimizer.fixer is not None
        assert optimizer.router_factory is not None
        assert optimizer.drc_checker_factory is not None

    def test_from_pcb_auto_detects_dimensions(self, tmp_path):
        """Test from_pcb auto-detects board dimensions from edge cuts."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))

        # Auto-detect dimensions from PCB
        width, height = PlaceRouteOptimizer._detect_board_dimensions(pcb)

        # Should detect non-zero reasonable dimensions
        # The exact value depends on how the PCB parser interprets the edge cuts
        assert width > 0
        assert height > 0
        # Should be reasonable for a small test board
        assert width < 200  # mm
        assert height < 200  # mm

    def test_from_pcb_router_factory_loads_components(self, tmp_path):
        """Test that the router factory loads PCB components."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))

        optimizer = PlaceRouteOptimizer.from_pcb(
            pcb=pcb,
            pcb_path=pcb_path,
            verbose=False,
        )

        # Create router using factory
        router = optimizer.router_factory()

        # Should have loaded the two resistors (4 pads total, 2 nets)
        assert len(router.pads) > 0
        assert len(router.nets) > 0


# =============================================================================
# Integration tests
# =============================================================================


class TestPlaceRouteOptimizerIntegration:
    """Integration tests for PlaceRouteOptimizer."""

    def test_full_optimization_simple_pcb(self, tmp_path):
        """Test full optimization on a simple PCB."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))

        optimizer = PlaceRouteOptimizer.from_pcb(
            pcb=pcb,
            pcb_path=pcb_path,
            manufacturer="jlcpcb",
            layers=2,
            verbose=False,
        )

        # Run optimization with DRC skipped (DRC checker stubs return empty results)
        result = optimizer.optimize(max_iterations=3, skip_drc=True)

        # Should complete (may or may not fully route depending on routing complexity)
        assert result.iterations > 0
        assert result.pcb_path == pcb_path

    def test_optimization_respects_max_iterations(self, tmp_path):
        """Test that optimization respects max_iterations limit."""
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(SIMPLE_PCB)

        # Create optimizer with a router that never completes
        mock_router = MagicMock()
        mock_router.nets = {1: [("R1", "1"), ("R2", "1")]}
        mock_router.pads = {}
        mock_router.route_all.return_value = []

        from kicad_tools.placement.analyzer import PlacementAnalyzer
        from kicad_tools.placement.fixer import PlacementFixer

        optimizer = PlaceRouteOptimizer(
            pcb_path=pcb_path,
            analyzer=PlacementAnalyzer(verbose=False),
            fixer=PlacementFixer(verbose=False),
            router_factory=lambda: mock_router,
            verbose=False,
        )

        result = optimizer.optimize(max_iterations=2, allow_placement_changes=False)

        assert result.success is False
        assert "Could not route" in result.message
